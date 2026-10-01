import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harbor_runtime import config
from harbor_runtime.config import load_settings, parse_settings, validate_settings
from harbor_runtime.protocol import MAX_MESSAGE, encode, validate


class SharedRuntimeProtocolTests(unittest.TestCase):
    def test_protocol_rejects_unknown_methods_and_redacts_secrets(self):
        for method, params in (("shell.exec", {}), ("runtime.start", {"argv": ["bad"]})):
            with self.assertRaises(ValueError):
                validate({"v": 1, "id": "test", "method": method, "params": params})
        with patch.dict(os.environ, {"TUNNEL_RUNTIME_KEY": 'secret-"-12345'}):
            data = json.loads(encode({"id": "x", "result": 'secret-"-12345'}))
            self.assertEqual(data["result"], "[REDACTED]")
        self.assertLess(len(encode({"result": "x" * MAX_MESSAGE})), MAX_MESSAGE)

    def test_legacy_direct_route_normalizes_to_shared_current_route(self):
        self.assertEqual(parse_settings({})["codex"]["routing_mode"], "current")
        self.assertEqual(
            parse_settings({"codex": {"routing_mode": "direct"}})["codex"]["routing_mode"],
            "current",
        )

    def test_current_platform_executable_namespace_is_preserved(self):
        settings = parse_settings({"windows": {"executables": {"tunnel": r"C:\\Tools\\tunnel-client.exe"}}})
        self.assertEqual(settings["windows"]["executables"]["tunnel"], r"C:\\Tools\\tunnel-client.exe")

    def test_windows_mcode_basename_is_case_insensitive_but_macos_is_not(self):
        settings = parse_settings({"windows": {"executables": {"minimax": r"C:\\Tools\\mcode.CMD"}}})
        self.assertEqual(settings["windows"]["executables"]["minimax"], r"C:\\Tools\\mcode.CMD")
        with self.assertRaisesRegex(ValueError, "mcode CLI"):
            parse_settings({"macos": {"executables": {"minimax": "/opt/tools/mcode.CMD"}}})

    def test_packaged_configure_is_reentrant_with_uppercase_windows_mcode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mcode = root / "mcode.CMD"
            env = {
                "HARBOR_RUNTIME_MODE": "packaged",
                "HARBOR_USER_SETTINGS_DIR": str(root / "settings"),
                "HARBOR_STATE_DIR": str(root / "state"),
                "HARBOR_JOBS_DIR": str(root / "state" / "jobs"),
                "HARBOR_CONTROL_DIR": str(root / "state" / "control"),
                "HARBOR_LOG_DIR": str(root / "state" / "logs"),
                "HARBOR_CACHE_DIR": str(root / "state" / "cache"),
                "HARBOR_TUNNEL_PROFILE_DIR": str(root / "settings" / "tunnel"),
            }

            def resolve(name, override=""):
                return str(mcode) if name == "mcode" else None

            with patch.object(config.sys, "platform", "win32"), patch.dict(os.environ, env, clear=False), patch.object(
                config, "load_settings", return_value=parse_settings({})
            ), patch.object(config, "resolve_executable", side_effect=resolve):
                first = config.configure()
                second = config.configure()

            self.assertEqual(first[2]["minimax"], str(mcode))
            self.assertEqual(second[2]["minimax"], str(mcode))

    def test_settings_validation_keeps_credential_references_non_secret(self):
        with self.assertRaisesRegex(ValueError, "Tunnel ID"):
            validate_settings({}, credentials={"tunnel": False, "custom": False}, require_connection=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertFalse(path.exists())

    def test_settings_are_loaded_as_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_dir = root / "settings"
            settings_dir.mkdir()
            (settings_dir / "settings.json").write_text(
                json.dumps({"codex": {"custom": {"default_model": "模型-中文"}}}, ensure_ascii=False),
                encoding="utf-8",
            )
            paths = config.PlatformPaths(root, {"HARBOR_USER_SETTINGS_DIR": str(settings_dir)}, "win32")
            self.assertEqual(load_settings(paths)["codex"]["custom"]["default_model"], "模型-中文")

    def test_bundle_override_must_cover_the_full_runtime_bundle(self):
        executable = Path(tempfile.gettempdir()) / "Harness Harbor" / "runtime" / "harbor-runtime.exe"
        with patch.object(config.sys, "executable", str(executable)), patch.dict(
            os.environ, {"HARBOR_BUNDLE_ROOT": str(executable.parent)}
        ):
            with self.assertRaisesRegex(ValueError, "entire runtime bundle"):
                config._bundle_root()

    def test_packaged_same_canonical_settings_path_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "bundle"
            executable = bundle / "runtime" / "harbor-runtime.exe"
            settings_dir = root / "settings"
            settings_dir.mkdir()
            env = {
                "HARBOR_RUNTIME_MODE": "packaged",
                "HARBOR_BUNDLE_ROOT": str(bundle),
                "HARBOR_USER_SETTINGS_DIR": str(settings_dir),
                "HARBOR_USER_SETTINGS_PATH": str(settings_dir / "." / "settings.json"),
            }
            with patch.object(config.sys, "frozen", True, create=True), patch.object(
                config.sys, "executable", str(executable)
            ), patch.dict(os.environ, env, clear=False), patch.object(
                config, "load_settings", return_value=parse_settings({})
            ) as load_settings, patch.object(config, "resolve_executable", return_value=None):
                result = config.configure()
                load_settings.assert_called_once()
                self.assertEqual(result[1]["codex"]["routing_mode"], "current")

    def test_packaged_different_settings_path_fails_before_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "bundle"
            executable = bundle / "runtime" / "harbor-runtime.exe"
            settings_dir = root / "settings"
            env = {
                "HARBOR_RUNTIME_MODE": "packaged",
                "HARBOR_BUNDLE_ROOT": str(bundle),
                "HARBOR_USER_SETTINGS_DIR": str(settings_dir),
                "HARBOR_USER_SETTINGS_PATH": str(root / "alternate.json"),
            }
            with patch.object(config.sys, "frozen", True, create=True), patch.object(
                config.sys, "executable", str(executable)
            ), patch.dict(os.environ, env, clear=False), patch.object(
                config, "load_settings", side_effect=AssertionError("settings must not be read")
            ):
                with self.assertRaisesRegex(ValueError, "HARBOR_USER_SETTINGS_DIR"):
                    config.configure()

    def test_packaged_settings_file_inside_bundle_fails_before_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "bundle"
            executable = bundle / "runtime" / "harbor-runtime.exe"
            settings_dir = root / "settings"
            env = {
                "HARBOR_RUNTIME_MODE": "packaged",
                "HARBOR_BUNDLE_ROOT": str(bundle),
                "HARBOR_USER_SETTINGS_DIR": str(settings_dir),
                "HARBOR_USER_SETTINGS_PATH": str(bundle / "settings.json"),
            }
            with patch.object(config.sys, "frozen", True, create=True), patch.object(
                config.sys, "executable", str(executable)
            ), patch.dict(os.environ, env, clear=False), patch.object(
                config, "load_settings", side_effect=AssertionError("settings must not be read")
            ):
                with self.assertRaisesRegex(ValueError, "runtime bundle"):
                    config.configure()


if __name__ == "__main__":
    unittest.main()
