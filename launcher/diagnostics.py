"""System diagnostics collection with strict secret redaction."""

from __future__ import annotations

import datetime
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from launcher.config import (
    DAEMON_SCRIPT_NAME,
    MCP_SCRIPT_NAME,
    PRODUCTION_PATH,
    TUNNEL_EXE,
    TUNNEL_PROFILE_DIR,
    TUNNEL_PROFILE_NAME,
    VENV_PYTHON,
)
from launcher.health_checker import get_harbor_health, probe_http_health, read_tunnel_health_url
from runtime_queue import resolve_queue_root


def redact_secrets(text: str | None) -> str:
    """Redacts API keys, bearer tokens, passwords, and sensitive env variables."""
    if not text:
        return ""

    # Flag-based tokens: --api-key <val>, --token <val>, etc.
    flag_pat = re.compile(
        r"(--(?:api[-_]?key|apikey|token|access[-_]?token|password|passwd|secret))"
        r"(\s*[:=]\s*|\s+)"
        r'([^\s"\'\\]+|"[^"]*"|\'[^\']*\')',
        re.IGNORECASE,
    )
    redacted = flag_pat.sub(r"\1\2<redacted>", text)

    # Environment-style keys: OPENAI_API_KEY=..., TUNNEL_RUNTIME_KEY=...
    env_pat = re.compile(
        r"\b([A-Za-z0-9_]*(?:KEY|TOKEN|PASSWORD|PASSWD|SECRET|AUTH)[A-Za-z0-9_]*)"
        r"(\s*=\s*)"
        r'([^\s"\'\\]+|"[^"]*"|\'[^\']*\')',
        re.IGNORECASE,
    )
    redacted = env_pat.sub(r"\1\2<redacted>", redacted)

    # Authorization Bearer headers
    auth_pat = re.compile(r"((?:Authorization\s*:\s*)?Bearer\s+)([^\s\"\'\r\n;]+)", re.IGNORECASE)
    redacted = auth_pat.sub(r"\1<redacted>", redacted)

    # JSON-style keys: "api_key": "...", "token": "...", etc.
    json_pat = re.compile(
        r'("(?:api[-_]?key|apikey|token|access[-_]?token|password|passwd|secret)"\s*:\s*)'
        r'("(?:[^"\\]|\\.)*"|\'[^\']*\'|[^\s,}\]]+)',
        re.IGNORECASE,
    )
    redacted = json_pat.sub(r'\1"<redacted>"', redacted)

    # Literal env:TUNNEL_RUNTIME_KEY placeholder
    redacted = re.sub(r"env:TUNNEL_RUNTIME_KEY", "env:TUNNEL_RUNTIME_KEY (protected)", redacted)

    return redacted


def get_git_head_info(repo_path: Path) -> dict[str, str]:
    """Retrieve git HEAD commit and branch for diagnostics."""
    info = {"commit": "unknown", "branch": "unknown"}
    if not (repo_path / ".git").exists() and not (repo_path / ".git").is_file():
        return info

    try:
        c_res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=2.0,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if c_res.returncode == 0:
            info["commit"] = c_res.stdout.strip()

        b_res = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=2.0,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if b_res.returncode == 0:
            info["branch"] = b_res.stdout.strip() or "detached"
    except Exception:
        pass
    return info


def collect_diagnostics() -> dict[str, Any]:
    """Gather complete, secret-safe diagnostic status of the Harbor host environment."""
    health = get_harbor_health()
    git_info = get_git_head_info(PRODUCTION_PATH)
    health_url = read_tunnel_health_url()

    probe_status = "N/A"
    probe_latency = None
    if health_url:
        ok, code, lat = probe_http_health(health_url)
        probe_status = f"HTTP {code} ({'OK' if ok else 'FAIL'})"
        probe_latency = f"{lat:.1f}ms"

    # Profile path
    profile_path = TUNNEL_PROFILE_DIR / f"{TUNNEL_PROFILE_NAME}.yaml"
    try:
        queue_root = resolve_queue_root(PRODUCTION_PATH)
        queue_diagnostics = queue_root.as_dict()
    except ValueError as exc:
        queue_diagnostics = {
            "jobs_dir": "invalid",
            "source": "invalid_environment",
            "canonical_path": "invalid",
            "fingerprint": "unavailable",
            "queue_config_error": str(exc),
        }

    diag = {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "production_path": str(PRODUCTION_PATH),
        "git_commit": git_info["commit"],
        "git_branch": git_info["branch"],
        "tunnel_executable": str(TUNNEL_EXE),
        "tunnel_executable_exists": TUNNEL_EXE.exists(),
        "tunnel_profile_path": str(profile_path),
        "tunnel_profile_exists": profile_path.exists(),
        "tunnel_health_url": health_url or "None",
        "tunnel_probe_status": probe_status,
        "tunnel_probe_latency": probe_latency or "N/A",
        "tunnel_pids": health.tunnel.pids,
        "tunnel_status": health.tunnel.status,
        "mcp_script": MCP_SCRIPT_NAME,
        "mcp_pids": health.mcp.pids,
        "mcp_status": health.mcp.status,
        "daemon_script": DAEMON_SCRIPT_NAME,
        "daemon_pids": health.daemon.pids,
        "daemon_status": health.daemon.status,
        "python_executable": str(VENV_PYTHON),
        "python_executable_exists": VENV_PYTHON.exists(),
        "overall_status": health.overall_status,
        "os_platform": sys.platform,
        "python_version": sys.version.split()[0],
        **queue_diagnostics,
    }
    return diag


def format_diagnostics_markdown(diag: dict[str, Any]) -> str:
    """Format diagnostics dictionary into clean, shareable Markdown."""
    lines = [
        "# Harness Harbor Diagnostics",
        f"**Generated**: {diag['timestamp']}",
        f"**Overall Status**: {diag['overall_status']}",
        "",
        "## Environment",
        f"- **Production Path**: `{diag['production_path']}`",
        f"- **Git HEAD**: `{diag['git_commit']}` (branch: `{diag['git_branch']}`)",
        f"- **Python Runtime**: `{diag['python_executable']}` (exists: {diag['python_executable_exists']})",
        f"- **OS / Python**: {diag['os_platform']} / Python {diag['python_version']}",
        f"- **Jobs Directory**: `{diag['jobs_dir']}`",
        f"- **Queue Root Source**: `{diag['source']}`",
        f"- **Queue Canonical Path**: `{diag['canonical_path']}`",
        f"- **Queue Fingerprint**: `{diag['fingerprint']}`",
        f"- **Queue Config Error**: `{diag.get('queue_config_error', 'None')}`",
        "",
        "## Tunnel Component",
        f"- **Status**: {diag['tunnel_status']}",
        f"- **Executable**: `{diag['tunnel_executable']}` (exists: {diag['tunnel_executable_exists']})",
        f"- **Profile**: `{diag['tunnel_profile_path']}` (exists: {diag['tunnel_profile_exists']})",
        f"- **PIDs**: `{diag['tunnel_pids']}`",
        f"- **Health URL**: `{diag['tunnel_health_url']}`",
        f"- **Health Probe**: {diag['tunnel_probe_status']} (latency: {diag['tunnel_probe_latency']})",
        "",
        "## Harbor MCP Component",
        f"- **Status**: {diag['mcp_status']}",
        f"- **Script**: `{diag['mcp_script']}` (never server.py)",
        f"- **PIDs**: `{diag['mcp_pids']}`",
        "",
        "## Codex Job Daemon Component",
        f"- **Status**: {diag['daemon_status']}",
        f"- **Script**: `{diag['daemon_script']}`",
        f"- **PIDs**: `{diag['daemon_pids']}`",
    ]
    raw_md = "\n".join(lines)
    return redact_secrets(raw_md)
