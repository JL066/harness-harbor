"""Read the existing non-secret settings schema; never mutate it from IPC."""
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sys
from urllib.parse import urlsplit

from harbor_platform.paths import PlatformPaths
from harbor_platform.commands import resolve_executable, child_path

EXE_KEYS = {"codex": "HARBOR_CODEX_EXE", "agy": "HARBOR_AGY_EXE",
            "minimax": "HARBOR_MINIMAX_CLI_EXE", "tunnel": "HARBOR_TUNNEL_EXE"}
EXE_NAMES = {"codex": "codex", "agy": "agy", "minimax": "mcode", "tunnel": "tunnel-client"}
DEFAULT_TUNNEL_BASE_URL = "https://api.openai.com"
ROUTES = ("current", "official", "custom", "official_then_custom")


def requires_custom(settings):
    codex = settings["codex"]
    return codex["routing_mode"] in {"custom", "official_then_custom"} or codex["custom"]["enabled"]


def child_environment(settings, environ=None, *, tunnel=False):
    """Resolve non-secret settings without mutating the GUI's environment."""
    env = dict(os.environ if environ is None else environ)
    settings = parse_settings(settings)
    values = {"HARBOR_CODEX_DEFAULT_ROUTE": settings["codex"]["routing_mode"],
              "HARBOR_CODEX_CUSTOM_BASE_URL": settings["codex"]["custom"]["base_url"],
              "HARBOR_CODEX_CUSTOM_MODEL": settings["codex"]["custom"]["default_model"]}
    namespace = "windows" if sys.platform == "win32" else "macos"
    for name, value in settings.get(namespace, {}).get("executables", {}).items():
        if value:
            values[EXE_KEYS[name]] = value
    for key, value in values.items():
        env.setdefault(key, value)
    if tunnel:
        for key in ("CONTROL_PLANE_TUNNEL_ID", "CONTROL_PLANE_API_KEY", "TUNNEL_ID"):
            env.pop(key, None)
    else:
        env.pop("TUNNEL_RUNTIME_KEY", None)
    return env


def validate_settings(data, *, credentials=None, require_connection=False):
    """Authoritative acceptance for both shells; credentials are presence flags only."""
    settings = parse_settings(data)
    conn, custom = settings["connection"], settings["codex"]["custom"]
    if require_connection and not conn["tunnel_id"].strip():
        raise ValueError("Tunnel ID is required.")
    if requires_custom(settings) and not custom["base_url"].strip():
        raise ValueError("Custom provider base URL is required.")
    if custom["profile_name"] and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", custom["profile_name"]):
        raise ValueError("Custom provider profile name is invalid.")
    from launcher.credential_store import CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY, CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY
    if conn["credential_ref"] != CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY or custom["credential_ref"] != CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY:
        raise ValueError("Unsupported credential reference.")
    if credentials is not None:
        if not isinstance(credentials, dict) or set(credentials) != {"tunnel", "custom"} or any(type(v) is not bool for v in credentials.values()):
            raise ValueError("Credentials must contain presence flags only.")
        tunnel_required = require_connection or bool(conn["tunnel_id"].strip()) or conn["base_url"] != DEFAULT_TUNNEL_BASE_URL
        if tunnel_required and not credentials["tunnel"]:
            raise ValueError("Tunnel Runtime Key is required and must remain configured.")
        if requires_custom(settings) and not credentials["custom"]:
            raise ValueError("Custom API key is required and must remain configured.")
    return settings


def discover_executables():
    return {key: resolve_executable(name) for key, name in EXE_NAMES.items()}


def valid_url(value):
    try:
        url = urlsplit(value)
        return bool(url.hostname and not url.username and not url.password and not url.query and not url.fragment
                    and (url.scheme == "https" or (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"})))
    except ValueError:
        return False


def _packaged_mode():
    return getattr(sys, "frozen", False) or os.environ.get("HARBOR_RUNTIME_MODE", "").strip().lower() == "packaged"


def _validate_packaged_settings_path(paths, bundle=None):
    """Keep packaged shells on one canonical non-bundle settings file."""
    if not _packaged_mode():
        return
    configured = os.environ.get("HARBOR_USER_SETTINGS_PATH", "").strip()
    if not configured:
        return
    configured_path = Path(configured).expanduser().resolve()
    expected_path = (paths.application_support_dir() / "settings.json").resolve()
    if bundle is None and getattr(sys, "frozen", False):
        bundle = _bundle_root()
    if bundle is not None and _path_is_within(configured_path, bundle):
        raise ValueError("Settings file cannot be stored inside the runtime bundle; use HARBOR_USER_SETTINGS_DIR")
    if configured_path != expected_path:
        raise ValueError("Packaged runtime settings must use application support settings.json; use HARBOR_USER_SETTINGS_DIR")


def load_settings(paths):
    _validate_packaged_settings_path(paths)
    path = paths.application_support_dir() / "settings.json"
    if not path.exists():
        return parse_settings({})
    if path.stat().st_size > 65536:
        raise ValueError("Settings file exceeds size limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    return parse_settings(data)


def parse_settings(data):
    from launcher.user_settings import UserSettings
    settings = UserSettings.from_dict(data).to_dict()
    extensions = {}
    for namespace in ("macos", "windows"):
        extension = data.get(namespace, {})
        if not isinstance(extension, dict) or not isinstance(extension.get("executables", {}), dict):
            raise ValueError(f"Invalid {namespace} settings")
        extension = dict(extension)
        executables = dict(extension.get("executables", {}))
        # The former minimax entry described a desktop binary; only retain its CLI sibling.
        if "mcode" in executables:
            executables["minimax"] = executables.pop("mcode")
        extension["executables"] = executables
        for key in ("start_at_launch", "open_dashboard", "setup_complete"):
            if key in extension and type(extension[key]) is not bool:
                raise ValueError(f"Invalid {namespace} preference")
        extensions[namespace] = extension
    settings.update(extensions)
    if settings["codex"]["routing_mode"] == "direct":
        # Preserve the launcher's legacy setting while sharing the runtime's
        # route vocabulary with the macOS shell.
        settings["codex"]["routing_mode"] = "current"
    if settings["codex"]["routing_mode"] not in ROUTES:
        raise ValueError("Invalid routing mode")
    conn = settings["connection"]
    if not conn["base_url"].strip():
        conn["base_url"] = DEFAULT_TUNNEL_BASE_URL
    if conn["base_url"] and not valid_url(conn["base_url"]):
        raise ValueError("Invalid connection URL")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", conn["profile_name"]):
        raise ValueError("Invalid profile name")
    custom = settings["codex"]["custom"]
    if custom["base_url"] and not valid_url(custom["base_url"]):
        raise ValueError("Invalid provider URL")
    for namespace in ("macos", "windows"):
        extension = settings.get(namespace, {})
        commands = extension.get("executables", {})
        if not isinstance(commands, dict) or set(commands) - set(EXE_KEYS):
            raise ValueError("Unsupported executable settings")
        from pathlib import PureWindowsPath, PurePosixPath
        for name, value in commands.items():
            if not isinstance(value, str) or (value and not (PureWindowsPath(value).is_absolute() if namespace == "windows" else PurePosixPath(value).is_absolute())):
                raise ValueError("Executable override must be an absolute path")
            if name == "minimax" and value:
                basename = PureWindowsPath(value).name.casefold() if namespace == "windows" else PurePosixPath(value).name
                if basename not in {"mcode", "mcode.cmd", "mcode.bat"}:
                    raise ValueError("MiniMax requires the mcode CLI executable")
    return settings


def _default_bundle_root(executable):
    executable = Path(executable).expanduser().resolve()
    app = next((parent for parent in (executable, *executable.parents) if parent.suffix == ".app"), None)
    if app is not None:
        return app
    # The packaged Windows layout places harbor-runtime.exe below bundle/runtime.
    return executable.parent.parent if executable.parent.name.lower() == "runtime" else executable.parent


def _bundle_root():
    executable = Path(sys.executable).expanduser().resolve()
    default = _default_bundle_root(executable)
    override = os.environ.get("HARBOR_BUNDLE_ROOT", "").strip()
    if override:
        root = Path(override).expanduser()
        if not root.is_absolute():
            raise ValueError("HARBOR_BUNDLE_ROOT must be an absolute path")
        root = root.resolve()
        try:
            executable.relative_to(root)
        except ValueError:
            raise ValueError("HARBOR_BUNDLE_ROOT must contain the runtime executable") from None
        try:
            default.relative_to(root)
        except ValueError:
            raise ValueError("HARBOR_BUNDLE_ROOT must cover the entire runtime bundle") from None
        return root
    return default


def _path_is_within(path, root):
    try:
        Path(path).resolve().relative_to(root)
        return True
    except ValueError:
        return False


def configure():
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        os.environ.setdefault("HARBOR_RUNTIME_MODE", "packaged")
    paths = PlatformPaths()
    if getattr(sys, "frozen", False):
        bundle = _bundle_root()
        if any(_path_is_within(p, bundle) for p in paths.environment().values()):
            raise ValueError("Mutable state cannot be stored inside the runtime bundle")
    _validate_packaged_settings_path(paths, bundle=bundle if getattr(sys, "frozen", False) else None)
    os.environ.update(paths.environment())
    settings = load_settings(paths)
    overrides = settings.get("windows" if sys.platform == "win32" else "macos", {}).get("executables", {})
    found = {}
    for name, key in EXE_KEYS.items():
        override = os.environ.get(key) or overrides.get(name, "")
        if not isinstance(override, str) or (override and not Path(override).is_absolute()):
            raise ValueError("Executable override must be an absolute path")
        if name == "minimax" and override:
            basename = PureWindowsPath(override).name.casefold() if sys.platform == "win32" else Path(override).name
            if basename not in {"mcode", "mcode.cmd", "mcode.bat"}:
                raise ValueError("MiniMax requires the mcode CLI executable")
        exe = resolve_executable(EXE_NAMES[name], override)
        if exe:
            os.environ[key] = exe
        elif override:
            os.environ[key] = override  # explicit invalid override must not fall back
        found[name] = exe
    os.environ["PATH"] = child_path(found.values())
    custom = settings["codex"]["custom"]
    os.environ.setdefault("HARBOR_CODEX_DEFAULT_ROUTE", settings["codex"]["routing_mode"])
    for field, key in (("base_url", "HARBOR_CODEX_CUSTOM_BASE_URL"), ("default_model", "HARBOR_CODEX_CUSTOM_MODEL")):
        os.environ.setdefault(key, custom[field])
    return paths, settings, found


def runtime_command(mode, *args):
    if getattr(sys, "frozen", False):
        return [sys.executable, mode, *args]
    return [sys.executable, "-m", "harbor_runtime", mode, *args]
