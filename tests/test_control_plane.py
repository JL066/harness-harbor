import json
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
from runtime_liveness import ProcessLiveness


class ControlPlaneTests(unittest.TestCase):
    def configure_temp_control(self, root: Path) -> None:
        self.patchers = [
            mock.patch.object(control_plane, "PROJECT_ROOT", root),
            mock.patch.object(control_plane, "JOBS_DIR", root / ".jobs"),
            mock.patch.object(control_plane, "CONTROL_DIR", root / ".control"),
            mock.patch.object(control_plane, "PROJECTS_FILE", root / ".control" / "projects.json"),
            mock.patch.object(control_plane, "ROUTE_STATE_FILE", root / ".control" / "route-state.json"),
            mock.patch.object(control_plane, "ROUTE_LOCK_FILE", root / ".control" / "route.lock"),
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

    def write_job_state(self, directory_id: str, cwd: Path, **fields) -> Path:
        control_plane.JOBS_DIR.mkdir(parents=True, exist_ok=True)
        job_dir = control_plane.JOBS_DIR / directory_id
        job_dir.mkdir()
        state = {
            "job_id": directory_id,
            "harness": "agy",
            "cwd": str(cwd),
            "queue_root_fingerprint": control_plane.queue_root_fingerprint(control_plane.JOBS_DIR),
        }
        state.update(fields)
        state_path = job_dir / "status.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        return state_path

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

    def test_task_cwd_grants_only_known_active_scoped_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            profile = root / "profiles" / "user"
            temp_root = root / "temp-root"
            system_root = root / "windows"
            program_files = root / "program-files"
            program_data = root / "program-data"
            narrow = temp_root / "workspace"
            for path in (profile, temp_root, system_root / "System32", program_files, program_data, narrow):
                path.mkdir(parents=True, exist_ok=True)

            environment = {
                "USERPROFILE": str(profile),
                "SystemRoot": str(system_root),
                "WINDIR": str(system_root),
                "ProgramFiles": str(program_files),
                "ProgramData": str(program_data),
            }
            with mock.patch.object(control_plane.Path, "home", return_value=profile), \
                 mock.patch.object(control_plane.tempfile, "gettempdir", return_value=temp_root), \
                 mock.patch.dict(control_plane.os.environ, environment):
                broad_roots = [
                    profile,
                    profile.parent,
                    temp_root,
                    system_root,
                    system_root / "System32",
                    program_files,
                    program_data,
                    Path(profile.anchor),
                ]
                for index, broad_root in enumerate(broad_roots):
                    if not broad_root.is_dir():
                        continue
                    job_id = f"broad{index:026d}"
                    with self.subTest(broad_root=broad_root):
                        self.write_job_state(job_id, broad_root, status="running")
                        self.assertNotIn(broad_root.resolve(), control_plane.write_roots())
                        denied = control_plane.file_write_result(str(broad_root / "denied.txt"), "no")
                        self.assertFalse(denied["ok"], denied)

                for index, status in enumerate(("queued", "running", "cancelling")):
                    job_id = f"narrow{index:026d}"
                    self.write_job_state(job_id, narrow, status=status)
                    self.assertNotIn(narrow.resolve(), control_plane.write_roots())
                denied = control_plane.file_write_result(str(narrow / "active.txt"), "no")
                self.assertFalse(denied["ok"], denied)

    def test_live_owned_worker_grants_only_its_narrow_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            workdir = root / "active-workdir"
            workdir.mkdir()
            state_path = self.write_job_state("live-owned-worker", workdir, status="running")
            from harbor_platform import process

            owned = process.spawn_owned([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                identity = process.process_identity(owned.pid)
                self.assertIsNotNone(identity)
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["stop_ownership"] = {
                    "job_id": "live-owned-worker", "harness": "agy",
                    "queue_root": str(control_plane.JOBS_DIR.resolve()),
                    "generation": "a" * 32, "worker": identity, "native": None,
                }
                state_path.write_text(json.dumps(state), encoding="utf-8")
                (state_path.parent / "worker.lock").write_text(f"{owned.pid} fixture\n", encoding="utf-8")

                self.assertTrue(process.owner_identity_valid(identity))
                self.assertIn(owned.pid, process.descendants(identity))
                self.assertIn(workdir.resolve(), control_plane.write_roots())
                allowed = control_plane.file_write_result(str(workdir / "active.txt"), "yes")
                self.assertTrue(allowed["ok"], allowed)
            finally:
                self.assertTrue(process.terminate_tree(owned, grace=0.2))

    def test_running_cwd_requires_present_dead_unknown_and_matching_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            for index, case in enumerate(("missing", "dead", "unknown", "identity-mismatch")):
                workdir = root / case
                workdir.mkdir()
                state_path = self.write_job_state(f"owner-{case}", workdir, status="running")
                if case != "missing":
                    pid = 44000 + index
                    identity = {"pid": pid, "started_at": str(pid), "job_name": f"Local\\HarnessHarbor-{pid}-{pid}"}
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    state["stop_ownership"] = {
                        "job_id": f"owner-{case}", "harness": "agy",
                        "queue_root": str(control_plane.JOBS_DIR.resolve()),
                        "generation": "b" * 32, "worker": identity,
                    }
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                    (state_path.parent / "worker.lock").write_text(f"{pid} test\n", encoding="utf-8")

            cases = {
                "missing": (None, None),
                "dead": (ProcessLiveness.DEAD, True),
                "unknown": (ProcessLiveness.UNKNOWN, True),
                "identity-mismatch": (ProcessLiveness.ALIVE, False),
            }
            for case, (liveness, identity_matches) in cases.items():
                with self.subTest(case=case):
                    patches = [
                        mock.patch("runtime_liveness.probe_pid_liveness", return_value=liveness),
                        mock.patch("harbor_platform.process.owner_identity_valid", return_value=identity_matches),
                        mock.patch("harbor_platform.process.descendants", side_effect=lambda identity: [identity["pid"]]),
                    ]
                    for patcher in patches:
                        patcher.start()
                    try:
                        target = root / case / "denied.txt"
                        self.assertNotIn((root / case).resolve(), control_plane.write_roots())
                        self.assertFalse(control_plane.file_write_result(str(target), "no")["ok"])
                    finally:
                        for patcher in reversed(patches):
                            patcher.stop()

    def test_terminal_and_malformed_job_metadata_never_grants_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            profile = root / "profile"
            completed_workdir = root / "completed-workspace"
            profile.mkdir()
            completed_workdir.mkdir()
            with mock.patch.object(control_plane.Path, "home", return_value=profile):
                terminal_statuses = ("completed", "failed", "cancelled", "canceled", "succeeded", "stopped")
                for index, status in enumerate(terminal_statuses):
                    with self.subTest(status=status):
                        self.write_job_state(f"terminal{index:024d}", profile, status=status)
                        self.assertNotIn(profile.resolve(), control_plane.write_roots())

                self.write_job_state("completed-narrow", completed_workdir, status="completed")
                self.assertNotIn(completed_workdir.resolve(), control_plane.write_roots())
                denied = control_plane.file_write_result(str(completed_workdir / "denied.txt"), "no")
                self.assertFalse(denied["ok"], denied)
                self.assertFalse((completed_workdir / "denied.txt").exists())

                invalid_jobs = (
                    ("missingstatus", {}),
                    ("unknownstatus", {"status": "waiting"}),
                    ("malformedstatus", {"status": ["running"]}),
                    ("malformedharness", {"status": "running", "harness": ["agy"]}),
                    ("wrongjobid", {"status": "running", "job_id": "another-job"}),
                    ("foreignqueue", {"status": "running", "queue_root_fingerprint": "foreign"}),
                )
                for name, fields in invalid_jobs:
                    with self.subTest(metadata=name):
                        job_id = f"invalid-{name}"
                        self.write_job_state(job_id, profile, **fields)
                        self.assertNotIn(profile.resolve(), control_plane.write_roots())

                malformed_id = "malformed-json"
                control_plane.JOBS_DIR.mkdir(parents=True, exist_ok=True)
                malformed_job_dir = control_plane.JOBS_DIR / malformed_id
                malformed_job_dir.mkdir()
                (malformed_job_dir / "status.json").write_text("{", encoding="utf-8")
                self.assertNotIn(profile.resolve(), control_plane.write_roots())

                missing_id = "missing-status-file"
                (control_plane.JOBS_DIR / missing_id).mkdir()
                self.assertNotIn(profile.resolve(), control_plane.write_roots())

    def test_registered_project_and_control_roots_survive_historical_task_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            profile = root / "profile"
            profile.mkdir()
            project = root / "project"
            project.mkdir()
            self.write_projects(project)
            state_path = self.write_job_state("active-profile-root", profile, status="running")
            identity = {"pid": 45000, "started_at": "45000", "job_name": "Local\\HarnessHarbor-45000-45000"}
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["stop_ownership"] = {
                "job_id": "active-profile-root", "harness": "agy",
                "queue_root": str(control_plane.JOBS_DIR.resolve()), "generation": "c" * 32,
                "worker": identity,
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")
            (state_path.parent / "worker.lock").write_text("45000 test\n", encoding="utf-8")
            with mock.patch.object(control_plane.Path, "home", return_value=profile), \
                 mock.patch("runtime_liveness.probe_pid_liveness", return_value=ProcessLiveness.ALIVE), \
                 mock.patch("harbor_platform.process.owner_identity_valid", return_value=True), \
                 mock.patch("harbor_platform.process.descendants", return_value=[45000]):
                self.write_job_state("completed-history", profile, status="completed")
                roots = control_plane.write_roots()
                self.assertIn(project.resolve(), roots)
                self.assertIn(control_plane.CONTROL_DIR.resolve(), roots)
                self.assertNotIn(profile.resolve(), roots)
                self.assertTrue(control_plane.file_write_result(str(project / "project.txt"), "ok")["ok"])
                self.assertTrue(control_plane.file_write_result(str(control_plane.CONTROL_DIR / "control.txt"), "ok")["ok"])

    def test_real_queued_task_cwd_is_scoped_until_terminal_transition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            workdir = root / "active-workdir"
            workdir.mkdir()
            fake_codex = root / "codex.exe"
            fake_codex.write_text("placeholder", encoding="utf-8")

            with mock.patch.object(control_plane, "CODEX_EXE", fake_codex), \
                 mock.patch.object(control_plane, "is_stale_harbor_managed_codex_config", return_value=(False, None)):
                started = control_plane.start_task(
                    harness="codex", prompt="active cwd authorization", project=None, cwd=str(workdir),
                    model=None, sandbox="workspace-write", reasoning_effort=None,
                )
                self.assertTrue(started["ok"], started)
                self.assertNotIn(workdir.resolve(), control_plane.write_roots())
                self.assertFalse(control_plane.file_write_result(str(workdir / "active.txt"), "active")["ok"])

                cancelled = control_plane.cancel_task(started["job_id"])
                self.assertTrue(cancelled["ok"], cancelled)
                self.assertNotIn(workdir.resolve(), control_plane.write_roots())

    def test_historical_profile_root_does_not_authorize_file_or_git_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            profile = root / "profile"
            temp_root = profile / "AppData" / "Local" / "Temp"
            target = temp_root / "Harbor-UAT-v117-unauthorized"
            repo = profile / "repo"
            init_target = target / "init-target"
            target.mkdir(parents=True)
            repo.mkdir(parents=True)
            init_target.mkdir()
            subprocess.run(["git", "init", "--initial-branch=main", str(repo)], check=True, capture_output=True)

            with mock.patch.object(control_plane.Path, "home", return_value=profile):
                self.write_job_state("completed-agy-history", profile, status="completed")
                readable = target / "readable.txt"
                readable.write_text("read only", encoding="utf-8")
                self.assertTrue(control_plane.file_read_result(str(readable))["ok"])
                self.assertTrue(control_plane.git_status_result(str(repo))["ok"])

                delivery_calls: list[list[str]] = []
                original_delivery = control_plane._git_delivery_command

                def capture_delivery(args, root_path, timeout=30):
                    delivery_calls.append(list(args))
                    return original_delivery(args, root_path, timeout=timeout)

                with mock.patch.object(control_plane, "_git_delivery_command", side_effect=capture_delivery):
                    file_result = control_plane.file_write_result(str(target / "write.txt"), "no")
                    init_result = control_plane.git_init_result(str(init_target))
                    fetch_result = control_plane.git_fetch_result(str(repo), "origin")
                    create_result = control_plane.git_worktree_create_result(
                        str(repo), str(profile / "new-worktree"), "feature/denied", "0" * 40
                    )
                    remove_result = control_plane.git_worktree_remove_result(str(repo), str(profile / "linked"))

                self.assertFalse(file_result["ok"], file_result)
                for result in (init_result, fetch_result, create_result, remove_result):
                    self.assertFalse(result["ok"], result)
                    self.assertEqual("refused", result["state"])
                self.assertEqual([], delivery_calls)
                self.assertFalse((init_target / ".git").exists())
                self.assertFalse((target / "write.txt").exists())

    def test_task_cwd_path_traversal_cannot_escape_active_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            self.write_job_state("traversal-job", allowed, status="running")

            result = control_plane.file_write_result(str(allowed / ".." / "outside" / "escape.txt"), "no")
            self.assertFalse(result["ok"], result)
            self.assertFalse((outside / "escape.txt").exists())

    def test_task_cwd_symlink_cannot_escape_active_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            self.write_job_state("symlink-job", allowed, status="running")
            link = allowed / "redirect"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"directory symlink unavailable: {type(exc).__name__}")

            result = control_plane.file_write_result(str(link / "escape.txt"), "no")
            self.assertFalse(result["ok"], result)
            self.assertFalse((outside / "escape.txt").exists())

    def test_generic_task_lifecycle_and_queued_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            project = root / "project"
            project.mkdir()
            self.write_projects(project)
            fake_codex = root / "codex.exe"
            fake_codex.write_text("placeholder", encoding="utf-8")

            with mock.patch.object(control_plane, "CODEX_EXE", fake_codex):
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

                with mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", side_effect=fake_run):
                    codex_job_worker.main(job_dir)

                polled = control_plane.poll_task(started["job_id"])
                self.assertTrue(polled["ok"])
                self.assertEqual("completed", polled["status"])
                self.assertEqual("codex", polled["harness"])
                self.assertEqual("fixture", polled["project"])
                self.assertEqual("done", polled["final_message"])
                argv = polled["native_process"]["argv"]
                self.assertIn("--model", argv)
                self.assertIn('model_reasoning_effort="high"', argv)

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
                    "  --input <value> use - to read stdin\n"
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
                "--input", "-", "--model", "provider/model",
            ],
            command,
        )
        self.assertNotIn("finish", command)

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
                started = control_plane.start_task(
                    harness="minimax", prompt="x", project="fixture", cwd=None,
                    model=None, sandbox="workspace-write", reasoning_effort=None,
                )
                read_only = control_plane.start_task(
                    harness="minimax", prompt="x", project="fixture", cwd=None,
                    model=None, sandbox="read-only", reasoning_effort=None,
                )
                self.assertTrue(started["ok"])
                self.assertFalse(read_only["ok"])
                self.assertIn("read-only sandbox", read_only["error"])
                self.assertEqual("workspace-write", started["sandbox_requested"])
                self.assertEqual("unenforced", started["sandbox_effective"])
                self.assertFalse(started["sandbox_enforced"])
                state = json.loads(
                    (control_plane.JOBS_DIR / started["job_id"] / "status.json").read_text(encoding="utf-8")
                )
                self.assertEqual("workspace-write", state["sandbox_requested"])
                self.assertEqual("unenforced", state["sandbox_effective"])
                self.assertFalse(state["sandbox_enforced"])

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

    def test_minimax_packaged_mode_keeps_sandbox_contract_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            project = root / "project"
            project.mkdir()
            self.write_projects(project)
            fake_mcode = root / "mcode.cmd"
            fake_mcode.write_text("placeholder", encoding="utf-8")
            missing_agy = root / "missing" / "agy.exe"
            with mock.patch.object(control_plane, "MINIMAX_CLI_EXE", fake_mcode), mock.patch.object(
                control_plane, "run_safe_subprocess", side_effect=self.minimax_probe
            ), mock.patch.object(control_plane, "AGY_EXE", missing_agy
            ), mock.patch.object(control_plane.shutil, "which", return_value=None), mock.patch.dict(
                control_plane.os.environ, {"HARBOR_RUNTIME_MODE": "packaged"}
            ):
                started = control_plane.start_task(
                    harness="minimax", prompt="packaged status", project="fixture", cwd=None,
                    model="provider/model", sandbox="workspace-write", reasoning_effort=None,
                )
            self.assertTrue(started["ok"])

            job_dir = control_plane.JOBS_DIR / started["job_id"]
            state = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual("workspace-write", state["sandbox_requested"])
            self.assertEqual("unenforced", state["sandbox_effective"])
            self.assertFalse(state["sandbox_enforced"])

    def test_minimax_task_canonical_success_with_cleanup_exit_is_normalized_in_poll(self) -> None:
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
            ), mock.patch.object(control_plane.shutil, "which", return_value=None
            ), probe_patch:
                started = control_plane.start_task(
                    harness="minimax", prompt="packaged cleanup", project="fixture", cwd=None,
                    model="provider/model", sandbox="workspace-write", reasoning_effort=None,
                )

            def cleanup_force_exit_lifecycle(command, result_path, **_kwargs):
                result_path.write_text("MINIMAX_CLEANUP_DONE", encoding="utf-8")
                stdout = json.dumps(
                    {"schemaVersion": 1, "type": "exec.result", "status": "succeeded", "output": "MINIMAX_CLEANUP_DONE"},
                )
                return {
                    "exit_code": 1,
                    "stdout": stdout,
                    "stderr": "",
                    "termination_reason": "grace_expired_after_result",
                    "forced_exit": True,
                    "result_completed_at": 0.0,
                    "result_text": "MINIMAX_CLEANUP_DONE",
                }

            job_dir = control_plane.JOBS_DIR / started["job_id"]
            with mock.patch.object(codex_job_worker, "run_minimax_with_lifecycle", side_effect=cleanup_force_exit_lifecycle):
                codex_job_worker.main(job_dir)
            completed = control_plane.poll_task(started["job_id"])
            self.assertEqual("completed", completed["status"])
            self.assertEqual("MINIMAX_CLEANUP_DONE", completed["final_message"])
            self.assertEqual(0, completed["exit_code"])
            self.assertEqual(1, completed["process_exit_code"])
            self.assertTrue(completed["forced_exit_after_result"])
            self.assertEqual("minimax_cleanup_after_result", completed["cleanup_anomaly"])

    def test_quota_classifier_and_route_state(self) -> None:
        self.assertTrue(control_plane.is_official_quota_exhausted("OpenAI Codex usage limit exhausted"))
        self.assertFalse(control_plane.is_official_quota_exhausted("HTTP 429 from upstream"))
        self.assertFalse(control_plane.is_official_quota_exhausted("OpenAI Codex rate limit; retry after 30 seconds"))
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.configure_temp_control(root)
            control_plane.record_official_quota_exhausted("OpenAI Codex quota exceeded")
            state = json.loads(control_plane.ROUTE_STATE_FILE.read_text(encoding="utf-8"))
            self.assertEqual("quota_exhausted", state["official"]["status"])
            self.assertIsNone(state["official"]["unavailable_until"])

    def test_route_lock_and_snapshot_restore_use_temporary_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = root / "config.toml"
            config.write_text("model = 'before'\n", encoding="utf-8")
            snapshot = control_plane.ConfigSnapshot(config)
            lock = root / "route.lock"
            with control_plane.route_lock(lock):
                self.assertTrue(lock.exists())
                config.write_text("model = 'changed'\n", encoding="utf-8")
            self.assertFalse(lock.exists())
            self.assertFalse(snapshot.unchanged())
            snapshot.restore()
            self.assertTrue(snapshot.unchanged())

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
            "harness_telemetry",
            "project_list", "project_resolve", "file_read", "file_write", "file_append",
            "file_stat", "directory_list", "git_status", "git_diff", "git_branch", "git_log",
            "git_worktree_list", "git_rev_parse", "git_add", "git_commit", "git_ls_remote",
            "git_push_dry_run", "git_push_ref",
        }.issubset(names))


if __name__ == "__main__":
    unittest.main()
