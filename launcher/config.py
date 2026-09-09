"""Configuration and constants for Harness Harbor Launcher.

Public constants in this module are the single import surface used by the
rest of the launcher (UI, process manager, health checker, lifecycle,
diagnostics, autostart, etc.). They are **derived at import time** from
:mod:`launcher.settings`, which means every value here can be overridden
without editing source via the environment variables documented in
:data:`launcher.settings.ENV_KEYS`.

Override examples
-----------------

Redirect the launcher at a different production install::

    $env:HARBOR_HOME = "C:\\Users\\Example\\HarnessHarbor"
    python run_launcher.py

Move the tunnel-client to a custom path::

    $env:HARBOR_TUNNEL_EXE = "E:\\bin\\tunnel-client\\tunnel-client.exe"

See ``docs/CONFIGURATION.md`` for the full list of supported overrides and
the resolution precedence.

Stability contract
------------------

The set of names exported from this module is part of the launcher's
**public import surface**. New names may be added; existing names must not
be removed without a deprecation cycle. Downstream code is expected to
keep using ``from launcher.config import X`` rather than reaching into
:mod:`launcher.settings` directly.
"""

from __future__ import annotations

from pathlib import Path

from launcher import settings


def _s(key: str):
    """Resolve a settings key with the legacy default behavior.

    Thin wrapper over :func:`launcher.settings.get` that returns ``None``
    for unknown keys rather than the user-supplied default; the launcher
    only ever asks for keys it knows exist.
    """
    return settings.get(key)


# ---------------------------------------------------------------------------
# Production Paths
# ---------------------------------------------------------------------------

#: Root of the production Harbor runtime install (contains ``server_legacy.py``,
#: ``codex_job_daemon.py``, the PowerShell supervisors, and the legacy venv).
PRODUCTION_PATH: Path = _s("harbor_home")

#: Legacy junction some supervisors historically wrote into.
JUNCTION_PATH: Path = _s("junction_path")

# ---------------------------------------------------------------------------
# Tunnel configuration
# ---------------------------------------------------------------------------

#: Tunnel client executable. Overridable via ``HARBOR_TUNNEL_EXE``.
TUNNEL_EXE: Path = _s("tunnel_exe")
#: Per-profile state directory for tunnel-client.
TUNNEL_PROFILE_DIR: Path = _s("tunnel_profile_dir")
#: Active tunnel-client profile name.
TUNNEL_PROFILE_NAME: str = _s("tunnel_profile_name")
#: Path to the dynamic health endpoint URL file written by tunnel-client.
TUNNEL_HEALTH_URL_FILE: Path = _s("tunnel_health_url_file")

# ---------------------------------------------------------------------------
# Scripts and log files
# ---------------------------------------------------------------------------

#: Supervisor log for the tunnel-client restart loop.
TUNNEL_LOG: Path = PRODUCTION_PATH / "tunnel-supervisor.log"
#: Supervisor log for the Codex Job Daemon.
DAEMON_LOG: Path = PRODUCTION_PATH / "codex-job-daemon.log"
#: PowerShell supervisor script for the tunnel-client restart loop.
START_TUNNEL_SCRIPT: Path = PRODUCTION_PATH / "start-tunnel.ps1"
#: PowerShell supervisor script for the Codex Job Daemon.
START_DAEMON_SCRIPT: Path = PRODUCTION_PATH / "start-codex-job-daemon.ps1"

# ---------------------------------------------------------------------------
# Python runtime and scripts
# ---------------------------------------------------------------------------

#: Legacy venv interpreter that drives ``server_legacy.py`` and the daemon.
VENV_PYTHON: Path = _s("venv_python")
#: Windowless variant used by some launch flows.
VENV_PYTHONW: Path = _s("venv_pythonw")
#: Real Harbor MCP server script. The launcher must never confuse this with
#: the forbidden ``server.py`` placeholder.
MCP_SCRIPT_NAME: str = _s("mcp_script_name")
#: Name of the simplified placeholder that the launcher explicitly refuses
#: to match when scanning process command lines.
FORBIDDEN_MCP_SCRIPT: str = _s("forbidden_mcp_script")
#: Codex Job Daemon entry-point script.
DAEMON_SCRIPT_NAME: str = _s("daemon_script_name")

# ---------------------------------------------------------------------------
# Windows Scheduled Task names
# ---------------------------------------------------------------------------

SCHEDULED_TASK_TUNNEL: str = _s("scheduled_task_tunnel")
SCHEDULED_TASK_DAEMON: str = _s("scheduled_task_daemon")

# ---------------------------------------------------------------------------
# Brand assets
# ---------------------------------------------------------------------------
#
# These are launch-local (relative to this package) and intentionally not
# routed through the settings resolver; they describe the launcher itself
# rather than the deployment it manages.

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
BRAND_DIR = ASSETS_DIR / "brand"
BRAND_PNG_DIR = BRAND_DIR / "generated" / "png"
BRAND_ICO_DIR = BRAND_DIR / "generated" / "ico"
BRAND_SYSTEM_ICO_PATH = BRAND_ICO_DIR / "harbor-system.ico"
BRAND_ICO_PATH = BRAND_ICO_DIR / "harbor.ico"
BRAND_HEADER_ICON = BRAND_PNG_DIR / "harbor-header-rounded.png"
BRAND_TRAY_ICON = BRAND_PNG_DIR / "harbor-system-24.png"

# ---------------------------------------------------------------------------
# Timeouts & intervals (seconds)
# ---------------------------------------------------------------------------

HTTP_PROBE_TIMEOUT: float = _s("http_probe_timeout")
STOP_TIMEOUT: float = _s("stop_timeout")
START_HEALTH_TIMEOUT: float = _s("start_health_timeout")
POLL_INTERVAL_SECONDS: float = _s("poll_interval_seconds")

# ---------------------------------------------------------------------------
# Apple-inspired Colors (UI palette; launcher-local, not deployment)
# ---------------------------------------------------------------------------

COLOR_STATUS_HEALTHY = "#34C759"     # Soft Green
COLOR_STATUS_RUNNING = "#34C759"     # Soft Green
COLOR_STATUS_STARTING = "#007AFF"    # System Blue
COLOR_STATUS_WARNING = "#FF9500"     # Warm Amber
COLOR_STATUS_FAILED = "#FF3B30"      # Coral Red
COLOR_STATUS_STOPPED = "#8E8E93"     # Slate Gray
COLOR_STATUS_RESTARTING = "#5856D6"  # Soft Indigo

COLOR_BG_LIGHT = "#F2F2F7"
COLOR_BG_DARK = "#1C1C1E"
COLOR_CARD_LIGHT = "#FFFFFF"
COLOR_CARD_DARK = "#2C2C2E"
COLOR_TEXT_PRIMARY_LIGHT = "#1D1D1F"
COLOR_TEXT_PRIMARY_DARK = "#F5F5F7"
COLOR_TEXT_MUTED_LIGHT = "#86868B"
COLOR_TEXT_MUTED_DARK = "#98989D"

COLOR_BTN_PRIMARY = "#0071E3"
COLOR_BTN_PRIMARY_HOVER = "#0077ED"
COLOR_BTN_DANGER = "#FF3B30"
COLOR_BTN_DANGER_HOVER = "#D70015"
COLOR_BTN_SECONDARY_LIGHT = "#E5E5EA"
COLOR_BTN_SECONDARY_DARK = "#3A3A3C"
COLOR_BTN_SECONDARY_HOVER_LIGHT = "#D1D1D6"
COLOR_BTN_SECONDARY_HOVER_DARK = "#48484A"

# ---------------------------------------------------------------------------
# Autostart Registry
# ---------------------------------------------------------------------------

AUTOSTART_REG_KEY: str = _s("autostart_reg_key")
AUTOSTART_APP_NAME: str = _s("autostart_app_name")
