"""Unit tests for Autostart."""

from launcher.autostart import is_autostart_enabled, set_autostart_enabled


def test_autostart_mock(monkeypatch):
    registry_store = {}

    class MockWinReg:
        HKEY_CURRENT_USER = "HKCU"
        KEY_READ = 1
        KEY_SET_VALUE = 2
        REG_SZ = 1

        @staticmethod
        def OpenKey(hkey, subkey, reserved, access):
            return MockWinReg()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        @staticmethod
        def QueryValueEx(key, name):
            if name in registry_store:
                return registry_store[name], 1
            raise FileNotFoundError()

        @staticmethod
        def SetValueEx(key, name, reserved, reg_type, val):
            registry_store[name] = val

        @staticmethod
        def DeleteValue(key, name):
            if name in registry_store:
                del registry_store[name]
            else:
                raise FileNotFoundError()

    import sys
    monkeypatch.setattr(sys, "platform", "win32")
    import builtins
    import launcher.autostart as autostart_mod

    # Inject mock winreg
    monkeypatch.setitem(sys.modules, "winreg", MockWinReg)

    assert autostart_mod.is_autostart_enabled() is False
    autostart_mod.set_autostart_enabled(True, "D:\\path\\launcher.exe")
    assert autostart_mod.is_autostart_enabled() is True

    autostart_mod.set_autostart_enabled(False)
    assert autostart_mod.is_autostart_enabled() is False
