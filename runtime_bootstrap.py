"""Small, dependency-free boundary for the packaged Harbor runtime.

The launcher can import this module before importing any ``launcher`` package
code.  Source checkouts remain in the legacy mode unless packaged mode is
explicitly requested; frozen launchers default to the bundled runtime sidecar
while an explicit legacy mode remains available for compatibility.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any


RUNTIME_MODE_ENV = "HARBOR_RUNTIME_MODE"
RUNTIME_EXECUTABLE_ENV = "HARBOR_RUNTIME_EXE"
BUNDLE_ROOT_ENV = "HARBOR_BUNDLE_ROOT"
SIDECAR_RELATIVE_PATH = Path("runtime") / "harbor-runtime.exe"
_VALID_MODES = frozenset({"packaged", "legacy"})

# PlatformPaths currently owns these mutable locations.  Keep the list here so
# every value supplied by the platform layer and every explicit user override
# is checked before any real environment is changed.
_MUTABLE_PATH_KEYS = frozenset(
    {
        "HARBOR_USER_SETTINGS_DIR",
        "HARBOR_USER_SETTINGS_PATH",
        "HARBOR_STATE_DIR",
        "HARBOR_JOBS_DIR",
        "HARBOR_CONTROL_DIR",
        "HARBOR_LOG_DIR",
        "HARBOR_CACHE_DIR",
        "HARBOR_TUNNEL_PROFILE_DIR",
    }
)


class RuntimeBootstrapError(RuntimeError):
    """Raised when packaged runtime setup cannot be completed safely."""


def _frozen_value(frozen: bool | None) -> bool:
    if frozen is None:
        return bool(getattr(sys, "frozen", False))
    return bool(frozen)


def _environment_snapshot(environ: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(os.environ if environ is None else environ)


def _resolve_mode(environ: Mapping[str, Any], frozen: bool) -> str:
    raw_mode = environ.get(RUNTIME_MODE_ENV)
    if raw_mode is None or not str(raw_mode).strip():
        mode = "packaged" if frozen else "legacy"
    else:
        mode = str(raw_mode).strip().lower()
        if mode not in _VALID_MODES:
            raise RuntimeBootstrapError(
                f"Unsupported {RUNTIME_MODE_ENV}={raw_mode!r}; expected 'packaged' or 'legacy'."
            )

    return mode


def _bundle_root(frozen: bool, executable: str | os.PathLike[str] | None) -> Path:
    if frozen:
        executable_path = executable if executable is not None else sys.executable
    elif executable is not None:
        executable_path = executable
    else:
        executable_path = __file__

    if not str(executable_path).strip():
        raise RuntimeBootstrapError("Cannot determine the launcher executable path.")
    return Path(executable_path).expanduser().resolve().parent


def _runtime_path(
    environ: Mapping[str, Any],
    *,
    frozen: bool,
    bundle_root: Path,
) -> Path:
    override = str(environ.get(RUNTIME_EXECUTABLE_ENV, "") or "").strip()
    canonical = (bundle_root / SIDECAR_RELATIVE_PATH).resolve()
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            raise RuntimeBootstrapError(f"{RUNTIME_EXECUTABLE_ENV} must be an absolute path.")
        candidate = candidate.resolve()
        if frozen and candidate != canonical:
            raise RuntimeBootstrapError(
                f"{RUNTIME_EXECUTABLE_ENV} cannot override the frozen runtime sidecar."
            )
    else:
        candidate = canonical

    if not candidate.is_file():
        raise RuntimeBootstrapError(f"Harbor runtime sidecar is missing: {candidate}")
    return candidate


def _validate_mutable_paths(environ: Mapping[str, Any], bundle_root: Path) -> None:
    resolved_bundle = bundle_root.resolve()
    for key in _MUTABLE_PATH_KEYS:
        raw_value = environ.get(key)
        if raw_value is None or not str(raw_value).strip():
            continue
        candidate = Path(str(raw_value)).expanduser()
        if not candidate.is_absolute():
            raise RuntimeBootstrapError(f"{key} must be an absolute path in packaged mode.")
        resolved_candidate = candidate.resolve()
        if resolved_candidate == resolved_bundle or resolved_bundle in resolved_candidate.parents:
            raise RuntimeBootstrapError(
                f"{key} points inside the immutable launcher bundle: {resolved_candidate}"
            )


def _platform_environment(
    environ: Mapping[str, Any],
    *,
    bundle_root: Path,
) -> dict[str, str]:
    try:
        from harbor_platform.paths import PlatformPaths
    except ImportError as exc:
        raise RuntimeBootstrapError(
            "Packaged mode requires harbor_platform.paths.PlatformPaths."
        ) from exc

    try:
        values = PlatformPaths(environ=environ).environment()
    except Exception as exc:
        raise RuntimeBootstrapError(f"Unable to resolve packaged mutable paths: {exc}") from exc
    if not isinstance(values, Mapping):
        raise RuntimeBootstrapError("PlatformPaths.environment() must return a mapping.")

    normalized = {str(key): str(value) for key, value in values.items()}
    candidate = dict(environ)
    candidate.update(normalized)
    _validate_mutable_paths(candidate, bundle_root)
    return normalized


def runtime_executable(
    environ: Mapping[str, Any] | None = None,
    frozen: bool | None = None,
    executable: str | os.PathLike[str] | None = None,
) -> Path:
    """Return the packaged runtime executable, rejecting missing sidecars."""
    environment = _environment_snapshot(environ)
    frozen_value = _frozen_value(frozen)
    mode = _resolve_mode(environment, frozen_value)
    if mode != "packaged":
        raise RuntimeBootstrapError("Legacy mode has no packaged runtime executable.")
    root = _bundle_root(frozen_value, executable)
    return _runtime_path(environment, frozen=frozen_value, bundle_root=root)


def bootstrap(
    environ: MutableMapping[str, Any] | None = None,
    frozen: bool | None = None,
    executable: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Configure packaged mode and return its runtime executable.

    ``environ`` is mutated only after all sidecar and mutable-path checks have
    succeeded.  Legacy mode returns immediately without reading or writing
    filesystem state and without importing launcher or platform code.
    """
    target = os.environ if environ is None else environ
    if not isinstance(target, MutableMapping):
        raise TypeError("environ must be a mutable mapping")

    environment = dict(target)
    frozen_value = _frozen_value(frozen)
    mode = _resolve_mode(environment, frozen_value)
    if mode == "legacy":
        return None

    root = _bundle_root(frozen_value, executable)
    _validate_mutable_paths(environment, root)
    runtime = _runtime_path(environment, frozen=frozen_value, bundle_root=root)
    platform_input = dict(environment)
    platform_input[RUNTIME_MODE_ENV] = "packaged"
    platform_input[BUNDLE_ROOT_ENV] = str(root)
    platform_input[RUNTIME_EXECUTABLE_ENV] = str(runtime)
    platform_values = _platform_environment(platform_input, bundle_root=root)
    settings_override = environment.get("HARBOR_USER_SETTINGS_PATH")
    if settings_override and Path(settings_override).expanduser().resolve() != (Path(platform_values["HARBOR_USER_SETTINGS_DIR"]) / "settings.json").resolve():
        raise RuntimeBootstrapError("Packaged settings use settings.json; configure HARBOR_USER_SETTINGS_DIR instead.")

    updates = dict(platform_values)
    updates[RUNTIME_MODE_ENV] = "packaged"
    updates[BUNDLE_ROOT_ENV] = str(root)
    updates[RUNTIME_EXECUTABLE_ENV] = str(runtime)
    target.update(updates)
    return runtime


__all__ = [
    "BUNDLE_ROOT_ENV",
    "RUNTIME_EXECUTABLE_ENV",
    "RUNTIME_MODE_ENV",
    "RuntimeBootstrapError",
    "SIDECAR_RELATIVE_PATH",
    "bootstrap",
    "runtime_executable",
]
