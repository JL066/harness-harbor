import io
import os
from unittest.mock import Mock, patch
import pytest
from launcher.runtime_client import BridgeClient
from launcher.user_settings import UserSettings
from launcher.credential_store import CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY, CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY

pytestmark = pytest.mark.windows_ci


def test_secrets_only_in_child_environment(monkeypatch, tmp_path):
    data = UserSettings.from_dict({'codex': {'routing_mode': 'custom', 'custom': {'enabled': True, 'profile_name': 'fixture', 'base_url': 'https://provider.example/v1'}}})
    monkeypatch.setattr('launcher.user_settings.load_user_settings', lambda: data)
    store = Mock()
    store.read.side_effect = lambda key: {CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY: 'fixture-tunnel', CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY: 'fixture-custom'}[key]
    parent = {'APPDATA': str(tmp_path / 'roaming'), 'LOCALAPPDATA': str(tmp_path / 'local'),
              'TUNNEL_RUNTIME_KEY': 'stale-tunnel', 'HARBOR_CODEX_CUSTOM_API_KEY': 'stale-custom',
              'HARBOR_PROCESS_REGISTRY': 'stale-registry'}
    with patch.dict(os.environ, parent, clear=True):
        env, secrets = BridgeClient(credential_store=store)._environment()
        assert dict(os.environ) == parent
    assert env['TUNNEL_RUNTIME_KEY'] == 'fixture-tunnel'
    assert env['HARBOR_CODEX_CUSTOM_API_KEY'] == 'fixture-custom'
    assert 'HARBOR_PROCESS_REGISTRY' not in env
    assert set(secrets) == {'fixture-tunnel', 'fixture-custom'}


def test_foreign_reference_never_reaches_vault(monkeypatch, tmp_path):
    data = UserSettings.from_dict({'connection': {'credential_ref': 'foreign-reference'}})
    monkeypatch.setattr('launcher.user_settings.load_user_settings', lambda: data)
    store = Mock()
    with patch.dict(os.environ, {'APPDATA': str(tmp_path), 'LOCALAPPDATA': str(tmp_path / 'local')}, clear=True):
        with pytest.raises(ValueError, match='credential reference'):
            BridgeClient(credential_store=store)._environment()
    store.read.assert_not_called()


def test_bridge_stderr_redacts_child_only_secret(tmp_path):
    proc = Mock(stderr=io.BytesIO(b'failure fixture-secret-value\n'))
    target = tmp_path / 'bridge.log'
    BridgeClient()._drain_errors(proc, target, ['fixture-secret-value'])
    text = target.read_text(encoding='utf-8')
    assert 'fixture-secret-value' not in text
    assert '[REDACTED]' in text


def test_closed_client_rejects_late_ui_poll(monkeypatch):
    from launcher.runtime_client import BridgeError
    client = BridgeClient()
    monkeypatch.setattr(client, '_environment', lambda: pytest.fail('late poll must not reconnect'))
    client.close()
    with pytest.raises(BridgeError, match='closed'):
        client.request('status.snapshot')
