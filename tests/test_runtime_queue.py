import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import codex_job_daemon
import control_plane
import runtime_queue
import server_legacy


class RuntimeQueueTests(unittest.TestCase):
    def test_explicit_absolute_root_is_canonical_and_cwd_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "queue"
            first = runtime_queue.resolve_queue_root(PROJECT_ROOT, {"HARBOR_JOBS_DIR": str(root)})
            with mock.patch("os.getcwd", return_value=str(PROJECT_ROOT / "tests")):
                second = runtime_queue.resolve_queue_root(PROJECT_ROOT, {"HARBOR_JOBS_DIR": str(root)})
            self.assertEqual(root.resolve(), first.path)
            self.assertEqual(first, second)
            self.assertEqual("environment", first.source)

    def test_fallback_is_project_local_and_blank_env_is_unset(self) -> None:
        fallback = runtime_queue.resolve_queue_root(PROJECT_ROOT, {})
        blank = runtime_queue.resolve_queue_root(PROJECT_ROOT, {"HARBOR_JOBS_DIR": "  "})
        self.assertEqual((PROJECT_ROOT / ".jobs").resolve(), fallback.path)
        self.assertEqual(fallback, blank)
        self.assertEqual("project_fallback", fallback.source)

    def test_relative_environment_root_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be an absolute path"):
            runtime_queue.resolve_queue_root(PROJECT_ROOT, {"HARBOR_JOBS_DIR": ".jobs"})

    @unittest.skipUnless(os.name == "nt", "Windows path semantics")
    def test_windows_absolute_path(self) -> None:
        root = runtime_queue.resolve_queue_root(PROJECT_ROOT, {"HARBOR_JOBS_DIR": r"D:\harbor-runtime\.jobs"})
        self.assertTrue(root.path.is_absolute())
        self.assertEqual("environment", root.source)

    def test_dev_and_production_roots_are_isolated(self) -> None:
        dev = runtime_queue.resolve_queue_root(Path(r"X:\Example\chatgpt-harbor-launcher"), {})
        prod = runtime_queue.resolve_queue_root(Path(r"X:\Example\chatgpt-harbor"), {})
        self.assertNotEqual(dev.fingerprint, prod.fingerprint)

    def test_cancel_uses_explicit_canonical_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            jobs = Path(temporary_directory).resolve()
            job_id = "a" * 32
            job_dir = jobs / job_id
            job_dir.mkdir()
            (job_dir / "status.json").write_text(json.dumps({"status": "queued"}), encoding="utf-8")
            result = control_plane.cancel_task(job_id, jobs_dir=jobs)
            self.assertTrue(result["ok"])
            state = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual("cancelled", state["status"])

    def test_poll_and_cancel_reject_foreign_queue_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            jobs = Path(temporary_directory)
            job_id = "c" * 32
            job_dir = jobs / job_id
            job_dir.mkdir()
            state_path = job_dir / "status.json"
            state_path.write_text(
                json.dumps({"status": "queued", "queue_root_fingerprint": "foreign"}),
                encoding="utf-8",
            )
            polled = control_plane.poll_task(job_id, immediate=True, jobs_dir=jobs)
            cancelled = control_plane.cancel_task(job_id, jobs_dir=jobs)
            self.assertTrue(polled["queue_root_mismatch"])
            self.assertTrue(cancelled["queue_root_mismatch"])
            self.assertEqual("queued", json.loads(state_path.read_text(encoding="utf-8"))["status"])

    def test_producer_and_daemon_same_config_claims_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            jobs = root / ".jobs"
            project = root / "project"
            project.mkdir()
            fake_codex = root / "codex.exe"
            fake_codex.write_text("placeholder", encoding="utf-8")
            queue_root = runtime_queue.resolve_queue_root(root, {"HARBOR_JOBS_DIR": str(jobs)})
            with (
                mock.patch.object(control_plane, "JOBS_DIR", queue_root.path),
                mock.patch.object(control_plane, "QUEUE_ROOT", queue_root),
                mock.patch.object(control_plane, "CODEX_EXE", fake_codex),
            ):
                started = control_plane.start_task(
                    harness="codex", prompt="run", project=None, cwd=str(project),
                    model=None, sandbox="read-only", reasoning_effort=None,
                )
            job_dir = jobs / started["job_id"]
            self.assertEqual([job_dir], codex_job_daemon.queued_jobs(jobs))
            scheduler = codex_job_daemon.HarborScheduler(
                jobs_dir=jobs,
                concurrency_limits={"codex": 1, "minimax": 0, "agy": 0},
            )
            process = mock.MagicMock()
            process.pid = 12345
            process.poll.return_value = None

            def claim_then_spawn(target: Path, _harness: str):
                self.assertIsNotNone(control_plane.claim_job(target))
                return process

            with mock.patch.object(scheduler, "spawn_worker", side_effect=claim_then_spawn):
                scheduler.tick()
            state = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual("running", state["status"])
            self.assertTrue((job_dir / "worker.lock").exists())

    def test_foreign_queue_identity_is_not_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            jobs = Path(temporary_directory)
            job_dir = jobs / ("b" * 32)
            job_dir.mkdir()
            (job_dir / "status.json").write_text(
                json.dumps({"status": "queued", "harness": "codex", "queue_root_fingerprint": "foreign"}),
                encoding="utf-8",
            )
            self.assertEqual([], codex_job_daemon.queued_jobs(jobs))

    def test_diagnostics_expose_non_secret_identity(self) -> None:
        diagnostics = control_plane.queue_diagnostics()
        self.assertEqual(str(control_plane.JOBS_DIR), diagnostics["jobs_dir"])
        self.assertEqual(16, len(diagnostics["fingerprint"]))
        self.assertIn(diagnostics["source"], {"environment", "project_fallback"})
        self.assertEqual({"ok": True, **diagnostics}, server_legacy.queue_status())

    def test_runtime_override_diagnostics_follow_actual_jobs_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            diagnostics = control_plane.queue_diagnostics(Path(temporary_directory))
            self.assertEqual("runtime_override", diagnostics["source"])
            self.assertEqual(
                runtime_queue.queue_root_fingerprint(Path(temporary_directory)),
                diagnostics["fingerprint"],
            )


if __name__ == "__main__":
    unittest.main()
