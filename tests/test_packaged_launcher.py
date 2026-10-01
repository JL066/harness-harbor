"""Focused packaged launcher/backend boundary checks."""

from __future__ import annotations

import json
import sys
import types
import time

import pytest

from launcher.config import COLOR_STATUS_FAILED, COLOR_STATUS_RUNNING, COLOR_STATUS_WARNING
from launcher.runtime_backend import LegacyBackend, PackagedBackend, create_backend


def _snapshot(state="healthy", *, component_states=None):
    states = component_states or {name: state for name in ("tunnel", "mcp", "daemon")}
    return {
        "state": state,
        "components": {
            name: {"state": value, "message": f"{name}:{value}"}
            for name, value in states.items()
        },
    }


class FakeBridge:
    def __init__(self, *, snapshot=None, telemetry=None, logs=None, diagnostics=None, close_result=None):
        self.snapshot = snapshot or _snapshot()
        self.telemetry = telemetry or {"harnesses": [], "agy_models": ["agy-model"]}
        self.logs = logs or {"text": ""}
        self.diagnostics = diagnostics if diagnostics is not None else {"state": "ok"}
        self.close_result = close_result
        self.calls = []
        self.restart_calls = 0
        self.close_calls = 0

    def request(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "status.snapshot":
            return self.snapshot
        if method == "harness.telemetry":
            return self.telemetry
        if method == "logs.tail":
            return self.logs
        if method == "diagnostics.run":
            return self.diagnostics
        if method in {"runtime.start", "runtime.stop"}:
            return self.snapshot
        raise AssertionError(method)

    def restart(self):
        self.restart_calls += 1
        return self.snapshot

    def close(self):
        self.close_calls += 1
        return self.close_result


def test_packaged_backend_uses_bridge_and_maps_without_pid_scan(monkeypatch):
    fake = FakeBridge(
        telemetry={"harnesses": [{"name": "agy", "available": True, "summary": "Ready"}], "agy_models": ["m1"]}
    )
    backend = PackagedBackend(client=fake)
    monkeypatch.setattr("launcher.process_manager.get_harbor_process_tree", lambda: (_ for _ in ()).throw(AssertionError("PID scan")))

    health = backend.health()
    telemetry = backend.telemetry()
    assert health.tunnel.status == "Healthy"
    assert health.daemon.status == "Running"
    assert health.tunnel.pids == []
    assert telemetry["rows"][0]["name"] == "agy"
    assert telemetry["agy_models"] == ["m1"]
    assert fake.calls[:2] == [("status.snapshot", {}), ("harness.telemetry", {})]


def test_running_unverified_mcp_maps_to_green_overall_without_claiming_protocol_health():
    snapshot = _snapshot(
        "running",
        component_states={"tunnel": "healthy", "mcp": "running", "daemon": "healthy"},
    )
    snapshot["components"]["mcp"]["message"] = (
        "Tunnel-owned MCP child is present; MCP protocol health remains unverified."
    )
    backend = PackagedBackend(client=FakeBridge(snapshot=snapshot))

    health = backend.health()

    assert health.overall_status == "Harbor is running"
    assert health.overall_color == COLOR_STATUS_RUNNING
    assert health.mcp.status == "Running"
    assert health.mcp.color == COLOR_STATUS_RUNNING
    assert "protocol health remains unverified" in health.mcp.detail
    assert health.mcp.status != "Healthy"
    assert backend.start()[0] is True


def test_packaged_mcp_warning_and_failure_keep_warning_and_error_overall():
    warning = PackagedBackend(client=FakeBridge(snapshot=_snapshot(
        "partial", component_states={"tunnel": "healthy", "mcp": "warning", "daemon": "healthy"}
    ))).health()
    failed = PackagedBackend(client=FakeBridge(snapshot=_snapshot(
        "failed", component_states={"tunnel": "healthy", "mcp": "failed", "daemon": "healthy"}
    ))).health()

    assert warning.overall_status == "Harbor running with warnings"
    assert warning.overall_color == COLOR_STATUS_WARNING
    assert failed.overall_status == "Issues detected"
    assert failed.overall_color == COLOR_STATUS_FAILED


def test_packaged_lifecycle_fails_for_top_level_or_component_partial():
    top_partial = FakeBridge(snapshot=_snapshot("partial"))
    assert PackagedBackend(client=top_partial).start()[0] is False

    component_partial = FakeBridge(snapshot=_snapshot("healthy", component_states={"tunnel": "healthy", "mcp": "starting", "daemon": "healthy"}))
    ok, message = PackagedBackend(client=component_partial).start()
    assert ok is False
    assert "partial" in message


def test_packaged_snapshot_requires_all_valid_components():
    bad = {"state": "healthy", "components": {"tunnel": {"state": "healthy"}, "mcp": {"state": "healthy"}}}
    with pytest.raises(Exception):
        PackagedBackend(client=FakeBridge(snapshot=bad)).health()


def test_packaged_logs_are_bounded_and_diagnostics_are_json():
    fake = FakeBridge(logs={"text": "\n".join(f"line-{n}" for n in range(300))}, diagnostics={"safe": True})
    backend = PackagedBackend(client=fake)
    lines, encoding = backend.logs.tail("tunnel", max_lines=500)
    assert len(lines) == 200
    assert encoding == "runtime"
    assert fake.calls[-1] == ("logs.tail", {"component": "tunnel", "lines": 200})
    rendered = backend.diagnostics.run()
    assert json.loads(rendered) == {"safe": True}
    assert fake.calls[-1] == ("diagnostics.run", {})


def test_backend_selection_respects_explicit_legacy_and_frozen_default():
    assert isinstance(create_backend(environ={}, frozen=False), LegacyBackend)
    assert isinstance(create_backend(environ={"HARBOR_RUNTIME_MODE": "legacy"}, frozen=True), LegacyBackend)
    assert isinstance(create_backend(environ={"HARBOR_RUNTIME_MODE": "packaged"}, frozen=False, client=FakeBridge()), PackagedBackend)


def test_app_health_and_actions_use_injected_packaged_backend(monkeypatch):
    pytest.importorskip("customtkinter")
    from launcher.ui.app import HarborLauncherApp

    class Backend:
        mode = "packaged"

        def __init__(self):
            self.health_calls = 0
            self.telemetry_calls = 0
            self.start_calls = 0

        def health(self):
            self.health_calls += 1
            return PackagedBackend(client=FakeBridge()).health()

        def telemetry(self):
            self.telemetry_calls += 1
            return {"rows": [], "agy_models": []}

        def start(self, progress_cb=None):
            self.start_calls += 1
            return True, "started"

    backend = Backend()
    app = HarborLauncherApp.__new__(HarborLauncherApp)
    app.backend = backend
    app.is_busy = False
    app._poll_active = True
    applied = []
    app.after = lambda _delay, callback, *args: applied.append((callback, args))
    app._apply_health_snapshot = lambda value: applied.append(("health", value))
    app._apply_harness_statuses = lambda rows, models: applied.append(("telemetry", rows, models))
    app._show_backend_error = lambda message: applied.append(("error", message))
    app._set_busy = lambda *_args: setattr(app, "is_busy", True)
    app._finish_action = lambda ok, message: applied.append(("action", ok, message))

    app._run_health_poll_async()
    deadline = time.time() + 2
    while backend.telemetry_calls == 0 and time.time() < deadline:
        time.sleep(0.01)
    app._handle_start()
    deadline = time.time() + 2
    while backend.start_calls == 0 and time.time() < deadline:
        time.sleep(0.01)

    assert backend.health_calls == 1
    assert backend.telemetry_calls == 1
    assert backend.start_calls == 1
    assert app._runtime_mode_label() == "Packaged Runtime"


def test_run_launcher_bootstraps_before_loading_ui(monkeypatch):
    import run_launcher

    events = []
    fake_app = types.ModuleType("launcher.ui.app")

    class FakeApp:
        def __init__(self):
            events.append("app")

        def mainloop(self):
            events.append("loop")

    fake_app.HarborLauncherApp = FakeApp
    monkeypatch.setitem(sys.modules, "launcher.ui.app", fake_app)
    monkeypatch.setattr(run_launcher, "bootstrap", lambda: events.append("bootstrap"))
    assert run_launcher.main() == 0
    assert events == ["bootstrap", "app", "loop"]


def test_run_launcher_bootstrap_failure_does_not_load_ui(monkeypatch):
    import builtins
    import run_launcher

    events = []
    monkeypatch.setattr(run_launcher, "bootstrap", lambda: (_ for _ in ()).throw(RuntimeError("missing sidecar")))
    monkeypatch.setattr(run_launcher, "_show_bootstrap_error", lambda error: events.append(str(error)))
    original_import = builtins.__import__

    def tracking_import(name, *args, **kwargs):
        if name == "launcher.ui.app":
            raise AssertionError("UI imported after bootstrap failure")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", tracking_import)
    assert run_launcher.main() == 1
    assert events == ["missing sidecar"]
