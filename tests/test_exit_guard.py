"""Unit tests for active-job exit guard (launcher/exit_guard.py and launcher/ui/app.py)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from launcher.exit_guard import ExitConfirmationDialog, get_authoritative_active_jobs
from launcher.ui.app import HarborLauncherApp
from runtime_liveness import ProcessLiveness


def test_get_authoritative_active_jobs_empty(tmp_path):
    res = get_authoritative_active_jobs(tmp_path)
    assert res["running_count"] == 0
    assert res["queued_count"] == 0
    assert res["running_job_ids"] == []
    assert res["queued_job_ids"] == []
    assert res["running_harnesses"] == []


def test_get_authoritative_active_jobs_live_pid(tmp_path):
    job_dir = tmp_path / "job_live"
    job_dir.mkdir()
    (job_dir / "status.json").write_text(
        json.dumps({
            "status": "running",
            "harness": "codex",
            "prompt": "live task",
            "worker_pid": os.getpid(),
        }),
        encoding="utf-8",
    )
    (job_dir / "worker.lock").write_text(str(os.getpid()), encoding="utf-8")

    res = get_authoritative_active_jobs(tmp_path)
    assert res["running_count"] == 1
    assert res["running_job_ids"] == ["job_live"]
    assert res["running_harnesses"] == ["codex"]


def test_get_authoritative_active_jobs_dead_pid(tmp_path, monkeypatch):
    job_dir = tmp_path / "job_dead"
    job_dir.mkdir()
    # 41520 is a known dead PID from observed reproduction
    dead_pid = 41520
    (job_dir / "status.json").write_text(
        json.dumps({
            "status": "running",
            "harness": "codex",
            "prompt": "abandoned task",
            "worker_pid": dead_pid,
        }),
        encoding="utf-8",
    )
    (job_dir / "worker.lock").write_text(str(dead_pid), encoding="utf-8")

    monkeypatch.setattr("launcher.exit_guard.probe_pid_liveness", lambda pid: ProcessLiveness.DEAD)
    monkeypatch.setattr(
        "launcher.exit_guard.probe_job_tree_liveness",
        lambda job_dir, owners: ProcessLiveness.DEAD,
    )

    # A proven-dead PID is safe to omit from the active warning.
    res = get_authoritative_active_jobs(tmp_path)
    assert res["running_count"] == 0
    assert res["running_job_ids"] == []
    assert res["running_harnesses"] == []


def test_get_authoritative_active_jobs_unknown_liveness_is_uncertain(tmp_path, monkeypatch):
    job_dir = tmp_path / "job_unknown"
    job_dir.mkdir()
    (job_dir / "status.json").write_text(
        json.dumps({"status": "running", "harness": "codex", "worker_pid": 424242}),
        encoding="utf-8",
    )

    monkeypatch.setattr("launcher.exit_guard.probe_pid_liveness", lambda pid: ProcessLiveness.UNKNOWN)
    res = get_authoritative_active_jobs(tmp_path)

    assert res["running_count"] == 0
    assert res["uncertain_count"] == 1
    assert res["uncertain_job_ids"] == ["job_unknown"]


def test_get_authoritative_active_jobs_missing_owner_is_uncertain(tmp_path):
    job_dir = tmp_path / "job_missing_owner"
    job_dir.mkdir()
    (job_dir / "status.json").write_text(
        json.dumps({"status": "running", "harness": "agy"}), encoding="utf-8"
    )

    res = get_authoritative_active_jobs(tmp_path)
    assert res["uncertain_count"] == 1
    assert res["uncertain_harnesses"] == ["agy"]


@pytest.mark.parametrize("tree_state, expected_key", [
    (ProcessLiveness.ALIVE, "running_count"),
    (ProcessLiveness.UNKNOWN, "uncertain_count"),
])
def test_dead_owner_with_live_or_unknown_tree_still_blocks_exit(
    tmp_path, monkeypatch, tree_state, expected_key
):
    job_dir = tmp_path / "job_tree"
    job_dir.mkdir()
    (job_dir / "status.json").write_text(
        json.dumps({"status": "running", "harness": "codex", "worker_pid": 424242}),
        encoding="utf-8",
    )
    monkeypatch.setattr("launcher.exit_guard.probe_pid_liveness", lambda pid: ProcessLiveness.DEAD)
    monkeypatch.setattr(
        "launcher.exit_guard.probe_job_tree_liveness", lambda job_dir, owners: tree_state
    )

    res = get_authoritative_active_jobs(tmp_path)
    assert res[expected_key] == 1


def test_malformed_worker_lock_is_uncertain_even_if_recorded_owner_is_dead(tmp_path, monkeypatch):
    job_dir = tmp_path / "job_bad_lock"
    job_dir.mkdir()
    (job_dir / "status.json").write_text(
        json.dumps({"status": "running", "harness": "codex", "worker_pid": 424242}),
        encoding="utf-8",
    )
    (job_dir / "worker.lock").write_text("not-a-pid", encoding="utf-8")
    monkeypatch.setattr("launcher.exit_guard.probe_pid_liveness", lambda pid: ProcessLiveness.DEAD)

    res = get_authoritative_active_jobs(tmp_path)
    assert res["uncertain_count"] == 1


def test_get_authoritative_active_jobs_unreadable_state_is_uncertain(tmp_path):
    job_dir = tmp_path / "job_corrupt"
    job_dir.mkdir()
    (job_dir / "status.json").write_text("{", encoding="utf-8")

    res = get_authoritative_active_jobs(tmp_path)
    assert res["inspection_error"] is True
    assert res["uncertain_count"] == 1
    assert res["uncertain_job_ids"] == ["job_corrupt"]


def test_get_authoritative_active_jobs_queued_context(tmp_path):
    job_dir = tmp_path / "job_queued"
    job_dir.mkdir()
    (job_dir / "status.json").write_text(
        json.dumps({
            "status": "queued",
            "harness": "agy",
            "prompt": "queued task",
        }),
        encoding="utf-8",
    )

    res = get_authoritative_active_jobs(tmp_path)
    assert res["running_count"] == 0
    assert res["queued_count"] == 1
    assert res["queued_job_ids"] == ["job_queued"]
    assert res["queued_harnesses"] == ["agy"]


def test_exit_confirmation_dialog_callbacks():
    confirmed = []
    cancelled = []

    dialog = ExitConfirmationDialog.__new__(ExitConfirmationDialog)
    dialog.on_confirm = lambda: confirmed.append(True)
    dialog.on_cancel = lambda: cancelled.append(True)
    dialog.destroy = lambda: None
    dialog.grab_release = lambda: None

    dialog._handle_cancel()
    assert len(cancelled) == 1
    assert len(confirmed) == 0

    dialog._handle_confirm()
    assert len(confirmed) == 1


def test_app_request_exit_zero_running_jobs(monkeypatch, tmp_path):
    app = HarborLauncherApp.__new__(HarborLauncherApp)
    app._exit_in_progress = False
    app.backend = None

    monkeypatch.setattr("control_plane.JOBS_DIR", tmp_path)

    exit_proceeded = []
    dialog_opened = []

    monkeypatch.setattr(app, "_proceed_with_exit", lambda: exit_proceeded.append(True))

    class MockDialog:
        def __init__(self, *args, **kwargs):
            dialog_opened.append(True)

    app._request_exit(confirm_dialog_factory=MockDialog)

    # Zero running jobs -> immediately exits without dialog
    assert len(exit_proceeded) == 1
    assert len(dialog_opened) == 0


def test_app_request_exit_with_running_jobs_cancel(monkeypatch, tmp_path):
    app = HarborLauncherApp.__new__(HarborLauncherApp)
    app._exit_in_progress = False
    app.backend = None

    monkeypatch.setattr("control_plane.JOBS_DIR", tmp_path)

    job_dir = tmp_path / "job_active"
    job_dir.mkdir()
    (job_dir / "status.json").write_text(
        json.dumps({
            "status": "running",
            "harness": "codex",
            "worker_pid": os.getpid(),
        }),
        encoding="utf-8",
    )
    (job_dir / "worker.lock").write_text(str(os.getpid()), encoding="utf-8")

    exit_proceeded = []

    monkeypatch.setattr(app, "_proceed_with_exit", lambda: exit_proceeded.append(True))

    class MockDialog:
        def __init__(self, *args, **kwargs):
            on_cancel = kwargs.get("on_cancel")
            if on_cancel:
                on_cancel()

    app._request_exit(confirm_dialog_factory=MockDialog)

    # Cancelled -> does not proceed with exit
    assert len(exit_proceeded) == 0


def test_app_request_exit_with_running_jobs_exit_anyway(monkeypatch, tmp_path):
    app = HarborLauncherApp.__new__(HarborLauncherApp)
    app._exit_in_progress = False
    app.backend = None

    monkeypatch.setattr("control_plane.JOBS_DIR", tmp_path)

    job_dir = tmp_path / "job_active"
    job_dir.mkdir()
    (job_dir / "status.json").write_text(
        json.dumps({
            "status": "running",
            "harness": "codex",
            "worker_pid": os.getpid(),
        }),
        encoding="utf-8",
    )
    (job_dir / "worker.lock").write_text(str(os.getpid()), encoding="utf-8")

    exit_proceeded = []

    monkeypatch.setattr(app, "_proceed_with_exit", lambda: exit_proceeded.append(True))

    class MockDialog:
        def __init__(self, *args, **kwargs):
            on_confirm = kwargs.get("on_confirm")
            if on_confirm:
                on_confirm()

    app._request_exit(confirm_dialog_factory=MockDialog)

    # Exit anyway -> proceeds with exit
    assert len(exit_proceeded) == 1


def test_app_request_exit_unknown_job_state_requires_confirmation(monkeypatch, tmp_path):
    app = HarborLauncherApp.__new__(HarborLauncherApp)
    app._exit_in_progress = False
    app._exit_prompt_open = False
    app.backend = None
    monkeypatch.setattr("control_plane.JOBS_DIR", tmp_path)

    job_dir = tmp_path / "job_unknown"
    job_dir.mkdir()
    (job_dir / "status.json").write_text(
        json.dumps({"status": "running", "harness": "codex", "worker_pid": 424242}),
        encoding="utf-8",
    )
    monkeypatch.setattr("launcher.exit_guard.probe_pid_liveness", lambda pid: ProcessLiveness.UNKNOWN)

    exit_proceeded = []
    dialog_args = []
    monkeypatch.setattr(app, "_proceed_with_exit", lambda: exit_proceeded.append(True))

    class MockDialog:
        def __init__(self, *args, **kwargs):
            dialog_args.append(kwargs)

    app._request_exit(confirm_dialog_factory=MockDialog)
    assert exit_proceeded == []
    assert dialog_args[0]["uncertain_count"] == 1
    assert app._exit_prompt_open is True
