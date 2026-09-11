"""Release integration regressions for launcher migration and packaging."""

from __future__ import annotations

import json
from pathlib import Path

import build_exe

from launcher.credential_store import CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY, CredentialStore, InMemoryCredentialBackend
from launcher.tunnel import TunnelProfileManager
from launcher.ui.setup_wizard import SettingsController, first_run_status
from launcher.user_settings import UserSettings, load_user_settings, save_user_settings


def _draft() -> dict:
    return {
        "connection": {
            "tunnel_id": "tun-release",
            "base_url": "https://control.example.test",
            "profile_name": "portable-profile",
        },
        "codex": {"routing_mode": "current", "custom": {"enabled": False}},
    }


def test_release_packaging_declares_all_runtime_lazy_imports_and_is_portable():
    required = {
        "launcher.credential_store",
        "launcher.harnesses",
        "launcher.tunnel",
        "launcher.tunnel_manager",
        "launcher.tunnel_profile",
        "launcher.ui.setup_wizard",
        "control_plane",
    }
    assert required <= set(build_exe.RUNTIME_HIDDEN_IMPORTS)

    spec = (Path(__file__).parents[2] / "harbor_launcher.spec").read_text(encoding="utf-8")
    assert "SPECPATH" in spec
    assert "pyinstaller_hooks" in spec
    assert str(Path(__file__).parents[2]).replace("\\", "/") not in spec
    assert str(Path.home()).replace("\\", "/") not in spec


def test_existing_new_format_install_opens_without_rewriting_settings(tmp_path):
    settings_path = tmp_path / "per-user" / "settings.json"
    configured = UserSettings.from_dict(_draft())
    save_user_settings(configured, settings_path)
    before = settings_path.read_bytes()
    store = CredentialStore(backend=InMemoryCredentialBackend())
    store.store(CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY, "runtime-secret")

    status = first_run_status(settings_path=settings_path, credential_store=store)

    assert not status.required
    assert settings_path.read_bytes() == before
    assert load_user_settings(settings_path) == configured


def test_settings_apply_has_no_lifecycle_side_effects(tmp_path, monkeypatch):
    store = CredentialStore(backend=InMemoryCredentialBackend())
    settings_path = tmp_path / "settings.json"
    profile_path = tmp_path / "managed-profile.yaml"
    controller = SettingsController(
        credential_store=store,
        settings_path=settings_path,
        profile_path=profile_path,
    )

    def unexpected_lifecycle_call(*_args, **_kwargs):
        raise AssertionError("saving settings must not change runtime lifecycle")

    monkeypatch.setattr("launcher.lifecycle.start_harbor", unexpected_lifecycle_call)
    monkeypatch.setattr("launcher.lifecycle.restart_harbor", unexpected_lifecycle_call)
    monkeypatch.setattr("launcher.lifecycle.stop_harbor", unexpected_lifecycle_call)
    controller.apply(_draft(), tunnel_runtime_key="runtime-secret")

    assert settings_path.exists()
    assert profile_path.exists()


def test_managed_profile_uses_configured_runtime_paths_and_never_embeds_secret(tmp_path, monkeypatch):
    runtime_root = tmp_path / "a portable runtime"
    runtime_root.mkdir()
    settings = UserSettings.from_dict(_draft())
    health_file = tmp_path / "state/health/portable-profile.url"
    monkeypatch.setenv("HARBOR_TUNNEL_HEALTH_URL_FILE", str(health_file))

    rendered = TunnelProfileManager(settings, runtime_path=runtime_root).render()
    profile = json.loads(rendered)
    command = profile["mcp"]["commands"][0]["command"]

    assert str(runtime_root / ".venv-legacy" / "Scripts" / "pythonw.exe") in command
    assert str(runtime_root / "server_legacy.py") in command
    assert profile["control_plane"]["api_key"] == "env:TUNNEL_RUNTIME_KEY"
    assert "runtime-secret" not in rendered
    assert profile["health"]["url_file"] == str(health_file)
