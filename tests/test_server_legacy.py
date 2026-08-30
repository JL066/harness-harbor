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


if __name__ == "__main__":
    unittest.main()
