from pathlib import Path
import tempfile
import unittest

from harbor_platform.paths import PlatformPaths


class PlatformPathsTests(unittest.TestCase):
    def test_windows_default_stays_in_repository_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(PlatformPaths(root, {}, "win32").jobs_dir(), (root / ".jobs").resolve())

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
