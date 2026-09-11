import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from harbor_platform.paths import PlatformPaths
from harbor_runtime.config import parse_settings
from harbor_runtime.lifecycle import Runtime


class LifecycleTests(unittest.TestCase):
    def test_stopped_runtime_clears_queue_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp})
            job = paths.jobs_dir() / "fixture" / "status.json"
            job.parent.mkdir(parents=True)
            job.write_text('{"status":"running","harness":"codex"}')
            runtime = Runtime(paths, parse_settings({}), {})
            snapshot = runtime.snapshot()
            self.assertEqual(snapshot["harnesses"][0]["running_job_ids"], [])
            self.assertEqual(snapshot["harnesses"][0]["activity_state"], "disconnected")

    def test_partial_start_repairs_only_missing_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp}), parse_settings({}), {})
            mcp = Mock()
            mcp.poll.return_value = None
            runtime.processes["mcp"] = mcp
            runtime.mcp_ready = True
            spawned = []
            def spawn(name, argv, **kwargs):
                spawned.append(name)
                proc = Mock()
                proc.poll.return_value = None
                runtime.processes[name] = proc
                return proc
            with patch.object(runtime, "spawn", spawn), patch.object(runtime, "test_connection", return_value={"ok": False}):
                first = runtime.start()
                runtime.start()
            self.assertEqual(spawned, ["daemon"])
            self.assertEqual(first["components"]["mcp"]["state"], "healthy")
            self.assertEqual(first["state"], "partial")

    def test_lifecycle_has_no_os_process_mechanisms(self):
        source = (Path(__file__).resolve().parents[1] / "harbor_runtime/lifecycle.py").read_text()
        for forbidden in ("fcntl", "select.select", "start_new_session", "os.kill", "taskkill", "CimInstance", 'identity["pgid"]'):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
