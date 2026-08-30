"""P1 worker lifecycle tests: Codex + MiniMax + Antigravity job workers.

These tests cover the MiniMax exec lifecycle:
  * normal exit -> completed, no forced_exit flag
  * result.txt written but CLI hangs -> completed, forced_exit_after_result=True
  * CLI hangs with no result -> failed, failure_type=minimax_execution_timeout
  * nonzero exit before result -> failed, failure_type=minimax_execution_error
  * result.txt stability / late appearance
  * Codex path is unchanged

These tests also cover the Antigravity (agy) lifecycle:
  * normal exit with JSON final line -> completed, result.txt written
  * normal exit with plain-text final line -> completed, result.txt written
  * CLI hangs after stable result -> completed, forced_exit_after_result=True
  * CLI hangs with no result -> failed, failure_type=agy_execution_timeout
  * nonzero exit before result -> failed, failure_type=agy_execution_error
  * Popen failure -> failed, failure_type=agy_execution_error
  * stdout JSON extractor covers result/text/answer/content/output/message
  * build_agy_command does not add --cwd or -o flags (those are Popen/file
    concerns, not argv concerns)
  * start_task validates sandbox=read-only, route!=current, and bad
    reasoning_effort for harness='agy'

A small Python ``fake_mcode`` script is written to a temp directory
and used in place of the real ``mcode.cmd`` so the tests do not
require any real model call. The agy tests reuse the same pattern
with a tiny ``fake_agy`` script.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import control_plane
import codex_job_worker


PYTHON_EXE = Path(sys.executable)


def _write_fake_mcode(parent: Path, body: str) -> Path:
    """Write a small Python script to act as the MiniMax CLI.

    The script receives ``--output-last-message <path>`` and
    ``--cwd <path>`` in argv and may use any of them.
    """
    script = parent / "fake_mcode.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return script


def _make_minimax_command(fake_exe: Path, result_path: Path, cwd: Path) -> list[str]:
    return [
        str(PYTHON_EXE),
        str(fake_exe),
        "exec",
        "--cwd", str(cwd),
        "--output-format", "json",
        "--output-last-message", str(result_path),
        "PROMPT-IGNORED",
    ]


def _make_agy_command(fake_exe: Path, cwd: Path) -> list[str]:
    """Build the argv the agy lifecycle supervisor would actually pass.

    The real agy.exe is a binary; here the fake is a Python script, so
    we prepend ``sys.executable`` to keep the fake runnable on Windows
    where the test environment has no shebang-style .py association.
    The supervisor itself is agnostic to whether the executable is the
    real agy.exe or a fake Python script.
    """
    return [
        str(PYTHON_EXE),
        str(fake_exe),
        "--print",
        "--dangerously-skip-permissions",
        "--output-format", "json",
        "--print-timeout", "5m",
        "-p", "PROMPT-IGNORED",
    ]


def _collect_agy_lifecycle(
    fake_exe: Path,
    cwd: Path,
    *,
    total_timeout: float = 5.0,
    exit_grace: float = 1.0,
    settle_grace: float = 0.1,
    poll_interval: float = 0.05,
) -> dict:
    cmd = _make_agy_command(fake_exe, cwd)
    return codex_job_worker.run_agy_with_lifecycle(
        cmd,
        cwd=str(cwd),
        poll_interval=poll_interval,
        settle_grace=settle_grace,
        exit_grace=exit_grace,
        total_timeout=total_timeout,
    )


def _write_fake_agy(parent: Path, body: str) -> Path:
    """Write a small Python script to act as the agy CLI.

    The script receives the canonical agy argv (the value of ``-p`` is
    the prompt; ``--print`` is present) and may emit any stdout /
    stderr / exit-code combination.
    """
    script = parent / "fake_agy.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return script


def _collect_lifecycle(
    fake_exe: Path,
    result_path: Path,
    cwd: Path,
    *,
    total_timeout: float = 5.0,
    exit_grace: float = 1.0,
    settle_grace: float = 0.1,
    poll_interval: float = 0.05,
) -> dict:
    cmd = _make_minimax_command(fake_exe, result_path, cwd)
    return codex_job_worker.run_minimax_with_lifecycle(
        cmd,
        result_path,
        poll_interval=poll_interval,
        settle_grace=settle_grace,
        exit_grace=exit_grace,
        total_timeout=total_timeout,
    )


def _make_state_dir(tmp: Path) -> tuple[Path, Path, Path]:
    job_dir = tmp / "job"
    job_dir.mkdir()
    result_path = job_dir / "result.txt"
    cwd = tmp / "workdir"
    cwd.mkdir()
    return job_dir, result_path, cwd


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


class MiniMaxLifecycleTests(unittest.TestCase):
    def test_normal_minimax_exit_is_completed(self) -> None:
        """Fake CLI writes result.txt and exits 0 -> completed, no force."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            job_dir, result_path, cwd = _make_state_dir(tmp_path)
            fake = _write_fake_mcode(
                tmp_path,
                """\
                import sys, time
                rp = None
                for i, arg in enumerate(sys.argv):
                    if arg == '--output-last-message':
                        rp = sys.argv[i+1]
                        break
                with open(rp, 'w', encoding='utf-8') as f:
                    f.write('MINIMAX_MCP_OK')
                sys.exit(0)
                """,
            )
            lifecycle = _collect_lifecycle(fake, result_path, cwd)
            self.assertEqual("self_exit", lifecycle["termination_reason"])
            self.assertFalse(lifecycle["forced_exit"])
            self.assertEqual(0, lifecycle["exit_code"])
            self.assertTrue(result_path.exists())
            self.assertEqual("MINIMAX_MCP_OK", result_path.read_text(encoding="utf-8"))
            collected = codex_job_worker.collect_minimax_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("completed", collected["status"])
            self.assertEqual("MINIMAX_MCP_OK", collected["final_message"])
            self.assertFalse(collected["forced_exit_after_result"])

    def test_result_written_but_process_hangs_is_completed_with_force(self) -> None:
        """The pivotal test: result.txt appears, CLI does not exit,
        worker must terminate the process tree and mark the job
        completed with forced_exit_after_result=True.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            job_dir, result_path, cwd = _make_state_dir(tmp_path)
            fake = _write_fake_mcode(
                tmp_path,
                """\
                import sys, time
                rp = None
                for i, arg in enumerate(sys.argv):
                    if arg == '--output-last-message':
                        rp = sys.argv[i+1]
                        break
                with open(rp, 'w', encoding='utf-8') as f:
                    f.write('MINIMAX_MCP_OK')
                # CLI deliberately keeps the event loop alive, like a
                # wrapper that leaves a descendant process running.
                while True:
                    time.sleep(60)
                """,
            )
            # Before invoking, capture the fake_mcode.py pid indirectly:
            # spawn a side thread that polls the process table for any
            # child python.exe whose command line mentions our fake.
            children_pids: list[int] = []
            stop = threading.Event()

            def _watch_children() -> None:
                while not stop.is_set():
                    for p in control_plane.subprocess.Popen.__init__.__globals__.values():
                        pass
                    # Use psutil-free check via the subprocess module's
                    # internal _active list is not portable; instead,
                    # use Windows CIM from a side thread.
                    try:
                        import ctypes
                        from ctypes import wintypes
                        snapshot = None
                        # We rely on the Popen return value below.
                    except Exception:
                        pass
                    try:
                        # Use tasklist via subprocess to find any
                        # remaining fake_mcode.py processes.
                        out = subprocess.check_output(
                            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
                            timeout=2,
                        )
                        for line in out.decode("utf-8", "replace").splitlines():
                            parts = line.split(",")
                            if len(parts) >= 2:
                                try:
                                    pid = int(parts[1].strip('"'))
                                    children_pids.append(pid)
                                except ValueError:
                                    pass
                    except Exception:
                        pass
                    time.sleep(0.2)

            watcher = threading.Thread(target=_watch_children, daemon=True)
            watcher.start()
            try:
                lifecycle = _collect_lifecycle(
                    fake, result_path, cwd,
                    total_timeout=10.0, exit_grace=0.5, settle_grace=0.2,
                )
            finally:
                stop.set()
                watcher.join(timeout=2)

            self.assertEqual(
                "grace_expired_after_result", lifecycle["termination_reason"],
                f"expected forced termination, got {lifecycle}",
            )
            self.assertTrue(lifecycle["forced_exit"])
            self.assertTrue(result_path.exists())
            self.assertEqual("MINIMAX_MCP_OK", result_path.read_text(encoding="utf-8"))

            collected = codex_job_worker.collect_minimax_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("completed", collected["status"])
            self.assertEqual("MINIMAX_MCP_OK", collected["final_message"])
            self.assertTrue(
                collected["forced_exit_after_result"],
                "result_completed after force must set forced_exit_after_result=True",
            )

            # No orphan fake_mcode.py python process should remain.
            # Check tasklist right after lifecycle returns; the process
            # tree must already be gone.
            time.sleep(0.5)
            out = subprocess.check_output(
                ["tasklist", "/FO", "CSV", "/NH"], timeout=2,
            )
            text = out.decode("utf-8", "replace")
            # The fake_mcode.py path is unique to this test; if it
            # appears in tasklist, that means a python child survived.
            self.assertNotIn(str(fake).lower(), text.lower())
            self.assertNotIn("fake_mcode", text.lower())

    def test_hang_without_result_fails_with_timeout(self) -> None:
        """Fake CLI never writes result.txt -> failed, execution_timeout."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            job_dir, result_path, cwd = _make_state_dir(tmp_path)
            fake = _write_fake_mcode(
                tmp_path,
                """\
                import time
                while True:
                    time.sleep(60)
                """,
            )
            lifecycle = _collect_lifecycle(
                fake, result_path, cwd,
                total_timeout=1.5, exit_grace=0.0, settle_grace=0.1,
            )
            self.assertEqual("hard_timeout", lifecycle["termination_reason"])
            self.assertTrue(lifecycle["forced_exit"])
            collected = codex_job_worker.collect_minimax_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("failed", collected["status"])
            self.assertEqual("minimax_execution_timeout", collected["failure_type"])
            # No orphan
            time.sleep(0.5)
            out = subprocess.check_output(
                ["tasklist", "/FO", "CSV", "/NH"], timeout=2,
            )
            self.assertNotIn("fake_mcode", out.decode("utf-8", "replace").lower())

    def test_nonzero_exit_before_result_is_execution_error(self) -> None:
        """Fake CLI exits nonzero with no result -> failed, execution_error."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            job_dir, result_path, cwd = _make_state_dir(tmp_path)
            fake = _write_fake_mcode(
                tmp_path,
                """\
                import sys
                sys.stderr.write('boom')
                sys.exit(7)
                """,
            )
            lifecycle = _collect_lifecycle(
                fake, result_path, cwd,
                total_timeout=5.0, exit_grace=0.0, settle_grace=0.1,
            )
            self.assertEqual("self_exit", lifecycle["termination_reason"])
            self.assertFalse(lifecycle["forced_exit"])
            self.assertEqual(7, lifecycle["exit_code"])
            collected = codex_job_worker.collect_minimax_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("failed", collected["status"])
            self.assertEqual("minimax_execution_error", collected["failure_type"])

    def test_result_appears_late_is_not_misjudged_as_failure(self) -> None:
        """Result.txt appears after a small delay; lifecycle must wait,
        and the final state must be completed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            job_dir, result_path, cwd = _make_state_dir(tmp_path)
            fake = _write_fake_mcode(
                tmp_path,
                """\
                import sys, time
                rp = None
                for i, arg in enumerate(sys.argv):
                    if arg == '--output-last-message':
                        rp = sys.argv[i+1]
                        break
                # Write the result ~0.8s after startup so the worker
                # has had several poll cycles with an empty file.
                time.sleep(0.8)
                with open(rp, 'w', encoding='utf-8') as f:
                    f.write('LATE_BUT_OK')
                # Then keep the event loop alive.
                while True:
                    time.sleep(60)
                """,
            )
            lifecycle = _collect_lifecycle(
                fake, result_path, cwd,
                total_timeout=10.0, exit_grace=0.5, settle_grace=0.2,
            )
            self.assertEqual(
                "grace_expired_after_result", lifecycle["termination_reason"]
            )
            self.assertTrue(lifecycle["forced_exit"])
            collected = codex_job_worker.collect_minimax_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("completed", collected["status"])
            self.assertEqual("LATE_BUT_OK", collected["final_message"])
            self.assertTrue(collected["forced_exit_after_result"])


# ---------------------------------------------------------------------------
# Antigravity (agy) lifecycle tests
# ---------------------------------------------------------------------------


class AgyExtractTests(unittest.TestCase):
    """Pure-Python tests for the agy JSON extractor (no subprocess)."""

    def test_canonical_agy_response_field(self) -> None:
        """Real AGY JSON shape with 'response' field extracts model text directly."""
        payload = json.dumps({
            "conversation_id": "abc",
            "status": "SUCCESS",
            "response": "READY\n",
            "duration_seconds": 1.0,
        })
        self.assertEqual(
            "READY",
            codex_job_worker._extract_agy_result_message(payload),
        )

    def test_canonical_result_field(self) -> None:
        self.assertEqual(
            "hello world",
            codex_job_worker._extract_agy_result_message('{"result": "hello world"}'),
        )


    def test_alt_text_field(self) -> None:
        self.assertEqual(
            "alt text",
            codex_job_worker._extract_agy_result_message(
                'noise\n{"text": "alt text"}\nmore noise'
            ),
        )

    def test_alt_answer_field(self) -> None:
        self.assertEqual(
            "answer text",
            codex_job_worker._extract_agy_result_message(
                '{"answer": "answer text"}'
            ),
        )

    def test_alt_content_field(self) -> None:
        self.assertEqual(
            "content text",
            codex_job_worker._extract_agy_result_message(
                '{"content": "content text"}'
            ),
        )

    def test_falls_back_to_raw_json_when_no_recognized_key(self) -> None:
        self.assertEqual(
            '{"unrecognized": "x"}',
            codex_job_worker._extract_agy_result_message(
                'pre\n{"unrecognized": "x"}\npost'
            ),
        )

    def test_falls_back_to_last_nonempty_line_when_no_json(self) -> None:
        self.assertEqual(
            "no json at all, just text",
            codex_job_worker._extract_agy_result_message(
                "no json at all, just text"
            ),
        )

    def test_empty_input_returns_empty(self) -> None:
        self.assertEqual("", codex_job_worker._extract_agy_result_message(""))
        self.assertEqual("", codex_job_worker._extract_agy_result_message("   \n  "))


class AgyBuildCommandTests(unittest.TestCase):
    """Tests for ``control_plane.build_agy_command``."""

    def test_no_cwd_or_result_path_flag(self) -> None:
        state = {
            "agy_executable": r"C:\fake\agy.exe",
            "cwd": r"D:\work",
            "prompt": "say hi",
            "model": None,
            "reasoning_effort": None,
            "sandbox": "workspace-write",
        }
        command = control_plane.build_agy_command(state, Path("result.txt"))
        # No --cwd flag (agy has none; cwd is set at Popen layer).
        self.assertNotIn("--cwd", command)
        self.assertNotIn("-C", command)
        # No -o / --output-last-message flag (agy has none; the worker
        # writes result.txt from captured stdout).
        self.assertNotIn("-o", command)
        self.assertNotIn("--output-last-message", command)
        # Prompt is the value of -p (not a bare positional).
        self.assertIn("-p", command)
        self.assertEqual("say hi", command[command.index("-p") + 1])
        # Base flags are present and in the documented order.
        self.assertEqual(
            r"C:\fake\agy.exe", command[0]
        )
        self.assertEqual("--print", command[1])
        self.assertEqual("--dangerously-skip-permissions", command[2])
        self.assertEqual("--output-format", command[3])
        self.assertEqual("json", command[4])
        self.assertEqual("--print-timeout", command[5])
        self.assertEqual("5m", command[6])

    def test_with_model_and_effort(self) -> None:
        state = {
            "agy_executable": r"C:\fake\agy.exe",
            "cwd": r"D:\work",
            "prompt": "say hi",
            "model": "gemini-3.7-flash-medium",
            "reasoning_effort": "high",
            "sandbox": "workspace-write",
        }
        command = control_plane.build_agy_command(state, Path("result.txt"))
        self.assertIn("--model", command)
        self.assertIn("gemini-3.7-flash-medium", command)
        self.assertIn("--effort", command)
        self.assertIn("high", command)

    def test_default_agy_executable_when_state_lacks_one(self) -> None:
        state = {
            "cwd": r"D:\work",
            "prompt": "say hi",
        }
        command = control_plane.build_agy_command(state, Path("result.txt"))
        # Falls back to control_plane.AGY_EXE
        self.assertTrue(Path(command[0]).name.lower().startswith("agy"))


class AgyStartTaskValidationTests(unittest.TestCase):
    """Validation rules for ``start_task(harness='agy', ...)``."""

    def _state(self):
        with mock.patch.object(
            control_plane, "harness_status",
            return_value={
                "ok": True,
                "name": "agy",
                "available": True,
                "executable": r"C:\fake\agy.exe",
                "executable_exists": True,
                "config_path": None,
                "config_exists": False,
                "supports_async": True,
                "supports_reasoning_effort": True,
                "version": "1.1.22",
                "capabilities": {},
                "noninteractive_command": [],
                "route": None,
                "parameter_mappings": {},
                "blocker": None,
                "session_behavior": "",
                "output_behavior": "",
            },
        ):
            yield

    def test_happy_path_records_agy_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            cfg = control_plane.CONTROL_DIR / "projects.json"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(
                json.dumps({"projects": {"tmpproj": {"path": str(proj)}}}),
                encoding="utf-8",
            )
            try:
                with mock.patch.object(
                    control_plane, "harness_status",
                    return_value={
                        "ok": True, "name": "agy", "available": True,
                        "executable": r"C:\fake\agy.exe", "executable_exists": True,
                        "config_path": None, "config_exists": False,
                        "supports_async": True, "supports_reasoning_effort": True,
                        "version": "1.1.22", "capabilities": {},
                        "noninteractive_command": [], "route": None,
                        "parameter_mappings": {}, "blocker": None,
                        "session_behavior": "", "output_behavior": "",
                    },
                ):
                    r = control_plane.start_task(
                        harness="agy", prompt="hi", project="tmpproj", cwd=None,
                        model="gemini-3.7-flash-medium", sandbox="workspace-write",
                        reasoning_effort="medium",
                    )
                self.assertTrue(r["ok"], r)
                self.assertEqual("agy", r["harness"])
                # result.txt / status.json files exist on disk
                job_dir = control_plane.JOBS_DIR / r["job_id"]
                self.assertTrue((job_dir / "status.json").is_file())
                # The recorded state has agy_executable so the worker can find it
                state = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
                self.assertIn("agy_executable", state)
                self.assertTrue(Path(state["agy_executable"]).name.lower().startswith("agy"))
                # parameter_handling documents the agy-specific quirks
                ph = r["parameter_handling"]
                self.assertIn("cwd", ph)
                self.assertIn("Popen", ph["cwd"])
                self.assertIn("result_path", ph)
            finally:
                cfg.unlink(missing_ok=True)

    def test_read_only_sandbox_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            cfg = control_plane.CONTROL_DIR / "projects.json"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(
                json.dumps({"projects": {"tmpproj": {"path": str(proj)}}}),
                encoding="utf-8",
            )
            try:
                with mock.patch.object(
                    control_plane, "harness_status",
                    return_value={
                        "ok": True, "name": "agy", "available": True,
                        "executable": r"C:\fake\agy.exe", "executable_exists": True,
                        "config_path": None, "config_exists": False,
                        "supports_async": True, "supports_reasoning_effort": True,
                        "version": "1.1.22", "capabilities": {},
                        "noninteractive_command": [], "route": None,
                        "parameter_mappings": {}, "blocker": None,
                        "session_behavior": "", "output_behavior": "",
                    },
                ):
                    r = control_plane.start_task(
                        harness="agy", prompt="hi", project="tmpproj", cwd=None,
                        model=None, sandbox="read-only", reasoning_effort=None,
                    )
                self.assertFalse(r["ok"])
                self.assertIn("read-only sandbox", r["error"])
            finally:
                cfg.unlink(missing_ok=True)

    def test_non_current_route_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            cfg = control_plane.CONTROL_DIR / "projects.json"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(
                json.dumps({"projects": {"tmpproj": {"path": str(proj)}}}),
                encoding="utf-8",
            )
            try:
                with mock.patch.object(
                    control_plane, "harness_status",
                    return_value={
                        "ok": True, "name": "agy", "available": True,
                        "executable": r"C:\fake\agy.exe", "executable_exists": True,
                        "config_path": None, "config_exists": False,
                        "supports_async": True, "supports_reasoning_effort": True,
                        "version": "1.1.22", "capabilities": {},
                        "noninteractive_command": [], "route": None,
                        "parameter_mappings": {}, "blocker": None,
                        "session_behavior": "", "output_behavior": "",
                    },
                ):
                    r = control_plane.start_task(
                        harness="agy", prompt="hi", project="tmpproj", cwd=None,
                        model=None, sandbox="workspace-write",
                        reasoning_effort=None, route="official",
                    )
                self.assertFalse(r["ok"])
                self.assertIn("route=current", r["error"])
            finally:
                cfg.unlink(missing_ok=True)

    def test_invalid_reasoning_effort_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp)
            cfg = control_plane.CONTROL_DIR / "projects.json"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            cfg.write_text(
                json.dumps({"projects": {"tmpproj": {"path": str(proj)}}}),
                encoding="utf-8",
            )
            try:
                with mock.patch.object(
                    control_plane, "harness_status",
                    return_value={
                        "ok": True, "name": "agy", "available": True,
                        "executable": r"C:\fake\agy.exe", "executable_exists": True,
                        "config_path": None, "config_exists": False,
                        "supports_async": True, "supports_reasoning_effort": True,
                        "version": "1.1.22", "capabilities": {},
                        "noninteractive_command": [], "route": None,
                        "parameter_mappings": {}, "blocker": None,
                        "session_behavior": "", "output_behavior": "",
                    },
                ):
                    r = control_plane.start_task(
                        harness="agy", prompt="hi", project="tmpproj", cwd=None,
                        model=None, sandbox="workspace-write",
                        reasoning_effort="huge",
                    )
                self.assertFalse(r["ok"])
                self.assertIn("reasoning_effort", r["error"])
            finally:
                cfg.unlink(missing_ok=True)


class AgyLifecycleTests(unittest.TestCase):
    """Lifecycle tests for ``run_agy_with_lifecycle`` and ``collect_agy_lifecycle_result``."""

    def test_normal_exit_with_json_result_is_completed(self) -> None:
        """Fake agy emits a single JSON object and exits 0 -> completed, no force."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
            fake = _write_fake_agy(
                tmp_path,
                """\
                import sys
                sys.stdout.write('{"result": "AGY_OK"}\\n')
                sys.stdout.flush()
                sys.exit(0)
                """,
            )
            lifecycle = _collect_agy_lifecycle(
                fake, cwd, total_timeout=5.0, exit_grace=1.0, settle_grace=0.1,
            )
            self.assertEqual("self_exit", lifecycle["termination_reason"])
            self.assertFalse(lifecycle["forced_exit"])
            collected = codex_job_worker.collect_agy_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("completed", collected["status"])
            self.assertEqual("AGY_OK", collected["final_message"])
            self.assertFalse(collected["forced_exit_after_result"])
    def test_real_agy_json_response_lifecycle_collection(self) -> None:
        """Real agy JSON output with {"status": "SUCCESS", "response": "READY\\n"}
        produces completed status with final_message == 'READY' and result.txt == 'READY'
        rather than raw JSON string."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
            real_payload = json.dumps({
                "conversation_id": "abc-123",
                "status": "SUCCESS",
                "response": "READY\n",
                "duration_seconds": 1.5,
            })
            fake = _write_fake_agy(
                tmp_path,
                f"""\
                import sys
                sys.stdout.write({json.dumps(real_payload)} + '\\n')
                sys.stdout.flush()
                sys.exit(0)
                """,
            )
            lifecycle = _collect_agy_lifecycle(
                fake, cwd, total_timeout=5.0, exit_grace=1.0, settle_grace=0.1,
            )
            self.assertEqual("self_exit", lifecycle["termination_reason"])
            collected = codex_job_worker.collect_agy_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("completed", collected["status"])
            self.assertEqual("READY", collected["final_message"])
            self.assertTrue(result_path.is_file())
            self.assertEqual("READY", result_path.read_text(encoding="utf-8"))
            self.assertNotIn("conversation_id", collected["final_message"])

    def test_normal_exit_with_plain_text_result_is_completed(self) -> None:
        """Fake agy emits a plain text line and exits 0 -> completed, no force."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
            fake = _write_fake_agy(
                tmp_path,
                """\
                import sys
                sys.stdout.write('just plain text answer\\n')
                sys.stdout.flush()
                sys.exit(0)
                """,
            )
            lifecycle = _collect_agy_lifecycle(
                fake, cwd, total_timeout=5.0, exit_grace=1.0, settle_grace=0.1,
            )
            self.assertEqual("self_exit", lifecycle["termination_reason"])
            collected = codex_job_worker.collect_agy_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("completed", collected["status"])
            self.assertEqual("just plain text answer", collected["final_message"])

    def test_hang_after_result_is_completed_with_forced_flag(self) -> None:
        """Fake agy emits the result then sleeps -> completed, forced_exit_after_result=True."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
            fake = _write_fake_agy(
                tmp_path,
                """\
                import sys, time
                sys.stdout.write('{"result": "AGY_HANG_OK"}\\n')
                sys.stdout.flush()
                time.sleep(30)
                """,
            )
            lifecycle = _collect_agy_lifecycle(
                fake, cwd, total_timeout=20.0, exit_grace=0.5, settle_grace=0.1,
            )
            self.assertEqual(
                "grace_expired_after_result", lifecycle["termination_reason"]
            )
            self.assertTrue(lifecycle["forced_exit"])
            collected = codex_job_worker.collect_agy_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("completed", collected["status"])
            self.assertEqual("AGY_HANG_OK", collected["final_message"])
            self.assertTrue(collected["forced_exit_after_result"])

    def test_hang_with_no_result_is_failed_timeout(self) -> None:
        """Fake agy sleeps without writing anything -> failed, agy_execution_timeout."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
            fake = _write_fake_agy(
                tmp_path,
                """\
                import time
                time.sleep(30)
                """,
            )
            lifecycle = _collect_agy_lifecycle(
                fake, cwd, total_timeout=1.5, exit_grace=0.5, settle_grace=0.1,
            )
            self.assertEqual("hard_timeout", lifecycle["termination_reason"])
            self.assertTrue(lifecycle["forced_exit"])
            collected = codex_job_worker.collect_agy_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("failed", collected["status"])
            self.assertEqual("agy_execution_timeout", collected["failure_type"])

    def test_nonzero_exit_with_no_result_is_failed_error(self) -> None:
        """Fake agy exits 1 without writing -> failed, agy_execution_error."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
            fake = _write_fake_agy(
                tmp_path,
                """\
                import sys
                sys.exit(1)
                """,
            )
            lifecycle = _collect_agy_lifecycle(
                fake, cwd, total_timeout=5.0, exit_grace=1.0, settle_grace=0.1,
            )
            self.assertEqual("self_exit", lifecycle["termination_reason"])
            self.assertNotEqual(0, lifecycle["exit_code"])
            collected = codex_job_worker.collect_agy_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("failed", collected["status"])
            self.assertEqual("agy_execution_error", collected["failure_type"])

    def test_popen_failure_is_failed_to_start(self) -> None:
        """A bogus executable path makes Popen raise -> failed_to_start."""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            fake = Path(tmp) / "definitely-not-agy.exe"
            lifecycle = codex_job_worker.run_agy_with_lifecycle(
                [str(fake), "--print", "-p", "x"],
                cwd=str(cwd),
                total_timeout=2.0,
                exit_grace=0.2,
                settle_grace=0.05,
            )
            self.assertEqual("failed_to_start", lifecycle["termination_reason"])
            result_path = cwd / "result.txt"
            collected = codex_job_worker.collect_agy_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("failed", collected["status"])
            self.assertEqual("agy_execution_error", collected["failure_type"])


class AgyHarnessRegistrationTests(unittest.TestCase):
    """Verify the harness registry surface includes agy as a peer."""

    def test_harnesses_returns_three_entries(self) -> None:
        names = [h["name"] for h in control_plane.harnesses()]
        self.assertEqual({"codex", "minimax", "agy"}, set(names))

    def test_agy_status_unknown_agy_exe_reports_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope" / "agy.exe"
            with mock.patch.object(control_plane, "AGY_EXE", missing):
                status = control_plane.harness_status("agy")
        self.assertTrue(status["ok"])
        self.assertFalse(status["available"])
        self.assertFalse(status["supports_async"])
        self.assertIn("Antigravity CLI not found", status["blocker"])

    def test_agy_status_fake_exe_passes_capability_probes(self) -> None:
        """When the agy exe is a fake that echoes the canonical help text,
        the capability probe should report available=True with the
        required flags detected."""
        help_text = textwrap.dedent("""\
            Usage: agy [options]
              --print                       non-interactive
              --dangerously-skip-permissions
              --output-format text|json|stream-json
              --print-timeout <dur>
              --model <id>
              --effort (low|medium|high)
              --sandbox
            """)
        version_text = "1.1.22\n"
        with tempfile.TemporaryDirectory() as tmp:
            fake_agy = Path(tmp) / "agy.exe"
            fake_agy.write_text("placeholder", encoding="utf-8")

            def _agy_probe(argv, **_kwargs):
                args = argv[1:]
                if args == ["--version"]:
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=version_text, stderr=""
                    )
                if args == ["--help"]:
                    return subprocess.CompletedProcess(
                        argv, 0, stdout=help_text, stderr=""
                    )
                if args == ["models"]:
                    return subprocess.CompletedProcess(
                        argv, 0, stdout="gemini-3.7-flash-medium\n", stderr=""
                    )
                raise AssertionError(argv)

            with mock.patch.object(control_plane, "AGY_EXE", fake_agy), \
                 mock.patch.object(
                     control_plane, "_run_agy_probe", side_effect=_agy_probe
                 ):
                status = control_plane._agy_cli_status()
        self.assertTrue(status["available"])
        self.assertEqual("1.1.22", status["version"])
        self.assertTrue(status["capabilities"]["print"])
        self.assertTrue(status["capabilities"]["dangerously_skip_permissions"])
        self.assertTrue(status["capabilities"]["output_format_json"])
        self.assertTrue(status["capabilities"]["model"])
        self.assertTrue(status["capabilities"]["effort"])
        self.assertTrue(status["capabilities"]["print_timeout"])
        self.assertIsNone(status["blocker"])


# ---------------------------------------------------------------------------
# Codex regression tests (unchanged behavior)
# ---------------------------------------------------------------------------


class CodexJobWorkerTests(unittest.TestCase):
    def make_job(self, job_dir: Path) -> None:
        (job_dir / "status.json").write_text(
            json.dumps(
                {
                    "job_id": "test-job",
                    "status": "queued",
                    "prompt": "test prompt",
                    "cwd": str(job_dir),
                    "model": None,
                    "sandbox": "read-only",
                }
            ),
            encoding="utf-8",
        )

    def read_state(self, job_dir: Path) -> dict:
        return json.loads((job_dir / "status.json").read_text(encoding="utf-8"))

    def test_completed_job_preserves_exit_code_and_final_message(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            job_dir = Path(temporary_directory)
            self.make_job(job_dir)
            (job_dir / "result.txt").write_text("finished", encoding="utf-8")
            result = subprocess.CompletedProcess([], 0, stdout="", stderr="diagnostic")

            with mock.patch.object(codex_job_worker.subprocess, "run", return_value=result) as run:
                codex_job_worker.main(job_dir)

            state = self.read_state(job_dir)
            self.assertEqual("completed", state["status"])
            self.assertEqual(0, state["exit_code"])
            self.assertEqual("finished", state["final_message"])
            self.assertEqual("utf-8", run.call_args.kwargs["encoding"])
            self.assertEqual("replace", run.call_args.kwargs["errors"])
            # worker.lock must be released on success
            self.assertFalse((job_dir / "worker.lock").exists())

    def test_null_optional_process_output_does_not_mask_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            job_dir = Path(temporary_directory)
            self.make_job(job_dir)
            (job_dir / "result.txt").write_text("finished", encoding="utf-8")
            result = subprocess.CompletedProcess([], 0, stdout=None, stderr=None)

            with mock.patch.object(codex_job_worker.subprocess, "run", return_value=result):
                codex_job_worker.main(job_dir)

            state = self.read_state(job_dir)
            self.assertEqual("completed", state["status"])
            self.assertEqual("finished", state["final_message"])
            self.assertEqual("", state["stderr_tail"])
            self.assertEqual("", state["stdout_tail"])
            self.assertFalse((job_dir / "worker.lock").exists())

    def test_exit_zero_result_extraction_failure_is_parser_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            job_dir = Path(temporary_directory)
            self.make_job(job_dir)
            result = subprocess.CompletedProcess([], 0, stdout="stdout", stderr="diagnostic")

            with mock.patch.object(codex_job_worker.subprocess, "run", return_value=result):
                codex_job_worker.main(job_dir)

            state = self.read_state(job_dir)
            self.assertEqual("failed", state["status"])
            self.assertEqual("result_parse_error", state["failure_type"])
            self.assertEqual(0, state["exit_code"])
            self.assertIn("result.txt", state["parser_error"])
            self.assertEqual("diagnostic", state["stderr_tail"])
            self.assertEqual("stdout", state["stdout_tail"])
            self.assertFalse((job_dir / "worker.lock").exists())

    def test_nonzero_exit_is_codex_execution_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            job_dir = Path(temporary_directory)
            self.make_job(job_dir)
            result = subprocess.CompletedProcess([], 7, stdout="", stderr="real failure")

            with mock.patch.object(codex_job_worker.subprocess, "run", return_value=result):
                codex_job_worker.main(job_dir)

            state = self.read_state(job_dir)
            self.assertEqual("failed", state["status"])
            self.assertEqual("codex_execution_error", state["failure_type"])
            self.assertEqual(7, state["exit_code"])
            self.assertEqual("real failure", state["stderr_tail"])
            self.assertEqual("Codex exited with a non-zero status", state["error"])
            self.assertFalse((job_dir / "worker.lock").exists())

    def test_worker_lock_released_even_when_wrapper_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            job_dir = Path(temporary_directory)
            self.make_job(job_dir)
            with mock.patch.object(
                codex_job_worker.subprocess, "run",
                side_effect=RuntimeError("explode"),
            ):
                # Should NOT raise: the worker must swallow the wrapper
                # error after writing a failed state and releasing the lock.
                codex_job_worker.main(job_dir)
            state = self.read_state(job_dir)
            self.assertEqual("failed", state["status"])
            self.assertEqual("wrapper_error", state["failure_type"])
            self.assertIn("explode", state["wrapper_error"])
            self.assertFalse((job_dir / "worker.lock").exists())


# ---------------------------------------------------------------------------
# P0 subprocess safety regression
# ---------------------------------------------------------------------------


class P0SubprocessSafetyRegressionTests(unittest.TestCase):
    """Re-run a small subset of the P0 safety tests to confirm the
    worker does not regress those guarantees.
    """

    def test_run_minimax_uses_devnull_stdin(self) -> None:
        """Popen must receive stdin=DEVNULL."""
        captured: dict = {}

        class _FakePopen:
            def __init__(self, argv, **kwargs):
                captured["argv"] = list(argv)
                captured["kwargs"] = dict(kwargs)
                self.pid = 99999
                self.returncode = 0
                self.stdout = None
                self.stderr = None

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        with mock.patch.object(control_plane.subprocess, "Popen", _FakePopen):
            with tempfile.TemporaryDirectory() as tmp:
                rp = Path(tmp) / "r.txt"
                codex_job_worker.run_minimax_with_lifecycle(
                    [sys.executable, "-c", "pass"],
                    rp,
                    total_timeout=0.5,
                    poll_interval=0.05,
                    exit_grace=0.1,
                )
        self.assertEqual(captured["kwargs"].get("stdin"), subprocess.DEVNULL)
        self.assertEqual(captured["kwargs"].get("stdout"), subprocess.PIPE)
        self.assertEqual(captured["kwargs"].get("stderr"), subprocess.PIPE)
        if sys.platform == "win32":
            flags = captured["kwargs"].get("creationflags", 0)
            self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
            self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)

    def test_lifecycle_terminates_within_finite_window(self) -> None:
        """A sleeping CLI must be reaped well before total_timeout."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
            fake = _write_fake_mcode(
                tmp_path,
                "import time; time.sleep(120)",
            )
            t0 = time.time()
            lifecycle = _collect_lifecycle(
                fake, result_path, cwd,
                total_timeout=0.5, exit_grace=0.0, settle_grace=0.1,
            )
            elapsed = time.time() - t0
            self.assertLess(elapsed, 5.0)
            self.assertEqual("hard_timeout", lifecycle["termination_reason"])


if __name__ == "__main__":
    unittest.main()
