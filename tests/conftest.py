import os
import sys
from pathlib import Path
import pytest

# Add root project dir to sys.path for test discovery
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))


@pytest.fixture(autouse=True)
def isolate_test_environment(monkeypatch):
    """Prevent inherited HARBOR_* state from redirecting tests into the running Harbor."""
    for key in list(os.environ.keys()):
        if key.startswith("HARBOR_"):
            monkeypatch.delenv(key, raising=False)
    if sys.platform == "win32":
        import ctypes
        ctypes.set_last_error(0)


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
