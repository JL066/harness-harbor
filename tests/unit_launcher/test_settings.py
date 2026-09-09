"""Unit tests for the configuration resolution foundation.

These tests pin down the public contract of :mod:`launcher.settings`:

* Defaults reproduce the checkout-relative public layout.
* Every key in :data:`launcher.settings.ENV_KEYS` actually overrides the
  matching default.
* Path keys are coerced to :class:`pathlib.Path`, float keys to ``float``.
* :func:`launcher.settings.reload` discards the cache so subsequent
  ``get`` calls see fresh env state (important for tests that mutate
  ``os.environ``).
* :mod:`launcher.config` re-exports the resolved values unchanged for
  every legacy constant name (backward-compatibility guarantee).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from launcher import config, settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch):
    """Snapshot env, clear HARBOR_* overrides, reload settings for every test.

    Without this, a test that exports ``HARBOR_HOME`` would leak into the
    next test and mask regressions. The reload call at the end guarantees
    the settings cache is recomputed against the cleared env on each test.
    """
    saved = {k: v for k, v in os.environ.items() if k.startswith("HARBOR_") or k == "APPDATA"}
    for k in list(os.environ):
        if k.startswith("HARBOR_"):
            monkeypatch.delenv(k, raising=False)
    settings.reload()
    yield
    # Restore (monkeypatch handles teardown of the delenv calls above;
    # anything we set during the test is reverted automatically).
    for k in list(os.environ):
        if k.startswith("HARBOR_"):
            monkeypatch.delenv(k, raising=False)
    for k, v in saved.items():
        monkeypatch.setenv(k, v)
    settings.reload()


# ---------------------------------------------------------------------------
# Default resolution
# ---------------------------------------------------------------------------


def test_defaults_are_public_and_checkout_relative():
    """Built-in defaults must not disclose a deployment or user identity."""
    expected_root = Path(settings.__file__).resolve().parent.parent
    assert settings.get("harbor_home") == expected_root
    assert settings.get("junction_path").name == ".harbor-junction"
    assert settings.get("tunnel_exe") == Path("tunnel-client.exe")
    assert settings.get("mcp_script_name") == "server_legacy.py"
    assert settings.get("forbidden_mcp_script") == "server.py"
    assert settings.get("daemon_script_name") == "codex_job_daemon.py"
    assert settings.get("autostart_app_name") == "HarnessHarborLauncher"


def test_default_float_timings():
    """Default timeouts must be float-typed so callers can multiply cleanly."""
    assert isinstance(settings.get("poll_interval_seconds"), float)
    assert isinstance(settings.get("stop_timeout"), float)
    assert isinstance(settings.get("start_health_timeout"), float)
    assert isinstance(settings.get("http_probe_timeout"), float)
    assert settings.get("poll_interval_seconds") == 3.0
    assert settings.get("stop_timeout") == 8.0


def test_all_settings_includes_every_default_key():
    """``all_settings`` is a superset of DEFAULTS keys."""
    snapshot = settings.all_settings()
    for key in settings.DEFAULTS:
        assert key in snapshot, f"Missing key in all_settings(): {key}"


def test_unknown_key_returns_caller_default():
    """An unknown key must not raise; the caller-supplied default wins."""
    assert settings.get("nope.does.not.exist", "fallback") == "fallback"
    assert settings.get("nope.does.not.exist") is None


# ---------------------------------------------------------------------------
# Env var overrides
# ---------------------------------------------------------------------------


def test_env_override_string_key(monkeypatch):
    monkeypatch.setenv("HARBOR_TUNNEL_PROFILE_NAME", "demo-profile")
    settings.reload()
    assert settings.get("tunnel_profile_name") == "demo-profile"
    assert settings.source_for("tunnel_profile_name") == "env:HARBOR_TUNNEL_PROFILE_NAME"


def test_env_override_path_key_coerces_to_path(monkeypatch):
    monkeypatch.setenv("HARBOR_HOME", r"E:\custom\harbor")
    settings.reload()
    value = settings.get("harbor_home")
    assert isinstance(value, Path)
    assert value == Path(r"E:\custom\harbor")
    assert settings.source_for("harbor_home") == "env:HARBOR_HOME"


def test_env_override_float_key_coerces_to_float(monkeypatch):
    monkeypatch.setenv("HARBOR_POLL_INTERVAL_SECONDS", "7.5")
    settings.reload()
    assert settings.get("poll_interval_seconds") == 7.5
    assert isinstance(settings.get("poll_interval_seconds"), float)


def test_legacy_env_alias_works(monkeypatch):
    """``HARBOR_PRODUCTION_PATH`` is an accepted alias for ``HARBOR_HOME``."""
    monkeypatch.setenv("HARBOR_PRODUCTION_PATH", r"D:\legacy\harbor")
    settings.reload()
    assert settings.get("harbor_home") == Path(r"D:\legacy\harbor")
    assert settings.source_for("harbor_home") == "env:HARBOR_PRODUCTION_PATH"


def test_malformed_float_override_falls_back_to_default(monkeypatch):
    """A non-numeric value for a float key must not crash the launcher."""
    monkeypatch.setenv("HARBOR_POLL_INTERVAL_SECONDS", "not-a-number")
    settings.reload()
    # Falls back to the default 3.0 rather than raising.
    assert settings.get("poll_interval_seconds") == 3.0
    assert settings.source_for("poll_interval_seconds") == "default"


def test_empty_env_value_treated_as_unset(monkeypatch):
    """An empty string is indistinguishable from 'no override'."""
    monkeypatch.setenv("HARBOR_HOME", "")
    settings.reload()
    assert settings.get("harbor_home") == Path(settings.__file__).resolve().parent.parent
    assert settings.source_for("harbor_home") == "default"


# ---------------------------------------------------------------------------
# describe / source_for / reload
# ---------------------------------------------------------------------------


def test_describe_returns_value_and_source_for_every_default_key():
    desc = settings.describe()
    for key, default in settings.DEFAULTS.items():
        assert key in desc, f"describe() missing {key}"
        entry = desc[key]
        assert entry["value"] == str(default)
        assert entry["source"] in {"default", f"env:HARBOR_{key.upper()}"}


def test_reload_picks_up_new_env_value(monkeypatch):
    """reload() must discard the cache and re-resolve from current env."""
    assert settings.get("poll_interval_seconds") == 3.0
    monkeypatch.setenv("HARBOR_POLL_INTERVAL_SECONDS", "9.0")
    # Without reload, the cached value is still 3.0.
    assert settings.get("poll_interval_seconds") == 3.0
    settings.reload()
    assert settings.get("poll_interval_seconds") == 9.0


def test_config_path_returns_under_appdata_by_default(monkeypatch):
    monkeypatch.delenv("HARBOR_LAUNCHER_CONFIG", raising=False)
    monkeypatch.setenv("APPDATA", r"C:\Users\test\AppData\Roaming")
    settings.reload()
    assert settings.config_path() == Path(
        r"C:\Users\test\AppData\Roaming\harbor-launcher\config.yaml"
    )


def test_config_path_respects_override(monkeypatch):
    monkeypatch.setenv("HARBOR_LAUNCHER_CONFIG", r"D:\elsewhere\custom.yaml")
    assert settings.config_path() == Path(r"D:\elsewhere\custom.yaml")


def test_config_path_falls_back_to_home_without_appdata(monkeypatch):
    monkeypatch.delenv("HARBOR_LAUNCHER_CONFIG", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    settings.reload()
    # Path.home() is the final fallback; the conventional hidden directory
    # is ``.harbor-launcher`` (Unix-style) under the user's home.
    p = settings.config_path()
    assert isinstance(p, Path)
    assert p.name == "config.yaml"
    assert p.parent.name == ".harbor-launcher"
    assert p.parent.parent == Path.home()


# ---------------------------------------------------------------------------
# Backward compatibility: launcher.config re-exports resolved values
# ---------------------------------------------------------------------------


def test_config_module_reexports_resolved_defaults():
    """Every legacy constant name must still be importable from config."""
    # Paths
    assert config.PRODUCTION_PATH == settings.get("harbor_home")
    assert config.JUNCTION_PATH == settings.get("junction_path")
    assert config.TUNNEL_EXE == settings.get("tunnel_exe")
    assert config.TUNNEL_PROFILE_DIR == settings.get("tunnel_profile_dir")
    assert config.TUNNEL_PROFILE_NAME == settings.get("tunnel_profile_name")
    assert config.TUNNEL_HEALTH_URL_FILE == settings.get("tunnel_health_url_file")
    assert config.VENV_PYTHON == settings.get("venv_python")
    assert config.VENV_PYTHONW == settings.get("venv_pythonw")
    # Names
    assert config.MCP_SCRIPT_NAME == settings.get("mcp_script_name")
    assert config.FORBIDDEN_MCP_SCRIPT == settings.get("forbidden_mcp_script")
    assert config.DAEMON_SCRIPT_NAME == settings.get("daemon_script_name")
    assert config.SCHEDULED_TASK_TUNNEL == settings.get("scheduled_task_tunnel")
    assert config.SCHEDULED_TASK_DAEMON == settings.get("scheduled_task_daemon")
    assert config.AUTOSTART_APP_NAME == settings.get("autostart_app_name")
    assert config.AUTOSTART_REG_KEY == settings.get("autostart_reg_key")
    # Timings
    assert config.POLL_INTERVAL_SECONDS == settings.get("poll_interval_seconds")
    assert config.STOP_TIMEOUT == settings.get("stop_timeout")
    assert config.START_HEALTH_TIMEOUT == settings.get("start_health_timeout")
    assert config.HTTP_PROBE_TIMEOUT == settings.get("http_probe_timeout")


def test_config_derived_paths_follow_production_path(monkeypatch):
    """Logs and supervisor scripts must track PRODUCTION_PATH on a fresh import.

    The launcher's ``config`` module binds its module-level constants once
    at import time; this is the production behavior (env vars are read on
    startup). Re-importing ``config`` after mutating env reproduces the
    effect of relaunching the launcher with a different ``HARBOR_HOME``.
    """
    monkeypatch.setenv("HARBOR_HOME", r"F:\alt\harbor")
    import importlib
    reloaded = importlib.reload(config)
    assert reloaded.PRODUCTION_PATH == Path(r"F:\alt\harbor")
    assert reloaded.TUNNEL_LOG == Path(r"F:\alt\harbor\tunnel-supervisor.log")
    assert reloaded.DAEMON_LOG == Path(r"F:\alt\harbor\codex-job-daemon.log")
    assert reloaded.START_TUNNEL_SCRIPT == Path(r"F:\alt\harbor\start-tunnel.ps1")
    assert reloaded.START_DAEMON_SCRIPT == Path(r"F:\alt\harbor\start-codex-job-daemon.ps1")


def test_brand_assets_remain_launcher_local(monkeypatch):
    """Brand assets are launcher-local and must not be affected by overrides.

    See :func:`test_config_derived_paths_follow_production_path` for why
    we reload ``config`` here.
    """
    import importlib
    monkeypatch.setenv("HARBOR_HOME", r"Z:\somewhere\else")
    reloaded = importlib.reload(config)
    # Brand paths are still under the launcher's own install dir, not under
    # the production runtime.
    assert reloaded.BRAND_DIR.name == "brand"
    assert "launcher" in reloaded.BRAND_DIR.parts
    assert "assets" in reloaded.BRAND_DIR.parts
    assert reloaded.PRODUCTION_PATH == Path(r"Z:\somewhere\else")
    assert reloaded.BRAND_DIR != reloaded.PRODUCTION_PATH / "launcher" / "assets" / "brand"
