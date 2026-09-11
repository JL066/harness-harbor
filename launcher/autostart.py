"""Windows Autostart configuration via CurrentUser Run registry key."""

from __future__ import annotations

import sys
from pathlib import Path

from launcher.config import AUTOSTART_APP_NAME, AUTOSTART_REG_KEY


def is_autostart_enabled() -> bool:
    """Check if Launcher is registered in HKCU Run registry key."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_KEY, 0, winreg.KEY_READ) as key:
            try:
                val, _ = winreg.QueryValueEx(key, AUTOSTART_APP_NAME)
                return bool(val)
            except FileNotFoundError:
                return False
    except Exception:
        return False


def set_autostart_enabled(enabled: bool, target_cmd: str | None = None) -> bool:
    """Enable or disable autostart with Windows. Only touches HarnessHarborLauncher key."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                cmd = target_cmd or sys.executable
                winreg.SetValueEx(key, AUTOSTART_APP_NAME, 0, winreg.REG_SZ, str(cmd))
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_APP_NAME)
                except FileNotFoundError:
                    pass
            return True
    except Exception:
        return False
