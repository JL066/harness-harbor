"""Unit tests for stale running job recovery after abrupt Harbor exit (codex_job_daemon.py)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest

from codex_job_daemon import (
    HarborScheduler,
    acquire_workspace_lease,
    get_canonical_workspace,
    get_disk_active_job_ids,
    is_workspace_leased,
    recover_abandoned_jobs,
    reserve_job,
)
from control_plane import (
    queue_root_fingerprint,
)
from runtime_liveness import ProcessLiveness


def _setup_job(
    jobs_dir: Path,
    job_id: str,
    status: str = "running",
    worker_pid: int | None = 41520,
    lock_pid: int | None = 41520,
    cwd: Path | None = None,
    result_text: str | None = None,
    process_exit_code: int | None = None,
    st_mtime_offset: float = -10.0,
) -> Path:
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    workdir = cwd or (job_dir / "workspace")
    workdir.mkdir(parents=True, exist_ok=True)

    state = {
        "job_id": job_id,
        "harness": "codex",
        "status": status,
        "prompt": "test task",
        "cwd": str(workdir),
        "queue_root_fingerprint": queue_root_fingerprint(jobs_dir),
    }
    if worker_pid is not None:
        state["worker_pid"] = worker_pid
    if process_exit_code is not None:
        state["process_exit_code"] = process_exit_code

    state_path = job_dir / "status.json"
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # Set mtime back so it's outside the grace period
    past = time.time() + st_mtime_offset
    os.utime(state_path, (past, past))

    if lock_pid is not None:
        (job_dir / "worker.lock").write_text(str(lock_pid), encoding="utf-8")

    if result_text is not None:
        (job_dir / "result.txt").write_text(result_text, encoding="utf-8")

    return job_dir


def test_recover_dead_pid_abandoned_job(tmp_path):
    """Exact reproduction: status=running, worker.lock PID 41520 (dead process)."""
    dead_pid = 41520
    job_dir = _setup_job(tmp_path, "f3ac2f289aa2463d908fa95b2c650c54", worker_pid=dead_pid, lock_pid=dead_pid)
    ws = get_canonical_workspace(job_dir / "workspace")

    # Simulate active locks and lease
    reserve_job(job_dir)
    acquire_workspace_lease(tmp_path, ws, job_dir, "codex")
    assert (job_dir / "worker.lock").exists()
    assert (job_dir / "dispatch.lock").exists()

    recovered = recover_abandoned_jobs(tmp_path, "codex")
    assert "f3ac2f289aa2463d908fa95b2c650c54" in recovered

    # 1. Forensic backup was created before mutation
    backup = job_dir / "status.before-recovery.json"
    assert backup.is_file()
    backup_data = json.loads(backup.read_text(encoding="utf-8"))
    assert backup_data["status"] == "running"
    assert backup_data["worker_pid"] == dead_pid

    # 2. Terminal state marked failed with worker_abandoned_after_runtime_exit
    curr_data = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert curr_data["status"] == "failed"
    assert curr_data["failure_type"] == "worker_abandoned_after_runtime_exit"
    assert "Worker exited or was abandoned" in curr_data["error"]
    assert "recovered_at" in curr_data

    # 3. Locks and workspace lease released
    assert not (job_dir / "worker.lock").exists()
    assert not (job_dir / "dispatch.lock").exists()
    assert is_workspace_leased(tmp_path, ws) is False


def test_recover_canonical_success_reconstruction(tmp_path):
    """When result.txt exists and process_exit_code == 0, reconstruct completed terminal state."""
    dead_pid = 41520
    job_dir = _setup_job(
        tmp_path,
        "completed_recon",
        worker_pid=dead_pid,
        lock_pid=dead_pid,
        result_text="Task finished successfully with full diff",
        process_exit_code=0,
    )

    recovered = recover_abandoned_jobs(tmp_path, "codex")
    assert "completed_recon" in recovered

    curr_data = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert curr_data["status"] == "completed"
    assert curr_data["final_message"] == "Task finished successfully with full diff"
    assert "recovered_at" in curr_data


def test_live_pid_preservation(tmp_path):
    """Running job with a live owner PID must NOT be recovered."""
    live_pid = os.getpid()
    job_dir = _setup_job(tmp_path, "live_job", worker_pid=live_pid, lock_pid=live_pid)

    recovered = recover_abandoned_jobs(tmp_path, "codex")
    assert "live_job" not in recovered

    curr_data = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert curr_data["status"] == "running"
    assert not (job_dir / "status.before-recovery.json").exists()
    assert (job_dir / "worker.lock").exists()


def test_unverifiable_ownership_fails_closed(tmp_path):
    """When ownership cannot be determined at all, fail-closed: do not mutate."""
    job_dir = _setup_job(tmp_path, "unverifiable_job", worker_pid=None, lock_pid=None)

    recovered = recover_abandoned_jobs(tmp_path, "codex")
    assert "unverifiable_job" not in recovered

    curr_data = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert curr_data["status"] == "running"
    assert not (job_dir / "status.before-recovery.json").exists()


def test_unknown_owner_liveness_fails_closed(tmp_path, monkeypatch):
    job_dir = _setup_job(tmp_path, "unknown_liveness", worker_pid=424242, lock_pid=424242)
    monkeypatch.setattr(
        "codex_job_daemon._probe_pid_liveness", lambda pid: ProcessLiveness.UNKNOWN
    )

    recovered = recover_abandoned_jobs(tmp_path, "codex")
    assert recovered == []
    assert json.loads((job_dir / "status.json").read_text(encoding="utf-8"))["status"] == "running"
    assert not (job_dir / "status.before-recovery.json").exists()
    assert (job_dir / "worker.lock").exists()


def test_unknown_tree_liveness_fails_closed(tmp_path, monkeypatch):
    job_dir = _setup_job(tmp_path, "unknown_tree", worker_pid=424242, lock_pid=424242)
    monkeypatch.setattr(
        "codex_job_daemon._probe_pid_liveness", lambda pid: ProcessLiveness.DEAD
    )
    monkeypatch.setattr(
        "codex_job_daemon._job_tree_liveness",
        lambda job_dir, owners: ProcessLiveness.UNKNOWN,
    )

    recovered = recover_abandoned_jobs(tmp_path, "codex")
    assert recovered == []
    assert json.loads((job_dir / "status.json").read_text(encoding="utf-8"))["status"] == "running"
    assert not (job_dir / "status.before-recovery.json").exists()


def test_concurrency_slot_release_and_get_disk_active(tmp_path):
    """Dead PID running jobs must not consume concurrency slots in get_disk_active_job_ids."""
    dead_pid = 41520
    _setup_job(tmp_path, "stale_1", worker_pid=dead_pid, lock_pid=dead_pid)
    _setup_job(tmp_path, "live_1", worker_pid=os.getpid(), lock_pid=os.getpid())

    active = get_disk_active_job_ids(tmp_path)
    codex_active = active.get("codex", set())

    # Live job is active; stale job with dead PID does not count as active
    assert "live_1" in codex_active
    assert "stale_1" not in codex_active


def test_unknown_liveness_keeps_concurrency_slot_occupied(tmp_path, monkeypatch):
    _setup_job(tmp_path, "uncertain_1", worker_pid=424242, lock_pid=424242)
    monkeypatch.setattr(
        "codex_job_daemon._probe_pid_liveness", lambda pid: ProcessLiveness.UNKNOWN
    )

    active = get_disk_active_job_ids(tmp_path)
    assert "uncertain_1" in active["codex"]


def test_idempotence_and_scheduler_lifecycle(tmp_path):
    """Recovery on scheduler init and repeated tick calls is idempotent."""
    dead_pid = 41520
    job_dir = _setup_job(tmp_path, "stale_idem", worker_pid=dead_pid, lock_pid=dead_pid)

    # 1. HarborScheduler init recovers abandoned jobs on startup
    scheduler = HarborScheduler(jobs_dir=tmp_path)
    state1 = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert state1["status"] == "failed"
    assert state1["failure_type"] == "worker_abandoned_after_runtime_exit"

    # Backup was created
    backup = job_dir / "status.before-recovery.json"
    assert backup.exists()
    backup_content = backup.read_text(encoding="utf-8")

    # 2. Subsequent tick() run
    scheduler.tick()
    state2 = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert state2["status"] == "failed"
    # Backup content untouched
    assert backup.read_text(encoding="utf-8") == backup_content

    # 3. Third explicit recover_abandoned_jobs returns empty (already terminal)
    rec3 = recover_abandoned_jobs(tmp_path, "codex")
    assert rec3 == []
