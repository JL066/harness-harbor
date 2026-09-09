"""Unit tests for Health Checker."""

from launcher.config import COLOR_STATUS_HEALTHY, COLOR_STATUS_RUNNING, COLOR_STATUS_STOPPED
from launcher.health_checker import (
    ComponentHealth,
    check_daemon_health,
    check_mcp_health,
    check_tunnel_health,
    get_harbor_health,
)
from launcher.process_manager import HarborProcessTree, ProcessInfo


def test_tunnel_health_stopped():
    tree = HarborProcessTree([], [], [], [], [])
    health = check_tunnel_health(tree)
    assert health.status == "Stopped"
    assert health.color == COLOR_STATUS_STOPPED


def test_tunnel_health_healthy(monkeypatch):
    tree = HarborProcessTree(
        tunnel_supervisors=[],
        tunnel_clients=[ProcessInfo(pid=200, ppid=1, name="tunnel-client.exe", command_line="tunnel-client")],
        mcp_servers=[],
        daemon_supervisors=[],
        job_daemons=[],
    )

    # Mock probe to return 200 OK
    monkeypatch.setattr("launcher.health_checker.probe_http_health", lambda url, timeout=2.0: (True, 200, 1.5))
    monkeypatch.setattr("launcher.health_checker.read_tunnel_health_url", lambda: "http://127.0.0.1:51260")

    health = check_tunnel_health(tree)
    assert health.status == "Healthy"
    assert "51260" in health.detail
    assert health.color == COLOR_STATUS_HEALTHY


def test_mcp_health_correlation():
    tree = HarborProcessTree(
        tunnel_supervisors=[],
        tunnel_clients=[ProcessInfo(pid=200, ppid=1, name="tunnel-client.exe", command_line="tunnel-client")],
        mcp_servers=[ProcessInfo(pid=300, ppid=200, name="pythonw.exe", command_line="server_legacy.py")],
        daemon_supervisors=[],
        job_daemons=[],
    )

    tun_healthy = ComponentHealth(name="Tunnel", status="Healthy", detail="", pids=[200], color=COLOR_STATUS_HEALTHY)
    mcp_h = check_mcp_health(tree, tun_healthy)
    assert mcp_h.status == "Healthy"
    assert mcp_h.pids == [300]

    tun_stopped = ComponentHealth(name="Tunnel", status="Stopped", detail="", pids=[], color=COLOR_STATUS_STOPPED)
    tree_no_mcp = HarborProcessTree([], [], [], [], [])
    mcp_stopped = check_mcp_health(tree_no_mcp, tun_stopped)
    assert mcp_stopped.status == "Stopped"


def test_daemon_health():
    tree = HarborProcessTree(
        tunnel_supervisors=[],
        tunnel_clients=[],
        mcp_servers=[],
        daemon_supervisors=[],
        job_daemons=[ProcessInfo(pid=500, ppid=1, name="python.exe", command_line="codex_job_daemon.py")],
    )
    health = check_daemon_health(tree)
    assert health.status == "Running"
    assert health.color == COLOR_STATUS_RUNNING
    assert health.pids == [500]
