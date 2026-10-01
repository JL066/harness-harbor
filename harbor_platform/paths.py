from __future__ import annotations

import os
import sys
from pathlib import Path


class PlatformPaths:
    def __init__(self, project_root=None, environ=None, platform=None):
        self.root = Path(project_root or Path(__file__).resolve().parents[1])
        self.env = os.environ if environ is None else environ
        self.platform = platform or sys.platform

    def _packaged_windows(self):
        return self.platform == "win32" and self.env.get("HARBOR_RUNTIME_MODE", "").strip().lower() == "packaged"

    def _appdata_root(self):
        value = self.env.get("APPDATA", "").strip()
        return Path(value) if value else Path.home() / ("AppData/Roaming" if self._packaged_windows() else ".config")

    def _localappdata_root(self):
        value = self.env.get("LOCALAPPDATA", "").strip()
        return Path(value) if value else Path.home() / "AppData/Local"

    def _path(self, key, default):
        value = self.env.get(key, "").strip()
        path = Path(value).expanduser() if value else Path(default)
        if not path.is_absolute():
            raise ValueError(f"{key} must be an absolute path")
        return path.resolve()

    def application_support_dir(self):
        default = (Path.home() / "Library/Application Support/Harness Harbor"
                   if self.platform == "darwin" else
                   self._appdata_root() / "Harness Harbor")
        return self._path("HARBOR_USER_SETTINGS_DIR", default)

    def state_dir(self):
        if self.platform == "darwin":
            default = self.application_support_dir() / "state"
        elif self._packaged_windows():
            default = self._localappdata_root() / "Harness Harbor"
        else:
            default = self.root
        return self._path("HARBOR_STATE_DIR", default)

    def jobs_dir(self):
        if self.env.get("HARBOR_JOBS_DIR", "").strip():
            return self._path("HARBOR_JOBS_DIR", self.root / ".jobs")
        default = (self.state_dir() / "jobs"
                   if self.platform == "darwin" or self._packaged_windows()
                   or self.env.get("HARBOR_STATE_DIR", "").strip()
                   else self.root / ".jobs")
        return self._path("HARBOR_JOBS_DIR", default)

    def control_dir(self):
        default = (self.state_dir() / "control"
                   if self.platform == "darwin" or self._packaged_windows()
                   or self.env.get("HARBOR_STATE_DIR", "").strip()
                   else self.root / ".control")
        return self._path("HARBOR_CONTROL_DIR", default)

    def logs_dir(self):
        default = (Path.home() / "Library/Logs/Harness Harbor"
                   if self.platform == "darwin" else self.state_dir() / "logs")
        return self._path("HARBOR_LOG_DIR", default)

    def cache_dir(self):
        default = Path.home() / "Library/Caches/Harness Harbor" if self.platform == "darwin" else self.state_dir() / "cache"
        return self._path("HARBOR_CACHE_DIR", default)

    def tunnel_state_dir(self):
        return self._path("HARBOR_TUNNEL_PROFILE_DIR", self.application_support_dir() / "tunnel")

    def environment(self):
        return {key: str(value) for key, value in {
            "HARBOR_USER_SETTINGS_DIR": self.application_support_dir(),
            "HARBOR_STATE_DIR": self.state_dir(), "HARBOR_JOBS_DIR": self.jobs_dir(),
            "HARBOR_CONTROL_DIR": self.control_dir(), "HARBOR_LOG_DIR": self.logs_dir(),
            "HARBOR_CACHE_DIR": self.cache_dir(),
            "HARBOR_TUNNEL_PROFILE_DIR": self.tunnel_state_dir(),
        }.items()}
