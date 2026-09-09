import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import codex_job_worker
import control_plane
import server_legacy


class ControlPlaneTests(unittest.TestCase):
    def configure_temp_control(self, root: Path) -> None:
        self.patchers = [
            mock.patch.object(control_plane, "PROJECT_ROOT", root),
            mock.patch.object(control_plane, "JOBS_DIR", root / ".jobs"),
            mock.patch.object(control_plane, "CONTROL_DIR", root / ".control"),
            mock.patch.object(control_plane, "PROJECTS_FILE", root / ".control" / "projects.json"),
            mock.patch.object(control_plane, "BACKUPS_DIR", root / ".control" / "backups"),
            mock.patch.object(control_plane, "CODEX_CONFIG", root / "user-config.toml"),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.addCleanup(lambda: [patcher.stop() for patcher in reversed(self.patchers)])
        control_plane.CONTROL_DIR.mkdir()

    def write_projects(self, project: Path) -> None:
        control_plane.PROJECTS_FILE.write_text(
            json.dumps({"projects": {"fixture": {"path": str(project)}}}),
            encoding="utf-8",
        )

    def test_registry_file_and_project_alias_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            project = root / "project"
            project.mkdir()
            self.write_projects(project)

            self.assertEqual(project.resolve(), control_plane.resolve_project("fixture"))
            listed = control_plane.list_projects()
            self.assertEqual("fixture", listed[0]["alias"])
            self.assertTrue(listed[0]["exists"])

    def test_file_tools_authorization_write_append_and_config_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory, tempfile.TemporaryDirectory() as outside_directory:
            root = Path(temporary_directory)
            outside = Path(outside_directory)
            self.configure_temp_control(root)
            project = root / "project"
            project.mkdir()
            self.write_projects(project)

            target = project / "note.txt"
            self.assertTrue(control_plane.file_write_result(str(target), "one")["ok"])
            refused = control_plane.file_write_result(str(target), "two")
            self.assertFalse(refused["ok"])
            self.assertIn("overwrite=true", refused["error"])
            self.assertTrue(control_plane.file_write_result(str(target), "two", overwrite=True)["ok"])
            self.assertTrue(control_plane.file_append_result(str(target), "!", create=False)["ok"])
            self.assertEqual("two!", control_plane.file_read_result(str(target))["content"])
            self.assertTrue(control_plane.file_stat_result(str(target))["ok"])
            self.assertTrue(control_plane.directory_list_result(str(project))["ok"])
            self.assertFalse(control_plane.file_write_result(str(outside / "denied.txt"), "no")["ok"])

            config = control_plane.CODEX_CONFIG
            config.write_text("model = 'before'\n", encoding="utf-8")
            result = control_plane.file_write_result(str(config), "model = 'after'\n", overwrite=True)
            self.assertTrue(result["ok"])
            self.assertTrue(result["backup_path"])
            self.assertEqual("model = 'before'\n", Path(result["backup_path"]).read_text(encoding="utf-8"))
            self.assertEqual("model = 'after'\n", config.read_text(encoding="utf-8"))

    def test_generic_task_lifecycle_and_queued_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            project = root / "project"
            project.mkdir()
            self.write_projects(project)
            fake_codex = root / "codex.exe"
            fake_codex.write_text("placeholder", encoding="utf-8")

            with mock.patch.object(control_plane, "CODEX_EXE", fake_codex), mock.patch.dict(
                os.environ,
                {
                    control_plane.CODEX_CUSTOM_BASE_URL_ENV: "https://api.acme.test/v1",
                    control_plane.CODEX_CUSTOM_API_KEY_ENV: "sk-" + "test-secret-value-12345678",
                },
                clear=False,
            ):
                cancelled = control_plane.start_task(
                    harness="codex", prompt="cancel", project="fixture", cwd=None,
                    model=None, sandbox="read-only", reasoning_effort=None,
                )
                self.assertTrue(control_plane.cancel_task(cancelled["job_id"])["ok"])

                started = control_plane.start_task(
                    harness="codex", prompt="finish", project="fixture", cwd=None,
                    model="test-model", sandbox="read-only", reasoning_effort="high",
                )
                job_dir = control_plane.JOBS_DIR / started["job_id"]

                def fake_run(command, **_kwargs):
                    result_path = Path(command[command.index("-o") + 1])
                    result_path.write_text("done", encoding="utf-8")
                    return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

                with mock.patch.object(codex_job_worker.subprocess, "run", side_effect=fake_run):
                    codex_job_worker.main(job_dir)

                polled = control_plane.poll_task(started["job_id"])
                self.assertTrue(polled["ok"])
                self.assertEqual("completed", polled["status"])
                self.assertEqual("codex", polled["harness"])
                self.assertEqual("fixture", polled["project"])
                self.assertEqual("done", polled["final_message"])
                self.assertEqual("current", polled["route_requested"])
                self.assertEqual("current", polled["route_used"])
                self.assertFalse(polled["fallback_used"])
                self.assertIsNone(polled["fallback_reason"])
                self.assertEqual(["current"], [item["route"] for item in polled["attempts"]])
                argv = polled["native_process"]["argv"]
                self.assertIn("--model", argv)
                self.assertIn('model_reasoning_effort="high"', argv)

    def test_codex_routes_are_accepted_and_non_codex_routes_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            project = root / "project"
            project.mkdir()
            self.write_projects(project)
            fake_codex = root / "codex.exe"
            fake_codex.write_text("placeholder", encoding="utf-8")
            with mock.patch.object(control_plane, "CODEX_EXE", fake_codex), mock.patch.dict(
                os.environ,
                {
                    control_plane.CODEX_CUSTOM_BASE_URL_ENV: "https://api.acme.test/v1",
                    control_plane.CODEX_CUSTOM_API_KEY_ENV: "sk-" + "test-secret-value-12345678",
                },
                clear=False,
            ):
                for route in ("current", "official", "custom", "official_then_custom"):
                    result = control_plane.start_task(
                        harness="codex", prompt="route", project="fixture", cwd=None,
                        model=None, sandbox="read-only", reasoning_effort=None, route=route,
                    )
                    self.assertTrue(result["ok"], result)
                    state = json.loads(
                        (control_plane.JOBS_DIR / result["job_id"] / "status.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    self.assertEqual(route, state["route_requested"])
                    self.assertEqual(control_plane.codex_route_attempts(route)[0], state["route_used"])
                refused = control_plane.start_task(
                    harness="minimax", prompt="route", project="fixture", cwd=None,
                    model=None, sandbox="workspace-write", reasoning_effort=None, route="custom",
                )
            self.assertFalse(refused["ok"])
            self.assertIn("route=current", refused["error"])

    def test_custom_route_fails_closed_when_configuration_is_missing_or_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            project = root / "project"
            project.mkdir()
            self.write_projects(project)
            fake_codex = root / "codex.exe"
            fake_codex.write_text("placeholder", encoding="utf-8")
            with mock.patch.object(control_plane, "CODEX_EXE", fake_codex), mock.patch.dict(
                os.environ,
                {
                    control_plane.CODEX_CUSTOM_BASE_URL_ENV: "not-a-url",
                    control_plane.CODEX_CUSTOM_API_KEY_ENV: "",
                },
                clear=False,
            ):
                missing = control_plane.start_task(
                    harness="codex", prompt="route", project="fixture", cwd=None,
                    model=None, sandbox="read-only", reasoning_effort=None, route="custom",
                )
                self.assertFalse(missing["ok"])
                self.assertIn("custom route requires", missing["error"])

                os.environ[control_plane.CODEX_CUSTOM_API_KEY_ENV] = "sk-" + "test-secret-value-12345678"
                invalid = control_plane.start_task(
                    harness="codex", prompt="route", project="fixture", cwd=None,
                    model=None, sandbox="read-only", reasoning_effort=None, route="official_then_custom",
                )
                self.assertFalse(invalid["ok"])
                self.assertIn("must be an http(s) URL", invalid["error"])

    @staticmethod
    def minimax_probe(argv, **_kwargs) -> subprocess.CompletedProcess:
        args = argv[1:]
        if args == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, stdout="0.2.6\n", stderr="")
        if args == ["--help"]:
            return subprocess.CompletedProcess(argv, 0, stdout="Usage: mcode [options] [command]\n  exec [options] [prompt]\n", stderr="")
        if args == ["exec", "--help"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=(
                    "Usage: mcode exec [options] [prompt]\n"
                    "  --cwd <path>\n"
                    "  --model <provider/model>\n"
                    "  --permission <policy>\n"
                    "  --output-format <format> text, json, or stream-json\n"
                    "  -o, --output-last-message <path>\n"
                ),
                stderr="",
            )
        raise AssertionError(argv)

    def test_minimax_cli_found_is_available_after_capability_probes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_mcode = Path(temporary_directory) / "mcode.cmd"
            fake_mcode.write_text("placeholder", encoding="utf-8")
            missing_agy = Path(temporary_directory) / "missing" / "agy.exe"
            with mock.patch.object(control_plane, "MINIMAX_CLI_EXE", fake_mcode), mock.patch.object(
                control_plane, "AGY_EXE", missing_agy
            ), mock.patch.object(
                control_plane.shutil, "which", return_value=None
            ), mock.patch.object(control_plane, "run_safe_subprocess", side_effect=self.minimax_probe):
                status = control_plane.harness_status("minimax")

            self.assertTrue(status["ok"])
            self.assertTrue(status["available"])
            self.assertTrue(status["supports_async"])
            self.assertEqual("0.2.6", status["version"])
            self.assertEqual([str(fake_mcode.resolve()), "exec"], status["noninteractive_command"])
            self.assertTrue(status["capabilities"]["output_last_message"])

    def test_minimax_cli_missing_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "missing" / "mcode.cmd"
            with mock.patch.object(control_plane, "MINIMAX_CLI_EXE", missing), mock.patch.object(
                control_plane.shutil, "which", return_value=None
            ):
                status = control_plane.harness_status("minimax")

            self.assertTrue(status["ok"])
            self.assertFalse(status["available"])
            self.assertFalse(status["supports_async"])
            self.assertIn("MiniMax CLI not found", status["blocker"])
            self.assertIsNone(status["noninteractive_command"])

    def test_minimax_command_construction_and_explicit_unsupported_mappings(self) -> None:
        result_path = Path("result.txt")
        command = control_plane.build_minimax_command(
            {
                "minimax_executable": r"C:\fixture\mcode.cmd",
                "cwd": r"C:\fixture\repo",
                "model": "provider/model",
                "prompt": "finish",
            },
            result_path,
        )
        self.assertEqual(
            [
                r"C:\fixture\mcode.cmd", "exec", "--cwd", r"C:\fixture\repo",
                "--output-format", "json", "--output-last-message", str(result_path),
                "--model", "provider/model", "--input", "-",
            ],
            command,
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            project = root / "project"
            project.mkdir()
            self.write_projects(project)
            fake_mcode = root / "mcode.cmd"
            fake_mcode.write_text("placeholder", encoding="utf-8")
            with mock.patch.object(control_plane, "MINIMAX_CLI_EXE", fake_mcode), mock.patch.object(
                control_plane.shutil, "which", return_value=None
            ), mock.patch.object(control_plane, "run_safe_subprocess", side_effect=self.minimax_probe):
                reasoning = control_plane.start_task(
                    harness="minimax", prompt="x", project="fixture", cwd=None,
                    model=None, sandbox="workspace-write", reasoning_effort="high",
                )
                read_only = control_plane.start_task(
                    harness="minimax", prompt="x", project="fixture", cwd=None,
                    model=None, sandbox="read-only", reasoning_effort=None,
                )
            self.assertFalse(reasoning["ok"])
            self.assertIn("reasoning_effort", reasoning["error"])
            self.assertFalse(read_only["ok"])
            self.assertIn("read-only sandbox", read_only["error"])

    def test_minimax_task_success_and_nonzero_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            project = root / "project"
            project.mkdir()
            self.write_projects(project)
            fake_mcode = root / "mcode.cmd"
            fake_mcode.write_text("placeholder", encoding="utf-8")
            missing_agy = root / "missing" / "agy.exe"
            probe_patch = mock.patch.object(control_plane, "run_safe_subprocess", side_effect=self.minimax_probe)
            with mock.patch.object(control_plane, "MINIMAX_CLI_EXE", fake_mcode), mock.patch.object(
                control_plane, "AGY_EXE", missing_agy
            ), mock.patch.object(
                control_plane.shutil, "which", return_value=None
            ), probe_patch:
                succeeded = control_plane.start_task(
                    harness="minimax", prompt="finish", project="fixture", cwd=None,
                    model="provider/model", sandbox="workspace-write", reasoning_effort=None,
                )
                failed = control_plane.start_task(
                    harness="minimax", prompt="fail", project="fixture", cwd=None,
                    model=None, sandbox="workspace-write", reasoning_effort=None,
                )

            success_dir = control_plane.JOBS_DIR / succeeded["job_id"]
            failure_dir = control_plane.JOBS_DIR / failed["job_id"]

            def success_lifecycle(command, result_path, **_kwargs):
                # Write the same content both into result.txt (used by
                # the worker) and into a JSON stdout (the realistic
                # mcode exec output) for coverage of the bounded
                # stdout tail.
                result_path.write_text("MINIMAX_DONE", encoding="utf-8")
                stdout = json.dumps({
                    "schemaVersion": 1,
                    "type": "exec.result",
                    "status": "succeeded",
                    "output": "MINIMAX_DONE",
                })
                return {
                    "exit_code": 0,
                    "stdout": stdout,
                    "stderr": "",
                    "termination_reason": "self_exit",
                    "forced_exit": False,
                    "result_completed_at": 0.0,
                    "result_text": "MINIMAX_DONE",
                }

            with mock.patch.object(codex_job_worker, "run_minimax_with_lifecycle", side_effect=success_lifecycle):
                codex_job_worker.main(success_dir)
            completed = control_plane.poll_task(succeeded["job_id"])
            self.assertEqual("completed", completed["status"])
            self.assertEqual("MINIMAX_DONE", completed["final_message"])
            self.assertEqual("minimax", completed["harness"])
            self.assertIn("exec", completed["native_process"]["argv"])

            huge_stdout = "x" * (codex_job_worker.OUTPUT_TAIL_CHARS + 100)
            huge_stderr = "y" * (codex_job_worker.OUTPUT_TAIL_CHARS + 100)

            def failure_lifecycle(command, result_path, **_kwargs):
                return {
                    "exit_code": 7,
                    "stdout": huge_stdout,
                    "stderr": huge_stderr,
                    "termination_reason": "self_exit",
                    "forced_exit": False,
                    "result_completed_at": None,
                    "result_text": None,
                }

            with mock.patch.object(codex_job_worker, "run_minimax_with_lifecycle", side_effect=failure_lifecycle):
                codex_job_worker.main(failure_dir)
            failure = control_plane.poll_task(failed["job_id"])
            self.assertEqual("failed", failure["status"])
            self.assertEqual("minimax_execution_error", failure["failure_type"])
            self.assertEqual(7, failure["exit_code"])
            self.assertLessEqual(len(failure["stdout_tail"]), codex_job_worker.OUTPUT_TAIL_CHARS)
            self.assertLessEqual(len(failure["stderr_tail"]), codex_job_worker.OUTPUT_TAIL_CHARS)

    def test_git_tools_on_temporary_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            repo = Path(temporary_directory) / "repo"
            repo.mkdir()
            for args in (["init"], ["config", "user.email", "test@example.invalid"], ["config", "user.name", "Test User"]):
                subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
            tracked = repo / "tracked.txt"
            tracked.write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
            tracked.write_text("changed\n", encoding="utf-8")

            self.assertTrue(control_plane.git_status_result(str(repo))["ok"])
            self.assertTrue(control_plane.git_diff_result(str(repo))["ok"])
            self.assertTrue(control_plane.git_branch_result(str(repo))["ok"])
            self.assertTrue(control_plane.git_log_result(str(repo), 1)["ok"])
            self.assertTrue(control_plane.git_worktree_list_result(str(repo))["ok"])
            self.assertTrue(control_plane.git_rev_parse_result(str(repo))["ok"])
            self.assertTrue(control_plane.git_add_result(str(repo), ["tracked.txt"])["ok"])
            committed = control_plane.git_commit_result(str(repo), "update tracked")
            self.assertTrue(committed["ok"], committed)

    def test_existing_and_new_mcp_tools_register(self) -> None:
        names = set(server_legacy.mcp._tool_manager._tools)
        self.assertTrue({"codex_status", "codex_run", "codex_start", "codex_poll"}.issubset(names))
        self.assertTrue({
            "harness_list", "harness_status", "task_start", "task_poll", "task_cancel",
            "project_list", "project_resolve", "file_read", "file_write", "file_append",
            "file_stat", "directory_list", "git_status", "git_diff", "git_branch", "git_log",
            "git_worktree_list", "git_rev_parse", "git_add", "git_commit",
        }.issubset(names))


if __name__ == "__main__":
    unittest.main()
