"""Mock contracts for native Windows ownership; no real process is started."""
import unittest
from unittest.mock import Mock, patch
import pytest

from harbor_platform import process, windows_process

pytestmark = pytest.mark.windows_ci


class WindowsOwnershipTests(unittest.TestCase):
    def test_recovered_process_observes_exit_with_readable_identity(self):
        identity = {"pid": 123, "started_at": "10", "job_name": "fixture"}
        recovered = process.RecoveredProcess(identity)
        with patch.object(process, "is_alive", return_value=False), patch.object(
            process, "process_identity", return_value=identity
        ) as identify:
            self.assertEqual(recovered.poll(), 0)
        identify.assert_not_called()

    def test_recovered_process_observation_failure_stays_alive(self):
        identity = {"pid": 123, "started_at": "10", "job_name": "fixture"}
        recovered = process.RecoveredProcess(identity)
        with patch.object(process, "is_alive", return_value=True), patch.object(
            process, "process_identity", return_value=None
        ):
            self.assertIsNone(recovered.poll())

    def test_recovered_process_identity_mismatch_is_not_owned(self):
        identity = {"pid": 123, "started_at": "10", "job_name": "fixture"}
        recovered = process.RecoveredProcess(identity)
        with patch.object(process, "is_alive", return_value=True), patch.object(
            process, "process_identity", return_value={**identity, "started_at": "11"}
        ):
            self.assertEqual(recovered.poll(), 0)

    def test_existing_job_collision_never_terminates_foreign_job(self):
        kernel = Mock()
        kernel.CreateJobObjectW.return_value = 42
        proc = Mock(pid=123, _handle=9)
        with patch.object(windows_process, "api", return_value=kernel), patch.object(
            windows_process, "process_identity", return_value={"job_name": "fixture"}
        ), patch("ctypes.set_last_error", create=True), patch(
            "ctypes.get_last_error", return_value=183, create=True
        ):
            with self.assertRaisesRegex(OSError, "already in use"):
                windows_process.spawn_owned(["fixture"], popen_factory=lambda *a, **k: proc)
        kernel.AssignProcessToJobObject.assert_not_called()
        kernel.TerminateJobObject.assert_not_called()
        proc.kill.assert_called_once()

    def test_spawn_retains_job_until_owner_is_released(self):
        kernel = Mock()
        kernel.CreateJobObjectW.return_value = 42
        kernel.CreateToolhelp32Snapshot.return_value = 43
        kernel.OpenThread.return_value = 44
        kernel.ResumeThread.return_value = 1
        kernel.Thread32Next.return_value = False

        def first(snapshot, entry):
            entry._obj.owner = 123
            entry._obj.thread = 456
            return True

        kernel.Thread32First.side_effect = first
        proc = Mock(pid=123, _handle=9)
        identity = {"pid": 123, "started_at": "10", "job_name": "Local\\HarnessHarbor-123-10"}
        with patch.object(windows_process, "api", return_value=kernel), patch.object(
            windows_process, "process_identity", return_value=identity
        ), patch("ctypes.set_last_error", create=True), patch(
            "ctypes.get_last_error", return_value=0, create=True
        ):
            windows_process.spawn_owned(["fixture"], popen_factory=lambda *a, **k: proc)
        self.assertNotIn(unittest.mock.call(42), kernel.CloseHandle.call_args_list)
        proc._harbor_job_finalizer()
        self.assertIn(unittest.mock.call(42), kernel.CloseHandle.call_args_list)

    def test_missing_job_is_unknown_not_an_empty_tree(self):
        kernel = Mock()
        kernel.OpenJobObjectW.return_value = 0
        with patch.object(windows_process, "api", return_value=kernel), patch(
            "ctypes.get_last_error", return_value=2
        ):
            self.assertIsNone(
                windows_process.descendants(
                    {"pid": 123, "started_at": "10", "job_name": "Local\\HarnessHarbor-123-10"}
                )
            )

    def test_malformed_identity_is_not_an_ownership_capability(self):
        with patch.object(windows_process, "api", side_effect=AssertionError("must not query")):
            self.assertIsNone(
                windows_process.descendants({"pid": 123, "started_at": "10", "job_name": "unrelated"})
            )

    def test_inspect_job_confirmed_missing_when_file_not_found(self):
        kernel = Mock()
        kernel.OpenJobObjectW.return_value = 0
        identity = {"pid": 123, "started_at": "10", "job_name": "Local\\HarnessHarbor-123-10"}
        with patch.object(windows_process, "api", return_value=kernel), patch(
            "ctypes.get_last_error", return_value=2
        ):
            status, members = windows_process.inspect_job(identity)
        self.assertEqual(status, "confirmed_missing")
        self.assertEqual(members, [])

    def test_inspect_job_unverifiable_on_access_denied(self):
        kernel = Mock()
        kernel.OpenJobObjectW.return_value = 0
        identity = {"pid": 123, "started_at": "10", "job_name": "Local\\HarnessHarbor-123-10"}
        with patch.object(windows_process, "api", return_value=kernel), patch(
            "ctypes.get_last_error", return_value=5
        ):
            status, members = windows_process.inspect_job(identity)
        self.assertEqual(status, "unverifiable")
        self.assertIsNone(members)

    def test_inspect_job_unverifiable_on_query_failure(self):
        kernel = Mock()
        kernel.OpenJobObjectW.return_value = 42
        kernel.QueryInformationJobObject.return_value = 0
        identity = {"pid": 123, "started_at": "10", "job_name": "Local\\HarnessHarbor-123-10"}
        with patch.object(windows_process, "api", return_value=kernel):
            status, members = windows_process.inspect_job(identity)
        self.assertEqual(status, "unverifiable")
        self.assertIsNone(members)
        kernel.CloseHandle.assert_called_once_with(42)

    def test_inspect_job_active_with_members(self):
        kernel = Mock()
        kernel.OpenJobObjectW.return_value = 42

        def query(handle, info_class, ptr, size, ret_len):
            obj = ptr._obj
            obj.assigned = 2
            obj.count = 2
            obj.pids[0] = 456
            obj.pids[1] = 789
            return 1

        kernel.QueryInformationJobObject.side_effect = query
        identity = {"pid": 123, "started_at": "10", "job_name": "Local\\HarnessHarbor-123-10"}
        with patch.object(windows_process, "api", return_value=kernel):
            status, members = windows_process.inspect_job(identity)
        self.assertEqual(status, "active")
        self.assertEqual(members, [456, 789])
        kernel.CloseHandle.assert_called_once_with(42)

    def test_recover_owned_discards_safe_stale_identity(self):
        from harbor_platform.process import recover_owned
        identity = {"pid": 123, "started_at": "10", "job_name": "Local\\HarnessHarbor-123-10"}
        with patch("harbor_platform.process.is_alive", return_value=False), patch(
            "harbor_platform.windows_process.inspect_job", return_value=("confirmed_missing", [])
        ):
            recovered = recover_owned(identity)
        self.assertIsNone(recovered)

    def test_recover_owned_fails_closed_when_job_unverifiable(self):
        from harbor_platform.process import recover_owned
        identity = {"pid": 123, "started_at": "10", "job_name": "Local\\HarnessHarbor-123-10"}
        with patch("harbor_platform.process.is_alive", return_value=False), patch(
            "harbor_platform.windows_process.inspect_job", return_value=("unverifiable", None)
        ):
            with self.assertRaisesRegex(RuntimeError, "Component ownership could not be verified"):
                recover_owned(identity)

    def test_recover_owned_fails_closed_on_identity_mismatch(self):
        from harbor_platform.process import recover_owned
        identity = {"pid": 123, "started_at": "10", "job_name": "Local\\HarnessHarbor-123-10"}
        with patch("harbor_platform.process.is_alive", return_value=True), patch(
            "harbor_platform.process.owner_identity_valid", return_value=False
        ):
            with self.assertRaisesRegex(RuntimeError, "Component identity mismatch"):
                recover_owned(identity)

    def test_recover_owned_returns_recovered_process_for_active_members(self):
        from harbor_platform.process import recover_owned
        identity = {"pid": 123, "started_at": "10", "job_name": "Local\\HarnessHarbor-123-10"}
        with patch("harbor_platform.process.is_alive", return_value=False), patch(
            "harbor_platform.windows_process.inspect_job", return_value=("active", [456, 789])
        ):
            recovered = recover_owned(identity)
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered.pid, 123)


if __name__ == "__main__":
    unittest.main()
