"""Mock contracts for native Windows ownership; real tree test lives in shared."""
import unittest
from unittest.mock import Mock, patch
import pytest
from harbor_platform import windows_process

pytestmark = pytest.mark.windows_ci


class WindowsOwnershipTests(unittest.TestCase):
    def test_existing_job_collision_never_terminates_foreign_job(self):
        kernel = Mock()
        kernel.CreateJobObjectW.return_value = 42
        proc = Mock(pid=123, _handle=9)
        with patch.object(windows_process, "api", return_value=kernel), patch.object(windows_process, "process_identity", return_value={"job_name": "fixture"}), patch("ctypes.set_last_error", create=True), patch("ctypes.get_last_error", return_value=183, create=True):
            with self.assertRaisesRegex(OSError, "already in use"):
                windows_process.spawn_owned(["fixture"], popen_factory=lambda *a, **k: proc)
        kernel.AssignProcessToJobObject.assert_not_called()
        kernel.TerminateJobObject.assert_not_called()
        proc.kill.assert_called_once()

    def test_malformed_identity_is_not_an_ownership_capability(self):
        with patch.object(windows_process, "api", side_effect=AssertionError("must not query")):
            self.assertIsNone(windows_process.descendants({"pid": 123, "started_at": "10", "job_name": "unrelated"}))


if __name__ == "__main__":
    unittest.main()
