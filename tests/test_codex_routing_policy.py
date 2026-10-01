"""Focused unit tests for Codex default routing policy and role-based model/effort resolution."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

import control_plane
from control_plane import (
    CODEX_PRIMARY_DEFAULT_MODEL,
    CODEX_PRIMARY_DEFAULT_REASONING_EFFORT,
    CODEX_WORKER_DEFAULT_MODEL,
    CODEX_WORKER_DEFAULT_REASONING_EFFORT,
    build_agy_command,
    build_codex_command,
    build_minimax_command,
    is_effective_openai_provider,
    resolve_codex_model_and_effort,
    start_task,
)


def test_primary_codex_defaults_on_official_route():
    model, effort = resolve_codex_model_and_effort(role="primary", route="official")
    assert model == CODEX_PRIMARY_DEFAULT_MODEL == "gpt-6-sol"
    assert effort == CODEX_PRIMARY_DEFAULT_REASONING_EFFORT == "medium"


def test_worker_codex_defaults_on_official_route():
    model, effort = resolve_codex_model_and_effort(role="worker", route="official")
    assert model == CODEX_WORKER_DEFAULT_MODEL == "gpt-6-luna"
    assert effort == CODEX_WORKER_DEFAULT_REASONING_EFFORT == "max"


def test_current_route_with_openai_provider(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model_provider = "openai"\n', encoding="utf-8")
    assert is_effective_openai_provider("current", config_path=config) is True

    model, effort = resolve_codex_model_and_effort(role="primary", route="current", config_path=config)
    assert model == "gpt-6-sol"
    assert effort == "medium"

    w_model, w_effort = resolve_codex_model_and_effort(role="worker", route="current", config_path=config)
    assert w_model == "gpt-6-luna"
    assert w_effort == "max"


def test_current_route_with_custom_provider_does_not_inject_gpt6(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model_provider = "local_vllm"\n', encoding="utf-8")
    assert is_effective_openai_provider("current", config_path=config) is False

    model, effort = resolve_codex_model_and_effort(role="primary", route="current", config_path=config)
    assert model is None
    assert effort is None


def test_explicit_caller_overrides_always_win():
    # Both explicit
    model, effort = resolve_codex_model_and_effort(
        role="primary",
        route="official",
        model="custom-gpt-4o",
        reasoning_effort="low",
    )
    assert model == "custom-gpt-4o"
    assert effort == "low"

    # Explicit model only: do NOT guess/inject effort
    model, effort = resolve_codex_model_and_effort(
        role="worker",
        route="official",
        model="gpt-5-preview",
        reasoning_effort=None,
    )
    assert model == "gpt-5-preview"
    assert effort is None

    # Explicit effort only: preserve effort, apply role default model on OpenAI route
    model, effort = resolve_codex_model_and_effort(
        role="worker",
        route="official",
        model=None,
        reasoning_effort="high",
    )
    assert model == "gpt-6-luna"
    assert effort == "high"


def test_codeflow_omitted_model_and_effort_no_injection():
    assert is_effective_openai_provider("codeflow") is False
    model, effort = resolve_codex_model_and_effort(role="worker", route="codeflow")
    assert model is None
    assert effort is None


def test_custom_route_preserves_default_model(monkeypatch):
    monkeypatch.setattr(
        control_plane,
        "_custom_codex_route_definition",
        lambda include_secret=False: {"ok": True, "provider_id": "test", "default_model": "deepseek-coder"},
    )
    assert is_effective_openai_provider("custom") is False
    model, effort = resolve_codex_model_and_effort(role="worker", route="custom")
    assert model == "deepseek-coder"
    assert effort is None


def test_official_then_fallback_commands(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    result_path = job_dir / "result.txt"

    state = {
        "job_id": "test_job_1",
        "sandbox": "workspace-write",
        "cwd": str(tmp_path),
        "prompt": "fix issue",
        "route_requested": "official_then_codeflow",
        "codex_role": "worker",
        "model": None,
        "reasoning_effort": None,
    }

    # First attempt: official route receives worker role defaults
    cmd1 = build_codex_command(state, result_path, route="official")
    assert "--model" in cmd1
    assert cmd1[cmd1.index("--model") + 1] == "gpt-6-luna"
    assert any('model_reasoning_effort="max"' in part for part in cmd1)

    # Fallback attempt: codeflow route must NOT inherit auto-injected GPT-6 model/effort
    cmd2 = build_codex_command(state, result_path, route="codeflow")
    assert "--model" not in cmd2
    assert not any("model_reasoning_effort" in part for part in cmd2)


def test_start_task_sets_codex_role_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(control_plane, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(control_plane, "CODEX_EXE", tmp_path / "codex.exe")
    (tmp_path / "codex.exe").write_text("stub", encoding="utf-8")
    monkeypatch.setattr(control_plane, "is_stale_harbor_managed_codex_config", lambda: (False, None))

    res = start_task(
        harness="codex",
        prompt="run analysis",
        project=None,
        cwd=str(tmp_path),
        model=None,
        sandbox="workspace-write",
        reasoning_effort=None,
        route="official",
    )
    assert res.get("ok") is True
    job_id = res["job_id"]
    state = json.loads((tmp_path / job_id / "status.json").read_text(encoding="utf-8"))
    assert state.get("codex_role") == "worker"
    assert state.get("model") is None
    assert state.get("reasoning_effort") is None

    # When worker runs build_codex_command, it resolves to worker defaults
    cmd = build_codex_command(state, tmp_path / job_id / "result.txt")
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "gpt-6-luna"
    assert any('model_reasoning_effort="max"' in part for part in cmd)


def test_agy_and_minimax_unchanged(tmp_path):
    result_path = tmp_path / "result.txt"
    agy_state = {
        "job_id": "agy_1",
        "sandbox": "workspace-write",
        "cwd": str(tmp_path),
        "prompt": "gemini task",
        "agy_executable": "agy.cmd",
        "model": "gemini-2.5-pro",
        "reasoning_effort": "high",
    }
    agy_cmd = build_agy_command(agy_state, result_path)
    assert "gpt-6" not in " ".join(agy_cmd)

    mm_state = {
        "job_id": "mm_1",
        "sandbox": "workspace-write",
        "cwd": str(tmp_path),
        "prompt": "minimax task",
        "minimax_executable": "mcode.cmd",
    }
    mm_cmd = build_minimax_command(mm_state, result_path)
    assert "gpt-6" not in " ".join(mm_cmd)
