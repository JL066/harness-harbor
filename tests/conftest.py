"""Execution lanes. Personal credentials and real installations are never CI fixtures."""
import sys
import pytest


def pytest_addoption(parser):
    parser.addoption("--windows-manual", action="store_true", help="Explicitly enable Windows real-installation tests")


def pytest_collection_modifyitems(config, items):
    for item in items:
        node = item.nodeid
        explicit = [lane for lane in ("shared", "macos", "windows_ci", "windows_manual") if item.get_closest_marker(lane)]
        if len(explicit) > 1:
            raise pytest.UsageError(f"Test belongs to multiple execution lanes: {node}")
        if explicit:
            lane = explicit[0]
        elif "tests/unit_launcher/" in node:
            lane = "windows_ci"
        elif "test_macos_queue_sessions.py" in node or "test_macos_runtime.py::PosixTests" in node:
            lane = "macos"
        elif "test_mac_launcher.py" in node and "test_agy_models_probe" not in node:
            lane = "macos"
        else:
            lane = "shared"
        item.add_marker(getattr(pytest.mark, lane))
        if lane == "macos" and sys.platform != "darwin":
            item.add_marker(pytest.mark.skip(reason="macOS-only execution lane"))
        if lane == "windows_manual" and (sys.platform != "win32" or not config.getoption("--windows-manual")):
            item.add_marker(pytest.mark.skip(reason="Requires explicit operator opt-in on a real Windows installation"))
