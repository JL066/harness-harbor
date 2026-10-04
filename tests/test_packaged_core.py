"""Packaged integration boundaries; no production processes or credentials."""
from pathlib import Path
from unittest.mock import Mock
import sys
import pytest
import control_plane as core
import codex_job_daemon as daemon

pytestmark = pytest.mark.windows_ci


def test_packaged_spawn_and_stop_use_owned_tree(monkeypatch):
    from harbor_platform import process
    monkeypatch.setenv('HARBOR_RUNTIME_MODE', 'packaged')
    child = Mock()
    spawn = Mock(return_value=child)
    stop = Mock(return_value=True)
    monkeypatch.setattr(process, 'spawn_owned', spawn)
    monkeypatch.setattr(process, 'terminate_tree', stop)
    assert core.spawn_runtime_child(['fixture']) is child
    core._escalate_terminate(child, ['fixture'])
    spawn.assert_called_once()
    stop.assert_called_once_with(child)
    monkeypatch.setattr(process, 'terminate_tree', lambda p: False)
    with pytest.raises(RuntimeError, match='Owned process tree'):
        core._escalate_terminate(child, ['fixture'])


def test_frozen_worker_uses_sidecar_mode(monkeypatch, tmp_path):
    monkeypatch.setenv('HARBOR_RUNTIME_MODE', 'packaged')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'runtime' / 'harbor-runtime.exe'))
    spawn = Mock()
    monkeypatch.setattr(daemon, 'spawn_runtime_child', spawn)
    scheduler = daemon.HarborScheduler(jobs_dir=tmp_path)
    scheduler.spawn_worker(tmp_path / 'job-1', 'codex')
    assert spawn.call_args.args[0] == [sys.executable, 'worker', str(tmp_path / 'job-1')]
    assert spawn.call_args.kwargs['owned'] is True


def test_snapshot_reads_only_selected_queue(tmp_path):
    import json
    selected = tmp_path / 'selected'
    job = selected / 'job-a'
    job.mkdir(parents=True)
    (job / 'status.json').write_text(json.dumps({'status': 'running', 'harness': 'codex'}))
    rows = core._harness_job_activity(selected)
    assert rows['codex']['running_job_ids'] == ['job-a']
    assert rows['agy']['running_job_ids'] == []


def test_packaged_custom_secret_uses_child_environment(monkeypatch):
    from launcher.user_settings import UserSettings
    settings = UserSettings()
    settings.codex.custom.enabled = True
    settings.codex.custom.profile_name = 'fixture'
    settings.codex.custom.base_url = 'https://provider.example/v1'
    monkeypatch.setattr(core, 'load_user_settings', lambda: settings)
    monkeypatch.setattr(core, 'CredentialStore', lambda: pytest.fail('runtime must not access real vault'))
    monkeypatch.setenv('HARBOR_RUNTIME_MODE', 'packaged')
    monkeypatch.setenv(core.CUSTOM_CODEX_ENV_KEY, 'fixture-only')
    assert core.resolve_custom_codex_route(include_secret=True)['api_key'] == 'fixture-only'


def test_packaged_cli_default_does_not_inherit_runtime_secrets(monkeypatch):
    from harbor_platform import process
    monkeypatch.setenv('HARBOR_RUNTIME_MODE', 'packaged')
    monkeypatch.setenv('TUNNEL_RUNTIME_KEY', 'fixture-tunnel')
    monkeypatch.setenv(core.CUSTOM_CODEX_ENV_KEY, 'fixture-custom')
    spawn = Mock()
    monkeypatch.setattr(process, 'spawn_owned', spawn)
    core.spawn_runtime_child(['fixture-cli'])
    env = spawn.call_args.kwargs['env']
    assert 'TUNNEL_RUNTIME_KEY' not in env
    assert core.CUSTOM_CODEX_ENV_KEY not in env
    core.spawn_runtime_child(['fixture-codex'], env={core.CUSTOM_CODEX_ENV_KEY: 'fixture-custom'})
    assert spawn.call_args.kwargs['env'][core.CUSTOM_CODEX_ENV_KEY] == 'fixture-custom'
