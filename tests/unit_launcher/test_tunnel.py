from pathlib import Path
import os
import json

import pytest

from launcher.credential_store import InMemoryCredentialBackend, CredentialStore
from launcher.tunnel import (
    ManagedTunnelSupervisor,
    TunnelCredentialError,
    TunnelProfileManager,
    test_connection as run_test_connection,
)
from launcher.user_settings import ConnectionSettings, UserSettings


def _settings():
    return UserSettings(connection=ConnectionSettings(tunnel_id="tun-123", base_url="https://example.test"))


def test_profile_render_is_deterministic_and_uses_legacy_entrypoint(tmp_path):
    manager = TunnelProfileManager(_settings(), profile_path=tmp_path / "profile.yaml", runtime_path=tmp_path / "harbor")
    assert manager.render() == manager.render()
    rendered = manager.render()
    profile = json.loads(rendered)
    assert set(profile) == {"admin_ui", "config_version", "control_plane", "health", "log", "mcp"}
    assert profile["config_version"] == 1
    assert profile["admin_ui"] == {"open_browser": False}
    assert profile["control_plane"] == {
        "api_key": "env:TUNNEL_RUNTIME_KEY",
        "base_url": "https://example.test",
        "tunnel_id": "tun-123",
    }
    assert profile["health"]["listen_addr"] == "127.0.0.1:0"
    assert profile["health"]["url_file"].endswith(".local\\state\\tunnel-client\\health\\harness-harbor.url") or profile["health"]["url_file"].endswith(".local/state/tunnel-client/health/harness-harbor.url")
    assert profile["log"]["format"] == "json"
    assert profile["log"]["level"] == "info"
    assert len(profile["mcp"]["commands"]) == 1
    command = profile["mcp"]["commands"][0]
    assert set(command) == {"channel", "command"}
    assert command["channel"] == "main"
    assert "server_legacy.py" in command["command"]
    assert "server.py" not in command["command"].replace("server_legacy.py", "")
    assert "env:TUNNEL_RUNTIME_KEY" in rendered
    for forbidden in ('"tunnel"', '"runtime_key"', '"command": [', '"env"'):
        assert forbidden not in rendered


def test_profile_schema_rejects_invented_batch2_keys(tmp_path):
    manager = TunnelProfileManager(_settings(), profile_path=tmp_path / "p.yaml", runtime_path=tmp_path / "harbor")
    profile = json.loads(manager.render())
    profile["tunnel"] = {}
    with pytest.raises(ValueError):
        manager._validate_rendered_profile(profile)

    profile = json.loads(manager.render())
    profile["mcp"] = {"command": "bad", "env": {}}
    with pytest.raises(ValueError):
        manager._validate_rendered_profile(profile)


def test_atomic_write_preserves_existing_on_render_failure(tmp_path):
    target = tmp_path / "profile.yaml"
    target.write_text("old", encoding="utf-8")
    manager = TunnelProfileManager(_settings(), profile_path=target)
    manager.validate = lambda: (_ for _ in ()).throw(ValueError("bad"))
    with pytest.raises(ValueError):
        manager.write_profile()
    assert target.read_text(encoding="utf-8") == "old"


def test_child_only_env_injection_and_duplicate_prevention(tmp_path):
    backend = InMemoryCredentialBackend()
    store = CredentialStore(backend=backend)
    store.store(_settings().connection.credential_ref, "runtime-secret")
    calls = []

    class Proc:
        def poll(self): return None

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return Proc()

    manager = TunnelProfileManager(_settings(), profile_path=tmp_path / "p.yaml", runtime_path=tmp_path / "harbor")
    supervisor = ManagedTunnelSupervisor(manager, store, tunnel_executable=tmp_path / "tunnel-client.exe", popen_factory=fake_popen)
    before = os.environ.get("TUNNEL_RUNTIME_KEY")
    first = supervisor.start()
    second = supervisor.start()
    assert first is second and len(calls) == 1
    assert calls[0][1]["env"]["TUNNEL_RUNTIME_KEY"] == "runtime-secret"
    assert os.environ.get("TUNNEL_RUNTIME_KEY") == before
    assert "runtime-secret" not in (tmp_path / "p.yaml").read_text(encoding="utf-8")


def test_missing_credential_fails_closed(tmp_path):
    supervisor = ManagedTunnelSupervisor(
        TunnelProfileManager(_settings(), profile_path=tmp_path / "p.yaml"),
        CredentialStore(backend=InMemoryCredentialBackend()),
        popen_factory=lambda *a, **k: pytest.fail("must not launch"),
    )
    with pytest.raises(TunnelCredentialError):
        supervisor.start()
    assert not (tmp_path / "p.yaml").exists()


def test_connection_is_read_only(tmp_path):
    backend = InMemoryCredentialBackend()
    store = CredentialStore(backend=backend)
    store.store(_settings().connection.credential_ref, "secret")
    result = run_test_connection(_settings(), store, profile_manager=TunnelProfileManager(_settings(), profile_path=tmp_path / "p.yaml"), tunnel_executable=tmp_path / "missing.exe")
    assert result.ok is False
    assert not (tmp_path / "p.yaml").exists()
