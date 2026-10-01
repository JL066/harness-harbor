"""Pure mock checks for the Windows bundle validator and smoke boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import windows_bundle as wb


pytestmark = pytest.mark.windows_ci


def _make_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "Harness Harbor Bundle 测试"
    runtime = bundle / "runtime"
    runtime.mkdir(parents=True)
    (bundle / "harbor_launcher.exe").write_bytes(b"launcher-fixture")
    (runtime / "harbor-runtime.exe").write_bytes(b"runtime-fixture")
    (bundle / "resources.txt").write_text("safe", encoding="utf-8")
    return bundle


def _metadata(*, runtime_version=wb.EXPECTED_RUNTIME_VERSION):
    return {
        "protocol_version": wb.EXPECTED_PROTOCOL_VERSION,
        "runtime_version": runtime_version,
        "build_version": "test",
        "capabilities": sorted({
            "hello", "status.snapshot", "runtime.start", "runtime.stop", "runtime.restart",
            "harness.telemetry", "tunnel.test", "settings.validate", "logs.tail",
            "diagnostics.run", "shutdown",
        }),
    }


def test_validate_bundle_returns_stable_relative_sha256_manifest(tmp_path: Path):
    bundle = _make_bundle(tmp_path)
    report = wb.validate_bundle(bundle)

    assert report["valid"] is True
    assert report["required"] == {"launcher": "harbor_launcher.exe", "runtime": "runtime/harbor-runtime.exe"}
    assert list(report["sha256"]) == sorted(report["sha256"], key=str.lower)
    assert report["sha256"]["harbor_launcher.exe"] == hashlib.sha256(b"launcher-fixture").hexdigest()
    assert all("\\" not in item["path"] for item in report["files"])


@pytest.mark.parametrize("entry", [".git", ".venv", ".reference", ".venv-legacy", ".reference-macos-main", ".build-venv", ".jobs", ".control", "Users", "AppData", "account"])
def test_validate_bundle_rejects_account_and_development_directories(tmp_path: Path, entry: str):
    bundle = _make_bundle(tmp_path)
    (bundle / entry).mkdir()
    with pytest.raises(wb.BundleValidationError, match="not allowed"):
        wb.validate_bundle(bundle)


@pytest.mark.parametrize("name", [".env", ".env.local", "settings.json", "codex.exe", "agy.exe", "mcode.cmd", "tunnel-client.exe", "helper.bat"])
def test_validate_bundle_rejects_mutable_or_external_executable_files(tmp_path: Path, name: str):
    bundle = _make_bundle(tmp_path)
    target = bundle / "extra" / name
    target.parent.mkdir()
    target.write_bytes(b"forbidden")
    with pytest.raises(wb.BundleValidationError, match="not allowed"):
        wb.validate_bundle(bundle)


def test_validate_bundle_rejects_symlink_entries_and_root(tmp_path: Path):
    bundle = _make_bundle(tmp_path)
    target = tmp_path / "outside.bin"
    target.write_bytes(b"outside")
    link = bundle / "link.bin"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows runner")
    with pytest.raises(wb.BundleValidationError, match="symbolic link"):
        wb.validate_bundle(bundle)

    linked_root = tmp_path / "linked-bundle"
    try:
        linked_root.symlink_to(bundle, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable on this Windows runner")
    with pytest.raises(wb.BundleValidationError, match="symbolic link"):
        wb.validate_bundle(linked_root)


class _FakeBridgeProcess:
    def __init__(self, calls, *, shutdown_state="stopped", **kwargs):
        self.calls = calls
        self.kwargs = kwargs
        self.shutdown_state = shutdown_state
        self.returncode = 0
        self.killed = False

    def communicate(self, payload, timeout=None):
        requests = [json.loads(line) for line in payload.splitlines() if line.strip()]
        capabilities = sorted(wb._REQUIRED_BRIDGE_METHODS)
        responses = []
        for request in requests:
            if request["method"] == "hello":
                result = {**_metadata(), "capabilities": capabilities}
            else:
                result = {"state": self.shutdown_state}
            responses.append(json.dumps({"v": wb.EXPECTED_PROTOCOL_VERSION, "id": request["id"], "ok": True, "result": result}))
        return "\n".join(responses) + "\n", ""

    def kill(self):
        self.killed = True
        self.returncode = -9


class _FakeRuntimeCommands:
    def __init__(self):
        self.commands = []
        self.bridge = []

    def run(self, command, *, env, cwd):
        self.commands.append((command, dict(env), cwd))
        if command[-1] == "version":
            payload = _metadata()
        elif command[-1] == "doctor":
            state = Path(cwd) / "state"
            payload = {**_metadata(), "paths": {
                "state": str(state), "jobs": str(state / "jobs"),
                "control": str(state / "control"), "logs": str(state / "logs"),
            }}
        else:
            raise AssertionError(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")


def test_smoke_bundle_isolates_environment_and_runs_only_owned_runtime(tmp_path: Path, monkeypatch):
    bundle = _make_bundle(tmp_path)
    smoke_root = tmp_path / "smoke root 空格"
    commands = _FakeRuntimeCommands()
    monkeypatch.setattr(wb, "_run_command", commands.run)
    bridge_processes = []
    monkeypatch.setattr(wb.subprocess, "Popen", lambda command, **kwargs: bridge_processes.append(_FakeBridgeProcess(bridge_processes, **kwargs)) or bridge_processes[-1])
    monkeypatch.setenv("HARBOR_CODEX_API_KEY", "should-not-leak")
    monkeypatch.setenv("TUNNEL_RUNTIME_KEY", "should-not-leak")
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "should-not-leak")
    monkeypatch.setenv("PATH", "host-path-with-cli")
    system_root = str(tmp_path / "windows-system")
    monkeypatch.setenv("SYSTEMROOT", system_root)

    report = wb.smoke_bundle(bundle, smoke_root)

    assert report["valid"] is True
    assert report["hashes_unchanged"] is True
    assert [Path(command[0][-1]).name if command[0][-1] in {"version", "doctor"} else command[0][-1] for command in commands.commands] == ["version", "doctor"]
    assert len(bridge_processes) == 1
    env = bridge_processes[0].kwargs["env"]
    assert env["APPDATA"] == str(smoke_root / "appdata")
    assert env["LOCALAPPDATA"] == str(smoke_root / "localappdata")
    assert env["USERPROFILE"] == str(smoke_root / "userprofile")
    assert env["HOME"] == str(smoke_root / "home")
    assert env["TMP"] == env["TEMP"] == str(smoke_root / "temp")
    assert env["PATH"] == str(smoke_root / "bin")
    assert any(key.upper() == "SYSTEMROOT" and value == system_root for key, value in env.items())
    assert "HARBOR_CODEX_API_KEY" not in env
    assert "TUNNEL_RUNTIME_KEY" not in env
    assert "CONTROL_PLANE_API_KEY" not in env
    assert all(not Path(value).resolve().is_relative_to(bundle.resolve()) for key, value in report["mutable_paths"].items())
    assert not (bundle / "settings.json").exists()

    version_env = commands.commands[0][1]
    assert Path(version_env["HARBOR_CODEX_EXE"]).name == "codex"
    assert Path(version_env["HARBOR_AGY_EXE"]).name == "agy"
    assert Path(version_env["HARBOR_MINIMAX_CLI_EXE"]).name == "mcode.cmd"
    assert Path(version_env["HARBOR_TUNNEL_EXE"]).name == "tunnel-client"
    assert all(not Path(version_env[key]).exists() for key in ("HARBOR_CODEX_EXE", "HARBOR_AGY_EXE", "HARBOR_MINIMAX_CLI_EXE", "HARBOR_TUNNEL_EXE"))


def test_smoke_bundle_rejects_mutable_root_inside_bundle(tmp_path: Path, monkeypatch):
    bundle = _make_bundle(tmp_path)
    with pytest.raises(wb.BundleSmokeError, match="outside the immutable bundle"):
        wb.smoke_bundle(bundle, bundle / "smoke")


def test_smoke_bundle_rejects_runtime_version_mismatch(tmp_path: Path, monkeypatch):
    bundle = _make_bundle(tmp_path)
    commands = _FakeRuntimeCommands()
    original = commands.run

    def mismatched(command, *, env, cwd):
        result = original(command, env=env, cwd=cwd)
        if command[-1] == "version":
            result.stdout = json.dumps(_metadata(runtime_version="0.0.0"))
        return result

    monkeypatch.setattr(wb, "_run_command", mismatched)
    with pytest.raises(wb.BundleSmokeError, match="runtime version is incompatible"):
        wb.smoke_bundle(bundle, tmp_path / "smoke")

def test_json_from_output_requires_one_json_document():
    assert wb._json_from_output('{"ok": true}', "runtime version") == {"ok": True}
    with pytest.raises(wb.BundleSmokeError, match="did not return a JSON object"):
        wb._json_from_output('diagnostic noise\n{"ok": true}', "runtime version")
    with pytest.raises(wb.BundleSmokeError, match="did not return a JSON object"):
        wb._json_from_output('{"ok": true}\n{"second": true}', "runtime doctor")


def test_smoke_bundle_rejects_incomplete_doctor_paths(tmp_path: Path, monkeypatch):
    bundle = _make_bundle(tmp_path)

    def run(command, *, env, cwd):
        if command[-1] == "version":
            payload = _metadata()
        else:
            state = Path(cwd) / "state"
            payload = {**_metadata(), "paths": {
                "state": str(state), "jobs": str(state / "jobs"),
                "control": str(state / "control"),
            }}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(wb, "_run_command", run)
    with pytest.raises(wb.BundleSmokeError, match="required runtime paths"):
        wb.smoke_bundle(bundle, tmp_path / "smoke")


def test_smoke_bundle_rejects_nonabsolute_doctor_path(tmp_path: Path, monkeypatch):
    bundle = _make_bundle(tmp_path)

    def run(command, *, env, cwd):
        if command[-1] == "version":
            payload = _metadata()
        else:
            state = Path(cwd) / "state"
            payload = {**_metadata(), "paths": {
                "state": "state", "jobs": str(state / "jobs"),
                "control": str(state / "control"), "logs": str(state / "logs"),
            }}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(wb, "_run_command", run)
    with pytest.raises(wb.BundleSmokeError, match="non-absolute path"):
        wb.smoke_bundle(bundle, tmp_path / "smoke")


def test_bridge_smoke_requires_stopped_shutdown(tmp_path: Path, monkeypatch):
    process = _FakeBridgeProcess([], shutdown_state="running")
    monkeypatch.setattr(wb.subprocess, "Popen", lambda command, **kwargs: process)

    with pytest.raises(wb.BundleSmokeError, match="shutdown did not stop runtime"):
        wb._bridge_smoke(
            tmp_path / "harbor-runtime.exe",
            env={},
            cwd=tmp_path,
            version=_metadata(),
        )
