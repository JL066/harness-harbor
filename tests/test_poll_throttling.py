import concurrent.futures
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import control_plane
import server_legacy


class PollThrottlingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.jobs_dir = Path(self.temp_dir.name) / ".jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        with control_plane._JOB_POLL_META_LOCK:
            control_plane._JOB_POLL_THREAD_LOCKS.clear()
            control_plane._JOB_POLL_IN_MEMORY_META.clear()
        self.patcher = mock.patch.object(control_plane, "JOBS_DIR", self.jobs_dir)
        self.patcher_legacy = mock.patch.object(server_legacy, "JOBS_DIR", self.jobs_dir)
        self.patcher.start()
        self.patcher_legacy.start()

    def tearDown(self):
        self.patcher_legacy.stop()
        self.patcher.stop()
        with control_plane._JOB_POLL_META_LOCK:
            control_plane._JOB_POLL_THREAD_LOCKS.clear()
            control_plane._JOB_POLL_IN_MEMORY_META.clear()
        self.temp_dir.cleanup()

    def _create_job(self, job_id: str, status: str = "running", harness: str = "codex") -> Path:
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "job_id": job_id,
            "harness": harness,
            "status": status,
            "prompt": "test prompt",
            "cwd": "D:\\test",
            "created_at": "2000-01-01T12:00:00.000000+00:00",
            "started_at": "2000-01-01T12:00:01.000000+00:00",
            "updated_at": "2000-01-01T12:00:01.000000+00:00",
        }
        (job_dir / "status.json").write_text(json.dumps(state), encoding="utf-8")
        return job_dir

    def test_first_poll_normal_execution(self):
        job_id = "test_job_001"
        self._create_job(job_id, status="running")

        base_time = 1756555200.0  # reference timestamp
        with mock.patch("time.time", return_value=base_time):
            result = control_plane.poll_task(job_id)

        self.assertTrue(result["ok"])
        self.assertEqual("running", result["status"])
        self.assertFalse(result["poll_throttled"])
        self.assertIn("last_polled_at", result)
        self.assertIn("next_allowed_at", result)

        # Verify poll_meta.json was created on disk with snapshot
        meta_path = self.jobs_dir / job_id / "poll_meta.json"
        self.assertTrue(meta_path.is_file())
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(base_time, meta["last_polled_ts"])
        self.assertIn("snapshot", meta)
        self.assertEqual("running", meta["snapshot"]["status"])

    def test_second_poll_within_10m_returns_cached_snapshot_without_reading_status_file(self):
        job_id = "test_job_002"
        job_dir = self._create_job(job_id, status="running")

        t0 = 1756555200.0
        with mock.patch("time.time", return_value=t0):
            res1 = control_plane.poll_task(job_id)
        self.assertFalse(res1["poll_throttled"])

        # Delete status.json to prove second poll does NOT touch status.json at all
        (job_dir / "status.json").unlink()

        t1 = t0 + 60.0
        with mock.patch("time.time", return_value=t1):
            res2 = control_plane.poll_task(job_id)

        self.assertTrue(res2["ok"])
        self.assertEqual("running", res2["status"])
        self.assertTrue(res2["poll_throttled"])
        self.assertEqual(540, res2["retry_after_seconds"])
        self.assertEqual(res1["next_allowed_at"], res2["next_allowed_at"])

    def test_status_change_cached_during_cooldown_and_refreshed_after_cooldown(self):
        job_id = "test_job_status_change"
        job_dir = self._create_job(job_id, status="running")

        t0 = 1756555200.0
        with mock.patch("time.time", return_value=t0):
            res1 = control_plane.poll_task(job_id)
        self.assertEqual("running", res1["status"])
        self.assertFalse(res1["poll_throttled"])

        # Background worker completes the job and updates status.json on disk
        completed_state = {
            "job_id": job_id,
            "harness": "codex",
            "status": "completed",
            "final_message": "job finished successfully",
            "exit_code": 0,
        }
        (job_dir / "status.json").write_text(json.dumps(completed_state), encoding="utf-8")

        # Within 10m (e.g. t0 + 100s), normal poll MUST still return cached running snapshot
        t_mid = t0 + 100.0
        with mock.patch("time.time", return_value=t_mid):
            res_cached = control_plane.poll_task(job_id, immediate=False)

        self.assertTrue(res_cached["ok"])
        self.assertEqual("running", res_cached["status"])
        self.assertTrue(res_cached["poll_throttled"])
        self.assertEqual(500, res_cached["retry_after_seconds"])

        # After 10m expires (t0 + 601s), normal poll reads disk and returns completed
        t_after = t0 + 601.0
        with mock.patch("time.time", return_value=t_after):
            res_refreshed = control_plane.poll_task(job_id, immediate=False)

        self.assertTrue(res_refreshed["ok"])
        self.assertEqual("completed", res_refreshed["status"])
        self.assertEqual("job finished successfully", res_refreshed["final_message"])
        self.assertFalse(res_refreshed["poll_throttled"])

    def test_immediate_true_immediately_reads_updated_status(self):
        job_id = "test_job_imm_read"
        job_dir = self._create_job(job_id, status="running")

        t0 = 1756555200.0
        with mock.patch("time.time", return_value=t0):
            control_plane.poll_task(job_id)

        # Worker completes job
        completed_state = {
            "job_id": job_id,
            "harness": "codex",
            "status": "completed",
            "final_message": "done",
            "exit_code": 0,
        }
        (job_dir / "status.json").write_text(json.dumps(completed_state), encoding="utf-8")

        # immediate=True at t0 + 30s bypasses cache and reads disk immediately
        t1 = t0 + 30.0
        with mock.patch("time.time", return_value=t1):
            res_imm = control_plane.poll_task(job_id, immediate=True)

        self.assertTrue(res_imm["ok"])
        self.assertEqual("completed", res_imm["status"])
        self.assertEqual("done", res_imm["final_message"])
        self.assertFalse(res_imm["poll_throttled"])

    def test_immediate_true_resets_cooldown_for_running_job(self):
        job_id = "test_job_imm_reset"
        self._create_job(job_id, status="running")

        t0 = 1756555200.0
        with mock.patch("time.time", return_value=t0):
            control_plane.poll_task(job_id)

        # immediate=True at t0 + 120s
        t1 = t0 + 120.0
        with mock.patch("time.time", return_value=t1):
            res_imm = control_plane.poll_task(job_id, immediate=True)
        self.assertFalse(res_imm["poll_throttled"])

        # Subsequent normal poll at t0 + 180s (60s after immediate poll) must be throttled with 540s left
        t2 = t0 + 180.0
        with mock.patch("time.time", return_value=t2):
            res_sub = control_plane.poll_task(job_id, immediate=False)
        self.assertTrue(res_sub["poll_throttled"])
        self.assertEqual(540, res_sub["retry_after_seconds"])

    def test_terminal_snapshot_repeated_reads_without_status_file_access(self):
        job_id = "test_job_terminal_cached"
        job_dir = self._create_job(job_id, status="completed")

        t0 = 1756555200.0
        # First poll observes terminal status and records terminal snapshot
        with mock.patch("time.time", return_value=t0):
            res1 = control_plane.poll_task(job_id)
        self.assertEqual("completed", res1["status"])
        self.assertFalse(res1["poll_throttled"])

        # Delete status.json to prove subsequent reads are completely served from terminal snapshot
        (job_dir / "status.json").unlink()

        for offset in (1, 5, 10, 100, 1000):
            with mock.patch("time.time", return_value=t0 + offset):
                res = control_plane.poll_task(job_id)
                self.assertTrue(res["ok"])
                self.assertEqual("completed", res["status"])
                self.assertFalse(res["poll_throttled"])

    def test_concurrent_first_polls_exact_one_disk_read(self):
        job_id = "test_job_concurrent_penetration"
        self._create_job(job_id, status="running")

        read_count = 0
        read_lock = threading.Lock()
        original_read_json = control_plane.read_json_object

        def tracked_read_json(path):
            nonlocal read_count
            if Path(path).name == "status.json":
                with read_lock:
                    read_count += 1
                time.sleep(0.02)  # amplify concurrency window
            return original_read_json(path)

        t0 = 1756555200.0

        with mock.patch("time.time", return_value=t0):
            with mock.patch.object(control_plane, "read_json_object", side_effect=tracked_read_json):
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [executor.submit(control_plane.poll_task, job_id) for _ in range(4)]
                    results = [f.result() for f in futures]

        # Exactly 1 thread performed actual status.json read
        self.assertEqual(1, read_count)

        # Exactly 1 returned poll_throttled=False, all other 3 were throttled
        unthrottled = [r for r in results if not r["poll_throttled"]]
        throttled = [r for r in results if r["poll_throttled"]]
        self.assertEqual(1, len(unthrottled))
        self.assertEqual(3, len(throttled))

    def test_thread_lock_timeout_fails_closed_without_status_read(self):
        job_id = "test_job_thread_lock_timeout"
        job_dir = self._create_job(job_id, status="running")

        read_count = 0
        original_read_json = control_plane.read_json_object

        def tracked_read_json(path):
            nonlocal read_count
            if Path(path).name == "status.json":
                read_count += 1
            return original_read_json(path)

        thread_lock = control_plane._get_job_thread_lock(str(job_dir))
        thread_lock.acquire()
        try:
            with mock.patch.object(control_plane, "read_json_object", side_effect=tracked_read_json):
                res = control_plane.poll_task(job_id, lock_timeout=0.02)

            self.assertFalse(res["ok"])
            self.assertTrue(res["poll_throttled"])
            self.assertTrue(res["lock_busy"])
            self.assertIn("busy", res["error"])
            self.assertEqual(600, res["retry_after_seconds"])
            # status.json was NOT read
            self.assertEqual(0, read_count)
        finally:
            thread_lock.release()

    def test_file_lock_timeout_fails_closed_without_status_read(self):
        job_id = "test_job_file_lock_timeout"
        job_dir = self._create_job(job_id, status="running")

        # Manually hold poll.lock file
        lock_file = job_dir / "poll.lock"
        fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, b"external_pid 1756555200.0\n")

        read_count = 0
        original_read_json = control_plane.read_json_object

        def tracked_read_json(path):
            nonlocal read_count
            if Path(path).name == "status.json":
                read_count += 1
            return original_read_json(path)

        try:
            with mock.patch.object(control_plane, "read_json_object", side_effect=tracked_read_json):
                res = control_plane.poll_task(job_id, lock_timeout=0.02)

            self.assertFalse(res["ok"])
            self.assertTrue(res["poll_throttled"])
            self.assertTrue(res["lock_busy"])
            self.assertIn("busy", res["error"])
            self.assertEqual(600, res["retry_after_seconds"])
            # status.json was NOT read
            self.assertEqual(0, read_count)
        finally:
            os.close(fd)
            lock_file.unlink(missing_ok=True)

    def test_immediate_true_fails_closed_on_lock_contention(self):
        job_id = "test_job_imm_lock_contention"
        job_dir = self._create_job(job_id, status="running")

        thread_lock = control_plane._get_job_thread_lock(str(job_dir))
        thread_lock.acquire()
        try:
            read_count = 0
            original_read_json = control_plane.read_json_object

            def tracked_read_json(path):
                nonlocal read_count
                if Path(path).name == "status.json":
                    read_count += 1
                return original_read_json(path)

            with mock.patch.object(control_plane, "read_json_object", side_effect=tracked_read_json):
                res = control_plane.poll_task(job_id, immediate=True, lock_timeout=0.02)

            self.assertFalse(res["ok"])
            self.assertTrue(res["poll_throttled"])
            self.assertTrue(res["lock_busy"])
            self.assertEqual(600, res["retry_after_seconds"])
            self.assertEqual(0, read_count)
        finally:
            thread_lock.release()

    def test_lock_busy_with_existing_meta_preserves_remaining_cooldown(self):
        job_id = "test_job_lock_busy_cooldown"
        job_dir = self._create_job(job_id, status="running")

        t0 = 1756555200.0
        with mock.patch("time.time", return_value=t0):
            res_init = control_plane.poll_task(job_id)
        self.assertTrue(res_init["ok"])
        self.assertFalse(res_init["poll_throttled"])

        # At t0 + 150s, external lock is held
        thread_lock = control_plane._get_job_thread_lock(str(job_dir))
        thread_lock.acquire()
        try:
            t1 = t0 + 150.0
            with mock.patch("time.time", return_value=t1):
                res_busy = control_plane.poll_task(job_id, lock_timeout=0.02)

            self.assertFalse(res_busy["ok"])
            self.assertTrue(res_busy["lock_busy"])
            self.assertTrue(res_busy["poll_throttled"])
            self.assertEqual("running", res_busy["status"])
            self.assertEqual(450, res_busy["retry_after_seconds"])
            self.assertEqual(res_init["next_allowed_at"], res_busy["next_allowed_at"])
        finally:
            thread_lock.release()

    def test_different_jobs_concurrency_not_blocking_each_other(self):
        job_a = "job_parallel_a"
        job_b = "job_parallel_b"
        self._create_job(job_a, status="running")
        self._create_job(job_b, status="running")

        t0 = 1756555200.0
        with mock.patch("time.time", return_value=t0):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                f_a = executor.submit(control_plane.poll_task, job_a)
                f_b = executor.submit(control_plane.poll_task, job_b)
                res_a = f_a.result()
                res_b = f_b.result()

        self.assertTrue(res_a["ok"])
        self.assertFalse(res_a["poll_throttled"])
        self.assertTrue(res_b["ok"])
        self.assertFalse(res_b["poll_throttled"])

    def test_task_poll_and_codex_poll_mcp_consistency(self):
        job_id = "test_job_mcp_sync"
        self._create_job(job_id, status="running")

        t0 = 1756555200.0
        with mock.patch("time.time", return_value=t0):
            res_codex = server_legacy.codex_poll(job_id)

        self.assertTrue(res_codex["ok"])
        self.assertFalse(res_codex["poll_throttled"])

        # Second poll via task_poll must be throttled identically
        with mock.patch("time.time", return_value=t0 + 30.0):
            res_task = server_legacy.task_poll(job_id)

        self.assertTrue(res_task["ok"])
        self.assertTrue(res_task["poll_throttled"])
        self.assertEqual(570, res_task["retry_after_seconds"])

        # Immediate via codex_poll bypasses and resets
        with mock.patch("time.time", return_value=t0 + 60.0):
            res_imm = server_legacy.codex_poll(job_id, immediate=True)

        self.assertTrue(res_imm["ok"])
        self.assertFalse(res_imm["poll_throttled"])

    def test_cooldown_persists_across_harbor_reloads(self):
        job_id = "test_job_persist_reload"
        self._create_job(job_id, status="running")

        t0 = 1756555200.0
        with mock.patch("time.time", return_value=t0):
            res1 = control_plane.poll_task(job_id)
        self.assertFalse(res1["poll_throttled"])

        # Verify disk persistence exists
        meta_path = self.jobs_dir / job_id / "poll_meta.json"
        self.assertTrue(meta_path.is_file())

        # Clear in-memory lock & meta dicts to simulate fresh process
        with control_plane._JOB_POLL_META_LOCK:
            control_plane._JOB_POLL_THREAD_LOCKS.clear()
            control_plane._JOB_POLL_IN_MEMORY_META.clear()

        # Simulate fresh process / reload by invoking poll_task at t0 + 200s
        t1 = t0 + 200.0
        with mock.patch("time.time", return_value=t1):
            res2 = control_plane.poll_task(job_id)

        self.assertTrue(res2["poll_throttled"])
        self.assertEqual(400, res2["retry_after_seconds"])
        self.assertEqual(res1["next_allowed_at"], res2["next_allowed_at"])

    def test_poll_meta_write_failure_uses_in_memory_cache_and_protects_subsequent_polls(self):
        job_id = "test_job_write_fail"
        job_dir = self._create_job(job_id, status="running")

        t0 = 1756555200.0
        with mock.patch("control_plane.write_json", side_effect=OSError("Disk full/permission denied")):
            with mock.patch("time.time", return_value=t0):
                res1 = control_plane.poll_task(job_id)

        self.assertTrue(res1["ok"])
        self.assertEqual("running", res1["status"])
        self.assertFalse(res1["poll_throttled"])
        self.assertFalse(res1.get("meta_persisted", True))

        # Delete status.json to ensure disk status cannot be read
        (job_dir / "status.json").unlink()

        # Second poll at t0 + 30s must use in-memory cache and be throttled without reading disk
        t1 = t0 + 30.0
        with mock.patch("time.time", return_value=t1):
            res2 = control_plane.poll_task(job_id)

        self.assertTrue(res2["ok"])
        self.assertTrue(res2["poll_throttled"])
        self.assertEqual("running", res2["status"])
        self.assertEqual(570, res2["retry_after_seconds"])

    def test_corrupt_poll_meta_fails_closed_on_normal_poll(self):
        job_id = "test_job_corrupt_meta"
        job_dir = self._create_job(job_id, status="running")

        # Write invalid corrupt content to poll_meta.json
        meta_path = job_dir / "poll_meta.json"
        meta_path.write_text("{this is corrupted json!", encoding="utf-8")

        res = control_plane.poll_task(job_id, immediate=False)
        self.assertFalse(res["ok"])
        self.assertTrue(res["poll_throttled"])
        self.assertTrue(res["metadata_error"])
        self.assertEqual(600, res["retry_after_seconds"])
        self.assertIn("Corrupted", res["error"])

    def test_corrupt_poll_meta_recovery_with_immediate_true(self):
        job_id = "test_job_corrupt_meta_repair"
        job_dir = self._create_job(job_id, status="running")

        meta_path = job_dir / "poll_meta.json"
        meta_path.write_text("{corrupt!", encoding="utf-8")

        res = control_plane.poll_task(job_id, immediate=True)
        self.assertTrue(res["ok"])
        self.assertEqual("running", res["status"])
        self.assertFalse(res["poll_throttled"])

        # poll_meta.json should now be cleanly repaired on disk
        repaired_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual("running", repaired_meta["snapshot"]["status"])

    def test_live_owner_poll_lock_never_deleted_even_if_old(self):
        job_id = "test_job_live_lock"
        job_dir = self._create_job(job_id, status="running")

        lock_file = job_dir / "poll.lock"
        # Write live PID (current process) with an old timestamp (1000s ago)
        lock_file.write_text(f"{os.getpid()} {time.time() - 1000.0}\n", encoding="utf-8")
        # Set file mtime to 1000s ago
        old_mtime = time.time() - 1000.0
        os.utime(lock_file, (old_mtime, old_mtime))

        res = control_plane.poll_task(job_id, lock_timeout=0.02)
        self.assertFalse(res["ok"])
        self.assertTrue(res["lock_busy"])
        # Lock file was NEVER deleted
        self.assertTrue(lock_file.is_file())

    def test_dead_owner_poll_lock_safely_reclaimed(self):
        job_id = "test_job_dead_lock"
        job_dir = self._create_job(job_id, status="running")

        lock_file = job_dir / "poll.lock"
        # Write dead PID with old timestamp
        dead_pid = 99999999
        lock_file.write_text(f"{dead_pid} {time.time() - 20.0}\n", encoding="utf-8")
        old_mtime = time.time() - 20.0
        os.utime(lock_file, (old_mtime, old_mtime))

        with mock.patch("control_plane._is_pid_alive", return_value=False):
            res = control_plane.poll_task(job_id, lock_timeout=0.5)

        self.assertTrue(res["ok"])
        self.assertEqual("running", res["status"])

    def test_malformed_poll_lock_fails_closed_when_recent(self):
        job_id = "test_job_malformed_lock"
        job_dir = self._create_job(job_id, status="running")

        lock_file = job_dir / "poll.lock"
        lock_file.write_text("invalid_lock_format\n", encoding="utf-8")

        res = control_plane.poll_task(job_id, lock_timeout=0.02)
        self.assertFalse(res["ok"])
        self.assertTrue(res["lock_busy"])
        self.assertTrue(lock_file.is_file())

    def test_retry_after_seconds_uses_ceil_rounding(self):
        job_id = "test_job_ceil_rounding"
        self._create_job(job_id, status="running")

        t0 = 1756555200.0
        with mock.patch("time.time", return_value=t0):
            res1 = control_plane.poll_task(job_id)
        self.assertFalse(res1["poll_throttled"])

        # At t0 + 0.1s: remaining = 599.9s -> ceil is 600
        with mock.patch("time.time", return_value=t0 + 0.1):
            res2 = control_plane.poll_task(job_id)
        self.assertEqual(600, res2["retry_after_seconds"])

        # At t0 + 59.4s: remaining = 540.6s -> ceil is 541
        with mock.patch("time.time", return_value=t0 + 59.4):
            res3 = control_plane.poll_task(job_id)
        self.assertEqual(541, res3["retry_after_seconds"])


if __name__ == "__main__":
    unittest.main()
