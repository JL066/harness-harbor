"""Health check and probe services for Harness Harbor runtime."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Literal

from launcher.config import (
    COLOR_STATUS_FAILED,
    COLOR_STATUS_HEALTHY,
    COLOR_STATUS_RESTARTING,
    COLOR_STATUS_RUNNING,
    COLOR_STATUS_STARTING,
    COLOR_STATUS_STOPPED,
    COLOR_STATUS_WARNING,
    HTTP_PROBE_TIMEOUT,
    TUNNEL_HEALTH_URL_FILE,
)
from launcher.process_manager import (
    HarborProcessTree,
    ProcessInfo,
    get_harbor_process_tree,
    query_windows_processes,
)

StatusType = Literal["Healthy", "Running", "Starting", "Restarting", "Warning", "Unhealthy", "Failed", "Stopped"]


@dataclass
class ComponentHealth:
    """Status snapshot of an individual Harbor component."""
    name: str
    status: StatusType
    detail: str
    pids: list[int]
    color: str


@dataclass
class HarborHealthSnapshot:
    """Consolidated health report for all Harbor components."""
    tunnel: ComponentHealth
    mcp: ComponentHealth
    daemon: ComponentHealth
    overall_status: str
    overall_color: str
    timestamp: float
    health_url: str | None
    tree: HarborProcessTree


def read_tunnel_health_url() -> str | None:
    """Read dynamic health endpoint URL from state file."""
    if not TUNNEL_HEALTH_URL_FILE.exists():
        return None
    try:
        content = TUNNEL_HEALTH_URL_FILE.read_text(encoding="utf-8").strip()
        if content.startswith("http://") or content.startswith("https://"):
            return content
    except Exception:
        pass
    return None


def probe_http_health(url: str, timeout: float = HTTP_PROBE_TIMEOUT) -> tuple[bool, int, float]:
    """Perform minimal HTTP probe on localhost health URL.

    Returns (is_ok, status_code, latency_ms).
    """
    start_time = time.time()
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Harness-Harbor-Launcher/1.0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            latency = (time.time() - start_time) * 1000
            return resp.status == 200, resp.status, latency
    except urllib.error.HTTPError as e:
        latency = (time.time() - start_time) * 1000
        return False, e.code, latency
    except Exception:
        latency = (time.time() - start_time) * 1000
        return False, 0, latency


def check_tunnel_health(tree: HarborProcessTree, health_url: str | None = None) -> ComponentHealth:
    """Check status of tunnel-client and its health probe."""
    pids = [p.pid for p in tree.tunnel_clients]
    has_supervisor = len(tree.tunnel_supervisors) > 0
    has_client = len(tree.tunnel_clients) > 0

    if not has_client and not has_supervisor:
        return ComponentHealth(
            name="Tunnel",
            status="Stopped",
            detail="Process not running",
            pids=[],
            color=COLOR_STATUS_STOPPED,
        )

    url = health_url or read_tunnel_health_url()
    if url:
        is_ok, code, latency = probe_http_health(url)
        if is_ok:
            return ComponentHealth(
                name="Tunnel",
                status="Healthy",
                detail=f"{url} ({latency:.0f}ms)",
                pids=pids,
                color=COLOR_STATUS_HEALTHY,
            )
        elif has_client:
            return ComponentHealth(
                name="Tunnel",
                status="Unhealthy",
                detail=f"Probe failed (HTTP {code})",
                pids=pids,
                color=COLOR_STATUS_WARNING,
            )

    if has_supervisor and not has_client:
        return ComponentHealth(
            name="Tunnel",
            status="Starting",
            detail="Supervisor waiting for tunnel-client",
            pids=[p.pid for p in tree.tunnel_supervisors],
            color=COLOR_STATUS_STARTING,
        )

    return ComponentHealth(
        name="Tunnel",
        status="Starting",
        detail="Awaiting health endpoint",
        pids=pids,
        color=COLOR_STATUS_STARTING,
    )


def check_mcp_health(tree: HarborProcessTree, tunnel_health: ComponentHealth) -> ComponentHealth:
    """Check status of Harbor MCP server (server_legacy.py)."""
    pids = [p.pid for p in tree.mcp_servers]

    if not pids:
        if tunnel_health.status in ("Stopped",):
            return ComponentHealth(
                name="Harbor MCP",
                status="Stopped",
                detail="Managed by tunnel-client",
                pids=[],
                color=COLOR_STATUS_STOPPED,
            )
        elif tunnel_health.status in ("Starting",):
            return ComponentHealth(
                name="Harbor MCP",
                status="Starting",
                detail="Starting with tunnel",
                pids=[],
                color=COLOR_STATUS_STARTING,
            )
        else:
            return ComponentHealth(
                name="Harbor MCP",
                status="Unhealthy",
                detail="server_legacy.py child process missing",
                pids=[],
                color=COLOR_STATUS_FAILED,
            )

    # MCP process exists
    if tunnel_health.status == "Healthy":
        return ComponentHealth(
            name="Harbor MCP",
            status="Healthy",
            detail=f"PID: {pids[0]} (server_legacy.py)",
            pids=pids,
            color=COLOR_STATUS_HEALTHY,
        )
    elif tunnel_health.status == "Starting":
        return ComponentHealth(
            name="Harbor MCP",
            status="Starting",
            detail=f"PID: {pids[0]}",
            pids=pids,
            color=COLOR_STATUS_STARTING,
        )
    else:
        return ComponentHealth(
            name="Harbor MCP",
            status="Warning",
            detail=f"Tunnel {tunnel_health.status}",
            pids=pids,
            color=COLOR_STATUS_WARNING,
        )


def check_daemon_health(tree: HarborProcessTree) -> ComponentHealth:
    """Check status of Codex Job Daemon (codex_job_daemon.py)."""
    pids = [p.pid for p in tree.job_daemons]
    has_supervisor = len(tree.daemon_supervisors) > 0

    if pids:
        return ComponentHealth(
            name="Job Daemon",
            status="Running",
            detail=f"PID: {pids[0]} (scheduler active)",
            pids=pids,
            color=COLOR_STATUS_RUNNING,
        )

    if has_supervisor:
        return ComponentHealth(
            name="Job Daemon",
            status="Starting",
            detail="Supervisor waiting for daemon",
            pids=[p.pid for p in tree.daemon_supervisors],
            color=COLOR_STATUS_STARTING,
        )

    return ComponentHealth(
        name="Job Daemon",
        status="Stopped",
        detail="Process not running",
        pids=[],
        color=COLOR_STATUS_STOPPED,
    )


def get_harbor_health(processes: list[ProcessInfo] | None = None) -> HarborHealthSnapshot:
    """Evaluate full health status of the Harbor production environment."""
    tree = get_harbor_process_tree(processes)
    health_url = read_tunnel_health_url()

    tunnel_h = check_tunnel_health(tree, health_url)
    mcp_h = check_mcp_health(tree, tunnel_h)
    daemon_h = check_daemon_health(tree)

    # Derive overall status
    if tunnel_h.status == "Healthy" and mcp_h.status == "Healthy" and daemon_h.status == "Running":
        overall = "All systems healthy"
        overall_color = COLOR_STATUS_HEALTHY
    elif tunnel_h.status == "Stopped" and mcp_h.status == "Stopped" and daemon_h.status == "Stopped":
        overall = "Harbor is stopped"
        overall_color = COLOR_STATUS_STOPPED
    elif any(s in ("Starting", "Restarting") for s in (tunnel_h.status, mcp_h.status, daemon_h.status)):
        overall = "Harbor is starting..."
        overall_color = COLOR_STATUS_STARTING
    elif any(s in ("Failed", "Unhealthy") for s in (tunnel_h.status, mcp_h.status, daemon_h.status)):
        overall = "Issues detected"
        overall_color = COLOR_STATUS_FAILED
    else:
        overall = "Harbor running with warnings"
        overall_color = COLOR_STATUS_WARNING

    return HarborHealthSnapshot(
        tunnel=tunnel_h,
        mcp=mcp_h,
        daemon=daemon_h,
        overall_status=overall,
        overall_color=overall_color,
        timestamp=time.time(),
        health_url=health_url,
        tree=tree,
    )
