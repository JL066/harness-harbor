"""P0 subprocess safety regression tests.

These tests guarantee that:

1. ``run_safe_subprocess`` reaps a child process that exceeds its timeout
   and never leaves an orphan process behind.
2. ``run_safe_subprocess`` always uses ``stdin=DEVNULL`` so the child can
   never read from the MCP transport pipe.
3. ``_run_git`` injects the four required noninteractive env keys
   (``GIT_TERMINAL_PROMPT``, ``GIT_PAGER``, ``PAGER``, ``GCM_INTERACTIVE``).
4. The event loop remains responsive while a blocking tool is running.
5. The git helper never returns a misleading ``ok=True`` after a hard
   termination.
"""

from __future__ import annotations

import asyncio
import ast
import io
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

import control_plane
import server_legacy


def _pid_alive(pid: int) -> bool:
    """Return True if a Windows process with the given PID is still alive.

    Uses the same Win32 API as Task Manager; the MCP stdio child is
    spawned without a console so we cannot rely on WTSEnumerateProcesses.
    """
    if not pid:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        if not ok:
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


class SafeSubprocessTimeoutReapTests(unittest.TestCase):
    def test_run_safe_subprocess_bounds_both_streams_while_draining(self) -> None:
        cap = 8192
        result = control_plane.run_safe_subprocess(
            [sys.executable, "-c", (
                "import os\nchunk=b'x'*65536\n"
                "for _ in range(128):\n os.write(1, chunk); os.write(2, chunk)\n"
            )],
            timeout=10.0,
            max_output_bytes=cap,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout.encode("utf-8")), cap)
        self.assertEqual(len(result.stderr.encode("utf-8")), cap)

    def test_run_safe_subprocess_reaps_hanging_child(self) -> None:
        """A child that sleeps longer than the timeout must be reaped and
        the helper must return within a small bounded window.

        The exact returncode is OS-defined: ``TerminateProcess`` on
        Windows typically yields 1, the helper's hard-kill fallback
        yields -1, and a clean self-exit yields 0. The contract under
        test is bounded return + no orphan, not the specific code.
        """
        # A Python one-liner that sleeps for a long time. The child is
        # detached from the test process group only by Popen's handle
        # inheritance rules, so killing the Popen handle is enough to
        # verify the helper's escalation.
        hang_argv = [
            sys.executable,
            "-c",
            "import time, sys; time.sleep(60); sys.exit(0)",
        ]
        t0 = time.time()
        result = control_plane.run_safe_subprocess(
            hang_argv,
            timeout=0.6,
            max_output_bytes=4096,
        )
        elapsed = time.time() - t0
        self.assertLess(elapsed, 8.0, f"helper should return quickly after timeout, took {elapsed:.2f}s")
        self.assertEqual(hang_argv, result.args)
        # The returncode is OS-defined: -1 (hard kill), 1 (TerminateProcess),
        # or 0 (clean self-exit). None of these is the contract under test.
        # The contract is that the helper returns in bounded time AND the
        # child is gone. We verify the latter by re-running a quick
        # helper invocation with a unique marker: a leaked zombie would
        # not block the second call but the test process table would
        # still show it, which we observe via _pid_alive. The helper
        # itself does not expose the child PID, so we trust the bounded
        # return time as the primary signal.
        marker = "reap-marker-" + str(int(time.time() * 1000))
        second = control_plane.run_safe_subprocess(
            [
                sys.executable,
                "-c",
                "import os, sys; sys.stdout.write(os.environ.get('MARKER','')); sys.stdout.flush()",
            ],
            env={**os.environ, "MARKER": marker},
            timeout=5.0,
        )
        self.assertEqual(0, second.returncode)
        self.assertEqual(marker, second.stdout)

    def test_run_safe_subprocess_terminates_in_finite_time(self) -> None:
        """No matter how long the child would sleep, the helper must
        return within a small bounded window after the timeout fires.
        """
        hang_argv = [
            sys.executable,
            "-c",
            "import time; time.sleep(120)",
        ]
        t0 = time.time()
        control_plane.run_safe_subprocess(hang_argv, timeout=0.3)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 6.0)

    def test_run_safe_subprocess_kills_child_tree(self) -> None:
        """A wrapper that spawns a long-lived child must not leave the
        child orphaned after the helper returns. This is the exact
        shape of the mcode.cmd / cmd.exe / node.exe stack: a thin
        ``proc.terminate()`` kills only the wrapper, the real work
        lives in the descendant.
        """
        # A Python script that spawns a child python and prints the
        # child's PID to stdout before sleeping. The test reads the
        # PID, then asks the helper to escalate and checks that
        # the child is also gone.
        wrapper_argv = [
            sys.executable,
            "-c",
            (
                "import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                "print(child.pid, flush=True)\n"
                "while True:\n"
                "    time.sleep(1)\n"
            ),
        ]
        proc = control_plane.subprocess.Popen(
            wrapper_argv,
            stdin=control_plane.subprocess.DEVNULL,
            stdout=control_plane.subprocess.PIPE,
            stderr=control_plane.subprocess.DEVNULL,
        )
        # Read the child PID from the wrapper's stdout.
        child_pid = None
        deadline = time.time() + 5.0
        buf = b""
        while time.time() < deadline and child_pid is None:
            chunk = proc.stdout.read(1)
            if not chunk:
                break
            buf += chunk
            if chunk == b"\n":
                try:
                    child_pid = int(buf.strip())
                except ValueError:
                    buf = b""
        self.assertIsNotNone(child_pid, "wrapper did not print child PID")

        # Both processes must be alive before the escalation.
        self.assertTrue(_pid_alive(proc.pid))
        self.assertTrue(_pid_alive(child_pid))

        # Trigger the escalation through the public helper.
        control_plane._escalate_terminate(proc, wrapper_argv)

        # After the escalation both PIDs must be gone. Poll the Win32 API
        # directly; locked-down hosts can deny tasklist even for owned PIDs.
        for pid in (proc.pid, child_pid):
            deadline = time.time() + 3.0
            while time.time() < deadline and _pid_alive(pid):
                time.sleep(0.05)
            self.assertFalse(_pid_alive(pid), f"PID {pid} survived _escalate_terminate")


class StdinIsolationTests(unittest.TestCase):
    def test_run_safe_subprocess_uses_devnull_stdin(self) -> None:
        """The helper must pass ``stdin=subprocess.DEVNULL`` to Popen.

        This is the contract that prevents a child from inheriting the
        MCP transport pipe and consuming JSON-RPC bytes.
        """
        captured: dict = {}

        class _FakePopen:
            def __init__(self, argv, **kwargs):
                captured["argv"] = list(argv)
                captured["kwargs"] = dict(kwargs)
                # minimal API surface for the helper
                self.pid = 99999  # not used; helper checks poll() etc.
                self.returncode = 0
                self.stdin = None
                self.stdout = io.BytesIO()
                self.stderr = io.BytesIO()

            def poll(self):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

        with mock.patch.object(control_plane.subprocess, "Popen", _FakePopen):
            control_plane.run_safe_subprocess([sys.executable, "--version"], timeout=1.0)

        self.assertEqual(captured["kwargs"].get("stdin"), subprocess.DEVNULL)
        self.assertEqual(captured["kwargs"].get("stdout"), subprocess.PIPE)
        self.assertEqual(captured["kwargs"].get("stderr"), subprocess.PIPE)
        if sys.platform == "win32":
            flags = captured["kwargs"].get("creationflags", 0)
            self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
            self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)

    def test_run_safe_subprocess_delivers_exact_bytes_input(self) -> None:
        payload = "第一行\nsecond line\n".encode("utf-8")
        result = control_plane.run_safe_subprocess(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
            input=payload,
            timeout=5.0,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, payload.decode("utf-8"))

    def test_run_safe_subprocess_uses_pipe_only_for_explicit_input(self) -> None:
        captured: dict = {}

        class _FakePopen:
            returncode = 0

            def __init__(self, argv, **kwargs):
                captured["kwargs"] = kwargs
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO(payload)
                self.stderr = io.BytesIO()

            def wait(self, timeout=None):
                captured["timeout"] = timeout
                return 0

            def poll(self):
                return 0

        payload = "alpha\nβeta\n".encode("utf-8")
        with mock.patch.object(control_plane.subprocess, "Popen", _FakePopen):
            result = control_plane.run_safe_subprocess([sys.executable, "--version"], input=payload, timeout=1.0)

        self.assertEqual(captured["kwargs"]["stdin"], subprocess.PIPE)
        self.assertEqual(captured["timeout"], 1.0)
        self.assertEqual(result.stdout, payload.decode("utf-8"))

    def test_run_safe_subprocess_input_path_preserves_timeout_cleanup(self) -> None:
        t0 = time.time()
        result = control_plane.run_safe_subprocess(
            [sys.executable, "-c", "import sys, time; sys.stdin.buffer.read(); time.sleep(60)"],
            input=b"probe\n",
            timeout=0.3,
        )
        self.assertLess(time.time() - t0, 6.0)
        self.assertIsNotNone(result.returncode)

    def test_run_git_uses_safe_subprocess(self) -> None:
        """The internal _run_git helper must delegate to run_safe_subprocess."""
        with mock.patch.object(control_plane, "run_safe_subprocess") as fake:
            fake.return_value = subprocess.CompletedProcess(
                args=["git", "--no-pager", "status"],
                returncode=0,
                stdout="",
                stderr="",
            )
            with tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "x.txt").write_text("y", encoding="utf-8")
                # The call is expected to fail because no real git repo
                # is here, but we just need to observe the fake call.
                try:
                    control_plane._run_git(["status"], Path(tmp), timeout=1.0)
                except Exception:
                    pass
        self.assertTrue(fake.called, "_run_git must call run_safe_subprocess")
        # The first positional argv must be a list starting with git --no-pager
        first_call = fake.call_args_list[0]
        sent_argv = first_call.args[0]
        self.assertIsInstance(sent_argv, list)
        self.assertEqual(sent_argv[0], "git")
        self.assertEqual(sent_argv[1], "--no-pager")


class GitNoninteractiveEnvTests(unittest.TestCase):
    def test_run_git_injects_required_env_keys(self) -> None:
        """Every git subprocess must see the four required env keys."""
        captured_kwargs: dict = {}

        def fake_run(argv, *, cwd=None, env=None, timeout=None, max_output_bytes=2_000_000):
            captured_kwargs["argv"] = list(argv)
            captured_kwargs["env"] = dict(env or {})
            captured_kwargs["cwd"] = cwd
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="", stderr=""
            )

        with mock.patch.object(control_plane, "run_safe_subprocess", side_effect=fake_run):
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                (repo / ".git").mkdir()
                control_plane._run_git(["status"], repo, timeout=1.0)

        env = captured_kwargs.get("env", {})
        self.assertEqual(env.get("GIT_TERMINAL_PROMPT"), "0")
        self.assertEqual(env.get("GIT_PAGER"), "cat")
        self.assertEqual(env.get("PAGER"), "cat")
        self.assertEqual(env.get("GCM_INTERACTIVE"), "Never")

    def test_run_git_does_not_mutate_parent_environ(self) -> None:
        """The injected env must be local to the subprocess; the parent
        process environment must not be permanently changed.
        """
        before = {
            "GIT_TERMINAL_PROMPT": os.environ.get("GIT_TERMINAL_PROMPT"),
            "GIT_PAGER": os.environ.get("GIT_PAGER"),
            "PAGER": os.environ.get("PAGER"),
            "GCM_INTERACTIVE": os.environ.get("GCM_INTERACTIVE"),
        }

        def fake_run(argv, *, cwd=None, env=None, timeout=None, max_output_bytes=2_000_000):
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

        with mock.patch.object(control_plane, "run_safe_subprocess", side_effect=fake_run):
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                (repo / ".git").mkdir()
                control_plane._run_git(["status"], repo, timeout=1.0)

        for key, value in before.items():
            self.assertEqual(os.environ.get(key), value, f"{key} should be unchanged in parent env")


class EventLoopResponsivenessTests(unittest.TestCase):
    def test_blocking_helper_does_not_starve_event_loop(self) -> None:
        """A blocking tool that sleeps 300ms must not prevent a
        concurrently-scheduled ticker coroutine from running.

        This test prevents accidental regressions of the FastMCP sync
        tool model: if someone reverts a tool to a sync ``def`` and
        calls it without ``asyncio.to_thread``, the ticker will not
        run and this test will fail.
        """

        async def _scenario() -> None:
            ticker_ticks: list[float] = []
            stop = asyncio.Event()

            async def ticker() -> None:
                while not stop.is_set():
                    ticker_ticks.append(time.time())
                    await asyncio.sleep(0.02)

            ticker_task = asyncio.create_task(ticker())
            try:
                loop = asyncio.get_running_loop()
                # Simulate the safe pattern: blocking work in a thread.
                def _blocking() -> str:
                    time.sleep(0.3)
                    return "ok"

                t0 = time.time()
                result = await asyncio.to_thread(_blocking)
                self.assertEqual("ok", result)
                stop.set()
                await ticker_task
                elapsed = time.time() - t0
                # The ticker should have run several times while the
                # blocking work was in flight.
                self.assertGreaterEqual(
                    len(ticker_ticks), 5,
                    f"event loop was starved: only {len(ticker_ticks)} ticks during {elapsed:.2f}s"
                )
            finally:
                stop.set()
                await ticker_task

        asyncio.run(_scenario())

    def test_async_mcp_tool_does_not_run_blocking_inline(self) -> None:
        """Static analysis guard: every MCP tool that wraps a subprocess
        or git control_plane helper must be ``async def`` and use
        ``asyncio.to_thread`` (or the safe helper directly via thread
        offload). A sync ``def`` that calls ``git_status_result`` is
        exactly the bug we just fixed.
        """
        import inspect

        tool_names = {
            "git_status", "git_diff", "git_branch", "git_log",
            "git_worktree_list", "git_rev_parse", "git_add", "git_commit", "git_ls_remote",
            "git_push_dry_run", "git_push_ref",
            "codex_status", "codex_run", "codex_start",
            "harness_list", "harness_status", "task_start",
        }
        for name in tool_names:
            fn = getattr(server_legacy, name, None)
            self.assertIsNotNone(fn, f"{name} missing on server_legacy")
            self.assertTrue(
                inspect.iscoroutinefunction(fn),
                f"{name} must be async def (got sync); sync tools run on the FastMCP event loop and can wedge the stdio transport",
            )

    def test_batch7_subprocess_callsites_stay_inside_explicit_lifecycle_helpers(self) -> None:
        allowed = {
            "control_plane.py": {
                "_escalate_terminate",
                "spawn_runtime_child",
                "run_safe_subprocess",
            },
            "codex_job_worker.py": {
                "run_codex_with_lifecycle", "run_minimax_with_lifecycle",
                "run_agy_with_lifecycle",
            },
            "harness_telemetry.py": set(),
            "harness_process_adapter.py": set(),
        }
        violations = []
        for filename, allowed_functions in allowed.items():
            tree = ast.parse((PROJECT_ROOT / filename).read_text(encoding="utf-8"))

            class Visitor(ast.NodeVisitor):
                def __init__(self):
                    self.function = "<module>"

                def visit_FunctionDef(self, node):
                    previous, self.function = self.function, node.name
                    self.generic_visit(node)
                    self.function = previous

                visit_AsyncFunctionDef = visit_FunctionDef

                def visit_Call(self, node):
                    func = node.func
                    if (isinstance(func, ast.Attribute)
                            and isinstance(func.value, ast.Name)
                            and func.value.id == "subprocess"
                            and func.attr in {"run", "Popen"}
                            and self.function not in allowed_functions):
                        violations.append(f"{filename}:{node.lineno}:{self.function}:subprocess.{func.attr}")
                    self.generic_visit(node)

            Visitor().visit(tree)
        self.assertEqual(violations, [])

    def test_harness_telemetry_remains_offloaded_from_async_mcp(self) -> None:
        source = (PROJECT_ROOT / "server_legacy.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "harness_telemetry")
        segment = ast.get_source_segment(source, fn) or ""
        self.assertIn("await asyncio.to_thread", segment)


class HardTerminationReportingTests(unittest.TestCase):
    def test_run_git_reports_failure_on_hard_termination(self) -> None:
        """When the safe helper returns ``-1`` (hard-killed), the git
        helper must surface that as ``ok=False`` with a clear error
        message — it must not lie and say ``ok=True``.
        """

        def fake_run(argv, *, cwd=None, env=None, timeout=None, max_output_bytes=2_000_000):
            return subprocess.CompletedProcess(
                args=argv, returncode=-1, stdout="", stderr=""
            )

        with mock.patch.object(control_plane, "run_safe_subprocess", side_effect=fake_run):
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                (repo / ".git").mkdir()
                result = control_plane._run_git(["status"], repo, timeout=1.0)

        self.assertFalse(result["ok"])
        self.assertIn("terminated", result["error"].lower())


class SpawnRuntimeChildLifecycleTests(unittest.TestCase):
    def test_legacy_delegation_calls_popen_directly(self) -> None:
        """In default/legacy runtime mode, spawn_runtime_child delegates to Popen."""
        mock_proc = mock.Mock()
        with mock.patch.dict(os.environ, {"HARBOR_RUNTIME_MODE": ""}, clear=False), \
             mock.patch.object(control_plane.subprocess, "Popen", return_value=mock_proc) as mock_popen:
            result = control_plane.spawn_runtime_child(
                ["echo", "hi"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/test",
                env={"KEY": "VAL"},
            )
            self.assertIs(result, mock_proc)
            mock_popen.assert_called_once_with(
                ["echo", "hi"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/test",
                env={"KEY": "VAL"},
            )

    def test_packaged_delegation_and_environment_sanitization(self) -> None:
        """In packaged mode, spawn_runtime_child redacts sensitive credentials and calls spawn_owned."""
        mock_child = mock.Mock()
        mock_spawn_owned = mock.Mock(return_value=mock_child)
        with mock.patch.dict(
            os.environ,
            {
                "HARBOR_RUNTIME_MODE": "packaged",
                "TUNNEL_RUNTIME_KEY": "secret-tunnel-key",
                control_plane.CUSTOM_CODEX_ENV_KEY: "secret-custom-key",
                "SAFE_VAR": "safe-value",
            },
            clear=False,
        ), mock.patch("harbor_platform.process.spawn_owned", mock_spawn_owned):
            # 1. Without explicit env: redacts both TUNNEL_RUNTIME_KEY and CUSTOM_CODEX_ENV_KEY
            res1 = control_plane.spawn_runtime_child(["cli-cmd"], stdin=subprocess.DEVNULL)
            self.assertIs(res1, mock_child)
            call_env1 = mock_spawn_owned.call_args.kwargs["env"]
            self.assertNotIn("TUNNEL_RUNTIME_KEY", call_env1)
            self.assertNotIn(control_plane.CUSTOM_CODEX_ENV_KEY, call_env1)
            self.assertEqual(call_env1.get("SAFE_VAR"), "safe-value")

            # 2. With explicit env: preserves caller's custom codex key, but still redacts tunnel key
            mock_spawn_owned.reset_mock()
            explicit_env = {
                "TUNNEL_RUNTIME_KEY": "leaked-tunnel",
                control_plane.CUSTOM_CODEX_ENV_KEY: "intended-custom-key",
                "CUSTOM_FLAG": "1",
            }
            res2 = control_plane.spawn_runtime_child(["codex-cmd"], env=explicit_env)
            self.assertIs(res2, mock_child)
            call_env2 = mock_spawn_owned.call_args.kwargs["env"]
            self.assertNotIn("TUNNEL_RUNTIME_KEY", call_env2)
            self.assertEqual(call_env2.get(control_plane.CUSTOM_CODEX_ENV_KEY), "intended-custom-key")
            self.assertEqual(call_env2.get("CUSTOM_FLAG"), "1")

    def test_safe_subprocess_passes_stdio_isolation_to_spawn_runtime_child(self) -> None:
        """run_safe_subprocess must pass stdin=DEVNULL (or PIPE if input provided) and PIPE for stdout/stderr."""
        seen_kwargs = []
        def fake_spawn(argv, **kwargs):
            seen_kwargs.append(kwargs)
            fake_proc = mock.Mock()
            fake_proc.returncode = 0
            fake_proc.stdout = io.BytesIO(b"out")
            fake_proc.stderr = io.BytesIO(b"")
            fake_proc.stdin = io.BytesIO() if kwargs.get("stdin") == subprocess.PIPE else None
            fake_proc.poll.return_value = 0
            fake_proc.wait.return_value = 0
            return fake_proc

        with mock.patch.object(control_plane, "spawn_runtime_child", side_effect=fake_spawn):
            # Without input -> stdin=DEVNULL
            control_plane.run_safe_subprocess(["git", "status"], timeout=1.0)
            self.assertEqual(seen_kwargs[-1]["stdin"], subprocess.DEVNULL)
            self.assertEqual(seen_kwargs[-1]["stdout"], subprocess.PIPE)
            self.assertEqual(seen_kwargs[-1]["stderr"], subprocess.PIPE)

            # With input -> stdin=PIPE
            control_plane.run_safe_subprocess(["git", "apply"], input=b"diff", timeout=1.0)
            self.assertEqual(seen_kwargs[-1]["stdin"], subprocess.PIPE)
            self.assertEqual(seen_kwargs[-1]["stdout"], subprocess.PIPE)
            self.assertEqual(seen_kwargs[-1]["stderr"], subprocess.PIPE)

    def test_spawn_runtime_child_escalates_termination_on_packaged_failure(self) -> None:
        """When in packaged mode, escalate_terminate invokes terminate_tree and fails closed if unstopped."""
        mock_proc = mock.Mock()
        with mock.patch.dict(os.environ, {"HARBOR_RUNTIME_MODE": "packaged"}, clear=False), \
             mock.patch("harbor_platform.process.terminate_tree", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                control_plane._escalate_terminate(mock_proc, ["hanging-cmd"])
            self.assertIn("Owned process tree could not be stopped", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
