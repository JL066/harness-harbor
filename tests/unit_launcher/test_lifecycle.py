"""Unit tests for Lifecycle management."""

import pytest
from launcher.config import COLOR_STATUS_HEALTHY, COLOR_STATUS_RUNNING, COLOR_STATUS_STOPPED
from launcher.health_checker import ComponentHealth, HarborHealthSnapshot
from launcher.lifecycle import restart_harbor, start_harbor, stop_harbor
from launcher.process_manager import HarborProcessTree, ProcessInfo


def test_start_harbor_idempotent(monkeypatch):
    """When Harbor is already running and healthy, start_harbor must not spawn duplicates."""
    mock_health = HarborHealthSnapshot(
        tunnel=ComponentHealth(name="Tunnel", status="Healthy", detail="ok", pids=[200], color=COLOR_STATUS_HEALTHY),
        mcp=ComponentHealth(name="Harbor MCP", status="Healthy", detail="ok", pids=[300], color=COLOR_STATUS_HEALTHY),
        daemon=ComponentHealth(name="Job Daemon", status="Running", detail="ok", pids=[500], color=COLOR_STATUS_RUNNING),
        overall_status="All systems healthy",
        overall_color=COLOR_STATUS_HEALTHY,
        timestamp=100.0,
        health_url="http://127.0.0.1:51260",
        tree=HarborProcessTree([], [], [], [], []),
    )

    monkeypatch.setattr("launcher.lifecycle.get_harbor_health", lambda: mock_health)

    spawned = []
    monkeypatch.setattr("launcher.lifecycle.spawn_detached_supervisor", lambda s: spawned.append(s))
    monkeypatch.setattr("launcher.lifecycle.start_scheduled_task", lambda s: spawned.append(s))

    ok, msg = start_harbor()
    assert ok is True
    assert "already running" in msg
    assert len(spawned) == 0  # Absolutely zero processes or tasks spawned!


def test_stop_harbor_sequence(monkeypatch):
    """Test stop_harbor calls task stop and process termination."""
    actions = []

    monkeypatch.setattr("launcher.lifecycle.stop_scheduled_tasks", lambda: actions.append("stop_tasks"))

    mock_tree = HarborProcessTree(
        tunnel_supervisors=[ProcessInfo(pid=100, ppid=1, name="powershell.exe", command_line="start-tunnel.ps1")],
        tunnel_clients=[ProcessInfo(pid=200, ppid=100, name="tunnel-client.exe", command_line="tunnel-client")],
        mcp_servers=[ProcessInfo(pid=300, ppid=200, name="pythonw.exe", command_line="server_legacy.py")],
        daemon_supervisors=[],
        job_daemons=[],
    )
    monkeypatch.setattr("launcher.lifecycle.get_harbor_process_tree", lambda: mock_tree)

    def mock_terminate(tree, timeout_seconds=6.0):
        actions.append("terminate_tree")
        return True, []

    monkeypatch.setattr("launcher.lifecycle.terminate_harbor_processes", mock_terminate)

    ok, msg = stop_harbor()
    assert ok is True
    assert actions == ["stop_tasks", "terminate_tree"]


def test_restart_harbor_calls_stop_then_start(monkeypatch):
    calls = []

    monkeypatch.setattr("launcher.lifecycle.stop_harbor", lambda progress_cb=None: (calls.append("stop"), (True, "stopped"))[1])
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr("launcher.lifecycle.start_harbor", lambda progress_cb=None: (calls.append("start"), (True, "started"))[1])

    ok, msg = restart_harbor()
    assert ok is True
    assert calls == ["stop", "start"]


def test_managed_stop_does_not_scan_or_terminate_legacy_tree(monkeypatch):
    actions = []

    class Managed:
        def stop(self, timeout):
            actions.append(("managed_stop", timeout))
            return True

    monkeypatch.setattr("launcher.lifecycle.stop_scheduled_tasks", lambda: pytest.fail("legacy tasks must remain enabled"))
    monkeypatch.setattr("launcher.lifecycle.get_harbor_process_tree", lambda: pytest.fail("must not scan broad tree"))
    monkeypatch.setattr("launcher.lifecycle.terminate_harbor_processes", lambda *a, **k: pytest.fail("must not terminate broad tree"))

    ok, msg = stop_harbor(managed_tunnel=Managed(), timeout_seconds=2.5)
    assert ok is True
    assert "Managed tunnel stopped" in msg
    assert actions == [("managed_stop", 2.5)]
