from __future__ import annotations

import json

import pytest

from launcher.credential_store import (
    CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY,
    CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY,
    CredentialStore,
    InMemoryCredentialBackend,
)
from launcher.ui.setup_wizard import canonical_route, first_run_status, legacy_installation_detected, SettingsController, validate_draft
from launcher.user_settings import UserSettings, save_user_settings


def draft(route="current"):
    return {
        "connection": {"tunnel_id": "tun-1", "base_url": "https://control.example", "profile_name": "harness-harbor"},
        "codex": {"routing_mode": route, "custom": {"enabled": route == "custom", "profile_name": "Acme", "base_url": "https://api.acme.test/v1", "default_model": ""}},
    }


def test_first_run_requires_connection_and_secret(tmp_path):
    backend = InMemoryCredentialBackend(); store = CredentialStore(backend=backend)
    path = tmp_path / "settings.json"
    assert first_run_status(UserSettings(), store).required
    save_user_settings(UserSettings.from_dict(draft()), path)
    assert first_run_status(UserSettings.from_dict(draft()), store).required
    store.store(CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY, "runtime")
    assert first_run_status(UserSettings.from_dict(draft()), store).required is False


def test_custom_route_requires_fields_and_secret(tmp_path):
    backend = InMemoryCredentialBackend(); store = CredentialStore(backend=backend)
    controller = SettingsController(credential_store=store, settings_path=tmp_path / "settings.json")
    with pytest.raises(ValueError):
        controller.apply({"connection": {"tunnel_id": "", "base_url": "bad", "profile_name": ""}, "codex": {"routing_mode": "custom", "custom": {}}})
    controller.apply(draft("custom"), tunnel_runtime_key="runtime", custom_api_key="custom-secret")
    assert (tmp_path / "settings.json").exists()
    payload = json.loads((tmp_path / "settings.json").read_text())
    encoded = json.dumps(payload)
    assert "custom-secret" not in encoded and '"runtime"' not in encoded
    assert controller.secret_state() == {"tunnel_runtime_key": True, "custom_api_key": True}


def test_apply_rolls_back_secret_when_settings_write_fails(tmp_path, monkeypatch):
    backend = InMemoryCredentialBackend(); store = CredentialStore(backend=backend)
    path = tmp_path / "settings.json"
    save_user_settings(UserSettings.from_dict(draft()), path)
    store.store(CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY, "old")
    controller = SettingsController(credential_store=store, settings_path=path)
    monkeypatch.setattr("launcher.ui.setup_wizard.save_user_settings", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(OSError):
        controller.apply(draft(), tunnel_runtime_key="new")
    assert store.read(CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY) == "old"
    assert json.loads(path.read_text())["connection"]["tunnel_id"] == "tun-1"


def test_validation_does_not_mutate_draft():
    value = draft(); before = json.dumps(value, sort_keys=True)
    assert validate_draft(value) == []
    assert json.dumps(value, sort_keys=True) == before


def test_visible_custom_route_maps_to_canonical_enabled_state():
    selected = canonical_route("Custom OpenAI-compatible provider")
    assert selected == "custom"
    assert {"routing_mode": selected, "custom": {"enabled": selected == "custom"}} == {
        "routing_mode": "custom", "custom": {"enabled": True}
    }


def test_required_secret_preflight_has_zero_mutation(tmp_path):
    backend = InMemoryCredentialBackend(); store = CredentialStore(backend=backend)
    settings_path = tmp_path / "settings.json"; profile_path = tmp_path / "profile.yaml"
    controller = SettingsController(credential_store=store, settings_path=settings_path, profile_path=profile_path)
    with pytest.raises(ValueError, match="Tunnel Runtime Key"):
        controller.apply(draft(), custom_api_key=None)
    assert not settings_path.exists()
    assert not profile_path.exists()
    assert backend._secrets == {}


def test_clearing_required_secret_is_rejected_without_mutation(tmp_path):
    backend = InMemoryCredentialBackend(); store = CredentialStore(backend=backend)
    settings_path = tmp_path / "settings.json"; profile_path = tmp_path / "profile.yaml"
    save_user_settings(UserSettings.from_dict(draft()), settings_path)
    store.store(CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY, "runtime")
    before = settings_path.read_bytes()
    controller = SettingsController(credential_store=store, settings_path=settings_path, profile_path=profile_path)
    with pytest.raises(ValueError, match="required"):
        controller.apply(draft(), clear_tunnel_key=True)
    assert settings_path.read_bytes() == before
    assert not profile_path.exists()
    assert store.read(CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY) == "runtime"


def test_replacement_and_clear_are_rejected_before_mutation(tmp_path):
    backend = InMemoryCredentialBackend(); store = CredentialStore(backend=backend)
    controller = SettingsController(
        credential_store=store,
        settings_path=tmp_path / "settings.json",
        profile_path=tmp_path / "profile.yaml",
    )
    with pytest.raises(ValueError, match="replaced and cleared"):
        controller.apply(draft(), tunnel_runtime_key="new", clear_tunnel_key=True)
    assert backend._secrets == {}
    assert not (tmp_path / "settings.json").exists()
    assert not (tmp_path / "profile.yaml").exists()


def test_custom_required_secret_missing_is_rejected_without_mutation(tmp_path):
    backend = InMemoryCredentialBackend(); store = CredentialStore(backend=backend)
    settings_path = tmp_path / "settings.json"; profile_path = tmp_path / "profile.yaml"
    store.store(CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY, "runtime")
    controller = SettingsController(credential_store=store, settings_path=settings_path, profile_path=profile_path)
    with pytest.raises(ValueError, match="Custom API key"):
        controller.apply(draft("custom"))
    assert not settings_path.exists()
    assert not profile_path.exists()
    assert store.read(CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY) == "runtime"


def test_legacy_detector_checks_runtime_and_profile_only(tmp_path):
    runtime = tmp_path / "harbor"; profiles = tmp_path / "profiles"
    runtime.mkdir(); profiles.mkdir()
    assert not legacy_installation_detected(production_path=runtime, tunnel_profile_dir=profiles, tunnel_profile_name="demo")
    (runtime / "server_legacy.py").write_text("# legacy", encoding="utf-8")
    assert not legacy_installation_detected(production_path=runtime, tunnel_profile_dir=profiles, tunnel_profile_name="demo")
    (profiles / "demo.yaml").write_text("opaque", encoding="utf-8")
    assert legacy_installation_detected(production_path=runtime, tunnel_profile_dir=profiles, tunnel_profile_name="demo")


def test_legacy_configured_user_skips_setup_without_settings(tmp_path):
    status = first_run_status(
        settings_path=tmp_path / "missing.json",
        legacy_detector=lambda: True,
        credential_store=None,
    )
    assert status.required is False


def test_new_install_without_legacy_evidence_still_requires_setup(tmp_path):
    status = first_run_status(settings_path=tmp_path / "missing.json", legacy_detector=lambda: False)
    assert status.required is True


def test_corrupt_settings_still_require_repair_even_with_legacy_evidence(tmp_path):
    path = tmp_path / "settings.json"; path.write_text("{not-json", encoding="utf-8")
    status = first_run_status(settings_path=path, legacy_detector=lambda: True)
    assert status.required is True and status.corrupted is True


def test_macos_unmodified_credentials_are_not_read(monkeypatch, tmp_path):
    from unittest.mock import Mock
    import launcher.ui.setup_wizard as setup

    monkeypatch.setattr(setup.sys, "platform", "darwin")
    store = Mock()
    store.exists.return_value = True
    controller = SettingsController(credential_store=store, settings_path=tmp_path / "settings.json")
    assert controller._preflight_secret(CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY, None, False, required=False, label="Custom API key") is None
    store.exists.assert_not_called()
    store.read.assert_not_called()
    assert controller._preflight_secret(CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY, None, False, required=True, label="Tunnel Runtime Key") is None
    store.exists.assert_called_once_with(CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY)
    store.read.assert_not_called()
