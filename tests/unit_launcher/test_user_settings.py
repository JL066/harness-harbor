"""Unit tests for user settings persistence, schema handling, and secret safety."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from launcher.credential_store import (
    CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY,
    CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY,
)
from launcher.user_settings import (
    CodexCustomSettings,
    CodexSettings,
    ConnectionSettings,
    SCHEMA_VERSION,
    SettingsCorruptionError,
    SettingsSecretPersistenceError,
    SettingsVersionError,
    UserSettings,
    get_user_settings_dir,
    get_user_settings_path,
    load_user_settings,
    save_user_settings,
)


# ---------------------------------------------------------------------------
# Absent-file defaults
# ---------------------------------------------------------------------------


def test_absent_file_returns_defaults(tmp_path: Path):
    """Loading when the settings file does not exist returns defaults without creating the file."""
    absent = tmp_path / "settings.json"
    assert not absent.exists()

    settings = load_user_settings(absent)
    assert isinstance(settings, UserSettings)
    assert settings.version == SCHEMA_VERSION
    assert settings.connection.tunnel_id == ""
    assert settings.connection.base_url == ""
    assert settings.connection.profile_name == "harness-harbor"
    assert settings.connection.credential_ref == CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY
    assert settings.codex.routing_mode == "current"
    assert settings.codex.custom.enabled is False
    assert settings.codex.custom.profile_name == ""
    assert settings.codex.custom.base_url == ""
    assert settings.codex.custom.default_model == ""
    assert settings.codex.custom.credential_ref == CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY

    # Loading an absent file must NOT implicitly create or write it
    assert not absent.exists()


# ---------------------------------------------------------------------------
# Round-trip non-secret settings
# ---------------------------------------------------------------------------


def test_round_trip_non_secret_settings(tmp_path: Path):
    """Ensure non-secret fields round-trip cleanly through disk persistence."""
    target_path = tmp_path / "settings.json"

    original = UserSettings(
        version=SCHEMA_VERSION,
        connection=ConnectionSettings(
            tunnel_id="tun-prod-42",
            base_url="https://tunnel.example.com",
            profile_name="prod-profile",
            credential_ref="Harness-Harbor:tunnel:prod_ref",
        ),
        codex=CodexSettings(
            routing_mode="custom",
            custom=CodexCustomSettings(
                enabled=True,
                profile_name="custom-codex",
                base_url="https://api.custom-codex.com/v1",
                default_model="custom-model-x",
                credential_ref="Harness-Harbor:codex:custom_ref",
            ),
        ),
    )

    saved_path = save_user_settings(original, target_path)
    assert saved_path == target_path
    assert target_path.exists()

    loaded = load_user_settings(target_path)
    assert loaded.version == SCHEMA_VERSION
    assert loaded.connection.tunnel_id == "tun-prod-42"
    assert loaded.connection.base_url == "https://tunnel.example.com"
    assert loaded.connection.profile_name == "prod-profile"
    assert loaded.connection.credential_ref == "Harness-Harbor:tunnel:prod_ref"
    assert loaded.codex.routing_mode == "custom"
    assert loaded.codex.custom.enabled is True
    assert loaded.codex.custom.profile_name == "custom-codex"
    assert loaded.codex.custom.base_url == "https://api.custom-codex.com/v1"
    assert loaded.codex.custom.default_model == "custom-model-x"
    assert loaded.codex.custom.credential_ref == "Harness-Harbor:codex:custom_ref"


def test_unknown_fields_round_trip_when_known_fields_change():
    source = {
        "version": 1,
        "connection": {"tunnel_id": "old", "future": {"items": [1, {"label": "x"}]}},
        "codex": {
            "routing_mode": "current",
            "future": {"flag": True},
            "custom": {"enabled": False, "future": [{"kind": "x"}]},
        },
        "macos": {"start_at_launch": True, "future": ["x"]},
        "windows": {"future": {"nested": False}},
    }

    settings = UserSettings.from_dict(source)
    settings.connection.tunnel_id = "new"
    settings.codex.routing_mode = "custom"
    settings.codex.custom.enabled = True

    saved = settings.to_dict()
    assert saved["connection"]["future"] == source["connection"]["future"]
    assert saved["codex"]["future"] == source["codex"]["future"]
    assert saved["codex"]["custom"]["future"] == source["codex"]["custom"]["future"]
    assert saved["macos"] == source["macos"]
    assert saved["windows"] == source["windows"]
    assert saved["connection"]["tunnel_id"] == "new"
    assert saved["codex"]["routing_mode"] == "custom"
    assert saved["codex"]["custom"]["enabled"] is True


def test_unknown_fields_require_json_values():
    with pytest.raises(SettingsCorruptionError):
        UserSettings.from_dict({"future": object()})


# ---------------------------------------------------------------------------
# Atomic write & Corruption handling
# ---------------------------------------------------------------------------


def test_atomic_write_leaves_valid_file_and_no_temp_leftovers(tmp_path: Path):
    """save_user_settings writes atomically and cleans up any temp sibling files."""
    target_path = tmp_path / "subdir" / "settings.json"
    settings = UserSettings()

    save_user_settings(settings, target_path)
    assert target_path.exists()

    # Verify no .tmp files remained in the directory
    tmp_files = list(target_path.parent.glob("*.tmp"))
    assert tmp_files == []

    # Verify valid JSON
    data = json.loads(target_path.read_text(encoding="utf-8"))
    assert data["version"] == SCHEMA_VERSION


def test_corrupt_json_raises_error_without_overwriting(tmp_path: Path):
    """Malformed settings JSON raises SettingsCorruptionError and leaves the corrupt file untouched."""
    target_path = tmp_path / "settings.json"
    corrupt_content = '{"version": 1, "connection": {broken_json'
    target_path.write_text(corrupt_content, encoding="utf-8")

    with pytest.raises(SettingsCorruptionError) as exc_info:
        load_user_settings(target_path)

    assert "Malformed JSON" in str(exc_info.value)
    # The file on disk must NOT be modified or overwritten
    assert target_path.read_text(encoding="utf-8") == corrupt_content


def test_non_dict_root_raises_corruption_error_without_overwriting(tmp_path: Path):
    """A settings file whose root is not a JSON object raises SettingsCorruptionError."""
    target_path = tmp_path / "settings.json"
    raw_array = json.dumps([1, 2, 3])
    target_path.write_text(raw_array, encoding="utf-8")

    with pytest.raises(SettingsCorruptionError) as exc_info:
        load_user_settings(target_path)

    assert "must be a mapping" in str(exc_info.value)
    assert target_path.read_text(encoding="utf-8") == raw_array


# ---------------------------------------------------------------------------
# Schema handling & Versioning
# ---------------------------------------------------------------------------


def test_partial_schema_defaults_missing_sections(tmp_path: Path):
    """A settings file with only a version loads defaults for missing sections."""
    target_path = tmp_path / "settings.json"
    target_path.write_text(json.dumps({"version": 1}), encoding="utf-8")

    settings = load_user_settings(target_path)
    assert settings.version == 1
    assert settings.connection.profile_name == "harness-harbor"
    assert settings.codex.routing_mode == "current"
    assert settings.codex.custom.enabled is False


def test_unsupported_future_version_raises_version_error(tmp_path: Path):
    """A future schema version raises SettingsVersionError."""
    target_path = tmp_path / "settings.json"
    future_data = {"version": 999, "connection": {}}
    target_path.write_text(json.dumps(future_data), encoding="utf-8")

    with pytest.raises(SettingsVersionError) as exc_info:
        load_user_settings(target_path)

    assert "unsupported" in str(exc_info.value).lower()
    # File untouched
    assert json.loads(target_path.read_text(encoding="utf-8"))["version"] == 999


def test_invalid_version_type_raises_corruption_error(tmp_path: Path):
    """A non-integer or zero version raises SettingsCorruptionError."""
    target_path = tmp_path / "settings.json"
    target_path.write_text(json.dumps({"version": 0}), encoding="utf-8")

    with pytest.raises(SettingsCorruptionError):
        load_user_settings(target_path)

    with pytest.raises(SettingsCorruptionError):
        UserSettings.from_dict({"version": True})

    target_path.write_text(json.dumps({"version": "v1"}), encoding="utf-8")
    with pytest.raises(SettingsCorruptionError):
        load_user_settings(target_path)


# ---------------------------------------------------------------------------
# No secrets persisted
# ---------------------------------------------------------------------------


def test_settings_to_dict_contains_only_non_secret_fields():
    """UserSettings.to_dict must not include any raw secret keys."""
    settings = UserSettings()
    data = settings.to_dict()

    # Ensure no secret keys exist in the exported dict
    serialized_str = json.dumps(data)
    for forbidden in ["api_key", "secret", "token", "password"]:
        # Only credential_ref is allowed as a reference name, never a raw secret
        assert f'"{forbidden}"' not in serialized_str


def test_attempting_to_load_secret_keys_raises_safety_error(tmp_path: Path):
    """Attempting to load a file containing secret keys raises SettingsSecretPersistenceError."""
    target_path = tmp_path / "settings.json"
    data_with_secret = {
        "version": 1,
        "connection": {
            "api_key": "sk-should-never-be-here",
            "profile_name": "test",
        },
    }
    target_path.write_text(json.dumps(data_with_secret), encoding="utf-8")

    with pytest.raises(SettingsSecretPersistenceError) as exc_info:
        load_user_settings(target_path)

    assert "Forbidden secret key detected" in str(exc_info.value)


def test_secret_keys_nested_in_arrays_are_rejected():
    with pytest.raises(SettingsSecretPersistenceError):
        UserSettings.from_dict({"macos": {"profiles": [{"nested": [{"api_token": "secret"}]}]}})


def test_custom_enabled_rejects_string_false():
    with pytest.raises(SettingsCorruptionError):
        UserSettings.from_dict({"codex": {"custom": {"enabled": "false"}}})


@pytest.mark.parametrize("namespace", ["macos", "windows"])
def test_platform_namespace_must_be_mapping(namespace: str):
    with pytest.raises(SettingsCorruptionError):
        UserSettings.from_dict({namespace: []})


def test_user_settings_repr_is_safe():
    """UserSettings repr should only show modeled fields and no raw secrets."""
    settings = UserSettings()
    r = repr(settings)
    assert "UserSettings" in r
    assert "ConnectionSettings" in r
    assert "CodexSettings" in r
    for secret in ["sk-", "Bearer ", "password"]:
        assert secret not in r


# ---------------------------------------------------------------------------
# Path derivation: no hardcoded user identity
# ---------------------------------------------------------------------------


def test_path_derivation_respects_appdata(monkeypatch):
    """Path derivation must honor %APPDATA% and never hardcode a user identity."""
    custom_appdata = r"C:\Users\Example\AppData\Roaming"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("HARBOR_USER_SETTINGS_PATH", raising=False)
    monkeypatch.delenv("HARBOR_USER_SETTINGS_DIR", raising=False)
    monkeypatch.setenv("APPDATA", custom_appdata)

    settings_dir = get_user_settings_dir()
    settings_path = get_user_settings_path()

    assert settings_dir == Path(custom_appdata) / "Harness Harbor"
    assert settings_path == Path(custom_appdata) / "Harness Harbor" / "settings.json"
    assert "Example" in str(settings_path)


def test_path_derivation_respects_explicit_override(monkeypatch):
    """Path derivation respects HARBOR_USER_SETTINGS_PATH."""
    override = r"E:\custom_location\my_settings.json"
    monkeypatch.setenv("HARBOR_USER_SETTINGS_PATH", override)

    assert get_user_settings_path() == Path(override)
