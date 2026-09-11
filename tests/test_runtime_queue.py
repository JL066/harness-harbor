from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime_queue import queue_root_matches, resolve_queue_root


class RuntimeQueueTests(unittest.TestCase):
    def test_environment_queue_is_canonical_and_fingerprinted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = resolve_queue_root(Path(tmp) / "project", {"HARBOR_JOBS_DIR": tmp})
            self.assertEqual(Path(tmp).resolve(), root.path)
            self.assertTrue(root.fingerprint)
            self.assertTrue(queue_root_matches({"queue_root_fingerprint": root.fingerprint}, root))

    def test_relative_environment_queue_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_queue_root(Path.cwd(), {"HARBOR_JOBS_DIR": "relative-jobs"})

    def test_mismatched_queue_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = resolve_queue_root(Path(tmp), {"HARBOR_JOBS_DIR": tmp + "/one"})
            other = resolve_queue_root(Path(tmp), {"HARBOR_JOBS_DIR": tmp + "/two"})
            self.assertFalse(queue_root_matches({"queue_root_fingerprint": other.fingerprint}, root))

    def test_macos_direct_and_app_entrypoints_share_default_queue(self):
        from harbor_platform.paths import PlatformPaths
        with tempfile.TemporaryDirectory() as tmp, patch("sys.platform", "darwin"):
            env = {"HARBOR_USER_SETTINGS_DIR": tmp}
            expected = PlatformPaths(Path(tmp), env).jobs_dir()
            for project in (Path(tmp) / "source", Path(tmp) / "app/Resources"):
                self.assertEqual(resolve_queue_root(project, env).path, expected)
                self.assertEqual(resolve_queue_root(project, PlatformPaths(project, env).environment()).path, expected)
        with patch("sys.platform", "win32"):
            self.assertEqual(resolve_queue_root(Path.cwd(), {}).path, Path.cwd() / ".jobs")


if __name__ == "__main__":
    unittest.main()
