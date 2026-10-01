import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from harbor_runtime.snapshot import harness_snapshot


class SnapshotTests(unittest.TestCase):
    def test_running_idle_disconnect_reconnect_and_unreadable_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = root / "fixture" / "status.json"
            job.parent.mkdir()
            job.write_text(json.dumps({"status": "running", "harness": "codex", "updated_at": "2026-09-11T00:00:00Z"}))
            old = [{"name": "codex", "running_job_ids": ["obsolete"], "running_jobs": 1}]
            with patch("control_plane.harness_telemetry_snapshot", side_effect=AssertionError("fast path probed")):
                running = harness_snapshot(root, old)[0]
                self.assertEqual(running["running_job_ids"], ["fixture"])
                self.assertEqual(running["latest_activity_at"], "2026-09-11T00:00:00+00:00")
                offline = harness_snapshot(root, old, connected=False)[0]
                self.assertEqual(offline["running_job_ids"], [])
                self.assertEqual(offline["activity_state"], "disconnected")
                self.assertEqual(harness_snapshot(root, old)[0]["running_job_ids"], ["fixture"])
                job.write_text('{"status":"completed","harness":"codex"}')
                self.assertEqual(harness_snapshot(root, old)[0]["activity_state"], "idle")
                job.write_text("invalid")
                stale = harness_snapshot(root, old)[0]
                self.assertEqual(stale["activity_state"], "stale")
                self.assertTrue(stale["queue_read_error"])
                self.assertEqual(stale["running_job_ids"], [])


if __name__ == "__main__":
    unittest.main()
