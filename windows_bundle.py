"""Validation and isolated smoke checks for a Windows Harbor bundle.

This module intentionally uses only the Python standard library.  It never
imports the launcher, starts a GUI, discovers host CLIs, or reads credentials.
The smoke check invokes only the two bundle-owned runtime commands and the
bundle-owned JSONL bridge.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid
from collections.abc import Mapping
from typing import Any

from harbor_runtime import PROTOCOL_VERSION as EXPECTED_PROTOCOL_VERSION
from harbor_runtime import RUNTIME_VERSION as EXPECTED_RUNTIME_VERSION


SMOKE_TIMEOUT_SECONDS = 20.0

LAUNCHER_RELATIVE = Path("harbor_launcher.exe")
RUNTIME_RELATIVE = Path("runtime") / "harbor-runtime.exe"

# Only the launcher and runtime sidecar are executable in the immutable
# bundle.  Host CLIs without an extension are listed separately so they are
# rejected even though the suffix check below cannot identify them.
_ALLOWED_EXECUTABLES = {
    LAUNCHER_RELATIVE.as_posix().lower(),
    RUNTIME_RELATIVE.as_posix().lower(),
}
_EXECUTABLE_SUFFIXES = {".exe", ".cmd", ".bat", ".com", ".ps1"}
_EXTERNAL_CLI_NAMES = {"codex", "agy", "mcode", "tunnel-client"}
_FORBIDDEN_DIRECTORY_NAMES = {
    ".git",
    ".venv",
    ".reference",
    ".jobs",
    ".control",
    ".codex",
    ".ssh",
    ".aws",
    ".config",
    "appdata",
    "localappdata",
    "users",
    "userprofile",
    "home",
    "account",
    "accounts",
    ".build-venv",
}
_FORBIDDEN_DIRECTORY_PREFIXES = (".venv-", ".reference-")
_FORBIDDEN_FILE_NAMES = {"settings.json", ".env"}
_REQUIRED_BRIDGE_METHODS = {
    "hello",
    "status.snapshot",
    "runtime.start",
    "runtime.stop",
    "runtime.restart",
    "harness.telemetry",
    "tunnel.test",
    "settings.validate",
    "logs.tail",
    "diagnostics.run",
    "shutdown",
}

# Keep only variables needed by a Windows process to start and by the runtime
# protocol.  In particular, do not inherit PATH, Python paths, account
# locations, or arbitrary application variables from the host.
_WINDOWS_ENV_KEYS = {
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "SYSTEMDRIVE",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_ARCHITEW6432",
    "NUMBER_OF_PROCESSORS",
    "OS",
}
_CLI_OVERRIDE_NAMES = {
    "HARBOR_CODEX_EXE": "codex",
    "HARBOR_AGY_EXE": "agy",
    "HARBOR_MINIMAX_CLI_EXE": "mcode.cmd",
    "HARBOR_TUNNEL_EXE": "tunnel-client",
}


class BundleValidationError(ValueError):
    """Raised when a bundle contains an unsafe or incomplete layout."""

    def __init__(self, errors: list[str] | tuple[str, ...], *, bundle: Path | None = None):
        self.errors = tuple(str(error) for error in errors)
        self.bundle = bundle
        detail = "; ".join(self.errors) or "unknown bundle validation error"
        super().__init__(f"Bundle validation failed: {detail}")


class BundleSmokeError(RuntimeError):
    """Raised when an isolated bundle smoke check fails."""


def _resolved(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _relative_key(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bundle_entries(root: Path) -> tuple[list[Path], list[str]]:
    entries: list[Path] = []
    errors: list[str] = []
    try:
        iterator = root.rglob("*")
        for path in sorted(iterator, key=lambda item: item.as_posix().lower()):
            rel = _relative_key(path, root)
            name = path.name.lower()
            if (name in _FORBIDDEN_DIRECTORY_NAMES or
                    any(name.startswith(prefix) for prefix in _FORBIDDEN_DIRECTORY_PREFIXES)):
                errors.append(f"account or development entry is not allowed: {rel}")
                continue
            if path.is_symlink():
                errors.append(f"symbolic link is not allowed: {rel}")
                continue
            if path.is_dir():
                continue
            if not path.is_file():
                errors.append(f"non-regular bundle entry is not allowed: {rel}")
                continue
            entries.append(path)
            if name in _FORBIDDEN_FILE_NAMES or name.startswith(".env."):
                errors.append(f"secret or mutable settings file is not allowed: {rel}")
            if path.suffix.lower() in _EXECUTABLE_SUFFIXES:
                normalized = rel.lower()
                if normalized not in _ALLOWED_EXECUTABLES:
                    errors.append(f"external CLI executable is not allowed: {rel}")
            elif name in _EXTERNAL_CLI_NAMES:
                errors.append(f"external CLI executable is not allowed: {rel}")
    except OSError as exc:
        errors.append(f"cannot enumerate bundle: {exc}")
    return entries, errors


def validate_bundle(bundle: Path) -> dict[str, Any]:
    """Validate a bundle and return a deterministic relative SHA256 manifest."""
    candidate_root = Path(bundle).expanduser()
    root = _resolved(candidate_root)
    errors: list[str] = []
    if candidate_root.is_symlink():
        errors.append("bundle root must not be a symbolic link")
    if not root.exists():
        errors.append(f"bundle does not exist: {root}")
    elif not root.is_dir():
        errors.append(f"bundle is not a directory: {root}")

    entries: list[Path] = []
    if not errors:
        entries, entry_errors = _bundle_entries(root)
        errors.extend(entry_errors)
        launcher = root / LAUNCHER_RELATIVE
        runtime = root / RUNTIME_RELATIVE
        for required, label in ((launcher, "harbor_launcher.exe"), (runtime, "runtime/harbor-runtime.exe")):
            if required.is_symlink():
                errors.append(f"required {label} must not be a symbolic link")
            elif not required.is_file():
                errors.append(f"required file is missing: {label}")

    if errors:
        raise BundleValidationError(errors, bundle=root)

    hashes: dict[str, str] = {}
    try:
        for path in entries:
            hashes[_relative_key(path, root)] = _sha256(path)
    except OSError as exc:
        raise BundleValidationError([f"cannot hash bundle: {exc}"], bundle=root) from None

    ordered_hashes = {key: hashes[key] for key in sorted(hashes, key=str.lower)}
    files = [{"path": key, "sha256": digest} for key, digest in ordered_hashes.items()]
    return {
        "valid": True,
        "bundle": str(root),
        "required": {
            "launcher": str((root / LAUNCHER_RELATIVE).relative_to(root).as_posix()),
            "runtime": str((root / RUNTIME_RELATIVE).relative_to(root).as_posix()),
        },
        "sha256": ordered_hashes,
        "files": files,
    }


def _isolated_environment(bundle: Path, smoke_root: Path) -> tuple[dict[str, str], dict[str, Path]]:
    env = {key: str(value) for key, value in os.environ.items() if key.upper() in _WINDOWS_ENV_KEYS}
    appdata = smoke_root / "appdata"
    localappdata = smoke_root / "localappdata"
    userprofile = smoke_root / "userprofile"
    home = smoke_root / "home"
    temp = smoke_root / "temp"
    state = smoke_root / "state"
    settings = smoke_root / "settings"
    jobs = state / "jobs"
    control = state / "control"
    logs = state / "logs"
    cache = state / "cache"
    tunnel = settings / "tunnel"
    mutable = {
        "APPDATA": appdata,
        "LOCALAPPDATA": localappdata,
        "USERPROFILE": userprofile,
        "HOME": home,
        "TMP": temp,
        "TEMP": temp,
        "HARBOR_USER_SETTINGS_DIR": settings,
        "HARBOR_STATE_DIR": state,
        "HARBOR_JOBS_DIR": jobs,
        "HARBOR_CONTROL_DIR": control,
        "HARBOR_LOG_DIR": logs,
        "HARBOR_CACHE_DIR": cache,
        "HARBOR_TUNNEL_PROFILE_DIR": tunnel,
    }
    for path in mutable.values():
        if _inside(path, bundle):
            raise BundleSmokeError(f"mutable smoke path is inside the bundle: {path}")
        path.mkdir(parents=True, exist_ok=True)

    env.update({key: str(path) for key, path in mutable.items()})
    env["PATH"] = str(smoke_root / "bin")
    (smoke_root / "bin").mkdir(parents=True, exist_ok=True)
    runtime = bundle / RUNTIME_RELATIVE
    env.update({
        "HARBOR_RUNTIME_MODE": "packaged",
        "HARBOR_BUNDLE_ROOT": str(bundle),
        "HARBOR_RUNTIME_EXE": str(runtime),
    })
    missing_cli_root = smoke_root / "missing-cli"
    for key, name in _CLI_OVERRIDE_NAMES.items():
        candidate = missing_cli_root / name
        if candidate.exists():
            raise BundleSmokeError(f"smoke CLI override unexpectedly exists: {candidate}")
        env[key] = str(candidate)

    # No inherited HARBOR/TUNNEL/CONTROL variable may leak a credential or a
    # legacy connection into the smoke process.  Re-apply only the explicit
    # non-secret packaged variables above.
    for key in list(env):
        upper = key.upper()
        if (upper.startswith("HARBOR_") or upper.startswith("TUNNEL_") or
                upper.startswith("CONTROL_")) and key not in {
                    "HARBOR_RUNTIME_MODE", "HARBOR_BUNDLE_ROOT", "HARBOR_RUNTIME_EXE",
                    *mutable.keys(), *(_CLI_OVERRIDE_NAMES.keys()),
                }:
            env.pop(key, None)
    return env, mutable


def _run_command(command: list[str], *, env: Mapping[str, str], cwd: Path):
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=SMOKE_TIMEOUT_SECONDS,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _json_from_output(output: str, label: str) -> dict[str, Any]:
    text = str(output or "").strip()
    if not text:
        raise BundleSmokeError(f"{label} did not return a JSON object")
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        raise BundleSmokeError(f"{label} did not return a JSON object") from None
    if not isinstance(parsed, dict):
        raise BundleSmokeError(f"{label} did not return a JSON object")
    return parsed


def _run_json(command: list[str], *, env: Mapping[str, str], cwd: Path, label: str) -> dict[str, Any]:
    try:
        result = _run_command(command, env=env, cwd=cwd)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BundleSmokeError(f"{label} could not start: {exc}") from None
    if result.returncode != 0:
        detail = str(result.stderr or "").strip()
        raise BundleSmokeError(f"{label} failed{': ' + detail if detail else ''}")
    return _json_from_output(str(result.stdout or ""), label)


def _validate_runtime_metadata(version: Mapping[str, Any], doctor: Mapping[str, Any]) -> None:
    for label, value in (("version", version), ("doctor", doctor)):
        if value.get("protocol_version") != EXPECTED_PROTOCOL_VERSION:
            raise BundleSmokeError(f"{label} protocol version is incompatible")
        if value.get("runtime_version") != EXPECTED_RUNTIME_VERSION:
            raise BundleSmokeError(f"{label} runtime version is incompatible")
    if version.get("runtime_version") != doctor.get("runtime_version"):
        raise BundleSmokeError("runtime version and doctor version differ")
    paths = doctor.get("paths")
    if not isinstance(paths, Mapping):
        raise BundleSmokeError("doctor did not report runtime paths")


def _validate_doctor_paths(doctor: Mapping[str, Any], bundle: Path, smoke_root: Path) -> dict[str, str]:
    paths = doctor.get("paths")
    if not isinstance(paths, Mapping):
        raise BundleSmokeError("doctor did not report runtime paths")
    required = {"state", "jobs", "control", "logs"}
    if not required.issubset(paths):
        raise BundleSmokeError("doctor did not report required runtime paths")
    checked: dict[str, str] = {}
    for key, value in paths.items():
        if not isinstance(value, str) or not value.strip():
            raise BundleSmokeError(f"doctor reported an invalid path for {key}")
        raw_path = Path(value).expanduser()
        if not raw_path.is_absolute():
            raise BundleSmokeError(f"doctor reported a non-absolute path for {key}")
        candidate = raw_path.resolve()
        if _inside(candidate, bundle):
            raise BundleSmokeError(f"doctor mutable path is inside the bundle: {key}")
        if not _inside(candidate, smoke_root):
            raise BundleSmokeError(f"doctor mutable path escaped the smoke root: {key}")
        checked[str(key)] = str(candidate)
    return checked


def _request(method: str, request_id: str) -> str:
    return json.dumps({"v": EXPECTED_PROTOCOL_VERSION, "id": request_id,
                       "method": method, "params": {}}, ensure_ascii=True)


def _validate_bridge_response(response: Mapping[str, Any], request_id: str, label: str) -> Mapping[str, Any]:
    if (response.get("v") != EXPECTED_PROTOCOL_VERSION or response.get("id") != request_id or
            response.get("ok") is not True or not isinstance(response.get("result"), Mapping)):
        raise BundleSmokeError(f"bridge {label} response is invalid")
    return response["result"]


def _bridge_smoke(runtime: Path, *, env: Mapping[str, str], cwd: Path, version: Mapping[str, Any]) -> dict[str, Any]:
    hello_id = uuid.uuid4().hex
    shutdown_id = uuid.uuid4().hex
    payload = _request("hello", hello_id) + "\n" + _request("shutdown", shutdown_id) + "\n"
    try:
        process = subprocess.Popen(
            [str(runtime), "bridge"],
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            stdout, stderr = process.communicate(payload, timeout=SMOKE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise BundleSmokeError("bridge smoke timed out") from None
    except BundleSmokeError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise BundleSmokeError(f"bridge could not start: {exc}") from None

    if process.returncode not in (0, None):
        detail = str(stderr or "").strip()
        raise BundleSmokeError(f"bridge exited with status {process.returncode}{': ' + detail if detail else ''}")
    responses: list[Mapping[str, Any]] = []
    for line in str(stdout or "").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            raise BundleSmokeError("bridge returned invalid JSONL") from None
        if not isinstance(value, Mapping):
            raise BundleSmokeError("bridge returned a non-object JSONL response")
        responses.append(value)
    if len(responses) != 2:
        raise BundleSmokeError("bridge did not return hello and shutdown responses")
    hello = _validate_bridge_response(responses[0], hello_id, "hello")
    shutdown = _validate_bridge_response(responses[1], shutdown_id, "shutdown")
    if hello.get("protocol_version") != EXPECTED_PROTOCOL_VERSION:
        raise BundleSmokeError("bridge hello protocol version is incompatible")
    if hello.get("runtime_version") != version.get("runtime_version"):
        raise BundleSmokeError("bridge hello runtime version differs from version")
    capabilities = hello.get("capabilities")
    if not isinstance(capabilities, list) or not _REQUIRED_BRIDGE_METHODS.issubset(set(capabilities)):
        raise BundleSmokeError("bridge hello capabilities are incomplete")
    if shutdown.get("state") != "stopped":
        raise BundleSmokeError("bridge shutdown did not stop runtime")
    return {"hello": dict(hello), "shutdown": dict(shutdown)}


def smoke_bundle(bundle: Path, smoke_root: Path) -> dict[str, Any]:
    """Run version, doctor, and hello/shutdown checks in an isolated tree."""
    report = validate_bundle(bundle)
    root = _resolved(bundle)
    smoke = Path(smoke_root).expanduser()
    if smoke.is_symlink():
        raise BundleSmokeError("smoke root must not be a symbolic link")
    smoke = smoke.resolve()
    if _inside(smoke, root):
        raise BundleSmokeError("smoke root must be outside the immutable bundle")
    smoke.mkdir(parents=True, exist_ok=True)
    env, mutable = _isolated_environment(root, smoke)
    runtime = root / RUNTIME_RELATIVE

    version = _run_json([str(runtime), "version"], env=env, cwd=smoke, label="runtime version")
    doctor = _run_json([str(runtime), "doctor"], env=env, cwd=smoke, label="runtime doctor")
    _validate_runtime_metadata(version, doctor)
    doctor_paths = _validate_doctor_paths(doctor, root, smoke)
    bridge = _bridge_smoke(runtime, env=env, cwd=smoke, version=version)

    after = validate_bundle(root)
    if after["sha256"] != report["sha256"]:
        raise BundleSmokeError("bundle hash manifest changed during smoke")
    return {
        "valid": True,
        "bundle": str(root),
        "smoke_root": str(smoke),
        "sha256": report["sha256"],
        "hashes_unchanged": True,
        "environment_keys": sorted(env),
        "mutable_paths": {key: str(value) for key, value in mutable.items()},
        "runtime": {
            "version": version,
            "doctor": doctor,
            "doctor_paths": doctor_paths,
            "bridge": bridge,
        },
    }


__all__ = [
    "BundleSmokeError",
    "BundleValidationError",
    "EXPECTED_PROTOCOL_VERSION",
    "EXPECTED_RUNTIME_VERSION",
    "RUNTIME_RELATIVE",
    "LAUNCHER_RELATIVE",
    "smoke_bundle",
    "validate_bundle",
]
