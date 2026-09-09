from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

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
            root = resolve_queue_root(Path(tmp) / "project", {})
            other = resolve_queue_root(Path(tmp) / "other", {})
            self.assertFalse(queue_root_matches({"queue_root_fingerprint": other.fingerprint}, root))


if __name__ == "__main__":
    unittest.main()
