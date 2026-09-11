"""Managed tunnel profile rendering and process lifecycle.

The managed path is deliberately separate from the legacy Scheduled Task
supervisor.  It keeps credentials in the OS credential store and passes the
runtime key only to the child tunnel process environment.
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from harbor_platform.commands import serialize_command

from launcher.config import PRODUCTION_PATH, TUNNEL_EXE, TUNNEL_PROFILE_DIR, VENV_PYTHONW
from launcher.credential_store import CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY, CredentialStore
from launcher.user_settings import UserSettings


class TunnelConfigurationError(ValueError):
    """Raised for invalid non-secret tunnel configuration."""


class TunnelCredentialError(RuntimeError):
    """Raised when the runtime credential cannot be read."""


def _safe_name(value: str) -> bool:
    return bool(value) and value == value.strip() and all(c.isalnum() or c in "-_." for c in value)


@dataclass(frozen=True)
class TestConnectionResult:
    ok: bool
    checks: Mapping[str, bool]
    message: str


class TunnelProfileManager:
    """Validate and atomically render a per-user Harbor tunnel profile."""

    def __init__(
        self,
        settings: UserSettings,
        *,
        profile_path: Path | str | None = None,
        runtime_path: Path | str | None = None,
        python_path: Path | str | None = None,
    ) -> None:
        self.settings = settings
        conn = settings.connection
        self.profile_name = conn.profile_name or "harness-harbor"
        self.runtime_path = Path(runtime_path) if runtime_path is not None else Path(PRODUCTION_PATH)
        runtime_root = self.runtime_path.parent if self.runtime_path.suffix.lower() == ".py" else self.runtime_path
        # tunnel-client launches the windowless legacy interpreter.  Resolve
        # this from the configured runtime (or the central setting) rather
        # than embedding a machine-specific path in the profile renderer.
        self.python_path = Path(python_path) if python_path is not None else (
            runtime_root / ".venv-legacy" / "Scripts" / "pythonw.exe"
            if runtime_path is not None
            else Path(VENV_PYTHONW)
        )
        self.profile_path = Path(profile_path) if profile_path is not None else (
            Path(TUNNEL_PROFILE_DIR) / f"{self.profile_name}.yaml"
        )

    def validate(self) -> None:
        from harbor_runtime.config import validate_settings
        from launcher.user_settings import SettingsError
        try:
            self.settings = UserSettings.from_dict(validate_settings(self.settings.to_dict(), require_connection=True))
        except (ValueError, SettingsError) as exc:
            raise TunnelConfigurationError(str(exc)) from exc
        if self.runtime_path.suffix.lower() == ".py" and self.runtime_path.name.lower() != "server_legacy.py":
            raise TunnelConfigurationError("Invalid Harbor runtime entrypoint.")

    @property
    def mcp_entrypoint(self) -> Path:
        return self.runtime_path if self.runtime_path.name.lower() == "server_legacy.py" else self.runtime_path / "server_legacy.py"

    def render(self) -> str:
        self.validate()
        conn = self.settings.connection
        state_root = self._user_state_root()
        health_override = os.environ.get("HARBOR_TUNNEL_HEALTH_URL_FILE")
        health_file = Path(health_override).expanduser() if health_override else state_root / "health" / f"{self.profile_name}.url"
        log_file = state_root / "logs" / f"{self.profile_name}.log"
        # mcp.commands.command is a single Windows command-line string;
        # quote path arguments when needed (e.g. a user profile containing
        # spaces) while retaining the live profile's two-argument semantics.
        command = serialize_command([str(self.python_path), str(self.mcp_entrypoint)])
        profile: dict[str, Any] = {
            "admin_ui": {"open_browser": False},
            "config_version": 1,
            "control_plane": {
                "api_key": "env:TUNNEL_RUNTIME_KEY",
                "base_url": conn.base_url,
                "tunnel_id": conn.tunnel_id,
            },
            "health": {"listen_addr": "127.0.0.1:0", "url_file": str(health_file)},
            "log": {"file": str(log_file), "format": "json", "level": "info"},
            "mcp": {"commands": [{"channel": "main", "command": command}]},
        }
        rendered = json.dumps(profile, indent=2, ensure_ascii=False) + "\n"
        self._validate_rendered_profile(profile)
        return rendered

    def _user_state_root(self) -> Path:
        override = os.environ.get("HARBOR_TUNNEL_HEALTH_URL_FILE")
        if override:
            return Path(override).expanduser().parent.parent
        user_root = Path(os.environ.get("USERPROFILE", str(Path.home()))).expanduser()
        return user_root / ".local" / "state" / "tunnel-client"

    @staticmethod
    def _validate_rendered_profile(profile: Mapping[str, Any]) -> None:
        expected = {"admin_ui", "config_version", "control_plane", "health", "log", "mcp"}
        if not isinstance(profile, Mapping) or set(profile) != expected or profile.get("config_version") != 1:
            raise TunnelConfigurationError("Rendered tunnel profile has an invalid schema.")
        if any(key in profile for key in ("tunnel", "runtime_key")):
            raise TunnelConfigurationError("Rendered tunnel profile uses an unsupported key.")
        if not isinstance(profile["admin_ui"], Mapping) or profile["admin_ui"] != {"open_browser": False}:
            raise TunnelConfigurationError("Invalid admin_ui configuration.")
        control = profile["control_plane"]
        if not isinstance(control, Mapping) or set(control) != {"api_key", "base_url", "tunnel_id"} or control["api_key"] != "env:TUNNEL_RUNTIME_KEY":
            raise TunnelConfigurationError("Invalid control_plane configuration.")
        health = profile["health"]
        if not isinstance(health, Mapping) or set(health) != {"listen_addr", "url_file"} or health["listen_addr"] != "127.0.0.1:0":
            raise TunnelConfigurationError("Invalid health configuration.")
        log = profile["log"]
        if not isinstance(log, Mapping) or set(log) != {"file", "format", "level"} or log["format"] != "json" or log["level"] != "info":
            raise TunnelConfigurationError("Invalid log configuration.")
        mcp = profile["mcp"]
        commands = mcp.get("commands") if isinstance(mcp, Mapping) else None
        if not isinstance(mcp, Mapping) or set(mcp) != {"commands"} or not isinstance(commands, list) or len(commands) != 1:
            raise TunnelConfigurationError("Invalid mcp.commands configuration.")
        command = commands[0]
        if not isinstance(command, Mapping) or set(command) != {"channel", "command"} or command["channel"] != "main":
            raise TunnelConfigurationError("Invalid MCP command configuration.")
        if not isinstance(command["command"], str) or "server_legacy.py" not in command["command"] or "server.py" in command["command"].replace("server_legacy.py", ""):
            raise TunnelConfigurationError("Invalid Harbor MCP entrypoint.")
        if "mcp.command" in profile or "mcp.env" in profile:
            raise TunnelConfigurationError("Unsupported MCP schema.")

    # Explicit aliases make the backend convenient for callers that prefer a
    # verb describing the operation.
    render_profile = render

    def write_profile(self) -> Path:
        rendered = self.render()  # validate/render before touching existing file
        try:
            self._validate_rendered_profile(json.loads(rendered))
        except (json.JSONDecodeError, TypeError, TunnelConfigurationError) as exc:
            raise TunnelConfigurationError("Rendered tunnel profile failed semantic validation.") from exc
        target = self.profile_path.expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
        try:
            with open(temp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        except Exception:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return target

    write = write_profile


class ManagedTunnelSupervisor:
    """Launch one managed tunnel-client child with an ephemeral environment."""

    def __init__(
        self,
        profile_manager: TunnelProfileManager,
        credential_store: CredentialStore,
        *,
        tunnel_executable: Path | str | None = None,
        popen_factory: Any = subprocess.Popen,
    ) -> None:
        self.profile_manager = profile_manager
        self.credential_store = credential_store
        self.tunnel_executable = Path(tunnel_executable) if tunnel_executable is not None else Path(TUNNEL_EXE)
        self._popen_factory = popen_factory
        self._process: Any = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> Any:
        if self.running:
            return self._process
        try:
            runtime_key = self.credential_store.read(self.profile_manager.settings.connection.credential_ref or CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY)
        except Exception as exc:
            raise TunnelCredentialError("Tunnel runtime credential is unavailable.") from exc
        if not runtime_key:
            raise TunnelCredentialError("Tunnel runtime credential is unavailable.")
        profile = self.profile_manager.write_profile()
        from harbor_runtime.config import child_environment
        env = child_environment(self.profile_manager.settings.to_dict(), tunnel=True)
        env["TUNNEL_RUNTIME_KEY"] = runtime_key
        cmd = [str(self.tunnel_executable), "run", "--profile-dir", str(profile.parent), "--profile", self.profile_manager.profile_name]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            from harbor_platform.process import spawn_owned
            self._process = spawn_owned(cmd, env=env, close_fds=True, creationflags=flags,
                                        popen_factory=self._popen_factory)
        except Exception:
            self._process = None
            raise
        return self._process

    def test_connection(self) -> TestConnectionResult:
        return test_connection(
            self.profile_manager.settings,
            self.credential_store,
            profile_manager=self.profile_manager,
            tunnel_executable=self.tunnel_executable,
        )

    def stop(self, timeout: float = 5.0) -> bool:
        proc = self._process
        if proc is None:
            return True
        from harbor_platform.process import terminate_tree
        stopped = terminate_tree(proc, grace=timeout)
        if stopped:
            self._process = None
        return stopped


def test_connection(
    settings: UserSettings,
    credential_store: CredentialStore,
    *,
    profile_manager: TunnelProfileManager | None = None,
    tunnel_executable: Path | str | None = None,
) -> TestConnectionResult:
    """Perform read-only validation; never starts a process or submits work."""
    checks: dict[str, bool] = {}
    try:
        manager = profile_manager or TunnelProfileManager(settings)
        manager.validate()
        manager.render()
        checks["settings"] = True
        checks["profile_renderable"] = True
    except Exception:
        checks["settings"] = False
        checks["profile_renderable"] = False
        return TestConnectionResult(False, checks, "Tunnel settings are invalid.")
    try:
        key = credential_store.read(settings.connection.credential_ref or CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY)
        checks["credential_readable"] = bool(key)
    except Exception:
        checks["credential_readable"] = False
    exe = Path(tunnel_executable) if tunnel_executable is not None else Path(TUNNEL_EXE)
    checks["tunnel_executable"] = exe.exists()
    ok = all(checks.values())
    return TestConnectionResult(ok, checks, "Tunnel connection configuration is valid." if ok else "Tunnel connection checks failed.")


__all__ = [
    "ManagedTunnelSupervisor",
    "TestConnectionResult",
    "TunnelConfigurationError",
    "TunnelCredentialError",
    "TunnelProfileManager",
    "test_connection",
]
