"""Runtime backends for the launcher UI.

The packaged backend is deliberately a small adapter around the shared
JSON-lines runtime bridge.  It does not inspect local Harbor processes, read
legacy log files, or invoke the legacy lifecycle helpers.  The legacy backend
keeps the existing launcher behavior behind the same UI-facing contract.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from launcher.health_checker import (
    COLOR_STATUS_FAILED,
    COLOR_STATUS_HEALTHY,
    COLOR_STATUS_RUNNING,
    COLOR_STATUS_STARTING,
    COLOR_STATUS_STOPPED,
    COLOR_STATUS_WARNING,
    ComponentHealth,
    HarborHealthSnapshot,
)
from launcher.harnesses import SUPPORTED_HARNESSES
from launcher.runtime_client import BridgeClient, BridgeError


class BackendError(RuntimeError):
    """Raised when a runtime backend cannot satisfy an operation."""


_STATUS_MAP = {
    "healthy": ("Healthy", COLOR_STATUS_HEALTHY),
    "running": ("Running", COLOR_STATUS_RUNNING),
    "starting": ("Starting", COLOR_STATUS_STARTING),
    "restarting": ("Restarting", COLOR_STATUS_STARTING),
    "warning": ("Warning", COLOR_STATUS_WARNING),
    "partial": ("Warning", COLOR_STATUS_WARNING),
    "failed": ("Failed", COLOR_STATUS_FAILED),
    "unhealthy": ("Unhealthy", COLOR_STATUS_FAILED),
    "stopped": ("Stopped", COLOR_STATUS_STOPPED),
}


def _state(value: Any) -> str:
    return str(value or "unknown").strip().lower()


def _display_name(name: str) -> str:
    return dict(SUPPORTED_HARNESSES).get(name, name.replace("_", " ").title())


def _empty_tree():
    # Importing the type is cheap; the important boundary is that packaged
    # health never discovers or derives this data from host processes.
    from launcher.process_manager import HarborProcessTree

    return HarborProcessTree([], [], [], [], [])


def _component_health(name: str, raw: Mapping[str, Any] | None, fallback: str) -> ComponentHealth:
    if not isinstance(raw, Mapping):
        raise BackendError(f"Runtime snapshot is missing the {name} component.")
    record = dict(raw)
    state = _state(record.get("state", record.get("status")))
    if state not in _STATUS_MAP:
        raise BackendError(f"Runtime snapshot has an invalid {name} state.")
    status, color = _STATUS_MAP[state]
    # The legacy card contract describes a healthy daemon as Running.
    if name == "Job Daemon" and status == "Healthy":
        status, color = "Running", COLOR_STATUS_RUNNING
    detail = record.get("message") or record.get("detail") or state.replace("_", " ").title()
    return ComponentHealth(
        name=name,
        status=status,
        detail=str(detail),
        pids=[],
        color=color,
    )


def map_runtime_snapshot(value: Any) -> HarborHealthSnapshot:
    """Map a complete shared runtime snapshot to the existing card model."""
    if isinstance(value, HarborHealthSnapshot):
        return value
    if not isinstance(value, Mapping):
        raise BackendError("Runtime returned an invalid health snapshot.")

    overall_state = _state(value.get("state", value.get("status")))
    if overall_state not in _STATUS_MAP:
        raise BackendError("Runtime snapshot has an invalid overall state.")
    components = value.get("components")
    if not isinstance(components, Mapping):
        raise BackendError("Runtime snapshot is missing component states.")
    required = {"tunnel", "mcp", "daemon"}
    if not required.issubset(components):
        raise BackendError("Runtime snapshot is missing component states.")

    tunnel = _component_health("Tunnel", components.get("tunnel"), overall_state)
    mcp = _component_health("Harbor MCP", components.get("mcp"), overall_state)
    daemon = _component_health("Job Daemon", components.get("daemon"), overall_state)

    if overall_state == "healthy":
        overall_status, overall_color = "All systems healthy", COLOR_STATUS_HEALTHY
    elif overall_state == "running":
        overall_status, overall_color = "Harbor is running", COLOR_STATUS_RUNNING
    elif overall_state in {"starting", "restarting"}:
        overall_status, overall_color = "Harbor is starting...", COLOR_STATUS_STARTING
    elif overall_state == "stopped":
        overall_status, overall_color = "Harbor is stopped", COLOR_STATUS_STOPPED
    elif overall_state in {"failed", "unhealthy"}:
        overall_status, overall_color = "Issues detected", COLOR_STATUS_FAILED
    elif overall_state == "partial":
        overall_status, overall_color = "Harbor running with warnings", COLOR_STATUS_WARNING
    else:
        overall_status, overall_color = "Runtime status unavailable", COLOR_STATUS_WARNING

    return HarborHealthSnapshot(
        tunnel=tunnel,
        mcp=mcp,
        daemon=daemon,
        overall_status=overall_status,
        overall_color=overall_color,
        timestamp=time.time(),
        health_url=value.get("health_url") if isinstance(value.get("health_url"), str) else None,
        tree=_empty_tree(),
    )


def _result_state(snapshot: HarborHealthSnapshot) -> str:
    statuses = {snapshot.tunnel.status, snapshot.mcp.status, snapshot.daemon.status}
    if "Failed" in statuses or "Unhealthy" in statuses:
        return "failed"
    if "Warning" in statuses:
        return "partial"
    if all(status == "Stopped" for status in statuses):
        return "stopped"
    if all(status in {"Healthy", "Running"} for status in statuses):
        return "healthy"
    return "partial"


def _operation_result(action: str, raw: Any) -> tuple[bool, str]:
    top_state = _state(raw.get("state", raw.get("status"))) if isinstance(raw, Mapping) else "unknown"
    if top_state in {"failed", "partial"}:
        return False, f"Packaged runtime {action} reported a {top_state} state."
    try:
        snapshot = map_runtime_snapshot(raw)
    except Exception as exc:
        return False, str(exc) or "Runtime returned an invalid snapshot."

    state = _result_state(snapshot)
    if state in {"failed", "partial"}:
        return False, f"Packaged runtime {action} reported a {state} state."
    if action == "stop" and state != "stopped":
        return False, "Packaged runtime did not reach a stopped state."
    if action in {"start", "restart"} and state != "healthy":
        return False, f"Packaged runtime {action} did not reach a healthy state."
    return True, f"Packaged runtime {action} completed successfully."
class _PackagedLogs:
    def __init__(self, client: BridgeClient):
        self._client = client

    def tail(self, component: str = "tunnel", max_lines: int = 100) -> tuple[list[str], str]:
        try:
            limit = max(1, min(int(max_lines), 200))
        except (TypeError, ValueError):
            limit = 100
        component = component if component in {"runtime", "daemon", "tunnel"} else "tunnel"
        result = self._client.request("logs.tail", {"component": component, "lines": limit})
        if isinstance(result, Mapping):
            text = result.get("text", "")
            values = result.get("lines")
            if isinstance(values, list):
                return [str(line) for line in values[-limit:]], "runtime"
        else:
            text = result
        if not isinstance(text, str):
            text = ""
        return text.splitlines()[-limit:], "runtime"


class _LegacyLogs:
    def tail(self, component: str = "tunnel", max_lines: int = 300) -> tuple[list[str], str]:
        from launcher.config import DAEMON_LOG, TUNNEL_LOG
        from launcher.log_reader import read_log_tail

        path = TUNNEL_LOG if component == "tunnel" else DAEMON_LOG
        return read_log_tail(path, max_lines=max_lines)


class _PackagedDiagnostics:
    def __init__(self, client: BridgeClient):
        self._client = client

    def run(self) -> str:
        result = self._client.request("diagnostics.run", {})
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except (TypeError, ValueError) as exc:
                raise BackendError("Runtime diagnostics returned invalid JSON.") from exc
        else:
            parsed = result
        try:
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as exc:
            raise BackendError("Runtime diagnostics returned invalid JSON.") from exc


class _LegacyDiagnostics:
    def run(self) -> str:
        from launcher.diagnostics import collect_diagnostics, format_diagnostics_markdown

        return format_diagnostics_markdown(collect_diagnostics())


def _normalise_telemetry(value: Any) -> dict[str, list[dict[str, Any]] | list[str]]:
    if isinstance(value, Mapping):
        records = value.get("harnesses", value.get("rows", []))
        models = value.get("agy_models", value.get("models", []))
    else:
        records, models = value, []
    records = records if isinstance(records, list) else []
    rows: list[dict[str, Any]] = []
    for item in records:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            continue
        row = dict(item)
        name = str(row["name"])
        row.setdefault("display_name", _display_name(name))
        row.setdefault("status", "Installed" if row.get("available") else "Not installed")
        row.setdefault("detail", row.get("summary") or ("Ready" if row.get("available") else "Not detected"))
        rows.append(row)
    if not rows and records:
        rows = []
    model_values = []
    if isinstance(models, list):
        for item in models:
            model = item.get("id") if isinstance(item, Mapping) else item
            if isinstance(model, str) and model and model not in model_values:
                model_values.append(model)
    return {"rows": rows, "agy_models": model_values, "models": model_values}


class PackagedBackend:
    mode = "packaged"

    def __init__(self, *, client: BridgeClient | None = None):
        self.client = client or BridgeClient()
        self.logs = _PackagedLogs(self.client)
        self.diagnostics = _PackagedDiagnostics(self.client)

    def health(self) -> HarborHealthSnapshot:
        return map_runtime_snapshot(self.client.request("status.snapshot", {}))

    def telemetry(self) -> dict[str, list[dict[str, Any]] | list[str]]:
        return _normalise_telemetry(self.client.request("harness.telemetry", {}))

    def _action(self, action: str, method: str, progress_cb: Callable[[str], None] | None = None) -> tuple[bool, str]:
        if progress_cb:
            progress_cb(f"Requesting packaged runtime {action}...")
        try:
            raw = self.client.request(method, {})
            return _operation_result(action, raw)
        except Exception as exc:
            if isinstance(exc, BridgeError):
                return False, str(exc) or "Packaged runtime bridge failed."
            return False, str(exc) or "Packaged runtime operation failed."

    def start(self, progress_cb: Callable[[str], None] | None = None) -> tuple[bool, str]:
        return self._action("start", "runtime.start", progress_cb)

    def stop(self, progress_cb: Callable[[str], None] | None = None) -> tuple[bool, str]:
        return self._action("stop", "runtime.stop", progress_cb)

    def restart(self, progress_cb: Callable[[str], None] | None = None) -> tuple[bool, str]:
        if progress_cb:
            progress_cb("Restarting packaged runtime...")
        try:
            return _operation_result("restart", self.client.restart())
        except Exception as exc:
            if isinstance(exc, BridgeError):
                return False, str(exc) or "Packaged runtime bridge failed."
            return False, str(exc) or "Packaged runtime restart failed."

    def close(self) -> bool:
        try:
            result = self.client.close()
            return result is not False
        except Exception:
            return False


class LegacyBackend:
    mode = "legacy"

    def __init__(self):
        self.logs = _LegacyLogs()
        self.diagnostics = _LegacyDiagnostics()

    def health(self) -> HarborHealthSnapshot:
        from launcher.health_checker import get_harbor_health

        return get_harbor_health()

    def telemetry(self) -> dict[str, list[dict[str, Any]] | list[str]]:
        from control_plane import harness_telemetry_snapshot
        from launcher.harnesses import agy_models, list_harnesses

        snapshot = harness_telemetry_snapshot()
        return {"rows": list_harnesses(telemetry_snapshot=snapshot),
                "agy_models": agy_models(telemetry_snapshot=snapshot),
                "models": agy_models(telemetry_snapshot=snapshot)}

    def _action(self, function, progress_cb=None) -> tuple[bool, str]:
        return function(progress_cb=progress_cb)

    def start(self, progress_cb: Callable[[str], None] | None = None) -> tuple[bool, str]:
        from launcher.lifecycle import start_harbor

        return self._action(start_harbor, progress_cb)

    def stop(self, progress_cb: Callable[[str], None] | None = None) -> tuple[bool, str]:
        from launcher.lifecycle import stop_harbor

        return self._action(stop_harbor, progress_cb)

    def restart(self, progress_cb: Callable[[str], None] | None = None) -> tuple[bool, str]:
        from launcher.lifecycle import restart_harbor

        return self._action(restart_harbor, progress_cb)

    def close(self) -> bool:
        # Legacy mode never owns the installed runtime.
        return True


def create_backend(*, environ: Mapping[str, Any] | None = None, frozen: bool | None = None,
                   client: BridgeClient | None = None):
    """Select packaged mode explicitly, or by the frozen executable default."""
    environment = dict(os.environ if environ is None else environ)
    raw_mode = str(environment.get("HARBOR_RUNTIME_MODE", "")).strip().lower()
    if raw_mode and raw_mode not in {"packaged", "legacy"}:
        raise BackendError("Unsupported HARBOR_RUNTIME_MODE; expected packaged or legacy.")
    frozen_value = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    mode = raw_mode or ("packaged" if frozen_value else "legacy")
    if mode == "packaged":
        return PackagedBackend(client=client)
    return LegacyBackend()


__all__ = [
    "BackendError",
    "LegacyBackend",
    "PackagedBackend",
    "create_backend",
    "map_runtime_snapshot",
]
