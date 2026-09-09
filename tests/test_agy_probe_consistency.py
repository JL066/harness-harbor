"""Regression tests for the public AGY CLI capability probe.

The public edition exposes the strict stderr model-catalogue fallback
for AGY 1.1.27 while keeping stdout authoritative.  These tests
exercise only the ``control_plane.harness_status("agy")`` surface, the
canonical probe cache, and the semantic model-catalogue validation
helpers — never ``start_task`` or the production-only Gemini-only
preflight, which is intentionally not part of the public release.
"""

import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import control_plane


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

    # ---- Required case 1: empty stdout + stderr catalogue + banner + exit 1 ----

    def test_agy_1127_exit_one_with_complete_model_catalogue_is_available(self) -> None:
        status = self._status_for_models_probe(
            1,
            AGY_1127_MODELS,
            "Fetching available models...\n",
        )

        self.assertTrue(status["available"], status)
        self.assertEqual("1.1.27", status["version"])
        self.assertEqual(AGY_1127_MODELS.splitlines(), status["models"])

    def test_agy_1127_stderr_catalogue_fallback_is_available(self) -> None:
        """AGY 1.1.27 may emit the valid catalogue on stderr before exiting 1."""
        status = self._status_for_models_probe(
            1,
            "",
            AGY_1127_MODELS + "Fetching available models...\n",
        )

        self.assertTrue(status["available"], status)
        self.assertEqual(AGY_1127_MODELS.splitlines(), status["models"])

    def test_agy_1127_abnormal_exit_two_with_complete_catalogue_is_available(self) -> None:
        status = self._status_for_models_probe(
            2,
            AGY_1127_MODELS,
            "Fetching available models...\n",
        )
        self.assertTrue(status["available"], status)
        self.assertEqual("1.1.27", status["version"])
        self.assertEqual(AGY_1127_MODELS.splitlines(), status["models"])

    # ---- Required case 2: stderr catalogue + sign-in/network/error => false ----

    def test_agy_stderr_catalogue_with_diagnostics_fails_closed(self) -> None:
        for diagnostic in (
            "Error: Please sign in to view available models.\n",
            "Error: network connection failed\n",
            "Error: unable to fetch available models\n",
        ):
            with self.subTest(diagnostic=diagnostic):
                control_plane._AGY_PROBE_CACHE = None
                status = self._status_for_models_probe(
                    1,
                    "",
                    AGY_1127_MODELS + diagnostic,
                )
                self.assertFalse(status["available"], status)
                self.assertEqual([], status["models"])

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

    def test_models_probe_with_unexpected_error_occurred_fails_closed(self) -> None:
        """A valid stdout catalogue must still be rejected when stderr carries an
        explicit generic error diagnostic such as ``Unexpected error occurred``.
        """
        status = self._status_for_models_probe(
            1,
            AGY_1127_MODELS,
            "Unexpected error occurred while enumerating models.\n",
        )

        self.assertFalse(status["available"], status)
        self.assertEqual([], status["models"])
        self.assertIn("Unexpected error occurred", status["blocker"])

    def test_models_probe_with_dns_resolution_failed_fails_closed(self) -> None:
        """A valid stdout catalogue must still be rejected when stderr carries a
        DNS resolution failure diagnostic such as ``DNS resolution failed``.
        """
        status = self._status_for_models_probe(
            1,
            AGY_1127_MODELS,
            "DNS resolution failed for models endpoint\n",
        )

        self.assertFalse(status["available"], status)
        self.assertEqual([], status["models"])
        self.assertIn("DNS resolution failed", status["blocker"])

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

    # ---- Required case 3: garbage stderr => false ----

    def test_garbage_stderr_is_not_parsed_as_models(self) -> None:
        status = self._status_for_models_probe(1, "", "some arbitrary stderr words\n")

        self.assertFalse(status["available"], status)
        self.assertEqual([], status["models"])

    # ---- Required case 4: preserve existing stdout semantics ----

    def test_normal_zero_exit_with_valid_model_enumeration_remains_available(self) -> None:
        status = self._status_for_models_probe(0, "gemini-3.7-flash-high\n")

        self.assertTrue(status["available"], status)
        self.assertEqual(["gemini-3.7-flash-high"], status["models"])

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

    # ---- Probe cache invalidation: public env-context concern ----

    def test_appdata_and_localappdata_changes_invalidate_reuse(self) -> None:
        """Changes to APPDATA / LOCALAPPDATA / USERPROFILE / proxy must each
        independently invalidate the cache. A fresh probe must run after the
        change. This test uses a temporary directory for portability and to
        avoid referencing any real user path.
        """
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

        with tempfile.TemporaryDirectory() as temporary_directory:
            profile_root = Path(temporary_directory) / "profile"
            profile_root.mkdir()
            profile_one = profile_root / "profile-one"
            profile_two = profile_root / "profile-two"
            appdata_one = profile_root / "appdata-one"
            appdata_two = profile_root / "appdata-two"
            scenarios = (
                (
                    "USERPROFILE",
                    str(profile_one),
                    str(profile_two),
                ),
                (
                    "APPDATA",
                    str(appdata_one),
                    str(appdata_two),
                ),
                (
                    "LOCALAPPDATA",
                    str(appdata_one),
                    str(appdata_two),
                ),
                (
                    "HTTPS_PROXY",
                    "http://proxy-one.invalid:8080",
                    "http://proxy-two.invalid:8080",
                ),
            )
            fake_agy = Path(temporary_directory) / "agy.exe"
            fake_agy.write_text("placeholder", encoding="utf-8")
            with mock.patch.object(control_plane, "AGY_EXE", fake_agy), mock.patch.object(
                control_plane, "_run_agy_probe", side_effect=probe
            ):
                for variable, before, after in scenarios:
                    models_calls = 0
                    base_env = {
                        "USERPROFILE": str(profile_one),
                        "APPDATA": str(appdata_one),
                        "LOCALAPPDATA": str(appdata_one),
                        "HTTPS_PROXY": "http://proxy-one.invalid:8080",
                    }
                    with mock.patch.dict(mock.os.environ if hasattr(mock, "os") else __import__("os").environ, base_env, clear=False):
                        first = control_plane._agy_cli_status()
                    # Same context: should be served from cache.
                    with mock.patch.dict(__import__("os").environ, base_env, clear=False):
                        cached = control_plane._agy_cli_status()
                    # Mutate the relevant variable, leave the others unchanged.
                    base_env[variable] = after
                    with mock.patch.dict(__import__("os").environ, base_env, clear=False):
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


if __name__ == "__main__":
    unittest.main()
