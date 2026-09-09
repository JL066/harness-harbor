"""Unit tests for Process Manager."""

import pytest
from pathlib import Path
import launcher.process_manager as process_manager
from launcher.process_manager import (
    HarborProcessTree,
    ProcessInfo,
    find_daemon_supervisors,
    find_job_daemons,
    find_mcp_servers,
    find_tunnel_clients,
    find_tunnel_supervisors,
    parse_powershell_json,
    parse_wmic_csv,
    terminate_harbor_processes,
)


@pytest.fixture(autouse=True)
def _public_fixture_paths(monkeypatch):
    monkeypatch.setattr(process_manager, "PRODUCTION_PATH", Path(r"C:\Users\Example\HarnessHarbor"))
    monkeypatch.setattr(process_manager, "JUNCTION_PATH", Path(r"C:\Users\Example\HarnessHarbor\.harbor-junction"))


def test_parse_wmic_csv():
    sample_csv = """Node,CommandLine,Name,ParentProcessId,ProcessId
DESKTOP-TEST,"powershell.exe" -File "C:\\Users\\Example\\HarnessHarbor\\start-tunnel.ps1",powershell.exe,100,200
DESKTOP-TEST,"tunnel-client.exe" run --profile harness-harbor,tunnel-client.exe,200,300
"""
    procs = parse_wmic_csv(sample_csv)
    assert len(procs) == 2
    assert procs[0].pid == 200
    assert procs[0].ppid == 100
    assert "start-tunnel.ps1" in procs[0].command_line
    assert procs[1].pid == 300
    assert procs[1].name == "tunnel-client.exe"


def test_parse_powershell_json():
    sample_json = """[
        {"ProcessId": 1234, "ParentProcessId": 5678, "Name": "python.exe", "CommandLine": "python C:\\\\Users\\\\Example\\\\HarnessHarbor\\\\codex_job_daemon.py"}
    ]"""
    procs = parse_powershell_json(sample_json)
    assert len(procs) == 1
    assert procs[0].pid == 1234
    assert procs[0].ppid == 5678
    assert "codex_job_daemon.py" in procs[0].command_line


def test_find_tunnel_supervisors():
    procs = [
        ProcessInfo(pid=10, ppid=1, name="powershell.exe", command_line='powershell -File "C:\\Users\\Example\\HarnessHarbor\\start-tunnel.ps1"'),
        ProcessInfo(pid=11, ppid=1, name="powershell.exe", command_line='powershell -File "C:\\Users\\Example\\other-project\\start-tunnel.ps1"'),
        ProcessInfo(pid=12, ppid=1, name="cmd.exe", command_line='cmd /c echo hi'),
    ]
    found = find_tunnel_supervisors(procs)
    assert len(found) == 1
    assert found[0].pid == 10


def test_find_tunnel_clients():
    procs = [
        ProcessInfo(pid=20, ppid=10, name="tunnel-client.exe", command_line='"tunnel-client.exe" run --profile harness-harbor'),
        ProcessInfo(pid=21, ppid=10, name="tunnel-client.exe", command_line='"tunnel-client.exe" run --profile other-profile'),
    ]
    found = find_tunnel_clients(procs)
    assert len(found) == 1
    assert found[0].pid == 20


def test_find_mcp_servers_rejects_forbidden_server_py():
    procs = [
        # Valid production MCP
        ProcessInfo(pid=30, ppid=20, name="pythonw.exe", command_line='pythonw.exe C:\\Users\\Example\\HarnessHarbor\\server_legacy.py'),
        # FORBIDDEN: legacy simplified server.py MUST be rejected
        ProcessInfo(pid=31, ppid=20, name="pythonw.exe", command_line='pythonw.exe C:\\Users\\Example\\HarnessHarbor\\server.py'),
    ]
    found = find_mcp_servers(procs)
    assert len(found) == 1
    assert found[0].pid == 30
    assert "server_legacy.py" in found[0].command_line
    # Ensure forbidden server.py was NOT matched
    assert not any(p.pid == 31 for p in found)


def test_find_job_daemons():
    procs = [
        ProcessInfo(pid=40, ppid=1, name="powershell.exe", command_line='powershell -File "C:\\Users\\Example\\HarnessHarbor\\start-codex-job-daemon.ps1"'),
        ProcessInfo(pid=41, ppid=40, name="python.exe", command_line='python.exe C:\\Users\\Example\\HarnessHarbor\\codex_job_daemon.py'),
    ]
    supervisors = find_daemon_supervisors(procs)
    daemons = find_job_daemons(procs)
    assert len(supervisors) == 1
    assert supervisors[0].pid == 40
    assert len(daemons) == 1
    assert daemons[0].pid == 41


def test_termination_order(monkeypatch):
    """Ensure supervisors are killed first to eliminate supervisor respawn loops."""
    killed_pids = []

    def mock_terminate(pid, force=True):
        killed_pids.append(pid)
        return True

    def mock_is_alive(pid):
        return False

    monkeypatch.setattr("launcher.process_manager.terminate_pid", mock_terminate)
    monkeypatch.setattr("launcher.process_manager.is_pid_alive", mock_is_alive)

    tree = HarborProcessTree(
        tunnel_supervisors=[ProcessInfo(pid=100, ppid=1, name="powershell.exe", command_line="start-tunnel.ps1")],
        tunnel_clients=[ProcessInfo(pid=200, ppid=100, name="tunnel-client.exe", command_line="tunnel-client")],
        mcp_servers=[ProcessInfo(pid=300, ppid=200, name="pythonw.exe", command_line="server_legacy.py")],
        daemon_supervisors=[ProcessInfo(pid=400, ppid=1, name="powershell.exe", command_line="start-codex-job-daemon.ps1")],
        job_daemons=[ProcessInfo(pid=500, ppid=400, name="python.exe", command_line="codex_job_daemon.py")],
    )

    ok, rem = terminate_harbor_processes(tree, timeout_seconds=1.0)
    assert ok is True
    assert rem == []

    # Supervisors (100, 400) MUST be killed before clients (200) and workers (300, 500)
    assert killed_pids.index(100) < killed_pids.index(200)
    assert killed_pids.index(400) < killed_pids.index(500)
