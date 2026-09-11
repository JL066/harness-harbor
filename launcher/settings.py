"""Configuration resolution foundation for Harness Harbor Launcher.

This module is the single source of truth for resolving every deployment
specific path, name, and timing constant the launcher needs at runtime.
Downstream modules (``launcher.config`` and consumers) read resolved values
through this layer instead of importing hard-coded literals.

Resolution precedence (highest to lowest)
-----------------------------------------

1. **Process environment variables** — operators can override individual
   fields without editing source. See :data:`ENV_KEYS` for the full list of
   accepted names. The ``HARBOR_`` prefix is reserved for the launcher.
2. **Built-in defaults** — a checkout-relative public layout
   layout used by the public checkout. Deployment-specific values can be
   supplied through environment variables or the setup wizard, while
   every consumer of this module is free to override.

A future batch may add a third tier (e.g. ``~/.harbor-launcher/config.yaml``);
the public API here is shaped to accept that without breaking changes.

Public API
----------

- :func:`get`              — resolve a single key.
- :func:`all_settings`     — snapshot of every resolved value.
- :func:`config_path`      — the optional YAML/JSON config file path
                             (always returns the candidate path; reads do
                             not depend on the file existing).
- :func:`reload`           — discard the in-process cache and re-read.
- :func:`describe`         — human-readable summary used by the Diagnostics
                             dialog so operators can see which tier won.

Stability
---------

The set of accepted environment variable names and the set of public keys
in :data:`DEFAULTS` are part of the public contract. New keys may be added;
existing keys must not be removed without a deprecation cycle.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# Environment variable contract
# ---------------------------------------------------------------------------

#: Mapping from environment variable name to a :data:`DEFAULTS` key.
#: Operators set the env var; ``settings.get`` resolves it through here.
ENV_KEYS: dict[str, str] = {
    "HARBOR_HOME": "harbor_home",
    "HARBOR_PRODUCTION_PATH": "harbor_home",  # legacy alias
    "HARBOR_JUNCTION_PATH": "junction_path",
    "HARBOR_TUNNEL_EXE": "tunnel_exe",
    "HARBOR_TUNNEL_PROFILE_DIR": "tunnel_profile_dir",
    "HARBOR_TUNNEL_PROFILE_NAME": "tunnel_profile_name",
    "HARBOR_TUNNEL_HEALTH_URL_FILE": "tunnel_health_url_file",
    "HARBOR_VENV_PYTHON": "venv_python",
    "HARBOR_VENV_PYTHONW": "venv_pythonw",
    "HARBOR_MCP_SCRIPT": "mcp_script_name",
    "HARBOR_DAEMON_SCRIPT": "daemon_script_name",
    "HARBOR_SCHEDULED_TASK_TUNNEL": "scheduled_task_tunnel",
    "HARBOR_SCHEDULED_TASK_DAEMON": "scheduled_task_daemon",
    "HARBOR_AUTOSTART_APP_NAME": "autostart_app_name",
    "HARBOR_POLL_INTERVAL_SECONDS": "poll_interval_seconds",
    "HARBOR_STOP_TIMEOUT": "stop_timeout",
    "HARBOR_START_HEALTH_TIMEOUT": "start_health_timeout",
    "HARBOR_HTTP_PROBE_TIMEOUT": "http_probe_timeout",
    # The literal env var that points at an optional YAML/JSON file is
    # handled separately (see ``_read_config_file``) and is therefore not
    # listed here. It is recognized under the name ``HARBOR_LAUNCHER_CONFIG``.
}


# ---------------------------------------------------------------------------
# Built-in defaults
# ---------------------------------------------------------------------------

#: Built-in defaults for a public source distribution. Deployment-specific
#: values remain configurable through the HARBOR_* environment contract.
DEFAULTS: dict[str, Any] = {
    # Production runtime install root (the directory that contains
    # server_legacy.py, codex_job_daemon.py, start-*.ps1, etc.).
    "harbor_home": Path(__file__).resolve().parent.parent,
    # Legacy junction that some supervisors historically wrote into.
    "junction_path": Path(__file__).resolve().parent.parent / ".harbor-junction",
    # Tunnel client executable (download location for the upstream binary).
    "tunnel_exe": Path("tunnel-client.exe"),
    # Where tunnel-client stores its per-profile state.
    "tunnel_profile_dir": (
        Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "tunnel-client"
    ),
    "tunnel_profile_name": "harness-harbor",
    # Per-profile dynamic health endpoint URL file written by tunnel-client.
    "tunnel_health_url_file": (
        Path(os.environ.get("USERPROFILE", Path.home()))
        / ".local"
        / "state"
        / "tunnel-client"
        / "health"
        / "harness-harbor.url"
    ),
    # Legacy venv interpreter that drives server_legacy.py and the daemon.
    "venv_python": Path(
        Path(__file__).resolve().parent.parent / ".venv" / "Scripts" / "python.exe"
    ),
    "venv_pythonw": Path(
        Path(__file__).resolve().parent.parent / ".venv" / "Scripts" / "pythonw.exe"
    ),
    # Script file names within ``harbor_home``.
    "mcp_script_name": "server_legacy.py",
    # The forbidden legacy simplified server name; the launcher must never
    # confuse it with the real MCP server.
    "forbidden_mcp_script": "server.py",
    "daemon_script_name": "codex_job_daemon.py",
    # Windows Scheduled Task names (used to start/stop the supervisors).
    "scheduled_task_tunnel": "Harness Harbor Tunnel",
    "scheduled_task_daemon": "Harness Harbor Job Daemon",
    # HKCU autostart registration.
    "autostart_app_name": "HarnessHarborLauncher",
    "autostart_reg_key": r"Software\Microsoft\Windows\CurrentVersion\Run",
    # Health polling and lifecycle timing (seconds).
    "poll_interval_seconds": 3.0,
    "stop_timeout": 8.0,
    "start_health_timeout": 15.0,
    "http_probe_timeout": 2.0,
}

#: Keys whose value must be a :class:`pathlib.Path`. Used by :func:`get` to
#: automatically coerce env-var strings into Path objects.
_PATH_KEYS: frozenset[str] = frozenset(
    {
        "harbor_home",
        "junction_path",
        "tunnel_exe",
        "tunnel_profile_dir",
        "tunnel_health_url_file",
        "venv_python",
        "venv_pythonw",
    }
)

#: Keys whose value must be a float.
_FLOAT_KEYS: frozenset[str] = frozenset(
    {
        "poll_interval_seconds",
        "stop_timeout",
        "start_health_timeout",
        "http_probe_timeout",
    }
)

#: All known configuration keys, computed once for membership tests.
_ALL_KEYS: frozenset[str] = frozenset(DEFAULTS.keys())


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

#: In-process resolved snapshot. Populated lazily by :func:`_resolve_all`.
_RESOLVED: dict[str, Any] | None = None

#: Source tier per key, populated alongside :data:`_RESOLVED`. Useful for the
#: Diagnostics dialog so operators can see which tier won each value.
_SOURCES: dict[str, str] = {}


def _coerce(key: str, raw: str) -> Any:
    """Coerce a raw env-var string into the canonical type for ``key``."""
    if key in _PATH_KEYS:
        return Path(raw).expanduser()
    if key in _FLOAT_KEYS:
        try:
            return float(raw)
        except ValueError as exc:
            raise ValueError(
                f"Environment override for {key!r} must be a float, got {raw!r}"
            ) from exc
    return raw


def _resolve_all() -> tuple[dict[str, Any], dict[str, str]]:
    """Resolve the full settings snapshot, with per-key source tracking.

    Returns a ``(resolved, sources)`` tuple. ``sources[key]`` is the tier
    that actually produced the value; when an env var is set but malformed
    (e.g. a non-numeric string for a float key) the source is reported as
    ``"default"`` because the default value was used. This keeps
    :func:`source_for` honest for the Diagnostics dialog.
    """
    resolved: dict[str, Any] = {}
    sources: dict[str, str] = {}

    for env_name, key in ENV_KEYS.items():
        env_val = os.environ.get(env_name)
        if env_val is None or env_val == "":
            continue
        if key not in _ALL_KEYS:
            # Defensive: forward-compat for an env var that points to a key
            # removed in a later release. Skip silently rather than crash.
            continue
        try:
            resolved[key] = _coerce(key, env_val)
        except ValueError:
            # Malformed override — fall through to default rather than abort
            # the entire launcher. The Diagnostics dialog surfaces the issue
            # via :func:`source_for` reporting the default tier.
            continue
        else:
            sources[key] = f"env:{env_name}"

    for key, default in DEFAULTS.items():
        if key not in resolved:
            resolved[key] = default
            sources.setdefault(key, "default")

    return resolved, sources


def _ensure_loaded() -> dict[str, Any]:
    global _RESOLVED, _SOURCES
    if _RESOLVED is None:
        resolved, sources = _resolve_all()
        _RESOLVED = resolved
        _SOURCES = sources
    return _RESOLVED


def get(key: str, default: Any = None) -> Any:
    """Resolve and return a single setting by key.

    Parameters
    ----------
    key:
        A key in :data:`DEFAULTS`. Unknown keys return ``default`` rather
        than raising, so callers can probe future keys safely.
    default:
        Returned when ``key`` is not recognized.
    """
    if key not in _ALL_KEYS:
        return default
    return _ensure_loaded()[key]


def all_settings() -> Mapping[str, Any]:
    """Return a read-only mapping of every resolved setting."""
    # Return a copy so callers cannot mutate the resolver's cache.
    return dict(_ensure_loaded())


def source_for(key: str) -> str:
    """Return a short tag describing where ``key`` was resolved from.

    One of ``"env:<NAME>"`` or ``"default"``. Useful for the Diagnostics
    dialog so operators can see whether an override is in effect.
    """
    if key not in _ALL_KEYS:
        return "unknown"
    _ensure_loaded()
    return _SOURCES.get(key, "default")


def describe() -> dict[str, dict[str, Any]]:
    """Return a structured summary suitable for the Diagnostics dialog.

    Each entry has the resolved ``value`` (rendered as ``str``) and a
    ``source`` tag explaining which precedence tier produced it.
    """
    _ensure_loaded()
    return {
        key: {
            "value": str(_RESOLVED[key]),
            "source": _SOURCES.get(key, "default"),
        }
        for key in DEFAULTS
    }


def config_path() -> Path:
    """Return the candidate path for an external YAML/JSON config file.

    Always returns a path; the file may or may not exist. The launcher
    never requires this file to be present — the env-var tier is the
    primary override mechanism in Batch 1.
    """
    override = os.environ.get("HARBOR_LAUNCHER_CONFIG")
    if override:
        return Path(override).expanduser()
    # Conventional per-user location; matches %APPDATA% on Windows.
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "harbor-launcher" / "config.yaml"
    return Path.home() / ".harbor-launcher" / "config.yaml"


def reload() -> None:
    """Discard the cached resolution and re-read on next access.

    Primarily useful for tests and for an operator who changes env vars at
    runtime and wants the next poll to see the new values.
    """
    global _RESOLVED, _SOURCES
    _RESOLVED = None
    _SOURCES = {}


__all__ = [
    "DEFAULTS",
    "ENV_KEYS",
    "all_settings",
    "config_path",
    "describe",
    "get",
    "reload",
    "source_for",
]
