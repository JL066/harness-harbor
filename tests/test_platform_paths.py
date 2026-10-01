from pathlib import Path
import tempfile
import unittest

from harbor_platform.paths import PlatformPaths


class PlatformPathsTests(unittest.TestCase):
    def test_windows_legacy_default_stays_in_repository_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = PlatformPaths(root, {}, "win32")
            self.assertEqual(paths.jobs_dir(), (root / ".jobs").resolve())
            self.assertEqual(paths.control_dir(), (root / ".control").resolve())

    def test_windows_packaged_defaults_split_config_and_mutable_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            appdata = root / "roaming"
            localappdata = root / "local"
            paths = PlatformPaths(
                root,
                {
                    "APPDATA": str(appdata),
                    "LOCALAPPDATA": str(localappdata),
                    "HARBOR_RUNTIME_MODE": "packaged",
                },
                "win32",
            )
            self.assertEqual(paths.application_support_dir(), (appdata / "Harness Harbor").resolve())
            state = (localappdata / "Harness Harbor").resolve()
            self.assertEqual(paths.state_dir(), state)
            self.assertEqual(paths.jobs_dir(), state / "jobs")
            self.assertEqual(paths.control_dir(), state / "control")
            self.assertEqual(paths.logs_dir(), state / "logs")
            self.assertEqual(paths.cache_dir(), state / "cache")
            self.assertEqual(Path(paths.environment()["HARBOR_CACHE_DIR"]), state / "cache")

    def test_windows_overrides_keep_jobs_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "shared-jobs"
            state = root / "state"
            self.assertEqual(
                PlatformPaths(root, {"HARBOR_JOBS_DIR": str(jobs), "HARBOR_STATE_DIR": str(state)}, "win32").jobs_dir(),
                jobs.resolve(),
            )
            self.assertEqual(
                PlatformPaths(root, {"HARBOR_STATE_DIR": str(state)}, "win32").jobs_dir(),
                (state / "jobs").resolve(),
            )

    def test_macos_default_remains_application_state_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = root / "settings"
            paths = PlatformPaths(root, {"HARBOR_USER_SETTINGS_DIR": str(settings)}, "darwin")
            self.assertEqual(paths.jobs_dir(), (settings / "state" / "jobs").resolve())


if __name__ == "__main__":
    unittest.main()
