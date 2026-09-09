"""Focused tests for the process-local generic Codex custom route."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

import control_plane
import codex_job_worker
from launcher.diagnostics import redact_secrets


def _env(**overrides):
    values = {
        control_plane.CODEX_CUSTOM_BASE_URL_ENV: "https://api.acme.test/v1",
        control_plane.CODEX_CUSTOM_API_KEY_ENV: "TEST_TOKEN",
        control_plane.CODEX_CUSTOM_MODEL_ENV: "acme-default",
    }
    values.update(overrides)
    return values


def test_custom_resolution_is_non_secret_and_uses_generic_env(monkeypatch):
    monkeypatch.setenv(control_plane.CODEX_CUSTOM_BASE_URL_ENV, "https://api.acme.test/v1")
    monkeypatch.setenv(control_plane.CODEX_CUSTOM_API_KEY_ENV, "TEST_TOKEN")
    monkeypatch.setenv(control_plane.CODEX_CUSTOM_MODEL_ENV, "acme-default")

    resolved = control_plane.codex_custom_route_config(include_secret=True)

    assert resolved["provider_id"] == "harbor_custom"
    assert resolved["env_key"] == control_plane.CODEX_CUSTOM_API_KEY_ENV
    assert resolved["base_url"] == "https://api.acme.test/v1"
    assert resolved["api_key"] == "TEST_TOKEN"
    assert "TEST_TOKEN" not in repr(control_plane.codex_custom_route_config())


def test_custom_command_has_safe_dynamic_overrides_and_default_model(monkeypatch):
    monkeypatch.setenv(control_plane.CODEX_CUSTOM_BASE_URL_ENV, "https://api.acme.test/v1")
    monkeypatch.setenv(control_plane.CODEX_CUSTOM_API_KEY_ENV, "TEST_TOKEN")
    monkeypatch.setenv(control_plane.CODEX_CUSTOM_MODEL_ENV, "acme-default")
    state = {"sandbox": "read-only", "cwd": ".", "prompt": "hello", "model": None}

    argv = control_plane.build_codex_command(state, Path("result.txt"), route="custom")

    assert 'model_provider="harbor_custom"' in argv
    assert 'model_providers.harbor_custom.base_url="https://api.acme.test/v1"' in argv
    assert 'model_providers.harbor_custom.wire_api="responses"' in argv
    assert 'model_providers.harbor_custom.env_key="HARBOR_CODEX_CUSTOM_API_KEY"' in argv
    assert argv[argv.index("--model") + 1] == "acme-default"
    assert all("TEST_TOKEN" not in item for item in argv)


def test_explicit_model_wins_and_secret_is_child_only(monkeypatch):
    monkeypatch.setenv(control_plane.CODEX_CUSTOM_BASE_URL_ENV, "https://api.acme.test/v1")
    monkeypatch.setenv(control_plane.CODEX_CUSTOM_API_KEY_ENV, "TEST_TOKEN")
    state = {"sandbox": "read-only", "cwd": ".", "prompt": "hello", "model": "caller-model"}

    argv = control_plane.build_codex_command(state, Path("result.txt"), route="custom")
    child_env = control_plane.codex_process_environment("custom")

    assert argv[argv.index("--model") + 1] == "caller-model"
    assert child_env is not os.environ
    assert child_env[control_plane.CODEX_CUSTOM_API_KEY_ENV] == "TEST_TOKEN"
    assert os.environ.get(control_plane.CODEX_CUSTOM_API_KEY_ENV) == "TEST_TOKEN"


def test_custom_invalid_configuration_fails_closed(monkeypatch):
    monkeypatch.setenv(control_plane.CODEX_CUSTOM_BASE_URL_ENV, "not-a-url")
    monkeypatch.setenv(control_plane.CODEX_CUSTOM_API_KEY_ENV, "TEST_TOKEN")
    with pytest.raises(ValueError):
        control_plane.codex_custom_route_config()

    monkeypatch.setenv(control_plane.CODEX_CUSTOM_BASE_URL_ENV, "https://api.acme.test/v1")
    monkeypatch.delenv(control_plane.CODEX_CUSTOM_API_KEY_ENV)
    with pytest.raises(ValueError):
        control_plane.codex_custom_route_config(include_secret=True)


def test_start_task_custom_missing_secret_fails_before_job_creation(tmp_path, monkeypatch):
    jobs = tmp_path / "jobs"
    monkeypatch.setattr(control_plane, "CODEX_EXE", tmp_path / "codex.exe")
    monkeypatch.setattr(control_plane, "JOBS_DIR", jobs)
    (tmp_path / "codex.exe").write_text("stub", encoding="utf-8")
    monkeypatch.setenv(control_plane.CODEX_CUSTOM_BASE_URL_ENV, "https://api.acme.test/v1")
    monkeypatch.delenv(control_plane.CODEX_CUSTOM_API_KEY_ENV, raising=False)

    result = control_plane.start_task(
        harness="codex", prompt="hello", project=None, cwd=str(tmp_path),
        model=None, sandbox="read-only", reasoning_effort=None, route="custom",
    )

    assert result["ok"] is False
    assert "custom route requires" in result["error"]
    assert not jobs.exists()


def test_private_route_is_rejected():
    state = {"sandbox": "read-only", "cwd": ".", "prompt": "hello", "model": None}
    with pytest.raises(ValueError):
        control_plane.validate_codex_route("private")


def test_generated_env_name_is_redacted_by_existing_helper():
    redacted = redact_secrets("HARBOR_CODEX_CUSTOM_API_KEY=TEST_TOKEN")
    assert "TEST_TOKEN" not in redacted
    assert "HARBOR_CODEX_CUSTOM_API_KEY" in redacted


def test_worker_passes_custom_key_only_as_subprocess_env(tmp_path, monkeypatch):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "status.json").write_text(
        json.dumps({
            "job_id": "job-fixture", "harness": "codex", "status": "queued",
            "prompt": "hello", "cwd": str(tmp_path), "sandbox": "read-only",
            "route_requested": "custom", "model": None, "reasoning_effort": None,
        }), encoding="utf-8",
    )
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        result_path = Path(argv[argv.index("-o") + 1])
        result_path.write_text("done", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(control_plane, "CODEX_EXE", tmp_path / "codex.exe")
    monkeypatch.setenv(control_plane.CODEX_CUSTOM_BASE_URL_ENV, "https://api.acme.test/v1")
    monkeypatch.setenv(control_plane.CODEX_CUSTOM_API_KEY_ENV, "TEST_TOKEN")
    (tmp_path / "codex.exe").write_text("stub", encoding="utf-8")
    with mock.patch.object(codex_job_worker.subprocess, "run", side_effect=fake_run):
        codex_job_worker.main(job_dir)

    assert seen["env"][control_plane.CODEX_CUSTOM_API_KEY_ENV] == "TEST_TOKEN"
    assert all("TEST_TOKEN" not in part for part in seen["argv"])
    state = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert "TEST_TOKEN" not in repr(state)
