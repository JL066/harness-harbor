"""Focused tests for the process-local generic Codex custom route."""

from pathlib import Path
from unittest import mock

import control_plane
import codex_job_worker
from launcher.diagnostics import redact_secrets
from launcher.user_settings import CodexCustomSettings, CodexSettings, UserSettings


def _settings(**kwargs):
    values = dict(
        enabled=True,
        profile_name="Acme Responses",
        base_url="https://api.acme.test/v1",
        default_model="acme-default",
        credential_ref="test:custom-key",
    )
    values.update(kwargs)
    custom = CodexCustomSettings(**values)
    return UserSettings(codex=CodexSettings(routing_mode="custom", custom=custom))


def test_custom_resolution_is_non_secret_and_uses_credential_store():
    with mock.patch.object(control_plane, "load_user_settings", return_value=_settings()), mock.patch.object(
        control_plane, "CredentialStore"
    ) as store_cls:
        store_cls.return_value.read.return_value = "sk-test-secret"
        resolved = control_plane.resolve_custom_codex_route(include_secret=True)

    assert resolved["provider_id"] == "harbor_custom"
    assert resolved["env_key"] == "HARBOR_CODEX_CUSTOM_API_KEY"
    assert resolved["base_url"] == "https://api.acme.test/v1"
    assert resolved["api_key"] == "sk-test-secret"
    assert "sk-test-secret" not in repr(control_plane.resolve_custom_codex_route(include_secret=False))


def test_custom_command_has_safe_dynamic_overrides_and_default_model():
    state = {"sandbox": "read-only", "cwd": ".", "prompt": "hello", "model": None}
    with mock.patch.object(control_plane, "load_user_settings", return_value=_settings()):
        argv = control_plane.build_codex_command(state, Path("result.txt"), route="custom")

    assert 'model_provider="harbor_custom"' in argv
    assert 'model_providers.harbor_custom.base_url="https://api.acme.test/v1"' in argv
    assert 'model_providers.harbor_custom.wire_api="responses"' in argv
    assert 'model_providers.harbor_custom.env_key="HARBOR_CODEX_CUSTOM_API_KEY"' in argv
    assert argv[argv.index("--model") + 1] == "acme-default"
    assert all("sk-test-secret" not in item for item in argv)


def test_explicit_model_wins_and_secret_is_child_only():
    state = {"sandbox": "read-only", "cwd": ".", "prompt": "hello", "model": "caller-model"}
    with mock.patch.object(control_plane, "load_user_settings", return_value=_settings()), mock.patch.object(
        control_plane, "CredentialStore"
    ) as store_cls:
        store_cls.return_value.read.return_value = "sk-test-secret"
        argv = control_plane.build_codex_command(state, Path("result.txt"), route="custom")
        child_env = control_plane.codex_child_environment("custom")

    assert argv[argv.index("--model") + 1] == "caller-model"
    assert child_env["HARBOR_CODEX_CUSTOM_API_KEY"] == "sk-test-secret"
    assert control_plane.CUSTOM_CODEX_ENV_KEY not in __import__("os").environ


def test_custom_missing_secret_and_invalid_base_url_fail_closed():
    with mock.patch.object(control_plane, "load_user_settings", return_value=_settings()), mock.patch.object(
        control_plane, "CredentialStore"
    ) as store_cls:
        store_cls.return_value.read.return_value = None
        assert control_plane.resolve_custom_codex_route(include_secret=True)["blocker"] == "custom_route_missing_credential"

    with mock.patch.object(control_plane, "load_user_settings", return_value=_settings(base_url="not-a-url")):
        assert control_plane.resolve_custom_codex_route()["blocker"] == "custom_route_invalid_base_url"


def test_start_task_custom_missing_secret_fails_before_job_creation(tmp_path):
    jobs = tmp_path / "jobs"
    with mock.patch.object(control_plane, "CODEX_EXE", tmp_path / "codex.exe"), mock.patch.object(
        control_plane, "JOBS_DIR", jobs
    ), mock.patch.object(control_plane, "load_user_settings", return_value=_settings()), mock.patch.object(
        control_plane, "CredentialStore"
    ) as store_cls:
        (tmp_path / "codex.exe").write_text("stub", encoding="utf-8")
        store_cls.return_value.read.return_value = None
        result = control_plane.start_task(
            harness="codex", prompt="hello", project=None, cwd=str(tmp_path),
            model=None, sandbox="read-only", reasoning_effort=None, route="custom",
        )
    assert result["ok"] is False
    assert result["blocker"] == "custom_route_missing_credential"
    assert not jobs.exists()


def test_legacy_codeflow_command_remains_unchanged():
    state = {"sandbox": "read-only", "cwd": ".", "prompt": "hello", "model": None}
    argv = control_plane.build_codex_command(state, Path("result.txt"), route="codeflow")
    assert 'model_provider="harbor_codeflow"' in argv
    assert 'model_providers.harbor_codeflow.base_url="https://codeflow.asia/v1"' in argv
    assert 'model_providers.harbor_codeflow.env_key="CODEFLOW_API_KEY"' in argv


def test_generated_env_name_is_redacted_by_existing_helper():
    text = "HARBOR_CODEX_CUSTOM_API_KEY=sk-test-secret"
    redacted = redact_secrets(text)
    assert "sk-test-secret" not in redacted
    assert "HARBOR_CODEX_CUSTOM_API_KEY" in redacted


def test_worker_passes_custom_key_only_as_subprocess_env(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "status.json").write_text(
        __import__("json").dumps(
            {
                "job_id": "j1", "harness": "codex", "status": "queued",
                "prompt": "hello", "cwd": str(tmp_path), "sandbox": "read-only",
                "route_requested": "custom", "model": None, "reasoning_effort": None,
            }
        ), encoding="utf-8",
    )
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        result_path = Path(argv[argv.index("-o") + 1])
        result_path.write_text("done", encoding="utf-8")
        return __import__("subprocess").CompletedProcess(argv, 0, stdout="", stderr="")

    with mock.patch.object(control_plane, "CODEX_EXE", tmp_path / "codex.exe"), mock.patch.object(
        codex_job_worker, "CODEX_CONFIG", tmp_path / "config.toml"
    ), mock.patch.object(control_plane, "load_user_settings", return_value=_settings()), mock.patch.object(
        codex_job_worker, "load_state", wraps=codex_job_worker.load_state
    ), mock.patch.object(control_plane, "CredentialStore") as store_cls, mock.patch.object(
        codex_job_worker, "run_codex_with_lifecycle", side_effect=fake_run
    ):
        (tmp_path / "codex.exe").write_text("stub", encoding="utf-8")
        store_cls.return_value.read.return_value = "sk-test-secret"
        codex_job_worker.main(job_dir)

    assert seen["env"]["HARBOR_CODEX_CUSTOM_API_KEY"] == "sk-test-secret"
    assert all("sk-test-secret" not in part for part in seen["argv"])
    state = __import__("json").loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert "sk-test-secret" not in repr(state)
