"""Tests for Harness Harbor multi-harness worker pool daemon (codex_job_daemon.py)."""

from __future__ import annotations

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

import codex_job_daemon
from codex_job_daemon import (
    DEFAULT_HARNESS,
    DEFAULT_HARNESS_CONCURRENCY,
    SUPPORTED_HARNESSES,
    ActiveWorker,
    HarborScheduler,
    acquire_workspace_lease,
    get_canonical_workspace,
    get_disk_active_job_ids,
    get_disk_busy_harnesses,
    get_job_cwd,
    get_job_harness,
    get_workspace_lease_path,
    is_workspace_leased,
    queued_jobs,
    release_workspace_lease,
)


def _create_job(
    jobs_dir: Path,
    job_id: str,
    harness: str | None = "codex",
    status: str = "queued",
    created_at: str = "2000-01-01T10:00:00Z",
    has_lock: bool = False,
    cwd: str | Path | None = None,
) -> Path:
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "job_id": job_id,
        "status": status,
        "created_at": created_at,
        "updated_at": created_at,
        "prompt": "test prompt",
        "cwd": str(cwd if cwd is not None else job_dir),
    }
    if harness is not None:
        state["harness"] = harness
    (job_dir / "status.json").write_text(json.dumps(state), encoding="utf-8")
    if has_lock:
        (job_dir / "worker.lock").write_text(f"{os.getpid()} {created_at}\n", encoding="utf-8")
    return job_dir



def _write_fake_worker(dir_path: Path) -> Path:
    """Create a controllable fake worker script.

    If an `exit_trigger` file exists in the job directory, it exits with
    code 0. If `crash_immediately` exists, it exits with code 42.
    """
    script = dir_path / "fake_worker.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys
            import time
            import json
            from pathlib import Path

            job_dir = Path(sys.argv[1])
            state_path = job_dir / "status.json"
            lock_path = job_dir / "worker.lock"
            trigger_path = job_dir / "exit_trigger"
            crash_path = job_dir / "crash_immediately"

            if crash_path.exists():
                sys.exit(42)

            lock_path.write_text(f"{time.time()}\\n", encoding="utf-8")
            if state_path.is_file():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    state["status"] = "running"
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                except Exception:
                    pass

            for _ in range(300):
                if trigger_path.exists():
                    break
                time.sleep(0.05)

            if lock_path.exists():
                try:
                    lock_path.unlink()
                except OSError:
                    pass

            if state_path.is_file():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    state["status"] = "completed"
                    state_path.write_text(json.dumps(state), encoding="utf-8")
                except Exception:
                    pass
            sys.exit(0)
            """
        ),
        encoding="utf-8",
    )
    return script


class TestCodexJobDaemon(unittest.TestCase):
    def test_zero_exit_without_terminal_preserves_original_and_retries_write(self):
        job = _create_job(self.jobs_dir, "zero_exit", status="running")
        scheduler = HarborScheduler(jobs_dir=self.jobs_dir)
        proc = mock.Mock()
        proc.poll.return_value = 0
        ownership = mock.patch("harbor_platform.process.owned_tree_alive", return_value=False)
        ownership.start()
        self.addCleanup(ownership.stop)
        scheduler.active_workers["codex"][job.name] = ActiveWorker(proc, job, "codex", time.monotonic(), str(job))
        with mock.patch.object(codex_job_daemon, "write_json", side_effect=OSError("disk")), mock.patch.object(codex_job_daemon, "release_workspace_lease") as release:
            self.assertEqual(scheduler.reap_workers(), [])
            release.assert_not_called()
        self.assertIn(job.name, scheduler.active_workers["codex"])
        self.assertEqual(len(scheduler.reap_workers()), 1)
        self.assertEqual(json.loads((job / "status.json").read_text())["status"], "failed")
        self.assertEqual(json.loads((job / "status.before-recovery.json").read_text())["status"], "running")

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.jobs_dir = self.root / ".jobs"
        self.jobs_dir.mkdir()
        self.fake_worker = _write_fake_worker(self.root)

    def tearDown(self) -> None:
        try:
            for d in self.root.rglob("status.json"):
                (d.parent / "exit_trigger").write_text("done", encoding="utf-8")
            time.sleep(0.05)
        except Exception:
            pass
        self.temp_dir.cleanup()

    def test_legacy_compatibility(self) -> None:
        """Missing or unknown harness defaults to 'codex'."""
        j_none = _create_job(self.jobs_dir, "job_none", harness=None)
        j_codex = _create_job(self.jobs_dir, "job_codex", harness="codex")
        j_minimax = _create_job(self.jobs_dir, "job_minimax", harness="minimax")
        j_agy = _create_job(self.jobs_dir, "job_agy", harness="agy")
        j_unknown = _create_job(self.jobs_dir, "job_unknown", harness="unknown_custom")

        self.assertEqual(get_job_harness(j_none), "codex")
        self.assertEqual(get_job_harness(j_codex), "codex")
        self.assertEqual(get_job_harness(j_minimax), "minimax")
        self.assertEqual(get_job_harness(j_agy), "agy")
        self.assertEqual(get_job_harness(j_unknown), "codex")

    def test_abandoned_worker_recovery_preserves_live_unknown_and_handoff_jobs(self):
        from harbor_platform.process import spawn_owned
        dead = spawn_owned([sys.executable, "-c", "pass"])
        dead.wait(timeout=5)
        jobs = {}
        for name, pid in (("dead", dead.pid), ("live", os.getpid()), ("unknown", None), ("handoff", dead.pid)):
            job = _create_job(self.jobs_dir, name, status="running")
            state = json.loads((job / "status.json").read_text())
            state["native_process"] = {"launcher_pid": pid}
            (job / "status.json").write_text(json.dumps(state))
            if pid:
                (job / "worker.lock").write_text(f"{pid}\n")
            if name != "handoff":
                os.utime(job / "status.json", (time.time() - 60, time.time() - 60))
            if name in {"dead", "handoff"} and isinstance(getattr(dead, "_harbor_identity", None), dict):
                (job / "worker.identity.json").write_text(json.dumps(dead._harbor_identity))
            jobs[name] = job
        with codex_job_daemon.harness_dispatch_lock(self.jobs_dir, "codex"):
            recovered = codex_job_daemon.recover_abandoned_jobs(self.jobs_dir, "codex")
        self.assertEqual(recovered, ["dead"])
        self.assertEqual(json.loads((jobs["dead"] / "status.json").read_text())["failure_type"], "worker_disappeared")
        self.assertEqual(json.loads((jobs["dead"] / "status.before-recovery.json").read_text())["status"], "running")
        self.assertFalse((jobs["dead"] / "worker.lock").exists())
        for name in ("live", "unknown", "handoff"):
            self.assertEqual(json.loads((jobs[name] / "status.json").read_text())["status"], "running")
        self.assertNotIn("dead", get_disk_active_job_ids(self.jobs_dir)["codex"])

    def test_queued_jobs_ordering_and_filtering(self) -> None:
        """Queued jobs are sorted FIFO and ignore running/locked jobs."""
        j2 = _create_job(self.jobs_dir, "job2", created_at="2000-01-01T10:05:00Z")
        j1 = _create_job(self.jobs_dir, "job1", created_at="2000-01-01T10:01:00Z")
        j3 = _create_job(self.jobs_dir, "job3", created_at="2000-01-01T10:10:00Z")
        _create_job(self.jobs_dir, "job_locked", created_at="2000-01-01T10:00:00Z", has_lock=True)
        _create_job(self.jobs_dir, "job_running", status="running", created_at="2000-01-01T10:00:00Z")
        _create_job(self.jobs_dir, "job_done", status="completed", created_at="2000-01-01T10:00:00Z")

        queued = queued_jobs(self.jobs_dir)
        self.assertEqual(queued, [j1, j2, j3])

    def test_codex_3_concurrency_and_4th_queued(self) -> None:
        """Codex supports 3 concurrent workers; the 4th job remains queued until a slot opens."""
        j1 = _create_job(self.jobs_dir, "codex_1", harness="codex", created_at="2000-01-01T10:01:00Z")
        j2 = _create_job(self.jobs_dir, "codex_2", harness="codex", created_at="2000-01-01T10:02:00Z")
        j3 = _create_job(self.jobs_dir, "codex_3", harness="codex", created_at="2000-01-01T10:03:00Z")
        j4 = _create_job(self.jobs_dir, "codex_4", harness="codex", created_at="2000-01-01T10:04:00Z")

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
        )

        # Tick 1: Spawns j1, j2, j3 in parallel; j4 remains queued
        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 3)
        spawned_jobs = {item[1] for item in spawned}
        self.assertEqual(spawned_jobs, {j1, j2, j3})
        self.assertEqual(len(scheduler.active_workers["codex"]), 3)

        time.sleep(0.08)
        self.assertEqual(len(queued_jobs(self.jobs_dir)), 1)
        self.assertEqual(queued_jobs(self.jobs_dir)[0], j4)

        # Tick 2: All 3 still running, j4 still queued
        spawned2 = scheduler.tick()
        self.assertEqual(len(spawned2), 0)

        # Release j1
        (j1 / "exit_trigger").write_text("done", encoding="utf-8")
        time.sleep(0.12)

        # Tick 3: j1 reaped, j4 spawned!
        spawned3 = scheduler.tick()
        self.assertEqual(len(spawned3), 1)
        self.assertEqual(spawned3[0][1], j4)
        self.assertEqual(len(scheduler.active_workers["codex"]), 3)
        active_codex_jobs = {w.job_dir for w in scheduler.active_workers["codex"].values()}
        self.assertEqual(active_codex_jobs, {j2, j3, j4})

        # Clean up j2, j3, j4
        for j in (j2, j3, j4):
            (j / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_minimax_and_agy_3_concurrency(self) -> None:
        """MiniMax and Antigravity (agy) each support 3 concurrent workers."""
        for harness in ("minimax", "agy"):
            with self.subTest(harness=harness):
                sub_jobs_dir = self.root / f".jobs_{harness}"
                sub_jobs_dir.mkdir(exist_ok=True)
                jobs = [
                    _create_job(sub_jobs_dir, f"{harness}_{i}", harness=harness, created_at=f"2000-01-01T10:0{i}:00Z")
                    for i in range(1, 5)
                ]

                scheduler = HarborScheduler(
                    jobs_dir=sub_jobs_dir,
                    python_exe=sys.executable,
                    worker_script=self.fake_worker,
                )

                spawned = scheduler.tick()
                self.assertEqual(len(spawned), 3)
                self.assertEqual({item[1] for item in spawned}, set(jobs[:3]))
                self.assertEqual(len(scheduler.active_workers[harness]), 3)

                time.sleep(0.08)
                self.assertEqual(len(queued_jobs(sub_jobs_dir)), 1)
                self.assertEqual(queued_jobs(sub_jobs_dir)[0], jobs[3])

                # Clean up
                for j in jobs:
                    (j / "exit_trigger").write_text("done", encoding="utf-8")
                scheduler.shutdown(timeout=3.0)

    def test_9_workers_active_simultaneously(self) -> None:
        """Codex 3 + MiniMax 3 + agy 3 = 9 concurrent active workers."""
        codex_jobs = [_create_job(self.jobs_dir, f"c_{i}", harness="codex", created_at=f"2000-01-01T10:0{i}:00Z") for i in range(3)]
        minimax_jobs = [_create_job(self.jobs_dir, f"m_{i}", harness="minimax", created_at=f"2000-01-01T10:0{i}:00Z") for i in range(3)]
        agy_jobs = [_create_job(self.jobs_dir, f"a_{i}", harness="agy", created_at=f"2000-01-01T10:0{i}:00Z") for i in range(3)]

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
        )

        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 9)

        # 3 on each harness
        self.assertEqual(len(scheduler.active_workers["codex"]), 3)
        self.assertEqual(len(scheduler.active_workers["minimax"]), 3)
        self.assertEqual(len(scheduler.active_workers["agy"]), 3)

        # Total 9
        total_active = sum(len(w) for w in scheduler.active_workers.values())
        self.assertEqual(total_active, 9)

        # Clean up
        for j in (codex_jobs + minimax_jobs + agy_jobs):
            (j / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_custom_concurrency_limits(self) -> None:
        """HarborScheduler respects custom concurrency limits (e.g. codex=2, minimax=1, agy=4)."""
        codex_jobs = [_create_job(self.jobs_dir, f"c_{i}", harness="codex", created_at=f"2000-01-01T10:0{i}:00Z") for i in range(3)]
        minimax_jobs = [_create_job(self.jobs_dir, f"m_{i}", harness="minimax", created_at=f"2000-01-01T10:0{i}:00Z") for i in range(2)]
        agy_jobs = [_create_job(self.jobs_dir, f"a_{i}", harness="agy", created_at=f"2000-01-01T10:0{i}:00Z") for i in range(5)]

        custom_limits = {"codex": 2, "minimax": 1, "agy": 4}
        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
            concurrency_limits=custom_limits,
        )

        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 7)  # 2 codex + 1 minimax + 4 agy

        self.assertEqual(len(scheduler.active_workers["codex"]), 2)
        self.assertEqual(len(scheduler.active_workers["minimax"]), 1)
        self.assertEqual(len(scheduler.active_workers["agy"]), 4)

        time.sleep(0.08)
        # 3 remaining queued (1 codex, 1 minimax, 1 agy)
        self.assertEqual(len(queued_jobs(self.jobs_dir)), 3)

        # Clean up
        for j in (codex_jobs + minimax_jobs + agy_jobs):
            (j / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_concurrent_race_empty_pool_barrier(self) -> None:
        """Two schedulers simultaneously racing from empty pool against 6 queued jobs with limit=3.

        Synchronized via threading.Barrier to ensure real simultaneous entry into dispatch.
        Total spawned must be strictly 3 (no oversubscription), with 0 duplicate executions.
        """
        import threading

        jobs = [_create_job(self.jobs_dir, f"c_{i}", harness="codex", created_at=f"2000-01-01T10:0{i}:00Z") for i in range(6)]

        scheduler_a = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
            concurrency_limits={"codex": 3},
        )
        scheduler_b = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
            concurrency_limits={"codex": 3},
        )

        barrier = threading.Barrier(2)
        spawned_a: list[tuple[str, Path, int]] = []
        spawned_b: list[tuple[str, Path, int]] = []

        def worker_a() -> None:
            barrier.wait()
            nonlocal spawned_a
            spawned_a = scheduler_a.tick()

        def worker_b() -> None:
            barrier.wait()
            nonlocal spawned_b
            spawned_b = scheduler_b.tick()

        t1 = threading.Thread(target=worker_a)
        t2 = threading.Thread(target=worker_b)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Total spawned across both schedulers must be strictly 3
        total_spawned = len(spawned_a) + len(spawned_b)
        self.assertEqual(total_spawned, 3, f"Expected strictly 3 spawned workers, got {total_spawned}")

        all_spawned_jobs = [item[1] for item in spawned_a + spawned_b]
        self.assertEqual(len(set(all_spawned_jobs)), 3, "No job should be duplicate spawned")

        time.sleep(0.08)
        self.assertEqual(len(queued_jobs(self.jobs_dir)), 3)

        # Clean up
        for j in jobs:
            (j / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler_a.shutdown(timeout=3.0)
        scheduler_b.shutdown(timeout=3.0)

    def test_concurrent_race_last_slot_barrier(self) -> None:
        """Two schedulers racing simultaneously for the 1 remaining slot in a nearly-full pool.

        Synchronized via threading.Barrier: exactly 1 scheduler wins the remaining slot.
        """
        import threading

        # 2 jobs already running
        j1 = _create_job(self.jobs_dir, "c_1", harness="codex", status="running", has_lock=True)
        j2 = _create_job(self.jobs_dir, "c_2", harness="codex", status="running", has_lock=True)
        # 2 queued jobs
        j3 = _create_job(self.jobs_dir, "c_3", harness="codex", created_at="2000-01-01T10:03:00Z")
        j4 = _create_job(self.jobs_dir, "c_4", harness="codex", created_at="2000-01-01T10:04:00Z")

        scheduler_a = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
            concurrency_limits={"codex": 3},
        )
        scheduler_b = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
            concurrency_limits={"codex": 3},
        )

        barrier = threading.Barrier(2)
        spawned_a: list[tuple[str, Path, int]] = []
        spawned_b: list[tuple[str, Path, int]] = []

        def run_a() -> None:
            barrier.wait()
            nonlocal spawned_a
            spawned_a = scheduler_a.tick()

        def run_b() -> None:
            barrier.wait()
            nonlocal spawned_b
            spawned_b = scheduler_b.tick()

        t1 = threading.Thread(target=run_a)
        t2 = threading.Thread(target=run_b)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        total_spawned = len(spawned_a) + len(spawned_b)
        self.assertEqual(total_spawned, 1, f"Expected exactly 1 newly spawned worker, got {total_spawned}")

        time.sleep(0.08)
        self.assertEqual(len(queued_jobs(self.jobs_dir)), 1)

        # Clean up
        for j in (j1, j2, j3, j4):
            (j / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler_a.shutdown(timeout=3.0)
        scheduler_b.shutdown(timeout=3.0)

    def test_harness_lock_timeout_fail_closed(self) -> None:
        """When harness dispatch lock cannot be acquired within timeout, scheduler fails closed (0 spawned)."""
        lock_path = self.jobs_dir / ".dispatch_codex.lock"
        # Simulate active lock held by current alive process
        lock_path.write_text(f"pid={os.getpid()} created_at={time.time()}\n", encoding="utf-8")

        j1 = _create_job(self.jobs_dir, "c_1", harness="codex")
        j2 = _create_job(self.jobs_dir, "c_2", harness="codex")

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
            concurrency_limits={"codex": 3},
            dispatch_lock_timeout=0.1,  # short timeout for test
        )

        # Tick times out on .dispatch_codex.lock and fails closed without dispatching
        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 0)
        self.assertEqual(len(scheduler.active_workers["codex"]), 0)
        self.assertEqual(len(queued_jobs(self.jobs_dir)), 2)

        # Release the lock
        lock_path.unlink(missing_ok=True)

        # Next tick successfully acquires lock and dispatches jobs
        spawned2 = scheduler.tick()
        self.assertEqual(len(spawned2), 2)
        self.assertEqual(len(scheduler.active_workers["codex"]), 2)

        # Clean up
        for j in (j1, j2):
            (j / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_live_owner_stale_protection(self) -> None:
        """A lock whose owner PID is alive is NEVER deleted, even if file age exceeds normal TTL."""
        lock_path = self.jobs_dir / ".dispatch_codex.lock"
        lock_path.write_text(f"pid={os.getpid()} created_at={time.time() - 3600}\n", encoding="utf-8")
        # Artificially set mtime to 1 hour in the past
        old_time = time.time() - 3600
        os.utime(lock_path, (old_time, old_time))

        _create_job(self.jobs_dir, "c_1", harness="codex")

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
            concurrency_limits={"codex": 3},
            dispatch_lock_timeout=0.1,
        )

        # Scheduler must NOT delete the lock because owner PID (current process) is alive
        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 0)
        self.assertTrue(lock_path.exists(), "Lock must NOT have been deleted because owner is alive")

        # Clean up lock manually
        lock_path.unlink(missing_ok=True)
        (self.jobs_dir / "c_1" / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_dead_owner_recovery(self) -> None:
        """A harness lock held by a dead PID is safely reclaimed on next tick."""
        lock_path = self.jobs_dir / ".dispatch_codex.lock"
        # Dead PID (9999999)
        lock_path.write_text("pid=9999999 created_at=1000.0\n", encoding="utf-8")

        j1 = _create_job(self.jobs_dir, "c_1", harness="codex")

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
            concurrency_limits={"codex": 3},
        )

        # Scheduler reclaims dead lock and spawns j1
        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][1], j1)
        self.assertEqual(len(scheduler.active_workers["codex"]), 1)

        (j1 / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_dispatch_lock_owner_aware_stale(self) -> None:
        """Unit verification that _is_stale_dispatch_lock protects live owners and cleans dead owners."""
        from codex_job_daemon import _is_stale_dispatch_lock

        j = _create_job(self.jobs_dir, "job_test_stale", harness="codex")
        lock_path = j / "dispatch.lock"

        # 1. Live PID with old mtime -> NOT stale
        lock_path.write_text(f"pid={os.getpid()} created_at={time.time() - 3600}\n", encoding="utf-8")
        os.utime(lock_path, (time.time() - 3600, time.time() - 3600))
        self.assertFalse(_is_stale_dispatch_lock(lock_path, j))

        # 2. Dead PID -> Stale
        lock_path.write_text("pid=9999999 created_at=1000.0\n", encoding="utf-8")
        self.assertTrue(_is_stale_dispatch_lock(lock_path, j))

        # 3. Corrupted lock with old mtime -> Stale
        lock_path.write_text("corrupted_content\n", encoding="utf-8")
        os.utime(lock_path, (time.time() - 3600, time.time() - 3600))
        self.assertTrue(_is_stale_dispatch_lock(lock_path, j))

        # Clean up
        lock_path.unlink(missing_ok=True)


    def test_spawn_failure_unreserves_job(self) -> None:
        """If worker spawn raises an exception, reservation is freed so the job can be rescheduled."""
        j_fail = _create_job(self.jobs_dir, "job_spawn_fail", harness="codex")

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
        )

        with mock.patch.object(scheduler, "spawn_worker", side_effect=OSError("fake spawn failure")):
            spawned = scheduler.tick()
            self.assertEqual(len(spawned), 0)

        self.assertFalse((j_fail / "dispatch.lock").exists())
        self.assertIn(j_fail, queued_jobs(self.jobs_dir))

        spawned2 = scheduler.tick()
        self.assertEqual(len(spawned2), 1)
        self.assertEqual(spawned2[0][1], j_fail)

        (j_fail / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_stale_dispatch_lock_auto_reclaimed(self) -> None:
        """A stale dispatch.lock (with dead PID / expired TTL) does not cause deadlock and is reclaimed."""
        j_stale = _create_job(self.jobs_dir, "job_stale", harness="codex")
        lock_path = j_stale / "dispatch.lock"
        lock_path.write_text("pid=9999999 created_at=1000.0\n", encoding="utf-8")

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
        )

        queued = queued_jobs(self.jobs_dir)
        self.assertIn(j_stale, queued)

        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][1], j_stale)
        self.assertEqual(len(scheduler.active_workers["codex"]), 1)

        (j_stale / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_worker_failure_isolation(self) -> None:
        """Worker crash on one harness frees only that slot and marks job failed."""
        j_crash = _create_job(self.jobs_dir, "job_crash", harness="codex")
        (j_crash / "crash_immediately").write_text("crash", encoding="utf-8")
        j_minimax = _create_job(self.jobs_dir, "job_minimax", harness="minimax")

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
        )

        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 2)

        time.sleep(0.2)
        scheduler.tick()

        # Codex slot freed, MiniMax still running
        self.assertEqual(len(scheduler.active_workers["codex"]), 0)
        self.assertEqual(len(scheduler.active_workers["minimax"]), 1)

        # Crashed job marked failed
        state = json.loads((j_crash / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed")

        # Clean up
        (j_minimax / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_existing_worker_lock_recovery(self) -> None:
        """Daemon restart accurately recovers occupied slot count from disk."""
        _create_job(self.jobs_dir, "c_1", harness="codex", status="running", has_lock=True)
        _create_job(self.jobs_dir, "c_2", harness="codex", status="running", has_lock=True)
        j_codex_q = _create_job(self.jobs_dir, "c_q", harness="codex", created_at="2000-01-01T10:03:00Z")
        j_minimax_q = _create_job(self.jobs_dir, "m_q", harness="minimax", created_at="2000-01-01T10:01:00Z")

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
            concurrency_limits={"codex": 2, "minimax": 2},
        )

        # Codex already at limit (2), only MiniMax should be spawned
        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][1], j_minimax_q)
        self.assertEqual(len(scheduler.active_workers["codex"]), 0)

        # Clean up
        for d in self.jobs_dir.iterdir():
            (d / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_acceptance_parallel_scheduling_timeline(self) -> None:
        """Full 9-worker acceptance timeline verification:
        T0: Codex 3 running + 1 queued, MiniMax 3 running + 1 queued, agy 3 running + 1 queued (9 running, 3 queued).
        T1: Codex A completes -> Codex B/C/D running (3), MiniMax & agy unaffected (9 running, 2 queued).
        T2: All complete cleanly.
        """
        codex_jobs = [_create_job(self.jobs_dir, f"codex_{c}", harness="codex", created_at=f"2000-01-01T10:0{i}:00Z") for i, c in enumerate(["A", "B", "C", "D"])]
        minimax_jobs = [_create_job(self.jobs_dir, f"minimax_{c}", harness="minimax", created_at=f"2000-01-01T10:0{i}:00Z") for i, c in enumerate(["E", "F", "G", "H"])]
        agy_jobs = [_create_job(self.jobs_dir, f"agy_{c}", harness="agy", created_at=f"2000-01-01T10:0{i}:00Z") for i, c in enumerate(["I", "J", "K", "L"])]

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
        )

        # --- T0: Initial Tick ---
        spawned_t0 = scheduler.tick()
        self.assertEqual(len(spawned_t0), 9, "T0 should spawn exactly 9 workers (3 per harness)")

        # Verify active slots at T0
        self.assertEqual(len(scheduler.active_workers["codex"]), 3)
        self.assertEqual(len(scheduler.active_workers["minimax"]), 3)
        self.assertEqual(len(scheduler.active_workers["agy"]), 3)

        time.sleep(0.08)
        queued_t0 = queued_jobs(self.jobs_dir)
        self.assertEqual(len(queued_t0), 3, "T0 should leave exactly 3 queued jobs")
        self.assertEqual({q.name for q in queued_t0}, {"codex_D", "minimax_H", "agy_L"})

        # --- T1: Finish Codex A -> Codex D starts running ---
        (codex_jobs[0] / "exit_trigger").write_text("done", encoding="utf-8")
        time.sleep(0.15)

        spawned_t1 = scheduler.tick()
        self.assertEqual(len(spawned_t1), 1)
        self.assertEqual(spawned_t1[0][0], "codex")
        self.assertEqual(spawned_t1[0][1], codex_jobs[3])  # codex_D

        # Codex A completed
        status_a = json.loads((codex_jobs[0] / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status_a["status"], "completed")

        # Codex B, C, D are active
        active_codex_at_t1 = {w.job_dir.name for w in scheduler.active_workers["codex"].values()}
        self.assertEqual(active_codex_at_t1, {"codex_B", "codex_C", "codex_D"})

        # MiniMax and Agy are completely unaffected (still 3 each)
        self.assertEqual(len(scheduler.active_workers["minimax"]), 3)
        self.assertEqual(len(scheduler.active_workers["agy"]), 3)

        # 2 remaining queued (minimax_H, agy_L)
        time.sleep(0.08)
        queued_t1 = queued_jobs(self.jobs_dir)
        self.assertEqual(len(queued_t1), 2)
        self.assertEqual({q.name for q in queued_t1}, {"minimax_H", "agy_L"})

        # --- T2: Complete running jobs, dispatch remaining H and L, and finish all ---
        # 1. Signal currently active jobs (codex_B, codex_C, codex_D, minimax_E, minimax_F, minimax_G, agy_I, agy_J, agy_K)
        active_t1 = [codex_jobs[1], codex_jobs[2], codex_jobs[3], minimax_jobs[0], minimax_jobs[1], minimax_jobs[2], agy_jobs[0], agy_jobs[1], agy_jobs[2]]
        for j in active_t1:
            (j / "exit_trigger").write_text("done", encoding="utf-8")

        time.sleep(0.15)
        # Tick reaps finished workers and dispatches minimax_H and agy_L
        spawned_t2 = scheduler.tick()
        self.assertEqual(len(spawned_t2), 2)
        self.assertEqual({item[1] for item in spawned_t2}, {minimax_jobs[3], agy_jobs[3]})

        # 2. Signal minimax_H and agy_L to finish
        for j in (minimax_jobs[3], agy_jobs[3]):
            (j / "exit_trigger").write_text("done", encoding="utf-8")

        scheduler.shutdown(timeout=3.0)
        # All active workers reaped
        self.assertEqual(sum(len(w) for w in scheduler.active_workers.values()), 0)

        # All 12 jobs completed
        for j in (codex_jobs + minimax_jobs + agy_jobs):
            status = json.loads((j / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "completed", f"{j.name} should be completed")

    def test_workspace_identity_git_and_subdirs(self) -> None:
        """Workspace identity resolves Git worktree top-level path and normalizes casing/slashes."""
        # 1. Main Git repo
        repo_dir = self.root / "WorkspaceAlpha"
        (repo_dir / ".git").mkdir(parents=True)
        sub1 = repo_dir / "src" / "app"
        sub2 = repo_dir / "tests"
        sub1.mkdir(parents=True)
        sub2.mkdir(parents=True)

        ws_top = get_canonical_workspace(repo_dir)
        ws_sub1 = get_canonical_workspace(sub1)
        ws_sub2 = get_canonical_workspace(sub2)
        self.assertEqual(ws_sub1, ws_top)
        self.assertEqual(ws_sub2, ws_top)

        # 2. Linked Git worktree (.git is a file pointing to gitdir)
        worktree_dir = self.root / "WorkspaceAlpha_night"
        worktree_dir.mkdir(parents=True)
        (worktree_dir / ".git").write_text("gitdir: /fake/path/.git/worktrees/night\n", encoding="utf-8")
        ws_worktree = get_canonical_workspace(worktree_dir)
        self.assertNotEqual(ws_worktree, ws_top)
        self.assertEqual(ws_worktree, os.path.normcase(os.path.normpath(str(worktree_dir.resolve()))))

        # 3. Case and path separator normalization
        ws_upper = get_canonical_workspace(str(repo_dir).upper().replace("\\", "/"))
        self.assertEqual(ws_upper, ws_top)

        # 4. Non-Git directory fallback
        non_git = self.root / "non_git_workspace" / "sub"
        non_git.mkdir(parents=True)
        self.assertEqual(get_canonical_workspace(non_git), os.path.normcase(os.path.normpath(str(non_git.resolve()))))

    def test_same_workspace_cross_harness_mutual_exclusion(self) -> None:
        """Codex and MiniMax targeting the same Git workspace cannot execute simultaneously."""
        repo_dir = self.root / "WorkspaceAlpha_Repo"
        (repo_dir / ".git").mkdir(parents=True)
        sub_src = repo_dir / "src"
        sub_tests = repo_dir / "tests"
        sub_src.mkdir(parents=True)
        sub_tests.mkdir(parents=True)

        j_codex = _create_job(self.jobs_dir, "job_codex", harness="codex", cwd=sub_src, created_at="2000-01-01T10:01:00Z")
        j_minimax = _create_job(self.jobs_dir, "job_minimax", harness="minimax", cwd=sub_tests, created_at="2000-01-01T10:02:00Z")

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
        )

        # Tick 1: Codex claims the alpha workspace lease; MiniMax remains queued
        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][0], "codex")
        self.assertEqual(spawned[0][1], j_codex)
        self.assertEqual(len(scheduler.active_workers["codex"]), 1)
        self.assertEqual(len(scheduler.active_workers["minimax"]), 0)

        time.sleep(0.08)
        self.assertEqual(len(queued_jobs(self.jobs_dir)), 1)
        self.assertEqual(queued_jobs(self.jobs_dir)[0], j_minimax)

        # Release Codex job
        (j_codex / "exit_trigger").write_text("done", encoding="utf-8")
        spawned2 = []
        for _ in range(50):
            time.sleep(0.05)
            spawned2 = scheduler.tick()
            if spawned2:
                break

        # Tick 2: Codex finishes, lease is released -> MiniMax job acquires lease and spawns!
        self.assertEqual(len(spawned2), 1)
        self.assertEqual(spawned2[0][0], "minimax")
        self.assertEqual(spawned2[0][1], j_minimax)
        self.assertEqual(len(scheduler.active_workers["minimax"]), 1)

        # Clean up
        (j_minimax / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_same_workspace_minimax_and_agy_exclusion(self) -> None:
        """MiniMax and AGY targeting the same workspace are mutually exclusive."""
        repo_dir = self.root / "WorkspaceBeta_Repo"
        repo_dir.mkdir(parents=True)

        j_minimax = _create_job(self.jobs_dir, "j_minimax", harness="minimax", cwd=repo_dir, created_at="2000-01-01T10:01:00Z")
        j_agy = _create_job(self.jobs_dir, "j_agy", harness="agy", cwd=repo_dir, created_at="2000-01-01T10:02:00Z")

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
        )

        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][0], "minimax")
        self.assertEqual(spawned[0][1], j_minimax)

        (j_minimax / "exit_trigger").write_text("done", encoding="utf-8")
        spawned2 = []
        for _ in range(50):
            time.sleep(0.05)
            spawned2 = scheduler.tick()
            if spawned2:
                break

        self.assertEqual(len(spawned2), 1)
        self.assertEqual(spawned2[0][0], "agy")
        self.assertEqual(spawned2[0][1], j_agy)

        (j_agy / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_same_workspace_same_harness_exclusion(self) -> None:
        """Two jobs on the same harness targeting the same workspace are serialized by workspace lease."""
        repo_dir = self.root / "Harbor_Repo"
        repo_dir.mkdir(parents=True)

        j1 = _create_job(self.jobs_dir, "c_1", harness="codex", cwd=repo_dir, created_at="2000-01-01T10:01:00Z")
        j2 = _create_job(self.jobs_dir, "c_2", harness="codex", cwd=repo_dir, created_at="2000-01-01T10:02:00Z")

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
            concurrency_limits={"codex": 3},
        )

        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][1], j1)

        (j1 / "exit_trigger").write_text("done", encoding="utf-8")
        spawned2 = []
        for _ in range(50):
            time.sleep(0.05)
            spawned2 = scheduler.tick()
            if spawned2:
                break

        self.assertEqual(len(spawned2), 1)
        self.assertEqual(spawned2[0][1], j2)

        (j2 / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_no_head_of_line_blocking_on_busy_workspace(self) -> None:
        """A queued job blocked by a busy workspace does NOT block subsequent jobs for free workspaces."""
        ws_alpha = self.root / "WorkspaceAlpha_Repo"
        ws_alpha.mkdir(parents=True)
        ws_beta = self.root / "WorkspaceBeta_Repo"
        ws_beta.mkdir(parents=True)

        # Job 1: MiniMax on the alpha workspace (running)
        j1 = _create_job(self.jobs_dir, "m_alpha", harness="minimax", cwd=ws_alpha, status="running", has_lock=True)
        # Acquire the initial alpha workspace lease
        acquire_workspace_lease(self.jobs_dir, get_canonical_workspace(ws_alpha), j1, "minimax")

        # Queue:
        # Job 2: Codex on alpha (blocked because MiniMax holds the lease)
        j2 = _create_job(self.jobs_dir, "c_alpha", harness="codex", cwd=ws_alpha, created_at="2000-01-01T10:01:00Z")
        # Job 3: Codex on beta (free workspace)
        j3 = _create_job(self.jobs_dir, "c_beta", harness="codex", cwd=ws_beta, created_at="2000-01-01T10:02:00Z")

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
            concurrency_limits={"codex": 3, "minimax": 3},
        )

        # Tick: Codex tries Job 2 (alpha is leased), skips it, and dispatches Job 3
        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][1], j3)
        self.assertEqual(len(scheduler.active_workers["codex"]), 1)

        # Job 2 is still queued
        time.sleep(0.08)
        self.assertIn(j2, queued_jobs(self.jobs_dir))

        # Finish MiniMax Job 1 on alpha and release its lease
        (j1 / "exit_trigger").write_text("done", encoding="utf-8")
        release_workspace_lease(self.jobs_dir, get_canonical_workspace(ws_alpha), j1)
        (j1 / "status.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        (j1 / "worker.lock").unlink(missing_ok=True)

        # Next tick: Job 2 on alpha can now be dispatched
        spawned2 = scheduler.tick()
        self.assertEqual(len(spawned2), 1)
        self.assertEqual(spawned2[0][1], j2)

        # Clean up
        for j in (j2, j3):
            (j / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_multi_workspace_parallelism_up_to_9(self) -> None:
        """9 jobs across 9 distinct repositories execute concurrently up to configured limits."""
        workspaces = [self.root / f"repo_{i}" for i in range(9)]
        for ws in workspaces:
            (ws / ".git").mkdir(parents=True)

        codex_jobs = [_create_job(self.jobs_dir, f"c_{i}", harness="codex", cwd=workspaces[i], created_at=f"2000-01-01T10:0{i}:00Z") for i in range(3)]
        minimax_jobs = [_create_job(self.jobs_dir, f"m_{i}", harness="minimax", cwd=workspaces[3 + i], created_at=f"2000-01-01T10:0{i}:00Z") for i in range(3)]
        agy_jobs = [_create_job(self.jobs_dir, f"a_{i}", harness="agy", cwd=workspaces[6 + i], created_at=f"2000-01-01T10:0{i}:00Z") for i in range(3)]

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
        )

        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 9, "All 9 distinct workspaces must be scheduled in parallel")

        total_active = sum(len(w) for w in scheduler.active_workers.values())
        self.assertEqual(total_active, 9)

        for j in (codex_jobs + minimax_jobs + agy_jobs):
            (j / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_dual_schedulers_concurrent_race_same_workspace(self) -> None:
        """Two independent schedulers racing simultaneously for the same workspace: only 1 succeeds."""
        import threading

        ws = self.root / "Contended_Repo"
        ws.mkdir(parents=True)

        j1 = _create_job(self.jobs_dir, "j_codex_race", harness="codex", cwd=ws, created_at="2000-01-01T10:01:00Z")
        j2 = _create_job(self.jobs_dir, "j_minimax_race", harness="minimax", cwd=ws, created_at="2000-01-01T10:02:00Z")

        scheduler_a = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
        )
        scheduler_b = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
        )

        barrier = threading.Barrier(2)
        spawned_a: list[tuple[str, Path, int]] = []
        spawned_b: list[tuple[str, Path, int]] = []

        def run_a() -> None:
            barrier.wait()
            nonlocal spawned_a
            spawned_a = scheduler_a.tick()

        def run_b() -> None:
            barrier.wait()
            nonlocal spawned_b
            spawned_b = scheduler_b.tick()

        t1 = threading.Thread(target=run_a)
        t2 = threading.Thread(target=run_b)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly 1 job must have acquired the workspace lease
        total_spawned = len(spawned_a) + len(spawned_b)
        self.assertEqual(total_spawned, 1, f"Expected strictly 1 spawned worker for shared workspace, got {total_spawned}")

        time.sleep(0.08)
        self.assertEqual(len(queued_jobs(self.jobs_dir)), 1)

        # Clean up
        for j in (j1, j2):
            (j / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler_a.shutdown(timeout=3.0)
        scheduler_b.shutdown(timeout=3.0)

    def test_spawn_failure_releases_workspace_lease(self) -> None:
        """If worker spawn fails, workspace lease is immediately released."""
        ws = self.root / "Fail_Repo"
        ws.mkdir(parents=True)

        j_fail = _create_job(self.jobs_dir, "j_fail", harness="codex", cwd=ws)

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
        )

        with mock.patch.object(scheduler, "spawn_worker", side_effect=OSError("fake spawn error")):
            spawned = scheduler.tick()
            self.assertEqual(len(spawned), 0)

        # Workspace lease must NOT linger
        self.assertFalse(is_workspace_leased(self.jobs_dir, get_canonical_workspace(ws)))

        # Next normal tick can acquire lease and spawn
        spawned2 = scheduler.tick()
        self.assertEqual(len(spawned2), 1)

        (j_fail / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_worker_crash_releases_workspace_lease(self) -> None:
        """When worker crashes, its workspace lease is released so queued jobs can proceed."""
        ws = self.root / "Crash_Repo"
        ws.mkdir(parents=True)

        j_crash = _create_job(self.jobs_dir, "j_crash", harness="codex", cwd=ws, created_at="2000-01-01T10:01:00Z")
        (j_crash / "crash_immediately").write_text("crash", encoding="utf-8")

        j_next = _create_job(self.jobs_dir, "j_next", harness="codex", cwd=ws, created_at="2000-01-01T10:02:00Z")

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
        )

        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][1], j_crash)

        time.sleep(0.2)
        # Tick reaps crashed worker, frees workspace lease, and dispatches j_next
        spawned2 = scheduler.tick()
        self.assertEqual(len(spawned2), 1)
        self.assertEqual(spawned2[0][1], j_next)

        (j_next / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_workspace_lease_dead_owner_recovery(self) -> None:
        """A workspace lease held by a dead PID is safely reclaimed on next tick."""
        ws = self.root / "Dead_Owner_Repo"
        ws.mkdir(parents=True)
        canonical_ws = get_canonical_workspace(ws)

        lease_path = get_workspace_lease_path(self.jobs_dir, canonical_ws)
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        # Write dead PID
        lease_path.write_text(f"pid=9999999 job_id=old_dead_job harness=codex created_at=1000.0 workspace={canonical_ws}\n", encoding="utf-8")

        j1 = _create_job(self.jobs_dir, "j_new", harness="codex", cwd=ws)

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
        )

        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][1], j1)

        (j1 / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_workspace_lease_live_owner_protection(self) -> None:
        """A workspace lease held by a live PID is NEVER deleted, even with old mtime."""
        ws = self.root / "Live_Owner_Repo"
        ws.mkdir(parents=True)
        canonical_ws = get_canonical_workspace(ws)

        lease_path = get_workspace_lease_path(self.jobs_dir, canonical_ws)
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease_path.write_text(f"pid={os.getpid()} job_id=live_job harness=codex created_at={time.time() - 3600} workspace={canonical_ws}\n", encoding="utf-8")
        os.utime(lease_path, (time.time() - 3600, time.time() - 3600))

        # Check is_workspace_leased
        self.assertTrue(is_workspace_leased(self.jobs_dir, canonical_ws))
        self.assertTrue(lease_path.exists())

        # Clean up
        lease_path.unlink(missing_ok=True)

    def test_workspace_lease_daemon_dead_worker_alive(self) -> None:
        """When daemon PID is dead but worker process is still ALIVE, workspace lease is actively preserved."""
        ws = self.root / "Daemon_Dead_Worker_Alive_Repo"
        ws.mkdir(parents=True)
        canonical_ws = get_canonical_workspace(ws)

        # Job 1: Running job with live worker PID (current process)
        j1 = _create_job(self.jobs_dir, "j1_running", harness="codex", cwd=ws, status="running", has_lock=True)
        # Lease recorded with a DEAD daemon PID (9999999)
        lease_path = get_workspace_lease_path(self.jobs_dir, canonical_ws)
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease_path.write_text(
            f"pid=9999999 job_id=j1_running harness=codex created_at={time.time() - 60} workspace={canonical_ws}\n",
            encoding="utf-8",
        )

        # Second job queued on the same workspace
        j2 = _create_job(self.jobs_dir, "j2_queued", harness="minimax", cwd=ws, created_at="2000-01-01T10:02:00Z")

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
        )

        # Tick: MiniMax job must NOT start because worker for j1 is still alive
        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 0, "Second job must NOT start while worker is still alive")
        self.assertTrue(is_workspace_leased(self.jobs_dir, canonical_ws), "Lease must be preserved for live worker")
        self.assertIn(j2, queued_jobs(self.jobs_dir))

        # Clean up
        lease_path.unlink(missing_ok=True)

    def test_workspace_lease_daemon_dead_worker_dead(self) -> None:
        """When daemon PID is dead and worker process is ALSO dead, lease is safely reclaimed."""
        ws = self.root / "Daemon_Dead_Worker_Dead_Repo"
        ws.mkdir(parents=True)
        canonical_ws = get_canonical_workspace(ws)

        # Job 1: Dead worker PID (9999998) in worker.lock
        j1 = _create_job(self.jobs_dir, "j1_dead", harness="codex", cwd=ws, status="running")
        (j1 / "worker.lock").write_text("9999998 2000-01-01T10:00:00Z\n", encoding="utf-8")

        # Lease has dead daemon PID (9999999)
        lease_path = get_workspace_lease_path(self.jobs_dir, canonical_ws)
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease_path.write_text(
            f"pid=9999999 job_id=j1_dead harness=codex created_at={time.time() - 60} workspace={canonical_ws}\n",
            encoding="utf-8",
        )

        # Job 2: Queued on same workspace
        j2 = _create_job(self.jobs_dir, "j2_queued", harness="codex", cwd=ws, created_at="2000-01-01T10:02:00Z")

        scheduler = HarborScheduler(
            jobs_dir=self.jobs_dir,
            python_exe=sys.executable,
            worker_script=self.fake_worker,
        )

        # Legacy Windows records cannot prove that an orphan child has exited.
        if codex_job_daemon.IS_WINDOWS:
            self.assertEqual(scheduler.tick(), [])
            self.assertTrue(lease_path.exists())
            return
        # POSIX group observation proves the dead lease can be reclaimed.
        spawned = scheduler.tick()
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][1], j2)

        (j2 / "exit_trigger").write_text("done", encoding="utf-8")
        scheduler.shutdown(timeout=3.0)

    def test_workspace_lease_daemon_dead_handoff_grace_window(self) -> None:
        """When daemon dies right after Popen, fresh lease is protected during grace window, reclaimed after."""
        ws = self.root / "Handoff_Grace_Repo"
        ws.mkdir(parents=True)
        canonical_ws = get_canonical_workspace(ws)

        # Job 1: queued/running, but no worker.lock yet (worker still starting up)
        j1 = _create_job(self.jobs_dir, "j1_handoff", harness="codex", cwd=ws, status="queued")

        # Lease has dead daemon PID (9999999), created 0.2s ago (within 5.0s grace)
        lease_path = get_workspace_lease_path(self.jobs_dir, canonical_ws)
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease_path.write_text(
            f"pid=9999999 job_id=j1_handoff harness=codex created_at={time.time()} workspace={canonical_ws}\n",
            encoding="utf-8",
        )

        # 1. Fresh lease is protected (not reclaimed)
        self.assertTrue(is_workspace_leased(self.jobs_dir, canonical_ws), "Fresh lease within handoff grace must be protected")

        # 2. Artificially age the lease file past the 5.0s handoff grace window
        past_time = time.time() - 10.0
        os.utime(lease_path, (past_time, past_time))

        # 3. Now that grace window expired with dead daemon and no worker.lock, lease is reclaimed
        self.assertFalse(is_workspace_leased(self.jobs_dir, canonical_ws), "Lease past handoff grace with dead daemon must be reclaimed")

    def test_workspace_lease_terminal_job_cleanup(self) -> None:
        """When job reaches terminal status (completed/failed/cancelled), leftover lease is immediately cleaned."""
        ws = self.root / "Terminal_Job_Repo"
        ws.mkdir(parents=True)
        canonical_ws = get_canonical_workspace(ws)

        # Terminal job (completed)
        j1 = _create_job(self.jobs_dir, "j1_completed", harness="codex", cwd=ws, status="completed")

        lease_path = get_workspace_lease_path(self.jobs_dir, canonical_ws)
        lease_path.parent.mkdir(parents=True, exist_ok=True)
        lease_path.write_text(
            f"pid={os.getpid()} job_id=j1_completed harness=codex created_at={time.time()} workspace={canonical_ws}\n",
            encoding="utf-8",
        )

        # Lease is immediately recognized as stale because associated job is completed
        self.assertFalse(is_workspace_leased(self.jobs_dir, canonical_ws))
        self.assertFalse(lease_path.exists())


if __name__ == "__main__":
    unittest.main()



