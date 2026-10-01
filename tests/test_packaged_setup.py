"""Packaged setup accepts shared configuration without source profiles."""
import json
import pytest
from launcher.credential_store import CredentialStore, InMemoryCredentialBackend
from launcher.ui.setup_wizard import SettingsController, first_run_status
from launcher.user_settings import UserSettings, save_user_settings

pytestmark = pytest.mark.windows_ci


def draft():
    return {'connection': {'tunnel_id': 'fixture', 'base_url': 'https://api.openai.com', 'profile_name': 'fixture'},
            'codex': {'routing_mode': 'current', 'custom': {}},
            'windows': {'executables': {'codex': r'C:\Tools\codex.exe'}}}


def test_packaged_apply_never_creates_source_profile(monkeypatch, tmp_path):
    monkeypatch.setenv('HARBOR_RUNTIME_MODE', 'packaged')
    monkeypatch.setattr('launcher.ui.setup_wizard.TunnelProfileManager', lambda *a, **k: pytest.fail('no source profile'))
    path = tmp_path / 'settings.json'
    saved = draft()
    saved['macos'] = {'open_dashboard': True}
    save_user_settings(UserSettings.from_dict(saved), path)
    controller = SettingsController(credential_store=CredentialStore(backend=InMemoryCredentialBackend()), settings_path=path)
    controller.apply(draft(), tunnel_runtime_key='fixture-only')
    payload = json.loads(path.read_text(encoding='utf-8'))
    assert payload['macos']['open_dashboard'] is True
    assert payload['windows'] == saved['windows']
    assert list(tmp_path.iterdir()) == [path]
    assert 'fixture-only' not in path.read_text(encoding='utf-8')


def test_packaged_invalid_executable_fails_before_any_secret_write(monkeypatch, tmp_path):
    monkeypatch.setenv('HARBOR_RUNTIME_MODE', 'packaged')
    backend = InMemoryCredentialBackend()
    value = draft()
    value['windows']['executables']['codex'] = 'relative.exe'
    controller = SettingsController(credential_store=CredentialStore(backend=backend), settings_path=tmp_path / 'settings.json')
    with pytest.raises(ValueError, match='absolute'):
        controller.apply(value, tunnel_runtime_key='fixture-only')
    assert backend._secrets == {}
    assert list(tmp_path.iterdir()) == []


def test_packaged_first_run_never_detects_production(monkeypatch, tmp_path):
    monkeypatch.setenv('HARBOR_RUNTIME_MODE', 'packaged')
    status = first_run_status(settings_path=tmp_path / 'settings.json',
                              credential_store=CredentialStore(backend=InMemoryCredentialBackend()),
                              legacy_detector=lambda: pytest.fail('must not inspect legacy installation'))
    assert status.required


def test_packaged_foreign_credential_reference_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv('HARBOR_RUNTIME_MODE', 'packaged')
    value = draft()
    value['connection']['credential_ref'] = 'foreign-account'
    backend = InMemoryCredentialBackend()
    controller = SettingsController(credential_store=CredentialStore(backend=backend), settings_path=tmp_path / 'settings.json')
    with pytest.raises(ValueError, match='credential reference'):
        controller.apply(value, tunnel_runtime_key='fixture-only')
    assert backend._secrets == {}
