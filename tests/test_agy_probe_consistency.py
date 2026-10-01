import asyncio
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import control_plane
import server_legacy


AGY_HELP = textwrap.dedent(
    """\
    Usage: agy [options]
      --print
      --dangerously-skip-permissions
      --output-format text|json|stream-json
      --print-timeout <dur>
      --model <id>
      --effort (low|medium|high)
    """
)

AGY_1127_MODELS = "\n".join(
    (
        "gemini-3.8-flash-high",
        "gemini-3.8-flash-medium",
        "gemini-3.8-flash-low",
        "gemini-3.7-flash-high",
        "gemini-3.6-flash-medium",
        "gemini-3.1-pro-high",
        "claude-sonnet-4",
        "gpt-oss-120b-medium",
    )
) + "\n"


class AgyProbeConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        control_plane._AGY_PROBE_CACHE = None
        self.addCleanup(setattr, control_plane, "_AGY_PROBE_CACHE", None)

    @staticmethod
    def _result(argv: list[str], returncode: int, stdout: str, stderr: str = ""):
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    def _status_for_models_probe(
        self,
        returncode: int,
        stdout: str,
        stderr: str = "",
        version: str = "1.1.27\n",
    ) -> dict:
        """Run only the canonical probe against a disposable fake executable."""
        def probe(argv, *, cwd, env):
            if argv[1:] == ["--version"]:
                return self._result(argv, 0, version)
            if argv[1:] == ["--help"]:
                return self._result(argv, 0, AGY_HELP)
            if argv[1:] == ["models"]:
                return self._result(argv, returncode, stdout, stderr)
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_agy = Path(temporary_directory) / "agy.exe"
            fake_agy.write_text("placeholder", encoding="utf-8")
            with mock.patch.object(control_plane, "AGY_EXE", fake_agy), mock.patch.object(
                control_plane, "_run_agy_probe", side_effect=probe
            ):
                return control_plane.harness_status("agy")

    def test_agy_1127_exit_one_with_complete_model_catalogue_is_available(self) -> None:
        status = self._status_for_models_probe(
            1,
            AGY_1127_MODELS,
            "Fetching available models...\n",
        )

        self.assertTrue(status["available"], status)
        self.assertEqual("1.1.27", status["version"])
        self.assertEqual(AGY_1127_MODELS.splitlines(), status["models"])

    def test_models_probe_success_not_bound_to_version(self) -> None:
        """Capability probe success must not be bound to version == 1.1.27."""
        for test_version in ("1.1.28\n", "1.2.0\n", "2.0.0-rc1\n"):
            with self.subTest(version=test_version):
                control_plane._AGY_PROBE_CACHE = None
                status = self._status_for_models_probe(
                    1,
                    "gemini-3.9-flash\n",
                    "Fetching available models...\n",
                    version=test_version,
                )
                self.assertTrue(status["available"], status)
                self.assertEqual(test_version.strip(), status["version"])
                self.assertEqual(["gemini-3.9-flash"], status["models"])

    def test_models_probe_succeeds_without_claude_or_gpt_oss(self) -> None:
        """Probe must not require Claude, GPT-OSS, or fixed Gemini model slugs."""
        single_gemini = "gemini-3.9-flash-super\n"
        status = self._status_for_models_probe(
            1,
            single_gemini,
            "Fetching available models...\n",
        )
        self.assertTrue(status["available"], status)
        self.assertEqual(["gemini-3.9-flash-super"], status["models"])

    def test_models_probe_with_unknown_model_families_does_not_break_probe(self) -> None:
        """Unknown model families (gemma, deepseek, qwen, etc.) must not make AGY unavailable."""
        mixed_catalogue = "\n".join([
            "gemini-3.8-flash",
            "gemma-2-9b-it",
            "deepseek-v3",
            "qwen-2.5-coder-32b",
        ]) + "\n"
        status = self._status_for_models_probe(
            1,
            mixed_catalogue,
            "Fetching available models...\n",
        )
        self.assertTrue(status["available"], status)
        self.assertEqual(
            ["gemini-3.8-flash", "gemma-2-9b-it", "deepseek-v3", "qwen-2.5-coder-32b"],
            status["models"],
        )

    def test_models_probe_without_any_gemini_model_remains_blocked(self) -> None:
        """A catalogue without at least one gemini-* model must fail closed."""
        non_gemini_catalogue = "\n".join([
            "claude-sonnet-4",
            "gpt-oss-120b-medium",
            "llama-3.3-70b",
        ]) + "\n"
        # Non-zero exit: blocked
        control_plane._AGY_PROBE_CACHE = None
        status_non_zero = self._status_for_models_probe(1, non_gemini_catalogue)
        self.assertFalse(status_non_zero["available"], status_non_zero)
        self.assertEqual([], status_non_zero["models"])
        self.assertIn("models exited 1", status_non_zero["blocker"])

        # Zero exit: still blocked because no Gemini model exists
        control_plane._AGY_PROBE_CACHE = None
        status_zero = self._status_for_models_probe(0, non_gemini_catalogue)
        self.assertFalse(status_zero["available"], status_zero)
        self.assertEqual([], status_zero["models"])
        self.assertIn("incomplete or blocked output", status_zero["blocker"])

    def test_models_probe_with_auth_error_fails_closed_even_on_exit_zero(self) -> None:
        """Real auth failure must fail closed even if exit code is 0."""
        status = self._status_for_models_probe(
            0,
            "gemini-3.8-flash\n",
            "Error: Please sign in to view available models.\n",
        )
        self.assertFalse(status["available"], status)
        self.assertEqual([], status["models"])
        self.assertIn("Please sign in", status["blocker"])

    def test_models_probe_with_network_error_fails_closed_on_exit_one(self) -> None:
        """Network failure must fail closed on non-zero exit."""
        status = self._status_for_models_probe(
            1,
            "gemini-3.8-flash\n",
            "Error: network connection failed\n",
        )
        self.assertFalse(status["available"], status)
        self.assertEqual([], status["models"])
        self.assertIn("network connection failed", status["blocker"])

    def test_models_probe_description_with_token_network_proxy_does_not_block(self) -> None:
        """Normal model descriptions containing token, network, proxy must not trigger false blocker."""
        stdout = "\n".join([
            "gemini-3.8-flash-high\tGemini 3.8 Flash (High) - 1M token context window",
            "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium) - neural network reasoning",
            "gemini-3.8-flash-low\tGemini 3.8 Flash (Low) - enterprise proxy support",
        ]) + "\n"
        stderr = "Fetching available models...\n"

        status = self._status_for_models_probe(1, stdout, stderr)
        self.assertTrue(status["available"], status)
        self.assertEqual(
            ["gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low"],
            status["models"],
        )
        self.assertIsNone(status.get("blocker"))

    def test_models_probe_info_and_banner_lines_not_parsed_as_models(self) -> None:
        """Informational/banner lines in stdout or stderr must not be mistakenly parsed as model IDs."""
        stdout = "\n".join([
            "Fetching available models...",
            "Available models:",
            "----------------------------------------",
            "[INFO] Local model cache refreshed in 12ms",
            "Notice: 2 models available",
            "gemini-3.8-flash-high\tGemini 3.8 Flash (High)",
            "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)",
            "Total models: 2",
            "Tip: run 'agy --help' for command options",
        ]) + "\n"
        stderr = "\n".join([
            "Fetching available models...",
            "[INFO] Connected to registry endpoint",
            "Loaded default configuration from profile",
            "Some unknown stderr banner line",
        ]) + "\n"

        status = self._status_for_models_probe(1, stdout, stderr)
        self.assertTrue(status["available"], status)
        self.assertEqual(
            ["gemini-3.8-flash-high", "gemini-3.8-flash-medium"],
            status["models"],
        )
        for forbidden_id in (
            "Fetching", "Available", "[INFO]", "Notice:", "Total", "Tip:",
            "Loaded", "Connected", "Some", "----------------------------------------",
        ):
            self.assertNotIn(forbidden_id, status["models"])

    def test_models_probe_explicit_network_connection_failed_fails_closed(self) -> None:
        """Explicit 'Error: network connection failed' must fail closed on both exit codes."""
        stdout = "gemini-3.8-flash-high\tGemini 3.8 Flash\n"
        stderr = "Error: network connection failed\n"

        # Non-zero exit code
        control_plane._AGY_PROBE_CACHE = None
        status_non_zero = self._status_for_models_probe(1, stdout, stderr)
        self.assertFalse(status_non_zero["available"], status_non_zero)
        self.assertEqual([], status_non_zero["models"])
        self.assertIn("network connection failed", status_non_zero["blocker"])

        # Zero exit code
        control_plane._AGY_PROBE_CACHE = None
        status_zero = self._status_for_models_probe(0, stdout, stderr)
        self.assertFalse(status_zero["available"], status_zero)
        self.assertEqual([], status_zero["models"])
        self.assertIn("network connection failed", status_zero["blocker"])

    def test_agy_1127_exit_one_catalogue_with_auth_error_remains_blocked(self) -> None:
        status = self._status_for_models_probe(
            1,
            AGY_1127_MODELS,
            "Error: Please sign in to view available models.\n",
        )

        self.assertFalse(status["available"], status)
        self.assertEqual([], status["models"])
        self.assertIn("models exited 1", status["blocker"])
        self.assertIn("Please sign in", status["blocker"])

    def test_agy_1127_exit_one_empty_unknown_or_error_only_output_remains_blocked(self) -> None:
        for stdout, stderr in (
            ("", ""),
            ("not-a-model-list\n", ""),
            ("", "Error: network connection failed\n"),
        ):
            with self.subTest(stdout=stdout, stderr=stderr):
                control_plane._AGY_PROBE_CACHE = None
                status = self._status_for_models_probe(1, stdout, stderr)
                self.assertFalse(status["available"], status)
                self.assertEqual([], status["models"])
                self.assertIn("models exited 1", status["blocker"])

    def test_normal_zero_exit_with_valid_model_enumeration_remains_available(self) -> None:
        status = self._status_for_models_probe(0, "gemini-3.7-flash-high\n")

        self.assertTrue(status["available"], status)
        self.assertEqual(["gemini-3.7-flash-high"], status["models"])

    def test_status_and_task_start_share_one_canonical_probe_context(self) -> None:
        calls: list[tuple[list[str], str, dict[str, str], int]] = []

        def probe(argv, *, cwd, env):
            calls.append((argv, cwd, env, id(env)))
            if argv[1:] == ["--version"]:
                return self._result(argv, 0, "1.1.26\n")
            if argv[1:] == ["--help"]:
                return self._result(argv, 0, AGY_HELP)
            if argv[1:] == ["models"]:
                return self._result(
                    argv,
                    0,
                    "gemini-3.7-flash-high\n",
                    "Fetching available models...\n",
                )
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_agy = root / "agy.exe"
            fake_agy.write_text("placeholder", encoding="utf-8")
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            project = root / "project"
            project.mkdir()
            inherited = {
                "USERPROFILE": str(root / "profile"),
                "APPDATA": str(root / "profile" / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(root / "profile" / "AppData" / "Local"),
                "HTTPS_PROXY": "http://proxy.example.invalid:8080",
            }
            with mock.patch.dict(os.environ, inherited, clear=False), mock.patch.object(
                control_plane, "AGY_EXE", fake_agy
            ), mock.patch.object(control_plane, "JOBS_DIR", jobs_dir), mock.patch.object(
                control_plane, "_run_agy_probe", side_effect=probe
            ):
                async def status_then_start():
                    status_result = await server_legacy.agy_status()
                    start_result = await server_legacy.task_start(
                        harness="agy",
                        prompt="test only",
                        project=None,
                        cwd=str(project),
                        model="gemini-3.7-flash-high",
                        sandbox="workspace-write",
                        reasoning_effort="high",
                    )
                    return status_result, start_result

                status, started = asyncio.run(status_then_start())

            self.assertTrue(status["available"], status)
            self.assertEqual(["gemini-3.7-flash-high"], status["models"])
            self.assertTrue(started["ok"], started)
            self.assertEqual(3, len(calls), "task_start must reuse the status probe")
            self.assertEqual(1, len({call[3] for call in calls}))
            for argv, cwd, env, _env_id in calls:
                self.assertEqual(str(fake_agy), argv[0])
                self.assertEqual(os.getcwd(), cwd)
                for key, value in inherited.items():
                    self.assertEqual(value, env[key])

            state = json.loads(
                (jobs_dir / started["job_id"] / "status.json").read_text(encoding="utf-8")
            )
            self.assertEqual(status["executable"], state["agy_executable"])

    def test_model_auth_failure_remains_a_task_start_blocker(self) -> None:
        calls: list[list[str]] = []

        def probe(argv, *, cwd, env):
            calls.append(argv)
            self.assertEqual(os.getcwd(), cwd)
            self.assertIsInstance(env, dict)
            if argv[1:] == ["--version"]:
                return self._result(argv, 0, "1.1.26\n")
            if argv[1:] == ["--help"]:
                return self._result(argv, 0, AGY_HELP)
            if argv[1:] == ["models"]:
                return self._result(
                    argv,
                    1,
                    "",
                    "Fetching available models...\nError: Please sign in to view available models.\n",
                )
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_agy = root / "agy.exe"
            fake_agy.write_text("placeholder", encoding="utf-8")
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            project = root / "project"
            project.mkdir()
            with mock.patch.object(control_plane, "AGY_EXE", fake_agy), mock.patch.object(
                control_plane, "JOBS_DIR", jobs_dir
            ), mock.patch.object(control_plane, "_run_agy_probe", side_effect=probe):
                status = control_plane.harness_status("agy")
                started = control_plane.start_task(
                    harness="agy",
                    prompt="must not enqueue",
                    project=None,
                    cwd=str(project),
                    model=None,
                    sandbox="workspace-write",
                    reasoning_effort=None,
                )

            self.assertFalse(status["available"])
            self.assertFalse(started["ok"])
            self.assertIn("models exited 1", started["error"])
            self.assertIn("Please sign in", started["error"])
            self.assertEqual(3, len(calls), "the same failed canonical probe is reused")
            self.assertEqual([], list(jobs_dir.iterdir()))

    def test_profile_or_proxy_change_invalidates_a_successful_probe(self) -> None:
        models_calls = 0

        def probe(argv, *, cwd, env):
            nonlocal models_calls
            if argv[1:] == ["--version"]:
                return self._result(argv, 0, "1.1.26\n")
            if argv[1:] == ["--help"]:
                return self._result(argv, 0, AGY_HELP)
            if argv[1:] == ["models"]:
                models_calls += 1
                if env["USERPROFILE"].endswith("profile-one"):
                    return self._result(argv, 0, "gemini-3.7-flash-high\n")
                return self._result(argv, 1, "", "Fetching available models...\n")
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_agy = root / "agy.exe"
            fake_agy.write_text("placeholder", encoding="utf-8")
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            project = root / "project"
            project.mkdir()
            with mock.patch.object(control_plane, "AGY_EXE", fake_agy), mock.patch.object(
                control_plane, "JOBS_DIR", jobs_dir
            ), mock.patch.object(control_plane, "_run_agy_probe", side_effect=probe):
                with mock.patch.dict(
                    os.environ,
                    {"USERPROFILE": str(root / "profile-one"), "HTTPS_PROXY": "http://one.invalid"},
                    clear=False,
                ):
                    status = control_plane.harness_status("agy")
                with mock.patch.dict(
                    os.environ,
                    {"USERPROFILE": str(root / "profile-two"), "HTTPS_PROXY": "http://two.invalid"},
                    clear=False,
                ):
                    started = control_plane.start_task(
                        harness="agy",
                        prompt="must re-probe",
                        project=None,
                        cwd=str(project),
                        model=None,
                        sandbox="workspace-write",
                        reasoning_effort=None,
                    )

            self.assertTrue(status["available"])
            self.assertFalse(started["ok"])
            self.assertIn("models exited 1", started["error"])
            self.assertEqual(2, models_calls)
            self.assertEqual([], list(jobs_dir.iterdir()))

    def test_cache_expiry_forces_a_fresh_fail_closed_re_probe(self) -> None:
        """Requirement 4: a successful canonical probe that ages past the
        TTL must NOT be reused; the next call must run a full fail-closed
        re-probe and a concurrent ``task_start`` must observe the same
        post-expiry result.
        """
        probe_calls: list[list[str]] = []

        def probe(argv, *, cwd, env):
            probe_calls.append(list(argv))
            self.assertEqual(os.getcwd(), cwd)
            self.assertIsInstance(env, dict)
            if argv[1:] == ["--version"]:
                return self._result(argv, 0, "1.1.26\n")
            if argv[1:] == ["--help"]:
                return self._result(argv, 0, AGY_HELP)
            if argv[1:] == ["models"]:
                return self._result(argv, 0, "gemini-3.7-flash-high\n")
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_agy = root / "agy.exe"
            fake_agy.write_text("placeholder", encoding="utf-8")
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            project = root / "project"
            project.mkdir()
            with mock.patch.object(control_plane, "AGY_EXE", fake_agy), mock.patch.object(
                control_plane, "JOBS_DIR", jobs_dir
            ), mock.patch.object(
                control_plane, "AGY_PROBE_CACHE_TTL_SECONDS", 0.05
            ), mock.patch.object(
                control_plane, "_run_agy_probe", side_effect=probe
            ):
                # First call: full probe (3 subprocess calls)
                first = control_plane.harness_status("agy")
                # Within TTL: cache hit, zero new subprocess calls
                second = control_plane.harness_status("agy")
                # Wait past the TTL.
                time.sleep(0.1)
                third = control_plane.harness_status("agy")
                # After expiry: task_start must observe the freshly probed
                # canonical result and not reuse the stale pre-expiry one.
                started = control_plane.start_task(
                    harness="agy",
                    prompt="post-expiry task",
                    project=None,
                    cwd=str(project),
                    model="gemini-3.7-flash-high",
                    sandbox="workspace-write",
                    reasoning_effort="high",
                )

            self.assertTrue(first["available"], first)
            self.assertTrue(second["available"], second)
            self.assertTrue(third["available"], third)
            self.assertTrue(started["ok"], started)
            # 3 (first) + 0 (cached) + 3 (post-expiry re-probe) = 6
            self.assertEqual(
                6,
                len(probe_calls),
                f"cache expiry must force a full re-probe (got {len(probe_calls)} calls)",
            )
            # The post-expiry probe must have hit the same shared lock and
            # yielded the same canonical executable in task state.
            state = json.loads(
                (jobs_dir / started["job_id"] / "status.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(str(fake_agy), state["agy_executable"])
            self.assertEqual(third["executable"], state["agy_executable"])

    def test_appdata_and_localappdata_changes_invalidate_reuse(self) -> None:
        """Requirement 3 (extended): changes to APPDATA / LOCALAPPDATA /
        USERPROFILE / proxy / executable identity must each independently
        invalidate the cache. A fresh probe must run after the change.
        """
        # The total ``agy models`` subprocess calls per scenario must be
        # exactly 2: one for the pre-change baseline and one for the forced
        # re-probe after the context variable changed. The same-context
        # call in between must be served from cache (no extra subprocess).
        models_calls = 0

        def probe(argv, *, cwd, env):
            nonlocal models_calls
            if argv[1:] == ["models"]:
                models_calls += 1
                return self._result(argv, 0, "gemini-3.7-flash-high\n")
            if argv[1:] == ["--version"]:
                return self._result(argv, 0, "1.1.26\n")
            if argv[1:] == ["--help"]:
                return self._result(argv, 0, AGY_HELP)
            raise AssertionError(argv)

        scenarios = (
            (
                "APPDATA",
                "C:\\Users\\ExampleUser\\AppData\\Roaming",
                "C:\\Users\\Other\\AppData\\Roaming",
            ),
            (
                "LOCALAPPDATA",
                "C:\\Users\\ExampleUser\\AppData\\Local",
                "C:\\Users\\Other\\AppData\\Local",
            ),
            (
                "USERPROFILE",
                "C:\\Users\\ExampleUser",
                "C:\\Users\\Other",
            ),
            (
                "HTTPS_PROXY",
                "http://proxy-one.invalid:8080",
                "http://proxy-two.invalid:8080",
            ),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_agy = root / "agy.exe"
            fake_agy.write_text("placeholder", encoding="utf-8")
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            project = root / "project"
            project.mkdir()
            with mock.patch.object(control_plane, "AGY_EXE", fake_agy), mock.patch.object(
                control_plane, "JOBS_DIR", jobs_dir
            ), mock.patch.object(
                control_plane, "_run_agy_probe", side_effect=probe
            ):
                for variable, before, after in scenarios:
                    models_calls = 0
                    base_env = {
                        "USERPROFILE": "C:\\Users\\Constant",
                        "APPDATA": "C:\\Users\\ExampleUser\\AppData\\Roaming",
                        "LOCALAPPDATA": "C:\\Users\\ExampleUser\\AppData\\Local",
                        "HTTPS_PROXY": "http://proxy-one.invalid:8080",
                    }
                    with mock.patch.dict(os.environ, base_env, clear=False):
                        first = control_plane._agy_cli_status()
                    # Same context: should be served from cache.
                    with mock.patch.dict(os.environ, base_env, clear=False):
                        cached = control_plane._agy_cli_status()
                    # Mutate the relevant variable, leave the others unchanged.
                    base_env[variable] = after
                    with mock.patch.dict(os.environ, base_env, clear=False):
                        repoked = control_plane._agy_cli_status()
                    self.assertTrue(first["available"], (variable, first))
                    self.assertTrue(cached["available"], (variable, cached))
                    self.assertTrue(repoked["available"], (variable, repoked))
                    # Exactly 2 ``agy models`` subprocess calls per scenario:
                    # one pre-change, one post-change. The same-context
                    # call must be a cache hit.
                    self.assertEqual(
                        2,
                        models_calls,
                        (
                            f"{variable} change must force exactly one re-probe "
                            f"(got {models_calls} models calls)"
                        ),
                    )

    def test_executable_identity_change_invalidates_reuse(self) -> None:
        """A change to the AGY executable's path or on-disk identity (mtime /
        size) must invalidate the cache. The cached entry is otherwise keyed
        on a stable identity snapshot so a real update is never silently
        masked by a stale success.
        """
        probe_calls = 0

        def probe(argv, *, cwd, env):
            nonlocal probe_calls
            if argv[1:] == ["models"]:
                probe_calls += 1
                return self._result(argv, 0, "gemini-3.7-flash-high\n")
            if argv[1:] == ["--version"]:
                return self._result(argv, 0, "1.1.26\n")
            if argv[1:] == ["--help"]:
                return self._result(argv, 0, AGY_HELP)
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_agy_v1 = root / "agy-v1.exe"
            fake_agy_v1.write_text("placeholder-v1", encoding="utf-8")
            fake_agy_v2 = root / "agy-v2.exe"
            fake_agy_v2.write_text("placeholder-v2-longer", encoding="utf-8")
            with mock.patch.object(control_plane, "AGY_EXE", fake_agy_v1), mock.patch.object(
                control_plane, "_run_agy_probe", side_effect=probe
            ):
                first = control_plane._agy_cli_status()
                # Same exe, same env: cache hit.
                cached = control_plane._agy_cli_status()
                # Switch to a different exe path: must re-probe.
                with mock.patch.object(control_plane, "AGY_EXE", fake_agy_v2):
                    repoked = control_plane._agy_cli_status()

            self.assertTrue(first["available"])
            self.assertTrue(cached["available"])
            self.assertTrue(repoked["available"])
            self.assertEqual(2, probe_calls, "exe identity change must force re-probe")

    def test_agy_1127_abnormal_exit_two_with_complete_catalogue_is_available(self) -> None:
        status = self._status_for_models_probe(
            2,
            AGY_1127_MODELS,
            "Fetching available models...\n",
        )
        self.assertTrue(status["available"], status)
        self.assertEqual("1.1.27", status["version"])
        self.assertEqual(AGY_1127_MODELS.splitlines(), status["models"])

    def test_gemini_task_starts_successfully_and_records_job_and_executable(self) -> None:
        """Requirement 4: Gemini tasks must start cleanly and persist state."""
        def probe(argv, *, cwd, env):
            if argv[1:] == ["--version"]:
                return self._result(argv, 0, "1.1.27\n")
            if argv[1:] == ["--help"]:
                return self._result(argv, 0, AGY_HELP)
            if argv[1:] == ["models"]:
                return self._result(argv, 0, AGY_1127_MODELS)
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_agy = root / "agy.exe"
            fake_agy.write_text("placeholder", encoding="utf-8")
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            project = root / "project"
            project.mkdir()
            with mock.patch.object(control_plane, "AGY_EXE", fake_agy), mock.patch.object(
                control_plane, "JOBS_DIR", jobs_dir
            ), mock.patch.object(control_plane, "_run_agy_probe", side_effect=probe):
                started = control_plane.start_task(
                    harness="agy",
                    prompt="implement feature",
                    project=None,
                    cwd=str(project),
                    model="gemini-3.8-flash-high",
                    sandbox="workspace-write",
                    reasoning_effort="high",
                )

            self.assertTrue(started["ok"], started)
            self.assertEqual("agy", started["harness"])
            self.assertEqual("queued", started["status"])
            job_status_file = jobs_dir / started["job_id"] / "status.json"
            self.assertTrue(job_status_file.is_file())
            state = json.loads(job_status_file.read_text(encoding="utf-8"))
            self.assertEqual("gemini-3.8-flash-high", state["model"])
            self.assertEqual("high", state["reasoning_effort"])
            self.assertEqual(str(fake_agy), state["agy_executable"])

    def test_non_gemini_models_rejected_in_preflight_and_do_not_create_jobs(self) -> None:
        """Requirement 2 & 4: Claude, GPT-OSS, and other non-Gemini models
        must be rejected in preflight, never create jobs, and never be silently replaced.
        """
        def probe(argv, *, cwd, env):
            if argv[1:] == ["--version"]:
                return self._result(argv, 0, "1.1.27\n")
            if argv[1:] == ["--help"]:
                return self._result(argv, 0, AGY_HELP)
            if argv[1:] == ["models"]:
                return self._result(argv, 0, AGY_1127_MODELS)
            raise AssertionError(argv)

        forbidden_models = (
            "claude-sonnet-4-6",
            "claude-opus-4-6-thinking",
            "gpt-oss-120b-medium",
            "llama-3-70b",
            "gemma-2-9b-it",
            "deepseek-v3",
            "other-custom-model",
            "",
            None,
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_agy = root / "agy.exe"
            fake_agy.write_text("placeholder", encoding="utf-8")
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            project = root / "project"
            project.mkdir()
            with mock.patch.object(control_plane, "AGY_EXE", fake_agy), mock.patch.object(
                control_plane, "JOBS_DIR", jobs_dir
            ), mock.patch.object(control_plane, "_run_agy_probe", side_effect=probe):
                for candidate in forbidden_models:
                    with self.subTest(model=candidate):
                        started = control_plane.start_task(
                            harness="agy",
                            prompt="must be rejected",
                            project=None,
                            cwd=str(project),
                            model=candidate,
                            sandbox="workspace-write",
                            reasoning_effort=None,
                        )
                        self.assertFalse(started["ok"], (candidate, started))
                        self.assertIn("Gemini-only", started["error"])
                        self.assertEqual(
                            [],
                            list(jobs_dir.iterdir()),
                            f"non-Gemini model {candidate!r} must never create a job directory",
                        )

    def test_server_legacy_task_start_enforces_gemini_only_preflight(self) -> None:
        """server_legacy.task_start must also fail closed for non-Gemini models."""
        def probe(argv, *, cwd, env):
            if argv[1:] == ["--version"]:
                return self._result(argv, 0, "1.1.27\n")
            if argv[1:] == ["--help"]:
                return self._result(argv, 0, AGY_HELP)
            if argv[1:] == ["models"]:
                return self._result(argv, 0, AGY_1127_MODELS)
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_agy = root / "agy.exe"
            fake_agy.write_text("placeholder", encoding="utf-8")
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            project = root / "project"
            project.mkdir()
            with mock.patch.object(control_plane, "AGY_EXE", fake_agy), mock.patch.object(
                control_plane, "JOBS_DIR", jobs_dir
            ), mock.patch.object(control_plane, "_run_agy_probe", side_effect=probe):
                result = asyncio.run(
                    server_legacy.task_start(
                        harness="agy",
                        prompt="forbidden model",
                        cwd=str(project),
                        model="claude-sonnet-4-6",
                    )
                )
                self.assertFalse(result["ok"], result)
                self.assertIn("Gemini-only", result["error"])
                self.assertEqual([], list(jobs_dir.iterdir()))

    def test_gemini_effort_behavior(self) -> None:
        """Requirement 3 & 4: Valid Gemini reasoning_effort is mapped correctly,
        and invalid effort is rejected in preflight.
        """
        def probe(argv, *, cwd, env):
            if argv[1:] == ["--version"]:
                return self._result(argv, 0, "1.1.27\n")
            if argv[1:] == ["--help"]:
                return self._result(argv, 0, AGY_HELP)
            if argv[1:] == ["models"]:
                return self._result(argv, 0, AGY_1127_MODELS)
            raise AssertionError(argv)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_agy = root / "agy.exe"
            fake_agy.write_text("placeholder", encoding="utf-8")
            jobs_dir = root / "jobs"
            jobs_dir.mkdir()
            project = root / "project"
            project.mkdir()
            with mock.patch.object(control_plane, "AGY_EXE", fake_agy), mock.patch.object(
                control_plane, "JOBS_DIR", jobs_dir
            ), mock.patch.object(control_plane, "_run_agy_probe", side_effect=probe):
                for effort in ("low", "medium", "high"):
                    with self.subTest(effort=effort):
                        started = control_plane.start_task(
                            harness="agy",
                            prompt="effort test",
                            project=None,
                            cwd=str(project),
                            model="gemini-3.8-flash-medium",
                            sandbox="workspace-write",
                            reasoning_effort=effort,
                        )
                        self.assertTrue(started["ok"], started)
                        job_dir = jobs_dir / started["job_id"]
                        state = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
                        self.assertEqual(effort, state["reasoning_effort"])
                        cmd = control_plane.build_agy_command(state, job_dir / "result.txt")
                        self.assertIn("--effort", cmd)
                        self.assertEqual(effort, cmd[cmd.index("--effort") + 1])
                        self.assertIn("--model", cmd)
                        self.assertEqual("gemini-3.8-flash-medium", cmd[cmd.index("--model") + 1])

                # Invalid effort rejected without job creation
                jobs_before = list(jobs_dir.iterdir())
                invalid_result = control_plane.start_task(
                    harness="agy",
                    prompt="invalid effort test",
                    project=None,
                    cwd=str(project),
                    model="gemini-3.8-flash-medium",
                    sandbox="workspace-write",
                    reasoning_effort="extreme",
                )
                self.assertFalse(invalid_result["ok"])
                self.assertIn("reasoning_effort", invalid_result["error"])
                self.assertEqual(jobs_before, list(jobs_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
