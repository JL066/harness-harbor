"""Per-user persisted settings abstraction for Harness Harbor Launcher.

This module models and manages non-secret user configuration persisted under
the user's APPDATA directory (typically ``%APPDATA%\\Harness Harbor\\settings.json``).

Key principles:
- Explicit schema and versioning (``SCHEMA_VERSION = 1``).
- Defensive loading: missing file returns default settings; malformed files
  raise :class:`SettingsCorruptionError` without overwriting the file on disk.
- Atomic writes: writes to a temporary file in the same directory and renames,
  preventing half-written or corrupted state.
- Strictly non-secret: secret keys, passwords, and tokens are NEVER stored in
  settings. Only safe configuration and credential reference keys (pointing to
  the secure :mod:`launcher.credential_store`) are persisted.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from launcher.credential_store import (
    CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY,
    CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY,
)

# ---------------------------------------------------------------------------
# Constants & Defaults
# ---------------------------------------------------------------------------

SCHEMA_VERSION: int = 1
DEFAULT_TUNNEL_PROFILE: str = "harness-harbor"
DEFAULT_CODEX_ROUTING_MODE: str = "current"

#: Forbidden key names that indicate an accidental attempt to persist a secret.
_FORBIDDEN_SECRET_KEY_PATTERNS = frozenset(
    {"api_key", "apikey", "secret", "token", "password", "passwd", "auth_token", "private_key"}
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SettingsError(Exception):
    """Base exception for user settings errors."""


class SettingsLoadError(SettingsError):
    """Raised when the settings file cannot be read from disk."""


class SettingsCorruptionError(SettingsError):
    """Raised when the settings file exists but contains malformed JSON or invalid schema."""


class SettingsVersionError(SettingsError):
    """Raised when the settings file version is unsupported."""


class SettingsSecretPersistenceError(SettingsError):
    """Raised when an attempt is made to persist a secret into user settings."""


# ---------------------------------------------------------------------------
# Models (Non-Secret)
# ---------------------------------------------------------------------------


@dataclass
class ConnectionSettings:
    """Non-secret connection configuration for tunnel management."""

    tunnel_id: str = ""
    base_url: str = ""
    profile_name: str = DEFAULT_TUNNEL_PROFILE
    credential_ref: str = CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY


@dataclass
class CodexCustomSettings:
    """Non-secret settings for custom Codex upstream routing."""

    enabled: bool = False
    profile_name: str = ""
    base_url: str = ""
    default_model: str = ""
    credential_ref: str = CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY


@dataclass
class CodexSettings:
    """Non-secret settings for Codex routing."""

    routing_mode: str = DEFAULT_CODEX_ROUTING_MODE
    custom: CodexCustomSettings = field(default_factory=CodexCustomSettings)


@dataclass
class UserSettings:
    """Root user settings container with explicit schema versioning."""

    version: int = SCHEMA_VERSION
    connection: ConnectionSettings = field(default_factory=ConnectionSettings)
    codex: CodexSettings = field(default_factory=CodexSettings)

    def to_dict(self) -> dict[str, Any]:
        """Convert settings to a serializable dictionary, verifying no secrets."""
        _assert_no_secrets(self)
        return {
            "version": self.version,
            "connection": asdict(self.connection),
            "codex": asdict(self.codex),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> UserSettings:
        """Construct UserSettings from a dictionary, defensively checking types and version."""
        if not isinstance(data, Mapping):
            raise SettingsCorruptionError(
                f"Settings root must be a mapping, got {type(data).__name__}"
            )

        _check_for_secret_keys(data)

        # Validate version
        raw_version = data.get("version", SCHEMA_VERSION)
        if not isinstance(raw_version, int) or raw_version < 1:
            raise SettingsCorruptionError(
                f"Invalid settings version: {raw_version!r} (expected positive integer)"
            )
        if raw_version > SCHEMA_VERSION:
            raise SettingsVersionError(
                f"Settings schema version {raw_version} is unsupported (max supported: {SCHEMA_VERSION})."
            )

        # Parse ConnectionSettings
        raw_conn = data.get("connection")
        if raw_conn is not None:
            if not isinstance(raw_conn, Mapping):
                raise SettingsCorruptionError(
                    f"'connection' must be a mapping, got {type(raw_conn).__name__}"
                )
            _check_for_secret_keys(raw_conn)
            conn = ConnectionSettings(
                tunnel_id=str(raw_conn.get("tunnel_id", "") or ""),
                base_url=str(raw_conn.get("base_url", "") or ""),
                profile_name=str(raw_conn.get("profile_name", DEFAULT_TUNNEL_PROFILE) or DEFAULT_TUNNEL_PROFILE),
                credential_ref=str(
                    raw_conn.get("credential_ref", CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY)
                    or CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY
                ),
            )
        else:
            conn = ConnectionSettings()

        # Parse CodexSettings
        raw_codex = data.get("codex")
        if raw_codex is not None:
            if not isinstance(raw_codex, Mapping):
                raise SettingsCorruptionError(
                    f"'codex' must be a mapping, got {type(raw_codex).__name__}"
                )
            _check_for_secret_keys(raw_codex)

            routing_mode = str(raw_codex.get("routing_mode", DEFAULT_CODEX_ROUTING_MODE) or DEFAULT_CODEX_ROUTING_MODE)

            raw_custom = raw_codex.get("custom")
            if raw_custom is not None:
                if not isinstance(raw_custom, Mapping):
                    raise SettingsCorruptionError(
                        f"'codex.custom' must be a mapping, got {type(raw_custom).__name__}"
                    )
                _check_for_secret_keys(raw_custom)
                custom = CodexCustomSettings(
                    enabled=bool(raw_custom.get("enabled", False)),
                    profile_name=str(raw_custom.get("profile_name", "") or ""),
                    base_url=str(raw_custom.get("base_url", "") or ""),
                    default_model=str(raw_custom.get("default_model", "") or ""),
                    credential_ref=str(
                        raw_custom.get("credential_ref", CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY)
                        or CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY
                    ),
                )
            else:
                custom = CodexCustomSettings()

            codex = CodexSettings(routing_mode=routing_mode, custom=custom)
        else:
            codex = CodexSettings()

        return cls(version=raw_version, connection=conn, codex=codex)


# ---------------------------------------------------------------------------
# Secret Safety Enforcement
# ---------------------------------------------------------------------------


def _check_for_secret_keys(data: Mapping[str, Any], prefix: str = "") -> None:
    """Recursively check for keys that suggest sensitive credentials."""
    for key, val in data.items():
        key_lower = str(key).lower()
        for forbidden in _FORBIDDEN_SECRET_KEY_PATTERNS:
            if forbidden in key_lower:
                path_str = f"{prefix}.{key}" if prefix else str(key)
                raise SettingsSecretPersistenceError(
                    f"Forbidden secret key detected in settings at '{path_str}'. "
                    "Secrets must NEVER be stored in settings; use CredentialStore instead."
                )
        if isinstance(val, Mapping):
            _check_for_secret_keys(val, prefix=f"{prefix}.{key}" if prefix else str(key))


def _assert_no_secrets(settings: UserSettings) -> None:
    """Double check that the UserSettings object only has non-secret references."""
    d = asdict(settings)
    _check_for_secret_keys(d)


# ---------------------------------------------------------------------------
# Path Derivation
# ---------------------------------------------------------------------------


def get_user_settings_dir() -> Path:
    """Return the directory housing user settings.

    Resolution:
    1. ``HARBOR_USER_SETTINGS_DIR`` environment variable (for testing/custom roots).
    2. ``%APPDATA%\\Harness Harbor`` on Windows.
    3. ``~/.config/Harness Harbor`` (or ``~/AppData/Roaming/Harness Harbor`` fallback).
    """
    override = os.environ.get("HARBOR_USER_SETTINGS_DIR")
    if override:
        return Path(override).expanduser()

    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Harness Harbor"

    if sys.platform == "win32":
        return Path.home() / "AppData" / "Roaming" / "Harness Harbor"
    return Path.home() / ".config" / "Harness Harbor"


def get_user_settings_path() -> Path:
    """Return the candidate path to ``settings.json``.

    Resolution:
    1. ``HARBOR_USER_SETTINGS_PATH`` environment variable.
    2. ``get_user_settings_dir() / "settings.json"``.
    """
    override = os.environ.get("HARBOR_USER_SETTINGS_PATH")
    if override:
        return Path(override).expanduser()
    return get_user_settings_dir() / "settings.json"


# ---------------------------------------------------------------------------
# Loading & Saving
# ---------------------------------------------------------------------------


def load_user_settings(path: Path | str | None = None) -> UserSettings:
    """Defensively load user settings from disk.

    Parameters
    ----------
    path:
        Optional path to load from. Defaults to :func:`get_user_settings_path`.

    Returns
    -------
    UserSettings:
        Loaded settings, or default settings if the file does not exist.

    Raises
    ------
    SettingsCorruptionError:
        If the file exists but contains invalid JSON or schema violations.
        The corrupt file on disk is NOT overwritten or modified.
    SettingsVersionError:
        If the file version is greater than :data:`SCHEMA_VERSION`.
    SettingsSecretPersistenceError:
        If the file contains secret keys.
    """
    target = Path(path).expanduser() if path is not None else get_user_settings_path()
    if not target.exists():
        return UserSettings()

    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsLoadError(f"Failed to read settings file at {target}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SettingsCorruptionError(
            f"Malformed JSON in settings file at {target}: {exc}"
        ) from exc

    return UserSettings.from_dict(data)


def save_user_settings(settings: UserSettings, path: Path | str | None = None) -> Path:
    """Atomically save user settings to disk.

    Parameters
    ----------
    settings:
        The :class:`UserSettings` instance to persist.
    path:
        Optional path to write to. Defaults to :func:`get_user_settings_path`.

    Returns
    -------
    Path:
        The resolved target path where settings were written.

    Raises
    ------
    SettingsSecretPersistenceError:
        If any secret fields are detected in the settings object.
    """
    target = Path(path).expanduser() if path is not None else get_user_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(settings.to_dict(), indent=2, ensure_ascii=False) + "\n"

    # Atomic write pattern: write to temporary sibling file in target.parent,
    # flush, fsync, then atomic replace. This avoids partial writes and guarantees
    # atomicity on NTFS/ext4 without crossing filesystems.
    temp_path = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        temp_path.replace(target)
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise

    return target


__all__ = [
    "CodexCustomSettings",
    "CodexSettings",
    "ConnectionSettings",
    "DEFAULT_CODEX_ROUTING_MODE",
    "DEFAULT_TUNNEL_PROFILE",
    "SCHEMA_VERSION",
    "SettingsCorruptionError",
    "SettingsError",
    "SettingsLoadError",
    "SettingsSecretPersistenceError",
    "SettingsVersionError",
    "UserSettings",
    "get_user_settings_dir",
    "get_user_settings_path",
    "load_user_settings",
    "save_user_settings",
]
