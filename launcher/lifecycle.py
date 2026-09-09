"""Harbor runtime lifecycle management (Start / Stop / Restart).

Guarantees:
- Supervisors are stopped first to prevent while ($true) auto-respawn loops.
- Idempotent start prevents duplicate instances of Tunnel, MCP, or Job Daemon.
- Accurate process tree targeting without killing unrelated Python or PowerShell processes.
"""

from __future__ import annotations

import subprocess
import sys
import time
from typing import Callable

from launcher.config import (
    POLL_INTERVAL_SECONDS,
    SCHEDULED_TASK_DAEMON,
    SCHEDULED_TASK_TUNNEL,
    START_DAEMON_SCRIPT,
    START_HEALTH_TIMEOUT,
    START_TUNNEL_SCRIPT,
    STOP_TIMEOUT,
)
from launcher.health_checker import get_harbor_health, probe_http_health, read_tunnel_health_url
from launcher.process_manager import (
    HarborProcessTree,
    get_harbor_process_tree,
    is_pid_alive,
    query_windows_processes,
    terminate_harbor_processes,
)
from launcher.tunnel import ManagedTunnelSupervisor, TunnelCredentialError


def start_managed_tunnel(supervisor: ManagedTunnelSupervisor):
    """Start a caller-owned managed supervisor (idempotent)."""
    return supervisor.start()


def stop_managed_tunnel(supervisor: ManagedTunnelSupervisor, timeout_seconds: float = STOP_TIMEOUT) -> bool:
    """Stop a caller-owned managed supervisor without touching legacy tasks."""
    return supervisor.stop(timeout=timeout_seconds)


def stop_scheduled_tasks() -> None:
    """Stop scheduled tasks if registered in Windows Task Scheduler."""
    if sys.platform != "win32":
        return
    for task_name in (SCHEDULED_TASK_TUNNEL, SCHEDULED_TASK_DAEMON):
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", f"Stop-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue"],
                capture_output=True,
                timeout=5.0,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass


def start_scheduled_task(task_name: str) -> bool:
    """Attempt to start a registered scheduled task."""
    if sys.platform != "win32":
        return False
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", f"Start-ScheduledTask -TaskName '{task_name}' -ErrorAction Stop"],
            capture_output=True,
            text=True,
            timeout=5.0,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return res.returncode == 0
    except Exception:
        return False


def spawn_detached_supervisor(script_path: str) -> bool:
    """Launch supervisor PowerShell script in a detached background process without console window."""
    if sys.platform != "win32":
        return False
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
    ]
    # DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    flags = 0x00000008 | 0x08000000 | 0x00000200
    try:
        subprocess.Popen(
            cmd,
            creationflags=flags,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def stop_harbor(
    progress_cb: Callable[[str], None] | None = None,
    timeout_seconds: float = STOP_TIMEOUT,
    *,
    managed_tunnel: ManagedTunnelSupervisor | None = None,
) -> tuple[bool, str]:
    """Gracefully and deterministically stop the Harbor runtime.

    Returns (success, message).
    """
    if progress_cb:
        progress_cb("Stopping Task Scheduler jobs..." if managed_tunnel is None else "Stopping managed tunnel...")
    # Managed mode owns its child process and must not disable an unrelated
    # legacy Scheduled Task installation on the same machine.
    if managed_tunnel is None:
        stop_scheduled_tasks()

    if managed_tunnel is not None:
        managed_tunnel.stop(timeout=timeout_seconds)
        # The process scanner cannot distinguish a caller-owned managed
        # tunnel-client from the legacy Scheduled Task instance (both use the
        # same profile name).  Never run the broad Harbor termination sweep in
        # managed mode: it could kill the unrelated legacy tunnel/MCP tree.
        if progress_cb:
            progress_cb("Managed tunnel stopped; legacy Harbor processes left untouched.")
        return True, "Managed tunnel stopped successfully."

    if progress_cb:
        progress_cb("Locating Harbor process tree...")
    tree = get_harbor_process_tree()

    if not tree.all_pids:
        if progress_cb:
            progress_cb("No running Harbor processes found.")
        return True, "Harbor is already stopped."

    if progress_cb:
        progress_cb(f"Stopping supervisors and child processes (PIDs: {list(tree.all_pids)})...")

    ok, remaining = terminate_harbor_processes(tree, timeout_seconds=timeout_seconds)
    if not ok:
        # Forceful secondary sweep if any survived
        for pid in remaining:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, timeout=3.0)
            except Exception:
                pass
        time.sleep(1.0)
        final_tree = get_harbor_process_tree()
        if final_tree.all_pids:
            return False, f"Failed to stop processes: {list(final_tree.all_pids)}"

    if progress_cb:
        progress_cb("Harbor successfully stopped.")
    return True, "Harbor stopped successfully."


def start_harbor(
    progress_cb: Callable[[str], None] | None = None,
    timeout_seconds: float = START_HEALTH_TIMEOUT,
    *,
    managed_tunnel: ManagedTunnelSupervisor | None = None,
) -> tuple[bool, str]:
    """Start Harbor runtime idempotently without spawning duplicate instances.

    Returns (success, message).
    """
    if progress_cb:
        progress_cb("Checking current status...")

    current_health = get_harbor_health()
    if current_health.tunnel.status == "Healthy" and current_health.daemon.status == "Running":
        return True, "Harbor is already running and healthy."

    # Start Tunnel if not healthy or running
    if current_health.tunnel.status in ("Stopped", "Failed"):
        if progress_cb:
            progress_cb("Starting Tunnel...")
        if managed_tunnel is not None:
            try:
                managed_tunnel.start()
            except TunnelCredentialError:
                return False, "Tunnel runtime credential is unavailable."
            except Exception:
                return False, "Failed to start managed tunnel."
        else:
            # Legacy mode remains unchanged: Scheduled Task first, then the
            # detached supervisor fallback. It does not inherit managed keys.
            started_by_task = start_scheduled_task(SCHEDULED_TASK_TUNNEL)
            if not started_by_task:
                spawn_detached_supervisor(str(START_TUNNEL_SCRIPT))

    # Start Job Daemon if not running
    if current_health.daemon.status in ("Stopped", "Failed"):
        if progress_cb:
            progress_cb("Starting Job Daemon...")
        started_by_task = start_scheduled_task(SCHEDULED_TASK_DAEMON)
        if not started_by_task:
            spawn_detached_supervisor(str(START_DAEMON_SCRIPT))

    # Wait for Healthy status
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if progress_cb:
            progress_cb("Probing health endpoint...")
        time.sleep(1.5)
        h = get_harbor_health()
        if h.tunnel.status == "Healthy" and h.daemon.status == "Running":
            if progress_cb:
                progress_cb("All systems healthy.")
            return True, "Harbor started successfully."

    final_h = get_harbor_health()
    if final_h.tunnel.status == "Healthy" or final_h.daemon.status == "Running":
        return True, f"Harbor partially started (Tunnel: {final_h.tunnel.status}, Daemon: {final_h.daemon.status})"

    return False, "Timed out waiting for Harbor components to become healthy."


def restart_harbor(
    progress_cb: Callable[[str], None] | None = None,
    *,
    managed_tunnel: ManagedTunnelSupervisor | None = None,
) -> tuple[bool, str]:
    """Perform clean, graceful restart sequence."""
    if progress_cb:
        progress_cb("Initiating graceful stop...")

    if managed_tunnel is None:
        stop_ok, stop_msg = stop_harbor(progress_cb=progress_cb)
    else:
        stop_ok, stop_msg = stop_harbor(progress_cb=progress_cb, managed_tunnel=managed_tunnel)
    if not stop_ok:
        return False, f"Restart failed during stop phase: {stop_msg}"

    if progress_cb:
        progress_cb("Waiting 2 seconds for ports and locks to release...")
    time.sleep(2.0)

    if progress_cb:
        progress_cb("Starting Harbor components...")
    if managed_tunnel is None:
        start_ok, start_msg = start_harbor(progress_cb=progress_cb)
    else:
        start_ok, start_msg = start_harbor(progress_cb=progress_cb, managed_tunnel=managed_tunnel)
    return start_ok, start_msg
