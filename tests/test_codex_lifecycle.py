from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import codex_job_worker


def _pid_alive(pid: int) -> bool:
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


class CodexLifecycleTests(unittest.TestCase):
    def test_stdin_is_devnull_and_success_output_is_bounded(self) -> None:
        cap = codex_job_worker.READER_BUFFER_BYTES
        result = codex_job_worker.run_codex_with_lifecycle([
            sys.executable,
            "-c",
            (
                "import os,sys\n"
                "data=sys.stdin.buffer.read()\n"
                f"os.write(1, b'x'*({cap}+65536))\n"
                f"os.write(2, b'y'*({cap}+65536))\n"
                "os.write(1, b'\\nEMPTY=' + str(not data).encode() + b'\\n')\n"
            ),
        ], total_timeout=10.0)
        self.assertEqual(result.returncode, 0)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), cap)
        self.assertLessEqual(len(result.stderr.encode("utf-8")), cap)
        self.assertIn("EMPTY=True", result.stdout)

    @unittest.skipUnless(sys.platform == "win32", "Windows tree-reap contract")
    def test_hard_timeout_reaps_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / "child.pid"
            command = [sys.executable, "-c", (
                "import pathlib,subprocess,sys,time\n"
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])\n"
                f"pathlib.Path({str(pid_path)!r}).write_text(str(p.pid))\n"
                "time.sleep(120)\n"
            )]
            result = codex_job_worker.run_codex_with_lifecycle(command, total_timeout=0.5)
            self.assertEqual(getattr(result, "termination_reason", None), "hard_timeout")
            collected = codex_job_worker.collect_result(result, Path(tmp) / "missing-result.txt")
            self.assertEqual(collected["failure_type"], "codex_execution_timeout")
            child_pid = int(pid_path.read_text())
            deadline = time.time() + 3.0
            while time.time() < deadline and _pid_alive(child_pid):
                time.sleep(0.05)
            self.assertFalse(_pid_alive(child_pid))

    def test_clean_success_preserves_result_handling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "result.txt"
            command = [sys.executable, "-c", (
                f"from pathlib import Path; Path({str(result_path)!r}).write_text('done', encoding='utf-8'); print('diagnostic')"
            )]
            process = codex_job_worker.run_codex_with_lifecycle(command, total_timeout=5.0)
            collected = codex_job_worker.collect_result(process, result_path)
            self.assertEqual(collected["status"], "completed")
            self.assertEqual(collected["final_message"], "done")
            self.assertIn("diagnostic", collected["stdout_tail"])

    def test_fallback_uses_same_lifecycle_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            state = {
                "harness": "codex", "prompt": "task", "cwd": tmp,
                "route_requested": "official_then_codeflow",
            }
            calls: list[tuple[list[str], dict[str, str] | None]] = []

            def run(command, *, env=None):
                calls.append((command, env))
                result_path = job_dir / "result.txt"
                if len(calls) == 1:
                    return subprocess.CompletedProcess(command, 1, "", "OpenAI Codex usage limit exhausted")
                result_path.write_text("fallback done", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "ok", "")

            def build(_state, _path, route=None):
                return ["codex", route or "current"]

            with mock.patch.object(codex_job_worker, "claim_job", return_value=state), \
                    mock.patch.object(codex_job_worker, "build_codex_command", side_effect=build), \
                    mock.patch.object(codex_job_worker, "is_stale_harbor_managed_codex_config", return_value=(False, None)), \
                    mock.patch.object(codex_job_worker, "_get_codex_config_provider", return_value="openai"), \
                    mock.patch.object(codex_job_worker, "codex_child_environment", side_effect=lambda route: {"ROUTE": route}), \
                    mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", side_effect=run), \
                    mock.patch.object(codex_job_worker, "record_official_quota_exhausted"), \
                    mock.patch.dict(os.environ, {"CODEFLOW_API_KEY": "test"}):
                codex_job_worker.main(job_dir)

            final = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual([call[0][1] for call in calls], ["official", "codeflow"])
            self.assertEqual([call[1]["ROUTE"] for call in calls], ["official", "codeflow"])
            self.assertTrue(final["fallback_used"])
            self.assertEqual(final["final_message"], "fallback done")


if __name__ == "__main__":
    unittest.main()
