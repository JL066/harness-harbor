import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import server_legacy


class CodexPollTests(unittest.TestCase):
    def test_null_job_state_returns_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            jobs_dir = Path(temporary_directory)
            job_dir = jobs_dir / "test-job"
            job_dir.mkdir()
            (job_dir / "status.json").write_text("null", encoding="utf-8")

            with mock.patch.object(server_legacy, "JOBS_DIR", jobs_dir):
                result = server_legacy.codex_poll("test-job")

            self.assertFalse(result["ok"])
            self.assertEqual(
                "Could not read job state: expected a JSON object",
                result["error"],
            )

    def test_mcp_tool_signatures_and_defaults(self) -> None:
        import inspect

        # task_start signature & annotations
        sig_task_start = inspect.signature(server_legacy.task_start)
        self.assertIn("harness", sig_task_start.parameters)
        self.assertIn("prompt", sig_task_start.parameters)

        # task_poll signature & defaults
        sig_task_poll = inspect.signature(server_legacy.task_poll)
        self.assertIn("job_id", sig_task_poll.parameters)
        self.assertIn("immediate", sig_task_poll.parameters)
        self.assertIs(sig_task_poll.parameters["immediate"].default, False)

        # codex_poll signature & defaults
        sig_codex_poll = inspect.signature(server_legacy.codex_poll)
        self.assertIn("job_id", sig_codex_poll.parameters)
        self.assertIn("immediate", sig_codex_poll.parameters)
        self.assertIs(sig_codex_poll.parameters["immediate"].default, False)


class McpStdioDisconnectTests(unittest.TestCase):
    def test_mcp_disconnect_exits_cleanly_without_closed_file_error(self) -> None:
        """Verify that when an MCP client disconnects by closing stdin,
        the server exits cleanly with returncode 0 and does not raise
        'ValueError: I/O operation on closed file.' on stdout flush.
        """
        proc = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "server_legacy.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            init_req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0"},
                },
            }
            proc.stdin.write(json.dumps(init_req) + "\n")
            proc.stdin.flush()
            init_resp = proc.stdout.readline()
            self.assertTrue(init_resp, "server did not respond to initialize")
            self.assertIn("protocolVersion", init_resp)

            init_notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            proc.stdin.write(json.dumps(init_notif) + "\n")
            proc.stdin.flush()

            tools_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            proc.stdin.write(json.dumps(tools_req) + "\n")
            proc.stdin.flush()
            tools_resp = proc.stdout.readline()
            self.assertTrue(tools_resp, "server did not respond to tools/list")
            self.assertIn("tools", tools_resp)
        finally:
            proc.stdin.close()
            exit_code = proc.wait(timeout=10)
            stderr = proc.stderr.read()

        self.assertEqual(0, exit_code, f"server exited with {exit_code}: {stderr}")
        self.assertNotIn("closed file", stderr.lower())
        self.assertNotIn("valueerror", stderr.lower())


if __name__ == "__main__":
    unittest.main()
