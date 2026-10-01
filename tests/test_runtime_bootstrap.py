"""Focused tests for the packaged runtime boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime_bootstrap import (
    BUNDLE_ROOT_ENV,
    RUNTIME_EXECUTABLE_ENV,
    RUNTIME_MODE_ENV,
    RuntimeBootstrapError,
    bootstrap,
    runtime_executable,
)


def _make_sidecar(bundle: Path) -> tuple[Path, Path]:
    executable = bundle / "Harbor Launcher.exe"
    sidecar = bundle / "runtime" / "harbor-runtime.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"launcher")
    sidecar.parent.mkdir()
    sidecar.write_bytes(b"runtime")
    return executable, sidecar


def test_source_defaults_to_legacy_without_mutations(tmp_path: Path):
    env = {
        "SENTINEL": "before",
        RUNTIME_EXECUTABLE_ENV: str(tmp_path / "missing-runtime.exe"),
    }
    before = dict(env)

    assert bootstrap(env, frozen=False, executable=tmp_path / "missing-launcher.py") is None
    assert env == before


def test_invalid_mode_fails_closed_without_mutations(tmp_path: Path):
    env = {RUNTIME_MODE_ENV: "unsupported", "SENTINEL": "before"}
    before = dict(env)

    with pytest.raises(RuntimeBootstrapError, match="expected 'packaged' or 'legacy'"):
        bootstrap(env, frozen=False, executable=tmp_path / "launcher.py")

    assert env == before


def test_frozen_packaged_bootstrap_handles_spaces_unicode_and_sets_paths(tmp_path: Path):
    bundle = tmp_path / "安装 Launcher with spaces"
    executable, sidecar = _make_sidecar(bundle)
    mutable_root = tmp_path / "用户 mutable data"
    values = {
        "HARBOR_USER_SETTINGS_DIR": str(mutable_root / "settings"),
        "HARBOR_STATE_DIR": str(mutable_root / "state"),
        "HARBOR_JOBS_DIR": str(mutable_root / "jobs"),
        "HARBOR_CONTROL_DIR": str(mutable_root / "control"),
        "HARBOR_LOG_DIR": str(mutable_root / "logs"),
        "HARBOR_CACHE_DIR": str(mutable_root / "cache"),
        "HARBOR_TUNNEL_PROFILE_DIR": str(mutable_root / "tunnel"),
    }
    env = {
        "APPDATA": str(mutable_root / "appdata"),
        "LOCALAPPDATA": str(mutable_root / "localappdata"),
        "SENTINEL": "before",
    }
    env.update(values)

    result = bootstrap(env, frozen=True, executable=executable)

    assert result == sidecar.resolve()
    assert env[RUNTIME_MODE_ENV] == "packaged"
    assert Path(env[BUNDLE_ROOT_ENV]) == bundle.resolve()
    assert Path(env[RUNTIME_EXECUTABLE_ENV]) == sidecar.resolve()
    for key, value in values.items():
        assert env[key] == value
    assert env["SENTINEL"] == "before"


def test_source_packaged_accepts_absolute_external_runtime_exe(tmp_path: Path):
    launcher = tmp_path / "Source Launcher" / "run launcher.py"
    launcher.parent.mkdir()
    launcher.write_text("# source", encoding="utf-8")
    external = tmp_path / "Runtime 世界" / "harbor runtime.exe"
    external.parent.mkdir()
    external.write_bytes(b"runtime")
    env = {
        RUNTIME_MODE_ENV: "packaged",
        RUNTIME_EXECUTABLE_ENV: str(external),
        "HARBOR_USER_SETTINGS_DIR": str(tmp_path / "settings"),
    }

    result = bootstrap(env, frozen=False, executable=launcher)

    assert result == external.resolve()
    assert Path(env[RUNTIME_EXECUTABLE_ENV]) == external.resolve()
    assert Path(env[BUNDLE_ROOT_ENV]) == launcher.parent.resolve()


def test_frozen_rejects_external_runtime_override_without_mutation(tmp_path: Path):
    executable, _ = _make_sidecar(tmp_path / "Launcher")
    external = tmp_path / "external" / "harbor-runtime.exe"
    external.parent.mkdir()
    external.write_bytes(b"runtime")
    env = {
        RUNTIME_MODE_ENV: "packaged",
        RUNTIME_EXECUTABLE_ENV: str(external),
        "SENTINEL": "before",
    }
    before = dict(env)

    with pytest.raises(RuntimeBootstrapError, match="cannot override"):
        runtime_executable(env, frozen=True, executable=executable)

    assert env == before


def test_frozen_accepts_canonical_runtime_override_and_bootstrap_is_idempotent(tmp_path: Path):
    executable, sidecar = _make_sidecar(tmp_path / "Launcher")
    env = {RUNTIME_MODE_ENV: "packaged"}

    first = bootstrap(env, frozen=True, executable=executable)
    second = bootstrap(env, frozen=True, executable=executable)

    assert first == sidecar.resolve()
    assert second == first
    assert runtime_executable(env, frozen=True, executable=executable) == sidecar.resolve()


def test_frozen_explicit_legacy_mode_is_a_noop(tmp_path: Path):
    env = {RUNTIME_MODE_ENV: "legacy", "SENTINEL": "before"}
    before = dict(env)

    assert bootstrap(env, frozen=True, executable=tmp_path / "missing-launcher.exe") is None
    assert env == before


def test_missing_frozen_sidecar_fails_without_fallback_or_mutation(tmp_path: Path):
    bundle = tmp_path / "Launcher"
    executable = bundle / "harbor_launcher.exe"
    bundle.mkdir()
    executable.write_bytes(b"launcher")
    env = {RUNTIME_MODE_ENV: "packaged", "SENTINEL": "before"}
    before = dict(env)

    with pytest.raises(RuntimeBootstrapError, match="sidecar is missing"):
        bootstrap(env, frozen=True, executable=executable)

    assert env == before


def test_mutable_path_inside_entire_bundle_is_rejected_atomically(tmp_path: Path):
    bundle = tmp_path / "Launcher"
    executable, sidecar = _make_sidecar(bundle)
    env = {
        RUNTIME_MODE_ENV: "packaged",
        "APPDATA": str(tmp_path / "safe-appdata"),
        "LOCALAPPDATA": str(bundle),
        "SENTINEL": "before",
    }
    before = dict(env)

    with pytest.raises(RuntimeBootstrapError, match="immutable launcher bundle"):
        bootstrap(env, frozen=True, executable=executable)

    assert env == before
    assert sidecar.exists()
