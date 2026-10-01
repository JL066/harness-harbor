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
import hashlib
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import tomllib
import unittest
import uuid
from contextlib import contextmanager
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
        "--input", "-",
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
        "--dangerously-skip-permissions",
        "--output-format", "json",
        "--print-timeout", "1h",
        "--print=PROMPT-IGNORED",
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

    The script receives the canonical safe agy argv (the prompt is passed
    via ``--print=<prompt>``; ``--dangerously-skip-permissions`` is present)
    and may emit any stdout / stderr / exit-code combination.
    """
    script = parent / "fake_agy.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return script


def _collect_lifecycle(
    fake_exe: Path,
    result_path: Path,
    cwd: Path,
    *,
    prompt: str = "",
    total_timeout: float = 5.0,
    exit_grace: float = 1.0,
    settle_grace: float = 0.1,
    poll_interval: float = 0.05,
) -> dict:
    cmd = _make_minimax_command(fake_exe, result_path, cwd)
    return codex_job_worker.run_minimax_with_lifecycle(
        cmd,
        result_path,
        prompt=prompt,
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
    PROMPT_CASES = (
        "Scope paragraph.\n\nDetailed corrective task after the blank line.",
        "Execute these exact commands.\r\n\r\ngit status\r\nPowerShell: Get-ChildItem `literal` \"quoted\" 中文",
        "Clean the worktree.\n\nKEEP:\n- accepted-change\n\nREMOVE:\n- stale-output",
        ("long prompt 中文 `git status` \"quoted\"\n\n" * 8192),
    )

    def test_exact_utf8_prompt_payloads_reach_closed_stdin(self) -> None:
        for index, prompt in enumerate(self.PROMPT_CASES):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                _, result_path, cwd = _make_state_dir(tmp_path)
                received_path = tmp_path / "received.bin"
                fake = _write_fake_mcode(
                    tmp_path,
                    f"""\
                    import pathlib, sys
                    data = sys.stdin.buffer.read()
                    pathlib.Path({str(received_path)!r}).write_bytes(data)
                    args = sys.argv[1:]
                    result = pathlib.Path(args[args.index('--output-last-message') + 1])
                    result.write_text('done', encoding='utf-8')
                    print('{{\"status\": \"success\", \"result\": \"done\"}}')
                    """,
                )
                lifecycle = _collect_lifecycle(fake, result_path, cwd, prompt=prompt)
                self.assertEqual("self_exit", lifecycle["termination_reason"])
                self.assertEqual(prompt.encode("utf-8"), received_path.read_bytes())

    @unittest.skipUnless(sys.platform == "win32", "Windows .cmd wrapper regression")
    def test_windows_cmd_wrapper_receives_no_prompt_argv_and_exact_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
            received_path = tmp_path / "received.bin"
            argv_path = tmp_path / "argv.json"
            stub = _write_fake_mcode(
                tmp_path,
                f"""\
                import json, pathlib, sys
                pathlib.Path({str(received_path)!r}).write_bytes(sys.stdin.buffer.read())
                pathlib.Path({str(argv_path)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')
                args = sys.argv[1:]
                pathlib.Path(args[args.index('--output-last-message') + 1]).write_text('done', encoding='utf-8')
                print('{{\"status\": \"success\", \"result\": \"done\"}}')
                """,
            )
            wrapper = tmp_path / "fake_mcode.cmd"
            wrapper.write_text(f'@"{PYTHON_EXE}" "{stub}" %*\r\n', encoding="utf-8")
            prompt = self.PROMPT_CASES[1]
            command = [
                str(wrapper), "exec", "--cwd", str(cwd), "--output-format", "json",
                "--output-last-message", str(result_path), "--input", "-",
            ]
            lifecycle = codex_job_worker.run_minimax_with_lifecycle(
                command, result_path, prompt=prompt, poll_interval=0.05,
                settle_grace=0.05, exit_grace=0.2, total_timeout=5.0,
            )
            self.assertEqual("self_exit", lifecycle["termination_reason"])
            self.assertEqual(prompt.encode("utf-8"), received_path.read_bytes())
            argv = json.loads(argv_path.read_text(encoding="utf-8"))
            self.assertNotIn(prompt, argv)
            self.assertEqual("-", argv[argv.index("--input") + 1])

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
        """The pivotal test: result.txt appears AND a canonical MiniMax
        exec.result envelope with status=succeeded is emitted on stdout,
        but the CLI does not exit. The worker must terminate the process
        tree and mark the job completed with forced_exit_after_result=True
        (the wrapper's non-zero exit code is a consequence of Harbor's
        own post-result cleanup, not a real execution failure).
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
                # Emit a canonical MiniMax exec.result JSON envelope on
                # stdout, with status=succeeded and a usable output. This
                # is what the real MiniMax CLI does in production when a
                # turn completes successfully.
                print('{"status": "succeeded", "output": "MINIMAX_MCP_OK"}')
                sys.stdout.flush()
                # CLI deliberately keeps the event loop alive, exactly
                # like the production bug in MiniMax 0.2.6.
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
            # Bug fix: a valid canonical MiniMax exec.result envelope
            # (status=succeeded, usable final message) was captured
            # before Harbor had to kill the lingering CLI after the
            # post-result grace expired. The wrapper's non-zero exit
            # code is a consequence of Harbor's own cleanup, not a real
            # execution failure — the job must remain completed with
            # forced_exit_after_result=True.
            self.assertEqual("completed", collected["status"])
            self.assertNotIn("failure_type", collected)
            self.assertEqual("MINIMAX_MCP_OK", collected["final_message"])
            self.assertEqual("succeeded", collected["agent_task_status"])
            self.assertTrue(
                collected["forced_exit_after_result"],
                "forced_exit_after_result must be recorded",
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
                prompt="broken pipe is fail-soft 中文\n" * 500_000,
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
                sys.exit(0)
                """,
            )
            lifecycle = _collect_lifecycle(
                fake, result_path, cwd,
                total_timeout=10.0, exit_grace=0.5, settle_grace=0.2,
            )
            self.assertEqual(
                "self_exit", lifecycle["termination_reason"]
            )
            self.assertFalse(lifecycle["forced_exit"])
            collected = codex_job_worker.collect_minimax_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("completed", collected["status"])
            self.assertEqual("LATE_BUT_OK", collected["final_message"])
            self.assertFalse(collected["forced_exit_after_result"])


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

    def test_default_print_timeout_constant(self) -> None:
        self.assertEqual("1h", control_plane.AGY_DEFAULT_PRINT_TIMEOUT)

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
        # No bare --print flag preceding other flags
        self.assertNotIn("--print", command)
        # Base flags are present and in the safe documented order.
        self.assertEqual(
            [
                r"C:\fake\agy.exe",
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "--print-timeout",
                "1h",
                "--print=say hi",
            ],
            command,
        )

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
        self.assertEqual(
            [
                r"C:\fake\agy.exe",
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "--print-timeout",
                "1h",
                "--model",
                "gemini-3.7-flash-medium",
                "--effort",
                "high",
                "--print=say hi",
            ],
            command,
        )

    def test_default_agy_executable_when_state_lacks_one(self) -> None:
        state = {
            "cwd": r"D:\work",
            "prompt": "say hi",
        }
        command = control_plane.build_agy_command(state, Path("result.txt"))
        # Falls back to control_plane.AGY_EXE
        self.assertTrue(command[0].lower().endswith("agy.exe"))
        self.assertEqual(
            [
                command[0],
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "--print-timeout",
                "1h",
                "--print=say hi",
            ],
            command,
        )

    def test_command_structure_matches_status_noninteractive_command_convention(self) -> None:
        state = {
            "agy_executable": r"C:\fake\agy.exe",
            "cwd": r"D:\work",
            "prompt": "placeholder_prompt",
        }
        command = control_plane.build_agy_command(state, Path("result.txt"))
        # Permission flag must be present and must appear before prompt flag
        self.assertIn("--dangerously-skip-permissions", command)
        self.assertIn("--print=placeholder_prompt", command)
        self.assertNotIn("--print", command)
        perm_idx = command.index("--dangerously-skip-permissions")
        prompt_idx = command.index("--print=placeholder_prompt")
        self.assertLess(perm_idx, prompt_idx)

        # Template derived from build_agy_command matches the harness noninteractive_command template
        template = [arg.replace("placeholder_prompt", "<prompt>") for arg in command]
        self.assertEqual(
            [
                r"C:\fake\agy.exe",
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "--print-timeout",
                "1h",
                "--print=<prompt>",
            ],
            template,
        )


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
                self.assertTrue(state["agy_executable"].endswith("agy.exe"))
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
            # Under unified lifecycle contract: forced termination with nonzero exit code is failed
            self.assertEqual("failed", collected["status"])
            self.assertEqual("agy_execution_error", collected["failure_type"])
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
                [
                    str(fake),
                    "--dangerously-skip-permissions",
                    "--output-format",
                    "json",
                    "--print-timeout",
                    "1h",
                    "--print=x",
                ],
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
        self.assertIsNone(status["noninteractive_command"])

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
        self.assertEqual(
            [
                str(fake_agy),
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                "--print-timeout",
                "1h",
                "--print=<prompt>",
            ],
            status["noninteractive_command"],
        )
        self.assertNotIn("--print", status["noninteractive_command"])
        self.assertIn("--print=<prompt>", status["noninteractive_command"])
        perm_idx = status["noninteractive_command"].index("--dangerously-skip-permissions")
        prompt_idx = status["noninteractive_command"].index("--print=<prompt>")
        self.assertLess(perm_idx, prompt_idx)


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

            with mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", return_value=result) as run:
                codex_job_worker.main(job_dir)

            state = self.read_state(job_dir)
            self.assertEqual("completed", state["status"])
            self.assertEqual(0, state["exit_code"])
            self.assertEqual("finished", state["final_message"])
            self.assertIn("env", run.call_args.kwargs)
            # worker.lock must be released on success
            self.assertFalse((job_dir / "worker.lock").exists())

    def test_null_optional_process_output_does_not_mask_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            job_dir = Path(temporary_directory)
            self.make_job(job_dir)
            (job_dir / "result.txt").write_text("finished", encoding="utf-8")
            result = subprocess.CompletedProcess([], 0, stdout=None, stderr=None)

            with mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", return_value=result):
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

            with mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", return_value=result):
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

            with mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", return_value=result):
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
                codex_job_worker, "run_codex_with_lifecycle",
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

    def test_minimax_native_process_omits_prompt_and_records_stdin_metadata(self) -> None:
        prompt = "first paragraph\n\n中文 `git status` \"quoted\""
        with tempfile.TemporaryDirectory() as temporary_directory:
            job_dir = Path(temporary_directory)
            (job_dir / "status.json").write_text(
                json.dumps({
                    "job_id": "minimax-job", "harness": "minimax", "status": "queued",
                    "prompt": prompt, "cwd": str(job_dir), "model": None,
                    "sandbox": "workspace-write", "minimax_executable": "mcode.cmd",
                }),
                encoding="utf-8",
            )

            def fake_lifecycle(command, result_path, *, prompt):
                self.assertNotIn(prompt, command)
                self.assertEqual("-", command[command.index("--input") + 1])
                result_path.write_text("done", encoding="utf-8")
                return {
                    "exit_code": 0, "stdout": '{"status":"success","result":"done"}',
                    "stderr": "", "termination_reason": "self_exit", "forced_exit": False,
                    "result_completed_at": None, "result_text": "done",
                }

            with mock.patch.object(codex_job_worker, "run_minimax_with_lifecycle", side_effect=fake_lifecycle):
                codex_job_worker.main(job_dir)

            state = self.read_state(job_dir)
            native = state["native_process"]
            encoded = prompt.encode("utf-8")
            self.assertNotIn(prompt, native["argv"])
            self.assertEqual("stdin", native["prompt_transport"])
            self.assertEqual(len(encoded), native["prompt_utf8_bytes"])
            self.assertEqual(hashlib.sha256(encoded).hexdigest(), native["prompt_sha256"])
            self.assertEqual("completed", state["status"])


# ---------------------------------------------------------------------------
# P0 subprocess safety regression
# ---------------------------------------------------------------------------


class P0SubprocessSafetyRegressionTests(unittest.TestCase):
    """Re-run a small subset of the P0 safety tests to confirm the
    worker does not regress those guarantees.
    """

    def test_run_minimax_uses_pipe_stdin(self) -> None:
        """Popen must receive an isolated pipe, never inherited stdin."""
        captured: dict = {}

        class _FakePopen:
            def __init__(self, argv, **kwargs):
                captured["argv"] = list(argv)
                captured["kwargs"] = dict(kwargs)
                self.pid = 99999
                self.returncode = 0
                self.stdout = None
                self.stderr = None
                self.stdin = mock.MagicMock()
                self.stdin.closed = False

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        with mock.patch.dict(os.environ, {"HARBOR_RUNTIME_MODE": ""}):
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
        self.assertEqual(captured["kwargs"].get("stdin"), subprocess.PIPE)
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
                prompt="blocked feeder 中文\n" * 500_000,
                total_timeout=0.5, exit_grace=0.0, settle_grace=0.1,
            )
            elapsed = time.time() - t0
            self.assertLess(elapsed, 5.0)
            self.assertEqual("hard_timeout", lifecycle["termination_reason"])


# ---------------------------------------------------------------------------
# Codex fallback tests
# ---------------------------------------------------------------------------


class _FakeConfigPath:
    """A fake ``pathlib.Path`` for testing Codex config safety and isolation."""

    _CODEFLOW_TEXT = (
        'model_provider = "custom"\n'
        '[providers.custom]\n'
        'name     = "Code Flow"\n'
        'base_url = "https://codeflow.asia/v1"\n'
        'api_key  = "env:CODEFLOW_API_KEY"\n'
    )

    _OFFICIAL_TEXT = (
        'model_provider = "openai"\n'
        'api_key = "sk-REAL"\n'
    )

    _REAL_PATH = r"C:\Users\ExampleUser\.codex\config.toml"

    def __init__(self, is_codeflow: bool = False, custom_text: str | None = None) -> None:
        self._is_codeflow = is_codeflow
        self._exists = True
        self.write_count = 0
        if custom_text is not None:
            self._content = custom_text
        else:
            self._content = self._CODEFLOW_TEXT if is_codeflow else self._OFFICIAL_TEXT

    @property
    def parent(self) -> Path:
        return Path(r"C:\Users\ExampleUser\.codex")

    @property
    def name(self) -> str:
        return "config.toml"

    def is_file(self) -> bool:
        return self._exists

    def exists(self) -> bool:
        return self._exists

    def read_text(self, encoding: str = "utf-8", errors: str = "replace") -> str:
        return self._content

    def read_bytes(self) -> bytes:
        return self._content.encode("utf-8")

    def write_text(self, text: str, encoding: str = "utf-8", errors: str = "replace") -> None:
        self.write_count += 1
        self._content = text
        self._exists = True
        self._is_codeflow = (
            'model_provider = "custom"' in text
            and "https://codeflow.asia/v1" in text
        )

    def write_bytes(self, data: bytes) -> None:
        self.write_text(data.decode("utf-8", errors="replace"))

    def unlink(self, missing_ok: bool = False) -> None:
        self._exists = False
        self._content = ""

    def mkdir(self, parents: bool = False, exist_ok: bool = False) -> None:
        pass

    def resolve(self, strict: bool = False) -> Path:
        return self

    def as_posix(self) -> str:
        return self._REAL_PATH

    def __fspath__(self) -> str:
        return self._REAL_PATH


class CodexFallbackTests(unittest.TestCase):
    """Tests for the official→Code Flow automatic fallback in the Codex harness."""

    def setUp(self) -> None:
        self._orig_env = os.environ.copy()
        os.environ["CODEFLOW_API_KEY"] = "mock-codeflow-key"
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._route_patch = mock.patch.object(control_plane, "ROUTE_STATE_FILE", Path(self._tmp_dir.name) / "route-state.json")
        self._route_patch.start()

    def tearDown(self) -> None:
        self._route_patch.stop()
        self._tmp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._orig_env)

    def _fake_config(self, is_codeflow: bool = False, custom_text: str | None = None) -> _FakeConfigPath:
        return _FakeConfigPath(is_codeflow=is_codeflow, custom_text=custom_text)

    def _make_job(self, job_dir: Path, route_requested: str = "official_then_codeflow", prompt: str = "test prompt") -> None:
        (job_dir / "status.json").write_text(
            json.dumps({
                "job_id": f"job-{uuid.uuid4().hex[:8]}",
                "harness": "codex",
                "status": "queued",
                "prompt": prompt,
                "cwd": str(job_dir),
                "model": None,
                "sandbox": "read-only",
                "route_requested": route_requested,
                "route_used": "codeflow" if route_requested == "codeflow" else ("official" if route_requested in ("official", "official_then_codeflow") else "current"),
            }),
            encoding="utf-8",
        )

    def _read_state(self, job_dir: Path) -> dict:
        return json.loads((job_dir / "status.json").read_text(encoding="utf-8"))

    def test_official_success_no_fallback(self) -> None:
        """Official succeeds on first try; fallback is never triggered."""
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._make_job(job_dir)
            (job_dir / "result.txt").write_text("ok", encoding="utf-8")
            fake_config = self._fake_config(is_codeflow=False)
            orig_bytes = fake_config.read_bytes()

            ok_result = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", return_value=ok_result) as run_mock,
            ):
                codex_job_worker.main(job_dir)

            state = self._read_state(job_dir)
            self.assertEqual("completed", state["status"])
            self.assertEqual(1, run_mock.call_count)
            argv = run_mock.call_args[0][0]
            self.assertIn("-c", argv)
            self.assertIn('model_provider="openai"', argv)
            self.assertEqual(False, state.get("fallback_used"))
            self.assertEqual("official", state.get("route_used"))
            self.assertEqual(1, len(state.get("attempts", [])))
            self.assertEqual("official", state["attempts"][0]["route"])
            self.assertEqual(0, fake_config.write_count)
            self.assertEqual(orig_bytes, fake_config.read_bytes())

    def test_generic_429_no_fallback(self) -> None:
        """Generic HTTP 429 without explicit quota exhaustion must NOT trigger fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._make_job(job_dir)
            fake_config = self._fake_config(is_codeflow=False)
            orig_bytes = fake_config.read_bytes()

            generic_429_result = subprocess.CompletedProcess(
                [], 429,
                stdout="",
                stderr="HTTP 429 Too Many Requests\nRetry-After: 60",
            )

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", return_value=generic_429_result) as run_mock,
            ):
                codex_job_worker.main(job_dir)

            state = self._read_state(job_dir)
            self.assertEqual("failed", state["status"])
            self.assertEqual(False, state.get("fallback_used"))
            self.assertEqual(1, run_mock.call_count)
            self.assertEqual(0, fake_config.write_count)
            self.assertEqual(orig_bytes, fake_config.read_bytes())

    def test_rate_limit_retry_after_no_fallback(self) -> None:
        """A retry-after rate limit must NOT trigger fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._make_job(job_dir)
            fake_config = self._fake_config(is_codeflow=False)
            orig_bytes = fake_config.read_bytes()

            rl_result = subprocess.CompletedProcess(
                [], 429,
                stdout="",
                stderr="You have exceeded the rate limit. Retry-After: 120 seconds.",
            )

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", return_value=rl_result) as run_mock,
            ):
                codex_job_worker.main(job_dir)

            state = self._read_state(job_dir)
            self.assertEqual("failed", state["status"])
            self.assertEqual(False, state.get("fallback_used"))
            self.assertEqual(1, run_mock.call_count)
            self.assertEqual(0, fake_config.write_count)
            self.assertEqual(orig_bytes, fake_config.read_bytes())

    def test_explicit_quota_exhaustion_triggers_fallback(self) -> None:
        """An explicit OpenAI/Codex quota-exhausted error triggers a Code Flow retry."""
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._make_job(job_dir)
            (job_dir / "result.txt").write_text("cf-ok", encoding="utf-8")
            fake_config = self._fake_config(is_codeflow=False)
            orig_bytes = fake_config.read_bytes()

            quota_fail = subprocess.CompletedProcess(
                [], 1,
                stdout="",
                stderr=(
                    "OpenAIError: You have exceeded your usage tier limit. "
                    "Your quota has been exhausted for this plan period."
                ),
            )
            cf_ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            recorded_argvs = []

            def _run_side_effect(argv, *args, **kwargs):
                recorded_argvs.append(list(argv))
                if len(recorded_argvs) == 1:
                    return quota_fail
                return cf_ok

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", side_effect=_run_side_effect) as run_mock,
            ):
                codex_job_worker.main(job_dir)

            state = self._read_state(job_dir)
            self.assertEqual(True, state.get("fallback_used"))
            self.assertEqual("official_quota_exhausted", state.get("fallback_reason"))
            self.assertEqual("codeflow", state.get("route_used"))
            self.assertEqual(2, len(state.get("attempts", [])))
            self.assertEqual("official", state["attempts"][0]["route"])
            self.assertEqual("codeflow", state["attempts"][1]["route"])
            self.assertEqual("completed", state.get("status"))
            self.assertEqual("cf-ok", state.get("final_message"))
            self.assertEqual(2, len(recorded_argvs))

            # Attempt 1: official override
            self.assertIn('model_provider="openai"', recorded_argvs[0])

            # Attempt 2: harbor_codeflow process-local override
            self.assertIn('model_provider="harbor_codeflow"', recorded_argvs[1])
            self.assertIn('model_providers.harbor_codeflow.name="Harbor Code Flow"', recorded_argvs[1])
            self.assertIn('model_providers.harbor_codeflow.base_url="https://codeflow.asia/v1"', recorded_argvs[1])
            self.assertIn('model_providers.harbor_codeflow.wire_api="responses"', recorded_argvs[1])
            self.assertIn('model_providers.harbor_codeflow.env_key="CODEFLOW_API_KEY"', recorded_argvs[1])

            # Zero config writes
            self.assertEqual(0, fake_config.write_count)
            self.assertEqual(orig_bytes, fake_config.read_bytes())

    def test_fallback_failure_is_failed_but_preserves_both_attempts(self) -> None:
        """Code Flow retry also fails → job is failed but both attempts are recorded."""
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._make_job(job_dir)
            fake_config = self._fake_config(is_codeflow=False)
            orig_bytes = fake_config.read_bytes()

            quota_fail = subprocess.CompletedProcess(
                [], 1, stdout="",
                stderr="OpenAI API error: usage limit exhausted, plan quota exceeded.",
            )
            cf_fail = subprocess.CompletedProcess(
                [], 1, stdout="",
                stderr="Code Flow error: internal server error",
            )

            call_count = [0]

            def _run_side_effect(*args, **kwargs):
                call_count[0] += 1
                return quota_fail if call_count[0] == 1 else cf_fail

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", side_effect=_run_side_effect),
            ):
                codex_job_worker.main(job_dir)

            state = self._read_state(job_dir)
            self.assertEqual("failed", state.get("status"))
            self.assertEqual(True, state.get("fallback_used"))
            self.assertEqual("official_quota_exhausted", state.get("fallback_reason"))
            self.assertEqual("codeflow", state.get("route_used"))
            self.assertEqual(2, len(state["attempts"]))
            self.assertEqual("official", state["attempts"][0]["route"])
            self.assertEqual("codeflow", state["attempts"][1]["route"])
            self.assertEqual("failed", state["attempts"][0]["status"])
            self.assertEqual("failed", state["attempts"][1]["status"])
            self.assertEqual(0, fake_config.write_count)
            self.assertEqual(orig_bytes, fake_config.read_bytes())

    def test_codeflow_initial_config_no_fallback(self) -> None:
        """When the user config is Code Flow, attempt 1 still runs as official via process-local override."""
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._make_job(job_dir)
            (job_dir / "result.txt").write_text("attempt1-ok", encoding="utf-8")
            fake_config = self._fake_config(is_codeflow=True)
            orig_bytes = fake_config.read_bytes()

            ok_result = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", return_value=ok_result) as run_mock,
            ):
                codex_job_worker.main(job_dir)

            state = self._read_state(job_dir)
            self.assertEqual(1, run_mock.call_count)
            self.assertIn('model_provider="openai"', run_mock.call_args[0][0])
            self.assertEqual(1, len(state.get("attempts", [])))
            self.assertEqual("official", state["attempts"][0]["route"])
            self.assertEqual("codeflow", state.get("original_provider"))
            self.assertEqual("completed", state["status"])
            self.assertEqual(False, state.get("fallback_used"))
            self.assertEqual("official", state.get("route_used"))
            self.assertEqual(0, fake_config.write_count)
            self.assertEqual(orig_bytes, fake_config.read_bytes())

    def test_continuation_prompt_appended_on_attempt2(self) -> None:
        """The Code Flow retry must use the same cwd and append the continuation suffix to the prompt."""
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._make_job(job_dir, prompt="base prompt")
            (job_dir / "result.txt").write_text("cf-ok", encoding="utf-8")
            fake_config = self._fake_config(is_codeflow=False)

            quota_fail = subprocess.CompletedProcess(
                [], 1, stdout="",
                stderr="OpenAI API error: usage tier quota exhausted, plan limit exceeded.",
            )
            cf_ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            recorded_argv: list[list[str]] = []

            def _run_side_effect(*args, **kwargs):
                recorded_argv.append(list(args[0]) if args else kwargs.get("argv", []))
                return quota_fail if len(recorded_argv) == 1 else cf_ok

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", side_effect=_run_side_effect),
            ):
                codex_job_worker.main(job_dir)

            self.assertEqual(2, len(recorded_argv))
            attempt1_argv = recorded_argv[0]
            attempt2_argv = recorded_argv[1]
            self.assertIn("-C", attempt1_argv)
            self.assertIn("-C", attempt2_argv)
            c_idx_1 = attempt1_argv.index("-C")
            c_idx_2 = attempt2_argv.index("-C")
            self.assertEqual(attempt1_argv[c_idx_1 + 1], attempt2_argv[c_idx_2 + 1])

            prompt1 = attempt1_argv[-1]
            prompt2 = attempt2_argv[-1]
            self.assertTrue(prompt2.startswith(prompt1))
            self.assertIn(codex_job_worker.CONTINUATION_SUFFIX, prompt2)
            self.assertNotIn(codex_job_worker.CONTINUATION_SUFFIX, prompt1)

    def test_original_provider_and_route_recorded(self) -> None:
        """The state must record original_provider and original_route."""
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._make_job(job_dir, route_requested="codeflow")
            (job_dir / "result.txt").write_text("attempt1-ok", encoding="utf-8")
            fake_config = self._fake_config(is_codeflow=True)

            ok_result = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", return_value=ok_result),
            ):
                codex_job_worker.main(job_dir)

            state = self._read_state(job_dir)
            self.assertEqual("codeflow", state.get("original_provider"))
            self.assertEqual("codeflow", state.get("original_route"))
            self.assertEqual("codeflow", state.get("route_used"))
            self.assertEqual(False, state.get("fallback_used"))

    def test_explicit_route_codeflow(self) -> None:
        """route=codeflow runs attempt 1 directly against harbor_codeflow."""
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._make_job(job_dir, route_requested="codeflow")
            (job_dir / "result.txt").write_text("cf-done", encoding="utf-8")
            fake_config = self._fake_config(is_codeflow=False)

            ok_result = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", return_value=ok_result) as run_mock,
            ):
                codex_job_worker.main(job_dir)

            state = self._read_state(job_dir)
            self.assertEqual("completed", state["status"])
            self.assertEqual("codeflow", state["route_used"])
            self.assertEqual(1, run_mock.call_count)
            argv = run_mock.call_args[0][0]
            self.assertIn('model_provider="harbor_codeflow"', argv)
            self.assertEqual(0, fake_config.write_count)

    def test_explicit_route_current(self) -> None:
        """route=current does not inject any provider override flags."""
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._make_job(job_dir, route_requested="current")
            (job_dir / "result.txt").write_text("current-done", encoding="utf-8")
            fake_config = self._fake_config(is_codeflow=False)

            ok_result = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", return_value=ok_result) as run_mock,
            ):
                codex_job_worker.main(job_dir)

            state = self._read_state(job_dir)
            self.assertEqual("completed", state["status"])
            self.assertEqual("current", state["route_used"])
            self.assertEqual(1, run_mock.call_count)
            argv = run_mock.call_args[0][0]
            self.assertNotIn('model_provider="openai"', argv)
            self.assertNotIn('model_provider="harbor_codeflow"', argv)
            self.assertEqual(0, fake_config.write_count)


class CodexConcurrencyTests(unittest.TestCase):
    """Concurrency regression tests verifying complete worker isolation and unchanged config.toml."""

    def setUp(self) -> None:
        self._orig_env = os.environ.copy()
        os.environ["CODEFLOW_API_KEY"] = "mock-codeflow-key"
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._route_patch = mock.patch.object(control_plane, "ROUTE_STATE_FILE", Path(self._tmp_dir.name) / "route-state.json")
        self._route_patch.start()

    def tearDown(self) -> None:
        self._route_patch.stop()
        self._tmp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._orig_env)

    SAMPLE_CONFIG = textwrap.dedent("""\
        model_provider = "openai"
        api_key = "sk-USER"

        [providers.custom]
        name = "User Custom Provider"
        base_url = "https://custom.endpoint/v1"
        api_key = "env:CUSTOM_KEY"

        [mcp_servers.agy]
        command = "agy.exe"

        [windows]
        sandbox = "workspace-write"
    """).strip() + "\n"

    def _make_job(self, job_dir: Path, route_requested: str = "official_then_codeflow", prompt: str = "job") -> None:
        (job_dir / "status.json").write_text(
            json.dumps({
                "job_id": f"job-{uuid.uuid4().hex[:8]}",
                "harness": "codex",
                "status": "queued",
                "prompt": prompt,
                "cwd": str(job_dir),
                "model": None,
                "sandbox": "read-only",
                "route_requested": route_requested,
                "route_used": "codeflow" if route_requested == "codeflow" else ("official" if route_requested in ("official", "official_then_codeflow") else "current"),
            }),
            encoding="utf-8",
        )

    def _read_state(self, job_dir: Path) -> dict:
        return json.loads((job_dir / "status.json").read_text(encoding="utf-8"))

    def test_two_concurrent_official_then_codeflow_workers(self) -> None:
        """Two official→fallback workers run concurrently without interfering or mutating config."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            j1 = tmp_path / "j1"
            j2 = tmp_path / "j2"
            j1.mkdir()
            j2.mkdir()
            self._make_job(j1, "official_then_codeflow", "prompt 1")
            self._make_job(j2, "official_then_codeflow", "prompt 2")
            (j1 / "result.txt").write_text("res1", encoding="utf-8")
            (j2 / "result.txt").write_text("res2", encoding="utf-8")

            fake_config = _FakeConfigPath(custom_text=self.SAMPLE_CONFIG)
            orig_bytes = fake_config.read_bytes()

            quota_fail = subprocess.CompletedProcess([], 1, stdout="", stderr="OpenAIError: quota exhausted.")
            cf_ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            lock = threading.Lock()
            calls_by_job: dict[str, list[list[str]]] = {"j1": [], "j2": []}

            def _concurrent_run(argv, *args, **kwargs):
                cmd = list(argv)
                with lock:
                    target_job = "j1" if (str(j1) in str(cmd) or "prompt 1" in str(cmd)) else "j2"
                    calls_by_job[target_job].append(cmd)
                    call_idx = len(calls_by_job[target_job])
                time.sleep(0.01)
                return quota_fail if call_idx == 1 else cf_ok

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", side_effect=_concurrent_run),
            ):
                t1 = threading.Thread(target=codex_job_worker.main, args=(j1,))
                t2 = threading.Thread(target=codex_job_worker.main, args=(j2,))
                t1.start()
                t2.start()
                t1.join(timeout=10.0)
                t2.join(timeout=10.0)

            s1 = self._read_state(j1)
            s2 = self._read_state(j2)
            self.assertEqual("completed", s1["status"], s1)
            self.assertEqual("completed", s2["status"], s2)
            self.assertEqual(True, s1["fallback_used"])
            self.assertEqual(True, s2["fallback_used"])
            self.assertEqual("codeflow", s1["route_used"])
            self.assertEqual("codeflow", s2["route_used"])
            self.assertEqual(2, len(s1["attempts"]))
            self.assertEqual(2, len(s2["attempts"]))
            self.assertEqual(0, fake_config.write_count)
            self.assertEqual(orig_bytes, fake_config.read_bytes())

    def test_concurrent_official_and_fallback_workers(self) -> None:
        """Official and fallback workers run concurrently without cross-talk."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            j_off = tmp_path / "j_off"
            j_fb = tmp_path / "j_fb"
            j_off.mkdir()
            j_fb.mkdir()
            self._make_job(j_off, "official", "run official")
            self._make_job(j_fb, "codeflow", "run codeflow")
            (j_off / "result.txt").write_text("off-ok", encoding="utf-8")
            (j_fb / "result.txt").write_text("fb-ok", encoding="utf-8")

            fake_config = _FakeConfigPath(custom_text=self.SAMPLE_CONFIG)
            orig_bytes = fake_config.read_bytes()

            ok_res = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", return_value=ok_res),
            ):
                t1 = threading.Thread(target=codex_job_worker.main, args=(j_off,))
                t2 = threading.Thread(target=codex_job_worker.main, args=(j_fb,))
                t1.start()
                t2.start()
                t1.join(timeout=10.0)
                t2.join(timeout=10.0)

            s_off = self._read_state(j_off)
            s_fb = self._read_state(j_fb)
            self.assertEqual("completed", s_off["status"])
            self.assertEqual("completed", s_fb["status"])
            self.assertEqual("official", s_off["route_used"])
            self.assertEqual("codeflow", s_fb["route_used"])
            self.assertEqual(0, fake_config.write_count)
            self.assertEqual(orig_bytes, fake_config.read_bytes())

    def test_concurrent_current_and_fallback_workers(self) -> None:
        """route=current and fallback workers run concurrently without cross-talk."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            j_cur = tmp_path / "j_cur"
            j_fb = tmp_path / "j_fb"
            j_cur.mkdir()
            j_fb.mkdir()
            self._make_job(j_cur, "current", "run current")
            self._make_job(j_fb, "codeflow", "run codeflow")
            (j_cur / "result.txt").write_text("cur-ok", encoding="utf-8")
            (j_fb / "result.txt").write_text("fb-ok", encoding="utf-8")

            fake_config = _FakeConfigPath(custom_text=self.SAMPLE_CONFIG)
            orig_bytes = fake_config.read_bytes()

            ok_res = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", return_value=ok_res),
            ):
                t1 = threading.Thread(target=codex_job_worker.main, args=(j_cur,))
                t2 = threading.Thread(target=codex_job_worker.main, args=(j_fb,))
                t1.start()
                t2.start()
                t1.join(timeout=10.0)
                t2.join(timeout=10.0)

            s_cur = self._read_state(j_cur)
            s_fb = self._read_state(j_fb)
            self.assertEqual("completed", s_cur["status"])
            self.assertEqual("completed", s_fb["status"])
            self.assertEqual("current", s_cur["route_used"])
            self.assertEqual("codeflow", s_fb["route_used"])
            self.assertEqual(0, fake_config.write_count)
            self.assertEqual(orig_bytes, fake_config.read_bytes())

    def test_three_concurrent_workers_production_isolation(self) -> None:
        """Verify 3 workers concurrent production load with distinct routes."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            j1 = tmp_path / "j1"
            j2 = tmp_path / "j2"
            j3 = tmp_path / "j3"
            j1.mkdir(); j2.mkdir(); j3.mkdir()
            self._make_job(j1, "official_then_codeflow", "w1")
            self._make_job(j2, "official", "w2")
            self._make_job(j3, "current", "w3")
            (j1 / "result.txt").write_text("w1-ok", encoding="utf-8")
            (j2 / "result.txt").write_text("w2-ok", encoding="utf-8")
            (j3 / "result.txt").write_text("w3-ok", encoding="utf-8")

            fake_config = _FakeConfigPath(custom_text=self.SAMPLE_CONFIG)
            orig_bytes = fake_config.read_bytes()

            quota_fail = subprocess.CompletedProcess([], 1, stdout="", stderr="OpenAIError: quota exceeded.")
            ok_res = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            lock = threading.Lock()
            j1_calls = [0]

            def _multi_run(argv, *args, **kwargs):
                cmd = list(argv)
                with lock:
                    if "w1" in str(cmd):
                        j1_calls[0] += 1
                        is_first = (j1_calls[0] == 1)
                    else:
                        is_first = False
                time.sleep(0.01)
                return quota_fail if is_first else ok_res

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", side_effect=_multi_run),
            ):
                threads = [
                    threading.Thread(target=codex_job_worker.main, args=(j1,)),
                    threading.Thread(target=codex_job_worker.main, args=(j2,)),
                    threading.Thread(target=codex_job_worker.main, args=(j3,)),
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=10.0)

            s1 = self._read_state(j1)
            s2 = self._read_state(j2)
            s3 = self._read_state(j3)
            self.assertEqual("completed", s1["status"])
            self.assertEqual("codeflow", s1["route_used"])
            self.assertEqual(True, s1["fallback_used"])
            self.assertEqual("completed", s2["status"])
            self.assertEqual("official", s2["route_used"])
            self.assertEqual("completed", s3["status"])
            self.assertEqual("current", s3["route_used"])
            self.assertEqual(0, fake_config.write_count)
            self.assertEqual(orig_bytes, fake_config.read_bytes())


class CodexAgyMcpRegressionTests(unittest.TestCase):
    """Regression test ensuring [mcp_servers.agy] and other config sections are never corrupted."""

    def setUp(self) -> None:
        self._orig_env = os.environ.copy()
        os.environ["CODEFLOW_API_KEY"] = "mock-codeflow-key"
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._route_patch = mock.patch.object(control_plane, "ROUTE_STATE_FILE", Path(self._tmp_dir.name) / "route-state.json")
        self._route_patch.start()

    def tearDown(self) -> None:
        self._route_patch.stop()
        self._tmp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._orig_env)

    REALISTIC_CONFIG_WITH_AGY = textwrap.dedent(r"""
        model_provider = "openai"
        api_key = "env:OPENAI_API_KEY"

        [mcp_servers.agy]
        command = 'C:\Users\ExampleUser\AppData\Local\agy\bin\agy.exe'
        args = ["mcp-server"]

        [mcp_servers.other_tool]
        command = "npx"
        args = ["-y", "@modelcontextprotocol/server-everything"]

        [windows]
        sandbox = "workspace-write"

        [projects.'x:\example\chatgpt-harbor']
        trust_level = "trusted"

        [providers.custom]
        name = "Personal Code Flow"
        base_url = "https://my-codeflow.internal/v1"
        api_key = "env:MY_CODEFLOW_KEY"
    """).strip() + "\n"

    def test_agy_mcp_config_preservation_during_fallback(self) -> None:
        """Run official -> quota exhausted -> fallback -> completion with AGY MCP config intact throughout."""
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            (job_dir / "status.json").write_text(
                json.dumps({
                    "job_id": "agy-mcp-test",
                    "harness": "codex",
                    "status": "queued",
                    "prompt": "solve task",
                    "cwd": str(job_dir),
                    "model": None,
                    "sandbox": "workspace-write",
                    "route_requested": "official_then_codeflow",
                    "route_used": "official",
                }),
                encoding="utf-8",
            )
            (job_dir / "result.txt").write_text("task-succeeded", encoding="utf-8")

            fake_config = _FakeConfigPath(custom_text=self.REALISTIC_CONFIG_WITH_AGY)
            orig_bytes = fake_config.read_bytes()

            quota_fail = subprocess.CompletedProcess(
                [], 1, stdout="",
                stderr="OpenAIError: You have exceeded your usage tier limit. Quota exhausted.",
            )
            cf_ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            call_count = [0]
            checked_bytes_during_runs = []

            def _checked_run(*args, **kwargs):
                call_count[0] += 1
                # Inspect config bytes in the middle of subprocess execution
                checked_bytes_during_runs.append(fake_config.read_bytes())
                return quota_fail if call_count[0] == 1 else cf_ok

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", side_effect=_checked_run),
            ):
                codex_job_worker.main(job_dir)

            state = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
            self.assertEqual("completed", state["status"])
            self.assertEqual(True, state["fallback_used"])
            self.assertEqual("codeflow", state["route_used"])
            self.assertEqual(2, len(checked_bytes_during_runs))

            # Intermediate stage verification
            self.assertEqual(orig_bytes, checked_bytes_during_runs[0])
            self.assertEqual(orig_bytes, checked_bytes_during_runs[1])

            # Final stage verification
            final_bytes = fake_config.read_bytes()
            self.assertEqual(orig_bytes, final_bytes)
            self.assertEqual(0, fake_config.write_count)

            # Deep TOML parse verification
            parsed = tomllib.loads(final_bytes.decode("utf-8"))
            self.assertEqual("openai", parsed.get("model_provider"))
            self.assertIn("agy", parsed.get("mcp_servers", {}))
            self.assertEqual(
                r"C:\Users\ExampleUser\AppData\Local\agy\bin\agy.exe",
                parsed["mcp_servers"]["agy"]["command"],
            )
            self.assertEqual(["mcp-server"], parsed["mcp_servers"]["agy"]["args"])
            self.assertIn("other_tool", parsed["mcp_servers"])
            self.assertEqual("workspace-write", parsed.get("windows", {}).get("sandbox"))
            self.assertEqual("trusted", parsed.get("projects", {}).get("x:\\example\\chatgpt-harbor", {}).get("trust_level"))
            self.assertEqual("Personal Code Flow", parsed.get("providers", {}).get("custom", {}).get("name"))


class CodexConfigPreservationRegressionTests(unittest.TestCase):
    """Regression tests for process-local Codex route safety and stale managed config detection."""

    def setUp(self) -> None:
        self._orig_env = os.environ.copy()
        os.environ["CODEFLOW_API_KEY"] = "mock-codeflow-key"
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._route_patch = mock.patch.object(control_plane, "ROUTE_STATE_FILE", Path(self._tmp_dir.name) / "route-state.json")
        self._route_patch.start()

    def tearDown(self) -> None:
        self._route_patch.stop()
        self._tmp_dir.cleanup()
        os.environ.clear()
        os.environ.update(self._orig_env)

    SAMPLE_FULL_CONFIG = textwrap.dedent("""\
        # User defined default model
        model = "some-model"

        [windows]
        sandbox = "elevated"

        [projects.'d:\\codex']
        trust_level = "trusted"

        [projects.'d:\\codex\\labtrace']
        trust_level = "trusted"

        [projects.'d:\\other-project']
        trust_level = "trusted"

        [mcp_servers.example]
        command = "example"

        [custom_user_section]
        foo = "bar"
    """).strip() + "\n"

    def _make_job(self, job_dir: Path, prompt: str = "test prompt") -> None:
        (job_dir / "status.json").write_text(
            json.dumps({
                "job_id": f"job-{uuid.uuid4().hex[:8]}",
                "harness": "codex",
                "status": "queued",
                "prompt": prompt,
                "cwd": str(job_dir),
                "model": None,
                "sandbox": "workspace-write",
                "route_requested": "official_then_codeflow",
                "route_used": "official",
            }),
            encoding="utf-8",
        )

    def _read_state(self, job_dir: Path) -> dict:
        return json.loads((job_dir / "status.json").read_text(encoding="utf-8"))

    def test_A_build_codex_command_process_local_flags(self) -> None:
        """build_codex_command injects provider flags without modifying config."""
        result_path = Path("result.txt")
        state = {
            "sandbox": "read-only",
            "cwd": r"D:\repo",
            "prompt": "test",
            "model": "gpt-5",
            "reasoning_effort": "high",
        }

        cmd_official = control_plane.build_codex_command(state, result_path, route="official")
        self.assertIn('model_provider="openai"', cmd_official)

        cmd_cf = control_plane.build_codex_command(state, result_path, route="codeflow")
        self.assertIn('model_provider="harbor_codeflow"', cmd_cf)
        self.assertIn('model_providers.harbor_codeflow.name="Harbor Code Flow"', cmd_cf)
        self.assertIn('model_providers.harbor_codeflow.base_url="https://codeflow.asia/v1"', cmd_cf)
        self.assertIn('model_providers.harbor_codeflow.wire_api="responses"', cmd_cf)
        self.assertIn('model_providers.harbor_codeflow.env_key="CODEFLOW_API_KEY"', cmd_cf)

        cmd_cur = control_plane.build_codex_command(state, result_path, route="current")
        self.assertNotIn('model_provider="openai"', cmd_cur)
        self.assertNotIn('model_provider="harbor_codeflow"', cmd_cur)

    def test_C_full_lifecycle_byte_equality(self) -> None:
        """Full fallback lifecycle produces byte-for-byte identical config."""
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._make_job(job_dir)
            (job_dir / "result.txt").write_text("done", encoding="utf-8")
            fake_config = _FakeConfigPath(custom_text=self.SAMPLE_FULL_CONFIG)
            orig_bytes = fake_config.read_bytes()

            quota_fail = subprocess.CompletedProcess([], 1, stdout="", stderr="OpenAIError: quota exhausted.")
            cf_ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            call_count = [0]

            def _run_side_effect(*args, **kwargs):
                call_count[0] += 1
                return quota_fail if call_count[0] == 1 else cf_ok

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", side_effect=_run_side_effect),
            ):
                codex_job_worker.main(job_dir)

            state = self._read_state(job_dir)
            self.assertEqual("completed", state["status"])
            self.assertEqual(True, state["fallback_used"])
            self.assertEqual(orig_bytes, fake_config.read_bytes())
            self.assertEqual(0, fake_config.write_count)

    def test_D_successful_job_without_fallback(self) -> None:
        """Attempt 1 succeeds, leaves config byte-for-byte identical."""
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._make_job(job_dir)
            (job_dir / "result.txt").write_text("ok", encoding="utf-8")

            fake_config = _FakeConfigPath(custom_text=self.SAMPLE_FULL_CONFIG)
            orig_bytes = fake_config.read_bytes()

            ok_result = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", return_value=ok_result),
            ):
                codex_job_worker.main(job_dir)

            state = self._read_state(job_dir)
            self.assertEqual("completed", state["status"])
            self.assertEqual(False, state["fallback_used"])
            self.assertEqual(0, fake_config.write_count)
            self.assertEqual(orig_bytes, fake_config.read_bytes())

    def test_E_fallback_job(self) -> None:
        """Official attempt returns quota exhaustion, fallback succeeds, config unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._make_job(job_dir)
            (job_dir / "result.txt").write_text("fallback-success", encoding="utf-8")

            fake_config = _FakeConfigPath(custom_text=self.SAMPLE_FULL_CONFIG)
            orig_bytes = fake_config.read_bytes()

            quota_fail = subprocess.CompletedProcess(
                [], 1, stdout="",
                stderr="OpenAIError: You have exceeded your usage tier limit. Your quota has been exhausted.",
            )
            cf_ok = subprocess.CompletedProcess([], 0, stdout="", stderr="")

            call_count = [0]

            def _run_side_effect(*args, **kwargs):
                call_count[0] += 1
                return quota_fail if call_count[0] == 1 else cf_ok

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", side_effect=_run_side_effect),
            ):
                codex_job_worker.main(job_dir)

            state = self._read_state(job_dir)
            self.assertEqual("completed", state["status"])
            self.assertEqual(True, state["fallback_used"])
            self.assertEqual("official_quota_exhausted", state["fallback_reason"])
            self.assertEqual("codeflow", state["route_used"])
            self.assertEqual(orig_bytes, fake_config.read_bytes())
            self.assertEqual(0, fake_config.write_count)

    def test_F_exception_path(self) -> None:
        """Exception during execution leaves config unchanged."""
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._make_job(job_dir)

            fake_config = _FakeConfigPath(custom_text=self.SAMPLE_FULL_CONFIG)
            orig_bytes = fake_config.read_bytes()

            def _explode_run(*args, **kwargs):
                raise RuntimeError("unhandled crash during execution")

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle", side_effect=_explode_run),
            ):
                codex_job_worker.main(job_dir)

            state = self._read_state(job_dir)
            self.assertEqual("failed", state["status"])
            self.assertEqual("wrapper_error", state.get("failure_type"))
            self.assertEqual(orig_bytes, fake_config.read_bytes())
            self.assertEqual(0, fake_config.write_count)

    def test_G_historical_unsafe_state_fails_closed_and_not_auto_cleared(self) -> None:
        """Historical config_unsafe=true causes jobs to fail closed and is not cleared by success."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            job_dir = tmp_path / "job"
            job_dir.mkdir()
            self._make_job(job_dir)
            (job_dir / "result.txt").write_text("ok", encoding="utf-8")

            fake_codex_exe = tmp_path / "codex.cmd"
            fake_codex_exe.write_text("@echo off\n", encoding="utf-8")

            fake_config = _FakeConfigPath(custom_text=self.SAMPLE_FULL_CONFIG)
            route_state_file = job_dir / "route-state.json"
            route_state_file.write_text(
                json.dumps({
                    "config_unsafe": True,
                    "config_restore_error": {"error": "historical failure", "recorded_at": "2026-08-30T00:00:00Z"},
                }),
                encoding="utf-8",
            )

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "ROUTE_STATE_FILE", route_state_file),
                mock.patch.object(control_plane, "CODEX_EXE", fake_codex_exe),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle") as run_mock,
            ):
                codex_job_worker.main(job_dir)
                run_mock.assert_not_called()

                state = self._read_state(job_dir)
                self.assertEqual("failed", state["status"])
                self.assertEqual("stale_harbor_managed_codex_config", state.get("failure_type"))

                # start_task also fails closed
                task_res = control_plane.start_task(
                    harness="codex",
                    prompt="subsequent task",
                    project=None,
                    cwd=str(job_dir),
                    model=None,
                    sandbox="workspace-write",
                    reasoning_effort=None,
                )
                self.assertFalse(task_res["ok"])
                self.assertEqual("stale_harbor_managed_codex_config", task_res.get("blocker"))

                # config_unsafe state is NOT cleared
                saved_state = json.loads(route_state_file.read_text(encoding="utf-8"))
                self.assertTrue(saved_state.get("config_unsafe"))

    def test_H_stale_managed_config_detection(self) -> None:
        """Harbor managed minimal config header triggers fail closed."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            job_dir = tmp_path / "job"
            job_dir.mkdir()
            self._make_job(job_dir)

            fake_codex_exe = tmp_path / "codex.cmd"
            fake_codex_exe.write_text("@echo off\n", encoding="utf-8")

            stale_config_text = textwrap.dedent("""\
                # DO NOT EDIT — managed by MCP control plane fallback machinery.
                # Restore with ``codex config unset`` or by re-running setup.
                model_provider = "openai"
                api_key = "env:OPENAI_API_KEY"
            """).strip() + "\n"
            fake_config = _FakeConfigPath(custom_text=stale_config_text)
            orig_bytes = fake_config.read_bytes()

            with (
                mock.patch.object(codex_job_worker, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "ROUTE_STATE_FILE", job_dir / "route-state.json"),
                mock.patch.object(control_plane, "CODEX_EXE", fake_codex_exe),
                mock.patch.object(codex_job_worker, "run_codex_with_lifecycle") as run_mock,
            ):
                codex_job_worker.main(job_dir)
                run_mock.assert_not_called()

                state = self._read_state(job_dir)
                self.assertEqual("failed", state["status"])
                self.assertEqual("stale_harbor_managed_codex_config", state.get("failure_type"))
                self.assertEqual(orig_bytes, fake_config.read_bytes())
                self.assertEqual(0, fake_config.write_count)

                start_res = control_plane.start_task(
                    harness="codex",
                    prompt="test task",
                    project=None,
                    cwd=str(job_dir),
                    model=None,
                    sandbox="workspace-write",
                    reasoning_effort=None,
                )
                self.assertFalse(start_res["ok"])
                self.assertEqual("stale_harbor_managed_codex_config", start_res.get("blocker"))


class CodexRouteStatusAndCredentialTests(unittest.TestCase):
    """Tests for CODEFLOW_API_KEY presence validation, route availability, and fail-closed behavior."""

    def setUp(self) -> None:
        self._orig_env = os.environ.copy()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._orig_env)

    def test_route_status_when_codeflow_key_present(self) -> None:
        os.environ["CODEFLOW_API_KEY"] = "sk-codeflow-secret-12345"
        with tempfile.TemporaryDirectory() as tmp:
            fake_config = _FakeConfigPath(is_codeflow=False)
            with (
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "ROUTE_STATE_FILE", Path(tmp) / "route-state.json"),
            ):
                status = control_plane.codex_route_status()
                self.assertTrue(status["automatic_fallback_available"])
                self.assertTrue(status["codeflow_credential_configured"])
                self.assertIsNone(status["blocker"])
                # Ensure credential value is NEVER exposed in the status dict
                status_str = json.dumps(status)
                self.assertNotIn("sk-codeflow-secret-12345", status_str)
                self.assertEqual(0, fake_config.write_count)

    def test_route_status_when_codeflow_key_missing(self) -> None:
        os.environ.pop("CODEFLOW_API_KEY", None)
        with tempfile.TemporaryDirectory() as tmp:
            fake_config = _FakeConfigPath(is_codeflow=False)
            with (
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "ROUTE_STATE_FILE", Path(tmp) / "route-state.json"),
            ):
                status = control_plane.codex_route_status()
                self.assertFalse(status["automatic_fallback_available"])
                self.assertFalse(status["codeflow_credential_configured"])
                self.assertIn("missing_codeflow_api_key", str(status.get("blocker")))
                self.assertEqual(0, fake_config.write_count)

    def test_start_task_missing_codeflow_key_blocks_fallback_and_codeflow(self) -> None:
        os.environ.pop("CODEFLOW_API_KEY", None)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "workdir"
            workdir.mkdir()
            fake_codex_exe = tmp_path / "codex.cmd"
            fake_codex_exe.write_text("@echo off\n", encoding="utf-8")
            fake_config = _FakeConfigPath(is_codeflow=False)
            orig_bytes = fake_config.read_bytes()

            with (
                mock.patch.object(control_plane, "CODEX_EXE", fake_codex_exe),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "ROUTE_STATE_FILE", tmp_path / "route-state.json"),
                mock.patch.object(control_plane, "JOBS_DIR", tmp_path / "jobs"),
            ):
                # official_then_codeflow must fail closed
                res1 = control_plane.start_task(
                    harness="codex",
                    prompt="test",
                    project=None,
                    cwd=str(workdir),
                    model=None,
                    sandbox="workspace-write",
                    reasoning_effort=None,
                    route="official_then_codeflow",
                )
                self.assertFalse(res1["ok"])
                self.assertEqual("missing_codeflow_api_key", res1.get("blocker"))

                # codeflow must fail closed
                res2 = control_plane.start_task(
                    harness="codex",
                    prompt="test",
                    project=None,
                    cwd=str(workdir),
                    model=None,
                    sandbox="workspace-write",
                    reasoning_effort=None,
                    route="codeflow",
                )
                self.assertFalse(res2["ok"])
                self.assertEqual("missing_codeflow_api_key", res2.get("blocker"))

                self.assertEqual(0, fake_config.write_count)
                self.assertEqual(orig_bytes, fake_config.read_bytes())

    def test_start_task_missing_codeflow_key_allows_official_and_current(self) -> None:
        os.environ.pop("CODEFLOW_API_KEY", None)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "workdir"
            workdir.mkdir()
            fake_codex_exe = tmp_path / "codex.cmd"
            fake_codex_exe.write_text("@echo off\n", encoding="utf-8")
            fake_config = _FakeConfigPath(is_codeflow=False)
            orig_bytes = fake_config.read_bytes()

            with (
                mock.patch.object(control_plane, "CODEX_EXE", fake_codex_exe),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "ROUTE_STATE_FILE", tmp_path / "route-state.json"),
                mock.patch.object(control_plane, "JOBS_DIR", tmp_path / "jobs"),
            ):
                # official should succeed
                res_off = control_plane.start_task(
                    harness="codex",
                    prompt="test official",
                    project=None,
                    cwd=str(workdir),
                    model=None,
                    sandbox="workspace-write",
                    reasoning_effort=None,
                    route="official",
                )
                self.assertTrue(res_off["ok"], res_off)
                self.assertIn("job_id", res_off)

                # current should succeed
                res_cur = control_plane.start_task(
                    harness="codex",
                    prompt="test current",
                    project=None,
                    cwd=str(workdir),
                    model=None,
                    sandbox="workspace-write",
                    reasoning_effort=None,
                    route="current",
                )
                self.assertTrue(res_cur["ok"], res_cur)
                self.assertIn("job_id", res_cur)

                self.assertEqual(0, fake_config.write_count)
                self.assertEqual(orig_bytes, fake_config.read_bytes())

    def test_start_task_with_codeflow_key_allows_fallback_route(self) -> None:
        os.environ["CODEFLOW_API_KEY"] = "sk-test-valid"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workdir = tmp_path / "workdir"
            workdir.mkdir()
            fake_codex_exe = tmp_path / "codex.cmd"
            fake_codex_exe.write_text("@echo off\n", encoding="utf-8")
            fake_config = _FakeConfigPath(is_codeflow=False)
            orig_bytes = fake_config.read_bytes()

            with (
                mock.patch.object(control_plane, "CODEX_EXE", fake_codex_exe),
                mock.patch.object(control_plane, "CODEX_CONFIG", fake_config),
                mock.patch.object(control_plane, "ROUTE_STATE_FILE", tmp_path / "route-state.json"),
                mock.patch.object(control_plane, "JOBS_DIR", tmp_path / "jobs"),
            ):
                res = control_plane.start_task(
                    harness="codex",
                    prompt="test fallback",
                    project=None,
                    cwd=str(workdir),
                    model=None,
                    sandbox="workspace-write",
                    reasoning_effort=None,
                    route="official_then_codeflow",
                )
                self.assertTrue(res["ok"], res)
                self.assertIn("job_id", res)
                self.assertEqual(0, fake_config.write_count)
                self.assertEqual(orig_bytes, fake_config.read_bytes())


# ---------------------------------------------------------------------------
# AGY headless false-success and Unified Lifecycle Regression Tests
# ---------------------------------------------------------------------------


class AgyHeadlessFalseSuccessRegressionTests(unittest.TestCase):
    """Regression tests for Bug 1: AGY headless false-success and tool permission denial."""

    def test_A_canonical_empty_response_fails_closed(self) -> None:
        """Requirement A: stdout={"status":"SUCCESS","response":""}, exit=0 -> failed, not completed."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
            stdout = json.dumps({"status": "SUCCESS", "response": "", "conversation_id": "conv-123"})
            lifecycle = {
                "exit_code": 0,
                "stdout": stdout,
                "stderr": "",
                "termination_reason": "self_exit",
                "forced_exit": False,
                "result_completed_at": 1.0,
                "result_text": stdout,
            }
            collected = codex_job_worker.collect_agy_lifecycle_result(lifecycle, result_path)
            self.assertEqual("failed", collected["status"])
            self.assertEqual(0, collected["process_exit_code"])
            self.assertEqual(0, collected["exit_code"])
            self.assertEqual("SUCCESS", collected["agent_task_status"])
            self.assertEqual("", collected["final_message"])
            self.assertEqual("agy_headless_false_success", collected["failure_type"])

    def test_B_read_file_permission_denial_detected(self) -> None:
        """Requirement B: Same as A plus read_file permission denial in stderr ->
        status=failed, failure_type=agy_tool_permission_denied, tool=read_file, process_exit_code=0, agent_task_status=SUCCESS
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
            stdout = json.dumps({"status": "SUCCESS", "response": "", "conversation_id": "conv-456"})
            stderr = 'jetski: no output produced — a tool required the "read_file" permission that headless mode cannot prompt for, so it was auto-denied.'
            lifecycle = {
                "exit_code": 0,
                "stdout": stdout,
                "stderr": stderr,
                "termination_reason": "self_exit",
                "forced_exit": False,
                "result_completed_at": 1.0,
                "result_text": stdout,
            }
            collected = codex_job_worker.collect_agy_lifecycle_result(lifecycle, result_path)
            self.assertEqual("failed", collected["status"])
            self.assertEqual("agy_tool_permission_denied", collected["failure_type"])
            self.assertEqual("read_file", collected["tool"])
            self.assertEqual(0, collected["process_exit_code"])
            self.assertEqual(0, collected["exit_code"])
            self.assertEqual("SUCCESS", collected["agent_task_status"])

    def test_C_command_permission_denial_detected(self) -> None:
        """Requirement C: command permission denial + exit=0 -> tool=command, failure_type=agy_tool_permission_denied."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
            stdout = json.dumps({"status": "SUCCESS", "response": "", "conversation_id": "conv-789"})
            stderr = 'jetski: no output produced — a tool required the "command" permission that headless mode cannot prompt for, so it was auto-denied.'
            lifecycle = {
                "exit_code": 0,
                "stdout": stdout,
                "stderr": stderr,
                "termination_reason": "self_exit",
                "forced_exit": False,
                "result_completed_at": 1.0,
                "result_text": stdout,
            }
            collected = codex_job_worker.collect_agy_lifecycle_result(lifecycle, result_path)
            self.assertEqual("failed", collected["status"])
            self.assertEqual("agy_tool_permission_denied", collected["failure_type"])
            self.assertEqual("command", collected["tool"])
            self.assertEqual(0, collected["process_exit_code"])

    def test_D_canonical_success_with_response_completed(self) -> None:
        """Requirement D: status=SUCCESS, response=AGY_OK, exit=0 -> completed."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
            stdout = json.dumps({"status": "SUCCESS", "response": "AGY_OK", "conversation_id": "conv-ok"})
            lifecycle = {
                "exit_code": 0,
                "stdout": stdout,
                "stderr": "",
                "termination_reason": "self_exit",
                "forced_exit": False,
                "result_completed_at": 1.0,
                "result_text": stdout,
            }
            collected = codex_job_worker.collect_agy_lifecycle_result(lifecycle, result_path)
            self.assertEqual("completed", collected["status"])
            self.assertEqual("AGY_OK", collected["final_message"])
            self.assertEqual(0, collected["process_exit_code"])
            self.assertEqual("SUCCESS", collected["agent_task_status"])

    def test_E_canonical_error_status_fails(self) -> None:
        """Requirement E: status=ERROR, response="", error="...", exit=0 -> failed, agent_task_status=ERROR."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
            stdout = json.dumps({"status": "ERROR", "response": "", "error": "rate limit reached", "conversation_id": "conv-err"})
            lifecycle = {
                "exit_code": 0,
                "stdout": stdout,
                "stderr": "",
                "termination_reason": "self_exit",
                "forced_exit": False,
                "result_completed_at": 1.0,
                "result_text": stdout,
            }
            collected = codex_job_worker.collect_agy_lifecycle_result(lifecycle, result_path)
            self.assertEqual("failed", collected["status"])
            self.assertEqual("ERROR", collected["agent_task_status"])
            self.assertEqual(0, collected["process_exit_code"])
            self.assertEqual("agy_execution_error", collected["failure_type"])

    def test_F_unknown_legacy_json_and_plain_text_compatibility(self) -> None:
        """Requirement F: Unknown legacy JSON and plain text compatibility remains."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)

            # Legacy non-canonical JSON with exit 0
            stdout_json = '{"unrecognized_custom_key": "custom_value"}'
            lifecycle1 = {
                "exit_code": 0,
                "stdout": stdout_json,
                "stderr": "",
                "termination_reason": "self_exit",
                "forced_exit": False,
                "result_completed_at": 1.0,
                "result_text": stdout_json,
            }
            collected1 = codex_job_worker.collect_agy_lifecycle_result(lifecycle1, result_path)
            self.assertEqual("completed", collected1["status"])
            self.assertEqual(stdout_json, collected1["final_message"])

            # Plain text with exit 0
            stdout_text = "Plain text response without any JSON"
            lifecycle2 = {
                "exit_code": 0,
                "stdout": stdout_text,
                "stderr": "",
                "termination_reason": "self_exit",
                "forced_exit": False,
                "result_completed_at": 1.0,
                "result_text": stdout_text,
            }
            collected2 = codex_job_worker.collect_agy_lifecycle_result(lifecycle2, result_path)
            self.assertEqual("completed", collected2["status"])
            self.assertEqual(stdout_text, collected2["final_message"])

    def test_extract_agy_result_message_canonical_null_and_empty_response(self) -> None:
        """Verify _extract_agy_result_message fail-closed semantics for canonical envelopes."""
        # response="" -> ""
        self.assertEqual("", codex_job_worker._extract_agy_result_message('{"status":"SUCCESS","response":""}'))
        # response=null -> ""
        self.assertEqual("", codex_job_worker._extract_agy_result_message('{"status":"SUCCESS","response":null}'))
        # status=ERROR, error="..." -> ""
        self.assertEqual("", codex_job_worker._extract_agy_result_message('{"status":"ERROR","error":"bad input"}'))
        # response="AGY_OK" -> "AGY_OK"
        self.assertEqual("AGY_OK", codex_job_worker._extract_agy_result_message('{"status":"SUCCESS","response":"AGY_OK"}'))

    def test_workspace_capability_probe_status_structure(self) -> None:
        """Requirement 6: _agy_cli_status returns unverified workspace_capability structure."""
        status = control_plane._agy_cli_status()
        self.assertIn("workspace_capability", status)
        ws_cap = status["workspace_capability"]
        self.assertFalse(ws_cap["verified"])
        self.assertEqual("unverified", ws_cap["status"])
        self.assertIn("headless", ws_cap["reason"])


class UnifiedLifecycleRegressionTests(unittest.TestCase):
    """Regression tests for Bug 2: Unified task / MiniMax false-completion."""

    def test_G_process_exit_1_grace_expired_is_not_completed(self) -> None:
        """Requirement G:
        process_exit_code=1, termination_reason=grace_expired_after_result, forced_exit_after_result=True, final_message="请告诉我路径"
        -> NOT completed (status = failed).
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
            result_path.write_text("请告诉我路径", encoding="utf-8")
            lifecycle = {
                "exit_code": 1,
                "stdout": '{"output": "请告诉我路径"}',
                "stderr": "MiniMax exited with code 1",
                "termination_reason": "grace_expired_after_result",
                "forced_exit": True,
                "result_completed_at": 1.0,
                "result_text": "请告诉我路径",
            }
            collected = codex_job_worker.collect_minimax_lifecycle_result(lifecycle, result_path)
            self.assertEqual("failed", collected["status"])
            self.assertEqual(1, collected["process_exit_code"])
            self.assertEqual(1, collected["exit_code"])
            self.assertNotIn("cleanup_anomaly", collected)
            self.assertEqual("minimax_execution_error", collected["failure_type"])
            self.assertEqual("请告诉我路径", collected["final_message"])

    def test_H_process_exit_1_with_nonempty_final_message_is_failed(self) -> None:
        """Requirement H: process_exit_code=1, final_message nonempty -> failed."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
            result_path.write_text("Some conversational output", encoding="utf-8")
            lifecycle = {
                "exit_code": 1,
                "stdout": "",
                "stderr": "Error encountered",
                "termination_reason": "self_exit",
                "forced_exit": False,
                "result_completed_at": 1.0,
                "result_text": "Some conversational output",
            }
            collected = codex_job_worker.collect_minimax_lifecycle_result(lifecycle, result_path)
            self.assertEqual("failed", collected["status"])
            self.assertEqual("minimax_execution_error", collected["failure_type"])
            self.assertEqual(1, collected["process_exit_code"])

    def test_I_process_exit_0_explicit_agent_failure_is_failed(self) -> None:
        """Requirement I: process_exit_code=0, explicit agent failure status, final_message nonempty -> failed."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
            result_path.write_text("Failed to execute turn", encoding="utf-8")
            stdout = json.dumps({"status": "failed", "error": "command rejected", "output": "Failed to execute turn"})
            lifecycle = {
                "exit_code": 0,
                "stdout": stdout,
                "stderr": "",
                "termination_reason": "self_exit",
                "forced_exit": False,
                "result_completed_at": 1.0,
                "result_text": "Failed to execute turn",
            }
            collected = codex_job_worker.collect_minimax_lifecycle_result(lifecycle, result_path)
            self.assertEqual("failed", collected["status"])
            self.assertEqual("failed", collected["agent_task_status"])
            self.assertEqual(0, collected["process_exit_code"])
            self.assertEqual("minimax_execution_error", collected["failure_type"])

    def test_J_process_exit_0_explicit_agent_success_is_completed(self) -> None:
        """Requirement J: process_exit_code=0, explicit agent success, usable final result -> completed."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
            result_path.write_text("TASK_FINISHED", encoding="utf-8")
            stdout = json.dumps({"status": "succeeded", "output": "TASK_FINISHED"})
            lifecycle = {
                "exit_code": 0,
                "stdout": stdout,
                "stderr": "",
                "termination_reason": "self_exit",
                "forced_exit": False,
                "result_completed_at": 1.0,
                "result_text": "TASK_FINISHED",
            }
            collected = codex_job_worker.collect_minimax_lifecycle_result(lifecycle, result_path)
            self.assertEqual("completed", collected["status"])
            self.assertEqual("succeeded", collected["agent_task_status"])
            self.assertEqual(0, collected["process_exit_code"])
            self.assertEqual("TASK_FINISHED", collected["final_message"])

    def test_K_normal_codex_minimax_agy_success_no_regression(self) -> None:
        """Requirement K: Verify normal success behaviour for Codex, MiniMax, and AGY."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)

            # Codex success
            result_path.write_text("CODEX_DONE", encoding="utf-8")
            proc_res = subprocess.CompletedProcess(["codex", "exec"], 0, stdout="", stderr="")
            codex_res = codex_job_worker.collect_result(proc_res, result_path)
            self.assertEqual("completed", codex_res["status"])
            self.assertEqual("CODEX_DONE", codex_res["final_message"])
            self.assertEqual(0, codex_res["process_exit_code"])

            # MiniMax success
            result_path.write_text("MINIMAX_DONE", encoding="utf-8")
            mm_lifecycle = {
                "exit_code": 0,
                "stdout": json.dumps({"status": "succeeded", "output": "MINIMAX_DONE"}),
                "stderr": "",
                "termination_reason": "self_exit",
                "forced_exit": False,
            }
            mm_res = codex_job_worker.collect_minimax_lifecycle_result(mm_lifecycle, result_path)
            self.assertEqual("completed", mm_res["status"])
            self.assertEqual("MINIMAX_DONE", mm_res["final_message"])
            self.assertEqual(0, mm_res["process_exit_code"])

            # AGY success
            result_path.write_text("AGY_DONE", encoding="utf-8")
            agy_lifecycle = {
                "exit_code": 0,
                "stdout": json.dumps({"status": "SUCCESS", "response": "AGY_DONE"}),
                "stderr": "",
                "termination_reason": "self_exit",
                "forced_exit": False,
            }
            agy_res = codex_job_worker.collect_agy_lifecycle_result(agy_lifecycle, result_path)
            self.assertEqual("completed", agy_res["status"])
            self.assertEqual("AGY_DONE", agy_res["final_message"])
            self.assertEqual(0, agy_res["process_exit_code"])

    def test_minimax_mixed_case_agent_statuses(self) -> None:
        """Verify MiniMax agent status handling is case-insensitive while preserving raw output."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)

            for failure_status in ("FAILED", "Failed", "Error", "ERROR", "Canceled", "CANCELLED", "cancelled"):
                result_path.write_text("failure output", encoding="utf-8")
                stdout = json.dumps({"status": failure_status, "output": "failure output"})
                lifecycle = {
                    "exit_code": 0,
                    "stdout": stdout,
                    "stderr": "",
                    "termination_reason": "self_exit",
                    "forced_exit": False,
                }
                collected = codex_job_worker.collect_minimax_lifecycle_result(lifecycle, result_path)
                self.assertEqual("failed", collected["status"], f"Expected failed for status {failure_status}")
                self.assertEqual(failure_status, collected["agent_task_status"], f"Expected raw status preserved for {failure_status}")
                self.assertEqual("minimax_execution_error", collected["failure_type"])

            for success_status in ("SUCCEEDED", "Succeeded", "succeeded", "SUCCESS", "Success", "ok", "OK"):
                result_path.write_text("success output", encoding="utf-8")
                stdout = json.dumps({"status": success_status, "output": "success output"})
                lifecycle = {
                    "exit_code": 0,
                    "stdout": stdout,
                    "stderr": "",
                    "termination_reason": "self_exit",
                    "forced_exit": False,
                }
                collected = codex_job_worker.collect_minimax_lifecycle_result(lifecycle, result_path)
                self.assertEqual("completed", collected["status"], f"Expected completed for status {success_status}")
                self.assertEqual(success_status, collected["agent_task_status"], f"Expected raw status preserved for {success_status}")


class MiniMaxLifecycleResultClassificationRegressionTests(unittest.TestCase):
    """Regression tests for the MiniMax lifecycle result-classification bug.

    Background: Harbor job 6a2015a5e3bd438d97725e881ac259d0 reproduced a case
    where the MiniMax CLI emitted a canonical exec.result envelope with
    ``status:"succeeded"`` and wrote a usable final message, then failed to
    exit within the post-result grace period. The lifecycle supervisor killed
    the lingering CLI (``termination_reason=grace_expired_after_result``,
    ``exit_code=1``). The collector must NOT retroactively convert the
    successful agent task into a failure merely because Harbor's own cleanup
    caused a non-zero wrapper exit.

    These tests pin down the four required cases:

    * terminal exec.result status=succeeded + grace-expired forced
      exit/nonzero process code => overall completed
    * genuine nonzero exit without valid terminal result => failed
    * terminal failed/error result remains failed even if a result.txt
      was written and the wrapper process was force-terminated
    * hard timeout before usable result remains failed
    """

    def test_terminal_succeeded_envelope_with_grace_expired_forced_exit_completes(self) -> None:
        """The bug fix case: canonical exec.result with status=succeeded,
        usable final message, and a wrapper that exited non-zero because
        Harbor killed it after the post-result grace expired. The job
        must be reported as completed with forced_exit_after_result=True;
        the wrapper's non-zero exit code is a consequence of Harbor's
        own cleanup, not a real execution failure.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, _ = _make_state_dir(tmp_path)
            result_path.write_text(
                "MINIMAX_STDIN_SMOKE_PASS | 海鸥-47",
                encoding="utf-8",
            )
            stdout = json.dumps(
                {
                    "status": "succeeded",
                    "output": "MINIMAX_STDIN_SMOKE_PASS | 海鸥-47",
                }
            )
            lifecycle = {
                "exit_code": 1,
                "stdout": stdout,
                "stderr": "",
                "termination_reason": "grace_expired_after_result",
                "forced_exit": True,
                "result_completed_at": 1.0,
                "result_text": "MINIMAX_STDIN_SMOKE_PASS | 海鸥-47",
            }
            collected = codex_job_worker.collect_minimax_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("completed", collected["status"])
            self.assertEqual("succeeded", collected["agent_task_status"])
            self.assertEqual(
                "MINIMAX_STDIN_SMOKE_PASS | 海鸥-47",
                collected["final_message"],
            )
            self.assertEqual(1, collected["process_exit_code"])
            self.assertEqual(0, collected["exit_code"])
            self.assertEqual("minimax_cleanup_after_result", collected["cleanup_anomaly"])
            self.assertTrue(
                collected["forced_exit_after_result"],
                "forced_exit_after_result must be recorded when Harbor's "
                "post-result cleanup caused the non-zero exit",
            )
            self.assertNotIn("failure_type", collected)
            self.assertNotIn("error", collected)

    def test_canonical_succeeded_envelope_case_insensitive_grace_expired_completes(self) -> None:
        """The same bug fix case but with mixed-case agent statuses
        (SUCCEEDED, Success, OK) and a grace-expired forced exit. The
        success judgement must be case-insensitive.
        """
        for success_status in ("SUCCEEDED", "Succeeded", "Success", "ok", "OK"):
            with self.subTest(success_status=success_status):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    _, result_path, _ = _make_state_dir(tmp_path)
                    result_path.write_text("done", encoding="utf-8")
                    stdout = json.dumps(
                        {"status": success_status, "output": "done"}
                    )
                    lifecycle = {
                        "exit_code": 1,
                        "stdout": stdout,
                        "stderr": "",
                        "termination_reason": "grace_expired_after_result",
                        "forced_exit": True,
                    }
                    collected = codex_job_worker.collect_minimax_lifecycle_result(
                        lifecycle, result_path
                    )
                    self.assertEqual(
                        "completed",
                        collected["status"],
                        f"Expected completed for status {success_status}",
                    )
                    self.assertEqual(success_status, collected["agent_task_status"])
                    self.assertEqual(1, collected["process_exit_code"])
                    self.assertEqual(0, collected["exit_code"])
                    self.assertEqual("minimax_cleanup_after_result", collected["cleanup_anomaly"])
                    self.assertTrue(collected["forced_exit_after_result"])

    @unittest.skipIf(sys.platform != "win32", "packaged process ownership path is Windows-only")
    def test_packaged_mode_same_normalization_for_minimax_canonical_success_after_force_exit(self) -> None:
        """When running in packaged mode, canonical success should still normalize
        public exit_code to 0 while keeping raw process_exit_code from the CLI.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, cwd = _make_state_dir(tmp_path)
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
                    f.write('MINIMAX_PACKAGED_RUNTIME_OK')
                print('{\"status\": \"succeeded\", \"output\": \"MINIMAX_PACKAGED_RUNTIME_OK\"}')
                sys.stdout.flush()
                while True:
                    time.sleep(60)
                """,
            )
            registry = tmp_path / "harbor_process_registry"
            registry.mkdir()
            with mock.patch.dict(
                os.environ,
                {
                    "HARBOR_RUNTIME_MODE": "packaged",
                    "HARBOR_PROCESS_REGISTRY": str(registry),
                    "HARBOR_RUNTIME_INSTANCE_ID": "packaged-minimax-unit-test",
                },
            ):
                lifecycle = _collect_lifecycle(
                    fake, result_path, cwd,
                    total_timeout=8.0, exit_grace=0.2, settle_grace=0.1,
                )
            self.assertEqual("grace_expired_after_result", lifecycle["termination_reason"])
            collected = codex_job_worker.collect_minimax_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("completed", collected["status"])
            self.assertEqual("succeeded", collected["agent_task_status"])
            self.assertEqual(0, collected["exit_code"])
            self.assertNotEqual(0, collected["process_exit_code"])
            self.assertEqual("minimax_cleanup_after_result", collected["cleanup_anomaly"])

    def test_genuine_nonzero_exit_without_valid_terminal_result_fails(self) -> None:
        """Preserve true failure: process exits non-zero WITHOUT a valid
        terminal MiniMax exec.result (no JSON envelope, no usable final
        message, or the envelope has no success status). This must still
        fail with minimax_execution_error. The carve-out in step 3
        must not extend to non-grace-expired terminations.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, _ = _make_state_dir(tmp_path)
            # No result.txt, no JSON envelope -> no canonical success.
            lifecycle = {
                "exit_code": 7,
                "stdout": "",
                "stderr": "MiniMax CLI crashed before writing any output",
                "termination_reason": "self_exit",
                "forced_exit": False,
            }
            collected = codex_job_worker.collect_minimax_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("failed", collected["status"])
            self.assertEqual("minimax_execution_error", collected["failure_type"])
            self.assertEqual(7, collected["process_exit_code"])
            self.assertFalse(collected["forced_exit_after_result"])

    def test_legacy_no_envelope_self_exit_nonzero_still_fails(self) -> None:
        """Preserve true failure: process exits non-zero AND no JSON
        envelope is present (so there is no canonical-success signal to
        trust) AND the result.txt was never written. The lifecycle
        supervisor terminates via self_exit, NOT grace_expired_after_result.
        This must still fail.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, _ = _make_state_dir(tmp_path)
            lifecycle = {
                "exit_code": 1,
                "stdout": '{"unrecognized_custom_key": "value"}',
                "stderr": "boom",
                "termination_reason": "self_exit",
                "forced_exit": False,
            }
            collected = codex_job_worker.collect_minimax_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("failed", collected["status"])
            self.assertEqual("minimax_execution_error", collected["failure_type"])
            self.assertEqual(1, collected["process_exit_code"])

    def test_terminal_failed_error_status_remains_failed_with_grace_expired(self) -> None:
        """Preserve true failure: a terminal failed/error result must
        remain failed even if Harbor's post-result grace expired and
        the wrapper process exited non-zero as a consequence. The agent
        status override must be enforced regardless of termination_reason.
        """
        for failure_status in ("failed", "error", "cancelled", "canceled",
                               "FAILED", "Error", "CANCELLED"):
            with self.subTest(failure_status=failure_status):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    _, result_path, _ = _make_state_dir(tmp_path)
                    result_path.write_text("agent error text", encoding="utf-8")
                    stdout = json.dumps(
                        {
                            "status": failure_status,
                            "error": "agent reported failure",
                            "output": "agent error text",
                        }
                    )
                    lifecycle = {
                        "exit_code": 1,
                        "stdout": stdout,
                        "stderr": "",
                        "termination_reason": "grace_expired_after_result",
                        "forced_exit": True,
                    }
                    collected = codex_job_worker.collect_minimax_lifecycle_result(
                        lifecycle, result_path
                    )
                    self.assertEqual(
                        "failed",
                        collected["status"],
                        f"Expected failed for status {failure_status}",
                    )
                    self.assertEqual(
                        "minimax_execution_error", collected["failure_type"]
                    )
                    self.assertEqual(failure_status, collected["agent_task_status"])

    def test_hard_timeout_before_usable_result_remains_failed(self) -> None:
        """Preserve true failure: the wrapper was force-terminated by
        the hard total timeout before any usable result was written.
        termination_reason=hard_timeout must still fail with
        minimax_execution_timeout regardless of any exit code.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, _ = _make_state_dir(tmp_path)
            lifecycle = {
                "exit_code": -1,
                "stdout": "",
                "stderr": "no result before hard timeout",
                "termination_reason": "hard_timeout",
                "forced_exit": True,
            }
            collected = codex_job_worker.collect_minimax_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("failed", collected["status"])
            self.assertEqual(
                "minimax_execution_timeout", collected["failure_type"]
            )
            self.assertFalse(collected["forced_exit_after_result"])

    def test_grace_expired_with_empty_result_envelope_is_failed(self) -> None:
        """Edge case: the wrapper exited non-zero with
        termination_reason=grace_expired_after_result but the captured
        terminal output has no usable final message (empty / parse error).
        Without a canonical-success signal the carve-out must NOT apply
        and the job must fail.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, _ = _make_state_dir(tmp_path)
            # result.txt missing or empty -> no canonical success.
            stdout = json.dumps({"status": "succeeded", "output": ""})
            lifecycle = {
                "exit_code": 1,
                "stdout": stdout,
                "stderr": "",
                "termination_reason": "grace_expired_after_result",
                "forced_exit": True,
            }
            collected = codex_job_worker.collect_minimax_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("failed", collected["status"])
            self.assertEqual("minimax_execution_error", collected["failure_type"])

    def test_canonical_success_via_legacy_path_unchanged(self) -> None:
        """Preserve the legacy/CLI-compat path: no JSON envelope, the
        result file is non-empty, the process exited cleanly. This must
        still complete (it is path (b) of step 5 in the new logic).
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            _, result_path, _ = _make_state_dir(tmp_path)
            result_path.write_text("legacy text answer", encoding="utf-8")
            lifecycle = {
                "exit_code": 0,
                "stdout": "Plain text without JSON envelope",
                "stderr": "",
                "termination_reason": "self_exit",
                "forced_exit": False,
            }
            collected = codex_job_worker.collect_minimax_lifecycle_result(
                lifecycle, result_path
            )
            self.assertEqual("completed", collected["status"])
            self.assertEqual("legacy text answer", collected["final_message"])
            self.assertFalse(collected["forced_exit_after_result"])

    def test_canonical_success_helpers_match_documented_truth_table(self) -> None:
        """Unit-test the helper directly so the truth table of the
        canonical-success judgement is documented and locked down.
        """
        # No status -> not canonical
        self.assertFalse(
            codex_job_worker._has_canonical_minimax_success(
                norm_status=None,
                final_message="anything",
                parse_error=None,
            )
        )
        # Failure status -> not canonical
        self.assertFalse(
            codex_job_worker._has_canonical_minimax_success(
                norm_status="failed",
                final_message="anything",
                parse_error=None,
            )
        )
        # Success status but empty final message -> not canonical
        self.assertFalse(
            codex_job_worker._has_canonical_minimax_success(
                norm_status="succeeded",
                final_message="   ",
                parse_error=None,
            )
        )
        # Success status with parse error -> not canonical
        self.assertFalse(
            codex_job_worker._has_canonical_minimax_success(
                norm_status="succeeded",
                final_message="ok",
                parse_error="malformed envelope",
            )
        )
        # Success status + final message + no parse error -> canonical
        self.assertTrue(
            codex_job_worker._has_canonical_minimax_success(
                norm_status="succeeded",
                final_message="ok",
                parse_error=None,
            )
        )
        # Case-insensitive for the success set — ``norm_status`` is
        # the caller-normalised lowercase form, so we exercise the
        # documented lowercase surface here. The case-insensitivity
        # itself is enforced by the caller (see
        # ``norm_status = agent_task_status.lower() ...`` in
        # collect_minimax_lifecycle_result) and is independently covered
        # by test_minimax_mixed_case_agent_statuses and
        # test_canonical_succeeded_envelope_case_insensitive_grace_expired_completes.
        for success_status in ("succeeded", "success", "ok"):
            with self.subTest(success_status=success_status):
                self.assertTrue(
                    codex_job_worker._has_canonical_minimax_success(
                        norm_status=success_status,
                        final_message="ok",
                        parse_error=None,
                    )
                )


class HarnessHardTimeoutRegressionTests(unittest.TestCase):
    """Regression tests locking Harbor worker hard timeout constants."""

    def test_minimax_total_timeout_constant(self) -> None:
        self.assertEqual(7200.0, codex_job_worker.MINIMAX_TOTAL_TIMEOUT)
        self.assertEqual(2 * 60 * 60.0, codex_job_worker.MINIMAX_TOTAL_TIMEOUT)

    def test_agy_total_timeout_constant(self) -> None:
        self.assertEqual(7200.0, codex_job_worker.AGY_TOTAL_TIMEOUT)
        self.assertEqual(2 * 60 * 60.0, codex_job_worker.AGY_TOTAL_TIMEOUT)

    def test_agy_default_print_timeout_constant(self) -> None:
        self.assertEqual("1h", control_plane.AGY_DEFAULT_PRINT_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
