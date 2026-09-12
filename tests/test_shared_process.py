import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from harbor_platform import process
from harbor_platform.host import acquire_lock


class ProcessContractTests(unittest.TestCase):
    def test_permission_error_is_alive_for_all_shared_callers(self):
        if sys.platform == "win32":
            self.skipTest("POSIX permission error fixture")
        from control_plane import _is_pid_alive as poll_alive
        from codex_job_daemon import _is_pid_alive as daemon_alive
        from launcher.process_manager import is_pid_alive as launcher_alive
        with patch("os.kill", side_effect=PermissionError):
            self.assertTrue(all(check(123) for check in (process.is_alive, poll_alive, daemon_alive, launcher_alive)))
        self.assertFalse(process.is_alive(-1))

    def test_host_lock_rejects_double_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bridge.lock"
            with acquire_lock(path):
                with self.assertRaises(RuntimeError):
                    acquire_lock(path)
            with acquire_lock(path):
                pass

    def test_owned_parent_child_grandchild_and_unrelated_survive(self):
        child = "import time;print('ready',flush=True);time.sleep(60)"
        middle = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(60)"
        parent = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{middle!r}]);time.sleep(60)"
        proc = process.spawn_owned([sys.executable, "-c", parent], stdout=subprocess.PIPE)
        other = process.spawn_owned([sys.executable, "-c", "import time;time.sleep(60)"])
        try:
            from harbor_platform.host import read_line
            self.assertEqual(read_line(proc.stdout, timeout=5).replace(b"\r\n", b"\n"), b"ready\n")
            identity = getattr(proc, "_harbor_identity", None) or process.settled_identity(proc.pid)
            self.assertGreaterEqual(len(process.descendants(identity)), 3)
            proc.kill()
            proc.wait(timeout=5)
            self.assertTrue(process.owned_tree_alive(proc))
            self.assertTrue(process.terminate_owned_tree(proc, grace=2))
            self.assertTrue(process.wait_owned_tree_exit(proc, timeout=2))
            self.assertIsNone(other.poll())
        finally:
            process.terminate_owned_tree(proc, grace=2)
            process.terminate_owned_tree(other, grace=2)
            proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
