"""Windows Process Management for Harness Harbor.

Discovers, correlates, and terminates Harbor-related processes strictly
by exact command line, path, and PID tree relationships.
"""

from __future__ import annotations

import csv
import io
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from launcher.config import (
    DAEMON_SCRIPT_NAME,
    FORBIDDEN_MCP_SCRIPT,
    JUNCTION_PATH,
    MCP_SCRIPT_NAME,
    PRODUCTION_PATH,
    START_DAEMON_SCRIPT,
    START_TUNNEL_SCRIPT,
    TUNNEL_PROFILE_NAME,
)


@dataclass(frozen=True)
class ProcessInfo:
    """Represents a running Windows process."""
    pid: int
    ppid: int
    name: str
    command_line: str

    def matches_path(self, target_path: Path | str) -> bool:
        norm_cmd = self.command_line.lower().replace("/", "\\")
        norm_tgt = str(target_path).lower().replace("/", "\\")
        return norm_tgt in norm_cmd


def parse_wmic_csv(output: str) -> list[ProcessInfo]:
    """Parse CSV output from WMIC process get."""
    processes: list[ProcessInfo] = []
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return processes

    reader = csv.reader(lines)
    header = None
    for row in reader:
        if not row:
            continue
        if header is None:
            header = [c.strip().lower() for c in row]
            continue
        if len(row) < len(header):
            continue

        row_dict = dict(zip(header, row))
        try:
            pid = int(row_dict.get("processid", 0))
            ppid = int(row_dict.get("parentprocessid", 0))
            name = row_dict.get("name", "").strip()
            cmd = row_dict.get("commandline", "").strip()
            if pid > 0:
                processes.append(ProcessInfo(pid=pid, ppid=ppid, name=name, command_line=cmd))
        except (ValueError, TypeError):
            continue

    return processes


def parse_powershell_json(output: str) -> list[ProcessInfo]:
    """Parse JSON output from Get-CimInstance Win32_Process."""
    import json
    processes: list[ProcessInfo] = []
    if not output.strip():
        return processes
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            data = [data]
        for item in data:
            pid = item.get("ProcessId") or item.get("processId")
            ppid = item.get("ParentProcessId") or item.get("parentProcessId")
            name = item.get("Name") or item.get("name") or ""
            cmd = item.get("CommandLine") or item.get("commandLine") or ""
            if pid:
                processes.append(ProcessInfo(pid=int(pid), ppid=int(ppid or 0), name=str(name), command_line=str(cmd)))
    except Exception:
        pass
    return processes


def query_windows_processes() -> list[ProcessInfo]:
    """Query all running processes with command lines on Windows.

    Uses wmic with fallback to PowerShell Get-CimInstance.
    """
    # Try wmic first
    try:
        cmd = ["wmic", "process", "get", "ProcessId,ParentProcessId,Name,CommandLine", "/format:csv"]
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=5.0,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if res.returncode == 0 and res.stdout.strip():
            procs = parse_wmic_csv(res.stdout)
            if procs:
                return procs
    except Exception:
        pass

    # Fallback to PowerShell
    try:
        ps_cmd = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, Name, CommandLine | ConvertTo-Json -Compress",
        ]
        res = subprocess.run(
            ps_cmd,
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=8.0,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if res.returncode == 0 and res.stdout.strip():
            return parse_powershell_json(res.stdout)
    except Exception:
        pass

    return []


# ---------------------------------------------------------------------------
# Component Identification Logic
# ---------------------------------------------------------------------------

def is_production_harbor_path(cmd: str) -> bool:
    """Check if command line references production Harness Harbor or codex-mcp junction."""
    cmd_lower = cmd.lower().replace("/", "\\")
    prod_norm = str(PRODUCTION_PATH).lower().replace("/", "\\")
    junc_norm = str(JUNCTION_PATH).lower().replace("/", "\\")
    return prod_norm in cmd_lower or junc_norm in cmd_lower


def find_tunnel_supervisors(processes: Sequence[ProcessInfo]) -> list[ProcessInfo]:
    """Find PowerShell supervisor processes running start-tunnel.ps1."""
    results: list[ProcessInfo] = []
    for p in processes:
        if p.name.lower() in ("powershell.exe", "pwsh.exe"):
            cmd = p.command_line.lower().replace("/", "\\")
            if "start-tunnel.ps1" in cmd and is_production_harbor_path(cmd):
                results.append(p)
    return results


def find_tunnel_clients(processes: Sequence[ProcessInfo]) -> list[ProcessInfo]:
    """Find tunnel-client.exe processes configured for harness-harbor profile."""
    results: list[ProcessInfo] = []
    for p in processes:
        if p.name.lower() == "tunnel-client.exe":
            cmd = p.command_line.lower()
            if TUNNEL_PROFILE_NAME.lower() in cmd:
                results.append(p)
    return results


def find_mcp_servers(processes: Sequence[ProcessInfo]) -> list[ProcessInfo]:
    """Find Harbor MCP server processes running server_legacy.py.

    NEVER matches server.py (the forbidden legacy simplified script).
    """
    results: list[ProcessInfo] = []
    for p in processes:
        name_lower = p.name.lower()
        if "python" in name_lower:
            cmd = p.command_line.lower().replace("/", "\\")
            # Explicit negative guard against server.py
            if re.search(r"\bserver\.py\b", cmd) and "server_legacy.py" not in cmd:
                continue

            if MCP_SCRIPT_NAME.lower() in cmd and is_production_harbor_path(cmd):
                results.append(p)
    return results


def find_daemon_supervisors(processes: Sequence[ProcessInfo]) -> list[ProcessInfo]:
    """Find PowerShell supervisor processes running start-codex-job-daemon.ps1."""
    results: list[ProcessInfo] = []
    for p in processes:
        if p.name.lower() in ("powershell.exe", "pwsh.exe"):
            cmd = p.command_line.lower().replace("/", "\\")
            if "start-codex-job-daemon.ps1" in cmd and is_production_harbor_path(cmd):
                results.append(p)
    return results


def find_job_daemons(processes: Sequence[ProcessInfo]) -> list[ProcessInfo]:
    """Find python processes running codex_job_daemon.py."""
    results: list[ProcessInfo] = []
    for p in processes:
        name_lower = p.name.lower()
        if "python" in name_lower:
            cmd = p.command_line.lower().replace("/", "\\")
            if DAEMON_SCRIPT_NAME.lower() in cmd and is_production_harbor_path(cmd):
                results.append(p)
    return results


@dataclass
class HarborProcessTree:
    """Consolidated snapshot of all running Harbor processes."""
    tunnel_supervisors: list[ProcessInfo]
    tunnel_clients: list[ProcessInfo]
    mcp_servers: list[ProcessInfo]
    daemon_supervisors: list[ProcessInfo]
    job_daemons: list[ProcessInfo]

    @property
    def all_pids(self) -> set[int]:
        pids: set[int] = set()
        for group in (
            self.tunnel_supervisors,
            self.tunnel_clients,
            self.mcp_servers,
            self.daemon_supervisors,
            self.job_daemons,
        ):
            pids.update(p.pid for p in group)
        return pids


def get_harbor_process_tree(processes: Sequence[ProcessInfo] | None = None) -> HarborProcessTree:
    """Scan and partition all active Harbor runtime processes."""
    if processes is None:
        processes = query_windows_processes()

    return HarborProcessTree(
        tunnel_supervisors=find_tunnel_supervisors(processes),
        tunnel_clients=find_tunnel_clients(processes),
        mcp_servers=find_mcp_servers(processes),
        daemon_supervisors=find_daemon_supervisors(processes),
        job_daemons=find_job_daemons(processes),
    )


# ---------------------------------------------------------------------------
# Safe Termination
# ---------------------------------------------------------------------------

def is_pid_alive(pid: int) -> bool:
    """Check if a given PID is currently active."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            # Query tasklist for PID
            res = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=2.0,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return str(pid) in res.stdout
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def terminate_pid(pid: int, force: bool = True) -> bool:
    """Terminate a specific PID safely using taskkill or OS kill."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            args = ["taskkill", "/PID", str(pid)]
            if force:
                args.append("/F")
            subprocess.run(
                args,
                capture_output=True,
                timeout=3.0,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return not is_pid_alive(pid)
        except Exception:
            return False
    else:
        try:
            os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
            return True
        except ProcessLookupError:
            return True
        except Exception:
            return False


def terminate_harbor_processes(tree: HarborProcessTree, timeout_seconds: float = 6.0) -> tuple[bool, list[int]]:
    """Terminate Harbor processes in strict hierarchical order to avoid respawn.

    Order:
    1. Supervisors first (PowerShell loops) so they cannot restart children.
    2. Main clients (tunnel-client.exe).
    3. MCP and Daemon child processes.

    Returns (all_terminated, remaining_pids).
    """
    # 1. Kill supervisors first
    for sup in tree.tunnel_supervisors + tree.daemon_supervisors:
        terminate_pid(sup.pid, force=True)

    # Short pause to ensure supervisors are dead before killing their children
    time.sleep(0.3)

    # 2. Kill tunnel clients
    for tc in tree.tunnel_clients:
        terminate_pid(tc.pid, force=True)

    # 3. Kill MCP servers and daemons
    for proc in tree.mcp_servers + tree.job_daemons:
        terminate_pid(proc.pid, force=True)

    # Wait and verify
    deadline = time.time() + timeout_seconds
    remaining: list[int] = []
    while time.time() < deadline:
        all_pids = tree.all_pids
        remaining = [pid for pid in all_pids if is_pid_alive(pid)]
        if not remaining:
            return True, []
        time.sleep(0.5)

    return len(remaining) == 0, remaining
