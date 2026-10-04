"""Codex / MiniMax / Antigravity job worker.

Each invocation handles exactly one job directory. The worker is
short-lived: it claims the job (creates ``worker.lock``), runs the
harness subprocess to completion (or to a forced termination), writes
back the final state, and always releases the lock in ``finally``.

Three execution models are supported:

* **Codex** — runs the Codex CLI through a bounded lifecycle supervisor
  with isolated stdin, process-tree cleanup, and a two-hour hard timeout.
* **MiniMax** — runs the MiniMax CLI through a lifecycle-aware
  supervisor that:
    - Polls the process every 200ms instead of blocking forever.
    - Watches ``--output-last-message`` for a stable, non-empty
      result file (the runtime-level signal that the model turn
      finished).
    - After result.txt is stable, grants a short grace period
      (default 3s) for the CLI to exit on its own.
    - If the CLI is still alive after the grace period, it is
      terminated via the same escalation used by
      ``control_plane.run_safe_subprocess`` (terminate -> kill ->
      ``taskkill /T /F`` -> final reap).
    - A hard total timeout (default 2 hours) bounds the entire job.
    - All of these terminations reclaim the process tree and never
      leak child processes.
* **Antigravity (agy)** — runs ``agy`` with ``--print=<prompt>`` through a
  lifecycle-aware supervisor that mirrors the MiniMax shape but
  watches the bounded stdout tail (not a result file) for the final
  JSON line. The worker writes ``result.txt`` from the captured
  stdout. The cwd is supplied at the Popen layer because agy has
  no ``--cwd`` flag. The hard total timeout is identical to MiniMax
  (2 hours). Forced exits after a stable result line are reported
  as ``completed`` with ``forced_exit_after_result=True``; true
  failures (no result before hard timeout) become ``failed`` with
  ``failure_type=agy_execution_timeout``.

The MiniMax and agy execution never trusts the CLI to exit on its
own: they use observable evidence (a stable result file for
MiniMax, a stable stdout tail for agy) to decide when the job is
done. Forced exits after a stable result are reported as
``completed`` with ``forced_exit_after_result=True``; true failures
(no result before hard timeout) become ``failed`` with
``failure_type={minimax,agy}_execution_timeout``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO

from control_plane import (
    spawn_runtime_child,
    CODEX_CONFIG,
    _escalate_terminate,
    _get_codex_config_provider,
    build_agy_command,
    build_codex_command,
    codex_child_environment,
    resolve_custom_codex_route,
    build_minimax_command,
    claim_job,
    is_official_quota_exhausted,
    is_stale_harbor_managed_codex_config,
    read_json_object,
    record_official_quota_exhausted,
    utc_now,
    write_json,
)


OUTPUT_TAIL_CHARS = 4000

# Suffix appended to the original prompt when the Codex harness retries the
# job on the Code Flow route (attempt 2). The instruction tells the model to
# build on the worktree state already produced by attempt 1 instead of
# redoing edits, so the retry is a continuation rather than a duplicate.
CONTINUATION_SUFFIX = (
    "Continue from the current working tree state. Preserve and inspect "
    "changes already made by the previous attempt; do not redo completed edits."
)

# Lifecycle defaults. Tests override these via parameters.
MINIMAX_POLL_INTERVAL = 0.2            # 200ms between poll()/result checks
MINIMAX_RESULT_SETTLE_GRACE = 0.2      # result.txt must be stable this long
MINIMAX_EXIT_GRACE = 3.0               # CLI gets this long to exit after result
MINIMAX_TOTAL_TIMEOUT = 2 * 60 * 60.0  # 2-hour hard upper bound
READER_BUFFER_BYTES = 4 * 1024 * 1024  # 4 MiB per stream before tail truncation
CODEX_TOTAL_TIMEOUT = MINIMAX_TOTAL_TIMEOUT  # same conservative harness-task bound


# ---------------------------------------------------------------------------
# state helpers
# ---------------------------------------------------------------------------


def write_state(path: Path, state: dict) -> None:
    from control_plane import _job_poll_lock
    with _job_poll_lock(path.parent):
        disk = load_state(path) if path.is_file() else {}
        if disk.get("status") == "cancelled" and disk.get("stop"):
            state["status"] = "cancelled"
            state["stop"] = disk["stop"]
            state["stop_ownership"] = disk.get("stop_ownership")
            state["cancelled_at"] = disk.get("cancelled_at")
        elif disk.get("status") == "cancelling":
            state["stop"] = disk["stop"]
            state["stop_ownership"] = disk.get("stop_ownership")
            # The stop request won the durable ordering race. Preserve output
            # and attempts, but do not publish a generic CLI failure/success.
            if state.get("status") != "cancelling":
                if state.get("status") in {"completed", "failed"}:
                    state["stop"]["worker_result_observed_at"] = utc_now()
                state["status"] = "cancelling"
        state["updated_at"] = utc_now()
        write_json(path, state)


_ACTIVE_STATE_PATH: Path | None = None


def _register_native(proc) -> None:
    """Persist identity while the caller holds the job poll lock."""
    if _ACTIVE_STATE_PATH is None:
        return
    from harbor_platform.process import process_identity
    identity = getattr(proc, "_harbor_identity", None) or process_identity(proc.pid)
    disk = load_state(_ACTIVE_STATE_PATH)
    owner = disk.get("stop_ownership")
    if not isinstance(owner, dict) or identity is None:
        raise RuntimeError("Native process ownership could not be registered")
    owner["native"] = identity
    owner["native_exit_verified"] = False
    disk["stop_ownership"] = owner
    write_json(_ACTIVE_STATE_PATH, disk)


def _spawn_native(command, **kwargs):
    if _ACTIVE_STATE_PATH is None:
        return spawn_runtime_child(command, **kwargs)
    from control_plane import _job_poll_lock
    with _job_poll_lock(_ACTIVE_STATE_PATH.parent):
        proc = spawn_runtime_child(command, owned=True, **kwargs)
        try:
            _register_native(proc)
        except BaseException:
            from harbor_platform.process import terminate_tree
            terminate_tree(proc)
            raise
        return proc


def _stop_requested(proc) -> bool:
    if _ACTIVE_STATE_PATH is None:
        return False
    from task_stop_control import request_native_stop
    return request_native_stop(proc, _ACTIVE_STATE_PATH)


def _owned_native_alive(proc) -> bool:
    from harbor_platform.process import owned_tree_alive
    return owned_tree_alive(proc)


def _mark_native_exit(proc) -> None:
    if _ACTIVE_STATE_PATH is None:
        return
    try:
        if _owned_native_alive(proc):
            return
    except (OSError, ValueError):
        return
    from control_plane import _job_poll_lock
    with _job_poll_lock(_ACTIVE_STATE_PATH.parent):
        disk = load_state(_ACTIVE_STATE_PATH)
        owner = disk.get("stop_ownership")
        if isinstance(owner, dict) and isinstance(owner.get("native"), dict) and owner["native"].get("pid") == proc.pid:
            owner["native_exit_verified"] = True
            write_json(_ACTIVE_STATE_PATH, disk)


def output_tail(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value[-OUTPUT_TAIL_CHARS:].strip()


def load_state(path: Path) -> dict:
    return read_json_object(path)


def release_job_lock(job_dir: Path) -> None:
    """Remove the worker.lock file the previous claim_job created.

    Best-effort: a missing lock is acceptable. The lock is only meant
    to coordinate between concurrent workers; the daemon never blocks
    on it.
    """
    lock_path = job_dir / "worker.lock"
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Codex execution
# ---------------------------------------------------------------------------


def run_codex_with_lifecycle(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    total_timeout: float = CODEX_TOTAL_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run one Codex attempt with bounded output and full tree cleanup."""
    popen_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        )

    proc = _spawn_native(command, **popen_kwargs)
    stdout_reader = _BoundedStreamReader(proc.stdout)
    stderr_reader = _BoundedStreamReader(proc.stderr)
    stdout_reader.start()
    stderr_reader.start()
    timed_out = False
    stop_requested = False
    try:
        deadline = time.monotonic() + total_timeout
        while proc.poll() is None or (stop_requested and _owned_native_alive(proc)):
            if not stop_requested and _stop_requested(proc):
                stop_requested = True
            if stop_requested:
                time.sleep(0.1)
                continue
            if time.monotonic() >= deadline:
                timed_out = True
                _escalate_terminate(proc, list(command))
                break
            try:
                proc.wait(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
    finally:
        if not stop_requested and (proc.poll() is None or os.environ.get("HARBOR_RUNTIME_MODE") == "packaged"):
            _escalate_terminate(proc, list(command))
        stdout = stdout_reader.drain(timeout=0.5)
        stderr = stderr_reader.drain(timeout=0.5)
        for handle, reader in ((proc.stdout, stdout_reader), (proc.stderr, stderr_reader)):
            try:
                if (reader._thread is None or not reader._thread.is_alive()) and handle is not None and not handle.closed:
                    handle.close()
            except (OSError, ValueError):
                pass

    result = subprocess.CompletedProcess(
        args=list(command),
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
    )
    result.termination_reason = "task_stop" if stop_requested else ("hard_timeout" if timed_out else "self_exit")
    _mark_native_exit(proc)
    return result


def collect_result(result: subprocess.CompletedProcess, result_path: Path) -> dict:
    stderr_tail = output_tail(result.stderr)
    stdout_tail = output_tail(result.stdout)
    collected = {
        "exit_code": result.returncode,
        "process_exit_code": result.returncode,
        "agent_task_status": None,
        "stderr": stderr_tail,
        "stderr_tail": stderr_tail,
        "stdout_tail": stdout_tail,
        "stderr_is_diagnostic": True,
    }

    try:
        final_message = result_path.read_text(
            encoding="utf-8",
            errors="replace",
        ).strip()
    except Exception as exc:
        final_message = ""
        if result.returncode == 0:
            collected.update(
                status="failed",
                failure_type="result_parse_error",
                error="Could not extract the final Codex message",
                parser_error=f"{type(exc).__name__}: {exc}",
                final_message=final_message,
            )
            return collected

    collected["final_message"] = final_message
    if getattr(result, "termination_reason", None) == "hard_timeout":
        collected.update(
            status="failed",
            failure_type="codex_execution_timeout",
            error="Codex CLI exceeded the hard task timeout",
        )
    elif result.returncode == 0:
        collected["status"] = "completed"
    else:
        collected.update(
            status="failed",
            failure_type="codex_execution_error",
            error="Codex exited with a non-zero status",
        )
    return collected


# ---------------------------------------------------------------------------
# MiniMax lifecycle execution
# ---------------------------------------------------------------------------


def _read_minimax_message(result_path: Path) -> tuple[str, str | None]:
    """Read the final message from result.txt. Returns (message, error)."""
    try:
        message = result_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return "", "result.txt could not be read"
    if not message:
        return "", "result.txt is empty"
    return message, None


class _BoundedStreamReader:
    """Drain a pipe into a bounded tail buffer in a daemon thread.

    The reader accumulates decoded text. ``Popen`` is invoked without
    ``encoding=`` so the underlying stream is binary; we decode here
    with ``errors="replace"`` to avoid surprises on the production
    side where large or partially-UTF-8 output is plausible.

    The reader uses ``os.read`` on the raw file descriptor rather than
    ``BufferedReader.read(N)``. On Windows, ``BufferedReader.read(N)``
    on a Popen pipe blocks until N bytes or EOF, even for small N, so
    a producer that wrote a short final result and then hung would
    never let the supervisor see the data. ``os.read`` honors the
    underlying OS pipe semantics: it returns as soon as at least one
    byte is available (or empty bytes on EOF). The thread loops with
    a small sleep so the supervisor's ``drain()`` always sees the
    latest buffered data.
    """

    _READ_CHUNK = 4096

    def __init__(self, stream: IO[bytes] | None, cap_bytes: int = READER_BUFFER_BYTES):
        self._stream = stream
        self._cap = cap_bytes
        self._chunks: list[bytes] = []
        self._size = 0
        self._lock = threading.Lock()
        self._eof = False
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None
        self._fd: int | None = None

    def start(self) -> None:
        if self._stream is None:
            self._eof = True
            return
        try:
            self._fd = self._stream.fileno()
        except (OSError, ValueError, AttributeError):
            self._fd = None
        self._thread = threading.Thread(
            target=self._run, name="worker-stream-reader", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        if self._fd is None:
            self._run_blocking()
            return
        try:
            while True:
                try:
                    chunk = os.read(self._fd, self._READ_CHUNK)
                except OSError as exc:
                    # Pipe closed / handle invalidated.
                    with self._lock:
                        self._error = exc
                    break
                if not chunk:
                    # EOF
                    break
                with self._lock:
                    self._chunks.append(chunk)
                    self._size += len(chunk)
                    while self._size > self._cap and len(self._chunks) > 1:
                        first = self._chunks[0]
                        self._chunks.pop(0)
                        self._size -= len(first)
                    if self._size > self._cap:
                        head = self._chunks[0]
                        keep = self._cap
                        self._chunks[0] = head[-keep:]
                        self._size = keep
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._error = exc
        finally:
            with self._lock:
                self._eof = True

    def _run_blocking(self) -> None:
        """Fallback path for streams that don't expose a fileno."""
        assert self._stream is not None
        try:
            while True:
                chunk = self._stream.read(self._READ_CHUNK)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                with self._lock:
                    self._chunks.append(chunk)
                    self._size += len(chunk)
                    while self._size > self._cap and len(self._chunks) > 1:
                        first = self._chunks[0]
                        self._chunks.pop(0)
                        self._size -= len(first)
                    if self._size > self._cap:
                        head = self._chunks[0]
                        keep = self._cap
                        self._chunks[0] = head[-keep:]
                        self._size = keep
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._error = exc
        finally:
            with self._lock:
                self._eof = True

    def drain(self, timeout: float = 0.5) -> str:
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        with self._lock:
            if self._error is not None:
                return f"[reader error: {type(self._error).__name__}: {self._error}]"
            return b"".join(self._chunks).decode("utf-8", errors="replace")


def _result_file_stable(
    result_path: Path, last_size: int | None, last_mtime_ns: int | None
) -> tuple[bool, int, int, str | None]:
    """Return (stable, size, mtime_ns, message_or_None).

    A result file is considered stable when:
      * it exists,
      * it is a regular file,
      * it is non-empty,
      * its size has not changed across two consecutive polls
        AND its mtime has not changed either.
    """
    try:
        st = result_path.stat()
    except OSError:
        return False, -1, -1, None
    if not result_path.is_file():
        return False, -1, -1, None
    size = st.st_size
    mtime_ns = st.st_mtime_ns
    if size <= 0:
        return False, size, mtime_ns, None
    stable = (
        last_size is not None
        and last_mtime_ns is not None
        and size == last_size
        and mtime_ns == last_mtime_ns
    )
    if not stable:
        return False, size, mtime_ns, None
    try:
        text = result_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return False, size, mtime_ns, None
    if not text:
        return False, size, mtime_ns, None
    return True, size, mtime_ns, text


def run_minimax_with_lifecycle(
    command: list[str],
    result_path: Path,
    *,
    prompt: str = "",
    poll_interval: float = MINIMAX_POLL_INTERVAL,
    settle_grace: float = MINIMAX_RESULT_SETTLE_GRACE,
    exit_grace: float = MINIMAX_EXIT_GRACE,
    total_timeout: float = MINIMAX_TOTAL_TIMEOUT,
) -> dict:
    """Run a MiniMax exec command with lifecycle supervision.

    Returns a dict containing:

    * ``exit_code`` — final returncode (or -1 if we forced termination)
    * ``stdout`` / ``stderr`` — bounded text captured by reader threads
    * ``termination_reason`` — one of:
        ``self_exit`` / ``grace_expired_after_result`` /
        ``hard_timeout`` / ``failed_to_start``
    * ``forced_exit`` — bool, True iff we terminated the process tree
    * ``result_completed_at`` — seconds (monotonic) when result.txt
      first became stable, or None
    """
    popen_kwargs: dict = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "bufsize": 0,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        )

    try:
        proc = _spawn_native(command, **popen_kwargs)
    except (OSError, ValueError) as exc:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "termination_reason": "failed_to_start",
            "forced_exit": True,
            "result_completed_at": None,
        }

    stdout_reader = _BoundedStreamReader(proc.stdout)
    stderr_reader = _BoundedStreamReader(proc.stderr)
    stdout_reader.start()
    stderr_reader.start()

    prompt_bytes = prompt.encode("utf-8")

    def feed_prompt() -> None:
        try:
            if proc.stdin is not None:
                proc.stdin.write(prompt_bytes)
                proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                if proc.stdin is not None and not proc.stdin.closed:
                    proc.stdin.close()
            except (OSError, ValueError):
                pass

    stdin_feeder = threading.Thread(target=feed_prompt, daemon=True, name="minimax-stdin-feeder")
    stdin_feeder.start()

    started = time.monotonic()
    deadline = started + total_timeout
    last_size: int | None = None
    last_mtime_ns: int | None = None
    first_stable_at: float | None = None
    first_stable_text: str | None = None
    result_completed_at: float | None = None
    exit_code: int | None = None
    termination_reason = "hard_timeout"
    forced_exit = False
    stop_requested = False

    try:
        while True:
            now = time.monotonic()
            exit_code = proc.poll()
            if exit_code is not None and (not stop_requested or not _owned_native_alive(proc)):
                termination_reason = "task_stop" if stop_requested else "self_exit"
                break

            if not stop_requested and _stop_requested(proc):
                stop_requested = True
            if stop_requested:
                termination_reason = "task_stop"
                time.sleep(poll_interval)
                continue

            if now >= deadline:
                termination_reason = "hard_timeout"
                forced_exit = True
                break

            stable, size, mtime_ns, text = _result_file_stable(
                result_path, last_size, last_mtime_ns
            )
            last_size, last_mtime_ns = size, mtime_ns
            if stable and first_stable_at is None:
                first_stable_at = now
                first_stable_text = text
            if (
                stable
                and first_stable_at is not None
                and (now - first_stable_at) >= settle_grace
            ):
                result_completed_at = first_stable_at
                # Was the process already done between the previous
                # poll and now? Give it a final chance.
                exit_code = proc.poll()
                if exit_code is not None:
                    termination_reason = "self_exit"
                    break
                # Now wait up to exit_grace for the CLI to wind down.
                wait_deadline = time.monotonic() + exit_grace
                while True:
                    now = time.monotonic()
                    if now >= wait_deadline:
                        break
                    if proc.poll() is not None:
                        exit_code = proc.returncode
                        termination_reason = "self_exit"
                        forced_exit = False
                        break
                    time.sleep(min(poll_interval, max(0.0, wait_deadline - now)))
                if termination_reason != "self_exit":
                    # Still alive after the grace period: kill the tree.
                    _escalate_terminate(proc, list(command))
                    exit_code = proc.returncode
                    forced_exit = True
                    termination_reason = "grace_expired_after_result"
                break

            time.sleep(poll_interval)
    finally:
        # Reap (idempotent) and drain remaining pipe bytes.
        try:
            was_running = proc.poll() is None
            if not stop_requested and (was_running or os.environ.get("HARBOR_RUNTIME_MODE") == "packaged"):
                _escalate_terminate(proc, list(command))
                exit_code = proc.returncode
                forced_exit = forced_exit or was_running
                if termination_reason == "self_exit":
                    termination_reason = "self_exit"
        except Exception:  # noqa: BLE001
            pass
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        stdin_feeder.join(timeout=0.5)
        # Give the reader threads a short window to drain whatever
        # they can from the pipes.
        stdout_text = stdout_reader.drain(timeout=0.5)
        stderr_text = stderr_reader.drain(timeout=0.5)
        try:
            if proc.poll() is None:
                # last-ditch: do not return until the child is gone.
                proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass

    if exit_code is None:
        exit_code = -1
    # Best-effort close of the pipe handles so the OS reclaims them
    # even if the reader threads are still draining.
    for handle in (proc.stdout, proc.stderr):
        try:
            if handle is not None and not handle.closed:
                handle.close()
        except Exception:  # noqa: BLE001
            pass
    _mark_native_exit(proc)
    return {
        "exit_code": exit_code,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "termination_reason": termination_reason,
        "forced_exit": forced_exit,
        "result_completed_at": result_completed_at,
        "result_text": first_stable_text,
    }


MINIMAX_NON_SUCCESS_STATUSES = {
    "failed",
    "error",
    "cancelled",
    "canceled",
}


def _parse_minimax_terminal_output(
    stdout_text: str,
    result_path: Path | None = None,
    result_text: str | None = None,
) -> dict:
    """Parse terminal output and result file from MiniMax execution."""
    message = ""
    parse_error: str | None = None

    if result_path is not None and result_path.is_file():
        try:
            message = result_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass

    if not message and result_text:
        message = result_text.strip()

    last_json_obj: dict | None = None
    if isinstance(stdout_text, str) and stdout_text:
        for raw_line in stdout_text.splitlines():
            line = raw_line.strip()
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                last_json_obj = payload

    agent_task_status: str | None = None
    error: str | None = None
    if last_json_obj is not None:
        status_val = last_json_obj.get("status")
        if isinstance(status_val, str):
            agent_task_status = status_val.strip()
        err_val = last_json_obj.get("error")
        if isinstance(err_val, str):
            error = err_val.strip()
        if not message:
            for k in ("output", "answer", "response", "result", "message"):
                val = last_json_obj.get(k)
                if isinstance(val, str) and val.strip():
                    message = val.strip()
                    break

    if not message:
        parse_error = "result.txt is empty or missing"

    return {
        "final_message": message,
        "agent_task_status": agent_task_status,
        "error": error,
        "parse_error": parse_error,
        "raw_envelope": last_json_obj,
    }


MINIMAX_SUCCESS_STATUSES = {
    "succeeded",
    "success",
    "ok",
}


def _has_canonical_minimax_success(
    *,
    norm_status: str | None,
    final_message: str,
    parse_error: str | None,
) -> bool:
    """Return True iff the captured terminal output qualifies as a canonical MiniMax success.

    A canonical success requires:

    * the agent task status from the exec.result JSON envelope is one of
      ``succeeded`` / ``success`` / ``ok`` (case-insensitive); an absent
      status is NOT canonical (we have no envelope-level confirmation).
    * a non-empty final message (from the result file or the JSON
      envelope's ``output``/``answer``/``response``/``result``/``message``
      field), and
    * no parser error (the captured output is not malformed or empty).
    """
    if not norm_status or norm_status not in MINIMAX_SUCCESS_STATUSES:
        return False
    if not final_message.strip():
        return False
    if parse_error:
        return False
    return True


def collect_minimax_lifecycle_result(
    lifecycle: dict, result_path: Path
) -> dict:
    """Translate a MiniMax lifecycle report into the worker state delta.

    Unified failure precedence:
    1. Fatal harness failure (failed_to_start)
    2. Timeout (hard_timeout)
    3. Process nonzero exit (process_exit_code != 0) — BUT skipped when a
       valid canonical MiniMax exec.result (agent status succeeded, usable
       final message) was already captured and the only reason the wrapper
       process exited non-zero is that Harbor's post-result grace expired
       and the lifecycle supervisor had to kill the lingering CLI
       (termination_reason=grace_expired_after_result). In that narrow
       case the agent task already completed inside the captured envelope
       and the wrapper's exit code is a consequence of Harbor's own
       cleanup, not a real execution failure.
    4. Agent explicit failure status (failed, error, cancelled, etc., case-insensitive)
    5. Canonical / structured success (usable final result):
       normally process_exit_code == 0, with a narrow nonzero exception when
       the result is canonical and Harbor cleaned up after grace expiry.
    6. Otherwise failed
    """
    stderr_full = lifecycle.get("stderr", "") or ""
    stdout_full = lifecycle.get("stdout", "") or ""
    stderr_tail = output_tail(stderr_full)
    stdout_tail = output_tail(stdout_full)
    process_exit_code = lifecycle.get("exit_code", -1)
    termination_reason = lifecycle.get("termination_reason", "unknown")
    forced = bool(lifecycle.get("forced_exit", False))

    parsed = _parse_minimax_terminal_output(
        stdout_full, result_path, lifecycle.get("result_text")
    )
    final_message = parsed["final_message"]
    agent_task_status = parsed["agent_task_status"]
    norm_status = agent_task_status.lower() if isinstance(agent_task_status, str) else None
    parse_error = parsed["parse_error"]

    collected: dict = {
        "exit_code": process_exit_code,
        "process_exit_code": process_exit_code,
        "agent_task_status": agent_task_status,
        "stderr": stderr_tail,
        "stderr_tail": stderr_tail,
        "stdout_tail": stdout_tail,
        "stderr_is_diagnostic": True,
        "termination_reason": termination_reason,
        "forced_exit_after_result": forced and (termination_reason == "grace_expired_after_result"),
        "final_message": final_message,
    }

    # 1. Fatal harness failure: failed to start
    if termination_reason == "failed_to_start":
        collected.update(
            status="failed",
            failure_type="minimax_execution_error",
            error=f"MiniMax CLI could not be started: {stderr_tail}",
        )
        return collected

    # 2. Timeout
    if termination_reason == "hard_timeout":
        collected.update(
            status="failed",
            failure_type="minimax_execution_timeout",
            error="MiniMax CLI did not write a usable result before the hard timeout",
        )
        return collected

    # 3. Process nonzero exit.
    # Narrow carve-out: a valid canonical MiniMax exec.result has already
    # been captured (agent status == succeeded with usable final message)
    # and the wrapper process was force-terminated by Harbor only because
    # the CLI failed to exit cleanly within the post-result grace period
    # (termination_reason == grace_expired_after_result). In that case the
    # agent task is genuinely complete; the wrapper's non-zero exit is a
    # consequence of Harbor's own cleanup, so we must not retroactively
    # convert a successful MiniMax task into a failure. All other
    # non-zero exits (self_exit with no canonical success, hard_timeout,
    # failed_to_start, process crashes before any envelope) still fail.
    has_canonical_success = _has_canonical_minimax_success(
        norm_status=norm_status,
        final_message=final_message,
        parse_error=parse_error,
    )
    if process_exit_code != 0:
        if (
            termination_reason == "grace_expired_after_result"
            and has_canonical_success
        ):
            # Defer to step 5's canonical-success branch below; record
            # the exit code and let the success path complete.
            pass
        else:
            collected.update(
                status="failed",
                failure_type="minimax_execution_error",
                error=f"MiniMax CLI exited with non-zero status ({process_exit_code})",
            )
            return collected

    # 4. Agent explicit failure status (case-insensitive)
    if norm_status in MINIMAX_NON_SUCCESS_STATUSES:
        collected.update(
            status="failed",
            failure_type="minimax_execution_error",
            error=parsed.get("error") or f"MiniMax agent reported non-success status: {agent_task_status}",
        )
        return collected

    # 5. Canonical / structured success.
    # Two flavours of completion are recognised here:
    #
    # (a) The agent emitted a canonical exec.result envelope whose
    #     ``status`` field is one of succeeded/success/ok AND we have a
    #     usable final message AND no parser error. In this case the
    #     agent task itself is genuinely complete and the wrapper
    #     process's exit code is incidental — including the case where
    #     Harbor killed the lingering CLI after the post-result grace
    #     expired (termination_reason=grace_expired_after_result). The
    #     process exit code is NOT required to be zero here.
    #
    # (b) A legacy/CLI-compat path: no canonical JSON envelope status
    #     (``norm_status is None``) but the result file is non-empty,
    #     there is no parser error, AND the process exited cleanly
    #     (process_exit_code == 0). This preserves the previous
    #     behavior for harnesses / wrappers that do not emit a
    #     structured JSON envelope on stdout.
    if final_message.strip() and not parse_error:
        if norm_status in MINIMAX_SUCCESS_STATUSES:
            if process_exit_code != 0:
                collected["exit_code"] = 0
                if forced and termination_reason == "grace_expired_after_result":
                    collected["cleanup_anomaly"] = "minimax_cleanup_after_result"
            collected["status"] = "completed"
            return collected
        if process_exit_code == 0 and norm_status is None:
            collected["status"] = "completed"
            return collected

    # 6. Otherwise: failed
    collected.update(
        status="failed",
        failure_type="minimax_execution_error",
        error=parse_error or (
            f"MiniMax CLI terminated (reason={termination_reason}, forced={forced}) "
            "without producing a usable result"
        ),
    )
    return collected


# ---------------------------------------------------------------------------
# Antigravity (agy) lifecycle execution
# ---------------------------------------------------------------------------
#
# The agy CLI runs as a one-shot ``agy`` process via ``--print=<prompt>``. Unlike Codex
# (which has its own lifecycle supervisor) and unlike MiniMax
# (which writes a result file we can poll for stability), agy emits a
# single JSON object on stdout at the end of the turn. The worker must
# therefore watch the *stdout stream* for the final non-empty line and
# then write that into ``result.txt`` so downstream consumers that
# already read result.txt (and the existing minimax-compatible
# collect_*_lifecycle_result shape) keep working without modification.
#
# Lifecycle semantics mirror the minimax shape:
#   * ``self_exit``                       — agy exited on its own
#   * ``grace_expired_after_result``      — agy was still alive after the
#                                           JSON final line became stable
#                                           and the post-result exit grace
#                                           expired; the worker killed it
#   * ``hard_timeout``                    — no result before the hard wall
#                                           clock; the worker killed it
#   * ``failed_to_start``                 — Popen itself raised
#
# Hard total timeout defaults to 2 hours, identical to the minimax
# harness, so the daemon-level abort budget stays consistent across
# harnesses.
# ---------------------------------------------------------------------------


AGY_RESULT_SETTLE_GRACE = 0.2      # final JSON line must be stable this long
AGY_EXIT_GRACE = 3.0               # CLI gets this long to exit after result
AGY_TOTAL_TIMEOUT = 2 * 60 * 60.0  # 2-hour hard upper bound


RECOGNIZED_RESULT_KEYS = (
    "response",
    "result",
    "text",
    "content",
    "answer",
    "output",
    "message",
)
CANONICAL_ENVELOPE_KEYS = ("status", "conversation_id")

AGY_PERMISSION_DENIED_RE = re.compile(
    r'(?:jetski:\s*no output produced\s*[-—–]+\s*)?a tool required the ["\'](?P<tool>[^"\']+)["\'] permission that headless mode cannot prompt for,\s*so it was auto-denied',
    re.IGNORECASE,
)

AGY_NON_SUCCESS_STATUSES = {
    "ERROR",
    "FAILED",
    "CANCELED",
    "CANCELLED",
    "INTERRUPTED",
    "INVALID",
    "WAITING",
}


def _detect_agy_tool_permission_denial(
    stderr_text: str, stdout_text: str = ""
) -> str | None:
    """Extract the tool name if a headless permission denial occurred, or None."""
    for text in (stderr_text, stdout_text):
        if not text:
            continue
        m = AGY_PERMISSION_DENIED_RE.search(text)
        if m:
            return m.group("tool")
    return None


def _parse_agy_terminal_output(stdout_text: str, stderr_text: str = "") -> dict:
    """Structured parser for AGY terminal output.

    Returns a dict containing:
      * canonical: bool
      * status: str | None
      * response: str | None
      * error: str | None
      * conversation_id: str | None
      * tool_denied: str | None
      * last_json_raw: str | None
      * raw_envelope: dict | None
    """
    last_json_obj: dict | None = None
    last_json_raw: str | None = None
    if isinstance(stdout_text, str) and stdout_text:
        for raw_line in stdout_text.splitlines():
            line = raw_line.strip()
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                last_json_obj = payload
                last_json_raw = line

    tool_denied = _detect_agy_tool_permission_denial(stderr_text, stdout_text)

    if last_json_obj is not None:
        status_raw = last_json_obj.get("status")
        status = status_raw.strip().upper() if isinstance(status_raw, str) else None

        response = None
        for k in RECOGNIZED_RESULT_KEYS:
            if k in last_json_obj:
                val = last_json_obj[k]
                response = val.strip() if isinstance(val, str) else ""
                break

        err_val = last_json_obj.get("error")
        error = err_val.strip() if isinstance(err_val, str) else None

        cid_val = last_json_obj.get("conversation_id")
        cid = cid_val.strip() if isinstance(cid_val, str) else None

        is_canonical = bool(
            "status" in last_json_obj
            or "response" in last_json_obj
            or "conversation_id" in last_json_obj
        )

        return {
            "canonical": is_canonical,
            "status": status,
            "response": response,
            "error": error,
            "conversation_id": cid,
            "tool_denied": tool_denied,
            "last_json_raw": last_json_raw,
            "raw_envelope": last_json_obj,
        }

    return {
        "canonical": False,
        "status": None,
        "response": None,
        "error": None,
        "conversation_id": None,
        "tool_denied": tool_denied,
        "last_json_raw": None,
        "raw_envelope": None,
    }


def _extract_agy_result_message(stdout_text: str) -> str:
    """Locate the last JSON object on stdout and return its ``response`` / ``result`` field.

    Agy with ``--output-format json`` emits a JSON object on stdout at the end
    of the turn with shape:
    ``{"conversation_id": "...", "status": "SUCCESS", "response": "..."}``

    Canonical AGY JSON envelopes:
    - If `response` field exists:
      - `response="AGY_OK"` -> returns `"AGY_OK"`
      - `response=""` -> returns `""`
      - `response=null` -> fails closed, returns `""` (no raw fallback)
    - If other recognized keys exist (result, text, content, answer, output, message):
      - returns stripped string value if present, or `""` if empty/null
    - If canonical envelope keys (status, conversation_id) are present but without
      a recognized result string, returns `""` (no raw fallback).
    - Unknown non-canonical JSON without recognized or envelope keys falls back to raw JSON.
    - Plain text output without JSON falls back to the last non-empty line.
    """
    if not isinstance(stdout_text, str) or not stdout_text:
        return ""
    last_json_obj: dict | None = None
    last_json_raw: str | None = None
    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            last_json_obj = payload
            last_json_raw = line

    if last_json_obj is not None:
        for key in RECOGNIZED_RESULT_KEYS:
            if key in last_json_obj:
                val = last_json_obj[key]
                if isinstance(val, str):
                    return val.strip()
                return ""
        if any(k in last_json_obj for k in CANONICAL_ENVELOPE_KEYS):
            return ""
        # Unknown legacy / non-canonical JSON: return the raw JSON so caller can inspect
        return last_json_raw or ""

    # No JSON: fall back to the last non-empty line
    for raw_line in reversed(stdout_text.splitlines()):
        line = raw_line.strip()
        if line:
            return line
    return ""



def _agy_stdout_has_final_result(stdout_text: str) -> tuple[bool, str]:
    """Return (is_final, message).

    The final-result signal is the last JSON object on stdout (or the
    last non-empty line if no JSON object is present). We do **not**
    require any specific key here; the lifecycle supervisor only needs
    to know that *some* terminal output exists so it can grant the
    exit grace and stop the polling loop. Field extraction is the job
    of ``_extract_agy_result_message`` and ``collect_agy_lifecycle_result``.
    """
    if not isinstance(stdout_text, str) or not stdout_text:
        return False, ""
    # Use the bounded drain: we want a stable tail to compare against.
    last_line = ""
    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if line:
            last_line = line
    if not last_line:
        return False, ""
    return True, last_line


def _agy_stdout_stable(
    stdout_text: str, last_tail: str | None
) -> tuple[bool, str]:
    """Return (stable, current_tail).

    The stdout stream is "stable" once the bounded reader has drained
    it AND its non-empty tail is unchanged across two consecutive
    polls. This is the agy analog of ``_result_file_stable`` for
    minimax; minimax watches a result file, agy watches the bounded
    stdout drain.
    """
    last_line = ""
    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if line:
            last_line = line
    if not last_line:
        return False, ""
    if last_tail is not None and last_line == last_tail:
        return True, last_line
    return False, last_line


def run_agy_with_lifecycle(
    command: list[str],
    *,
    cwd: str | None = None,
    poll_interval: float = 0.2,
    settle_grace: float = AGY_RESULT_SETTLE_GRACE,
    exit_grace: float = AGY_EXIT_GRACE,
    total_timeout: float = AGY_TOTAL_TIMEOUT,
) -> dict:
    """Run an agy command with lifecycle supervision.

    The shape mirrors ``run_minimax_with_lifecycle`` but the "result
    is ready" signal comes from the bounded stdout tail becoming
    stable (not from a result file). After that signal, the worker
    grants ``exit_grace`` seconds for agy to exit on its own and
    then escalates to terminate.

    ``cwd`` is supplied at the Popen layer because agy has no ``--cwd``
    flag (verified in the A1 discovery report).

    The function always returns a dict; it never raises.
    """
    popen_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": cwd,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        )

    try:
        proc = _spawn_native(command, **popen_kwargs)
    except (OSError, ValueError) as exc:
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "termination_reason": "failed_to_start",
            "forced_exit": True,
            "result_completed_at": None,
            "result_text": "",
        }

    stdout_reader = _BoundedStreamReader(proc.stdout)
    stderr_reader = _BoundedStreamReader(proc.stderr)
    stdout_reader.start()
    stderr_reader.start()

    started = time.monotonic()
    deadline = started + total_timeout
    last_tail: str | None = None
    first_stable_at: float | None = None
    first_stable_text: str | None = None
    result_completed_at: float | None = None
    exit_code: int | None = None
    termination_reason = "hard_timeout"
    forced_exit = False
    stop_requested = False

    try:
        while True:
            now = time.monotonic()
            exit_code = proc.poll()
            if exit_code is not None and (not stop_requested or not _owned_native_alive(proc)):
                termination_reason = "task_stop" if stop_requested else "self_exit"
                break

            if not stop_requested and _stop_requested(proc):
                stop_requested = True
            if stop_requested:
                termination_reason = "task_stop"
                time.sleep(poll_interval)
                continue

            if now >= deadline:
                termination_reason = "hard_timeout"
                forced_exit = True
                break

            # Drain the bounded reader to get a stable tail.
            current_stdout = stdout_reader.drain(timeout=0.05)
            stable, tail = _agy_stdout_stable(current_stdout, last_tail)
            last_tail = tail
            if stable and first_stable_at is None:
                first_stable_at = now
                first_stable_text = tail
            if (
                stable
                and first_stable_at is not None
                and (now - first_stable_at) >= settle_grace
            ):
                result_completed_at = first_stable_at
                # Was the process already done between the previous
                # poll and now? Give it a final chance.
                exit_code = proc.poll()
                if exit_code is not None:
                    termination_reason = "self_exit"
                    break
                # Wait up to exit_grace for the CLI to wind down.
                wait_deadline = time.monotonic() + exit_grace
                while True:
                    now = time.monotonic()
                    if now >= wait_deadline:
                        break
                    if proc.poll() is not None:
                        exit_code = proc.returncode
                        termination_reason = "self_exit"
                        forced_exit = False
                        break
                    time.sleep(min(poll_interval, max(0.0, wait_deadline - now)))
                if termination_reason != "self_exit":
                    # Still alive after the grace period: kill the tree.
                    _escalate_terminate(proc, list(command))
                    exit_code = proc.returncode
                    forced_exit = True
                    termination_reason = "grace_expired_after_result"
                break

            time.sleep(poll_interval)
    finally:
        try:
            was_running = proc.poll() is None
            if not stop_requested and (was_running or os.environ.get("HARBOR_RUNTIME_MODE") == "packaged"):
                _escalate_terminate(proc, list(command))
                exit_code = proc.returncode
                forced_exit = forced_exit or was_running
                if termination_reason == "self_exit":
                    termination_reason = "self_exit"
        except Exception:  # noqa: BLE001
            pass
        stdout_text = stdout_reader.drain(timeout=0.5)
        stderr_text = stderr_reader.drain(timeout=0.5)
        try:
            if proc.poll() is None:
                proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass

    if exit_code is None:
        exit_code = -1
    for handle in (proc.stdout, proc.stderr):
        try:
            if handle is not None and not handle.closed:
                handle.close()
        except Exception:  # noqa: BLE001
            pass
    _mark_native_exit(proc)
    return {
        "exit_code": exit_code,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "termination_reason": termination_reason,
        "forced_exit": forced_exit,
        "result_completed_at": result_completed_at,
        "result_text": first_stable_text or "",
    }


def collect_agy_lifecycle_result(
    lifecycle: dict, result_path: Path
) -> dict:
    """Translate an agy lifecycle report into the worker state delta.

    Unified failure precedence:
    1. Fatal structured harness failure: headless permission denial (agy_tool_permission_denied)
    2. Fatal harness failure: failed_to_start
    3. Process nonzero exit (process_exit_code != 0)
    4. Timeout / Forced termination without result (hard_timeout)
    5. Agent explicit failure status (ERROR, FAILED, CANCELED, etc.)
    6. Canonical success (status == SUCCESS and usable non-empty response)
    7. Legacy fallback (non-canonical JSON or plain text with exit_code == 0)
    8. Otherwise failed
    """
    stderr_full = lifecycle.get("stderr", "") or ""
    stdout_full = lifecycle.get("stdout", "") or ""
    stderr_tail = output_tail(stderr_full)
    stdout_tail = output_tail(stdout_full)
    process_exit_code = lifecycle.get("exit_code", -1)
    termination_reason = lifecycle.get("termination_reason", "unknown")
    forced = bool(lifecycle.get("forced_exit", False))

    parsed = _parse_agy_terminal_output(stdout_full, stderr_full)
    agent_task_status = parsed.get("status")
    final_message = _extract_agy_result_message(stdout_full)
    tool_denied = parsed.get("tool_denied")

    # Always write result.txt
    try:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(final_message or "", encoding="utf-8")
    except OSError:
        pass

    collected: dict = {
        "exit_code": process_exit_code,
        "process_exit_code": process_exit_code,
        "agent_task_status": agent_task_status,
        "stderr": stderr_tail,
        "stderr_tail": stderr_tail,
        "stdout_tail": stdout_tail,
        "stderr_is_diagnostic": True,
        "termination_reason": termination_reason,
        "forced_exit_after_result": forced and (termination_reason == "grace_expired_after_result"),
        "final_message": final_message,
    }

    # 1. Fatal structured harness failure: permission denial
    if tool_denied:
        collected.update(
            status="failed",
            failure_type="agy_tool_permission_denied",
            tool=tool_denied,
            error=(
                f'Antigravity headless mode auto-denied tool permission: "{tool_denied}". '
                f'Stderr: {stderr_tail[-240:] if stderr_tail else "none"}'
            ),
        )
        return collected

    # 2. Fatal harness failure: failed to start
    if termination_reason == "failed_to_start":
        collected.update(
            status="failed",
            failure_type="agy_execution_error",
            error=f"Antigravity CLI could not be started: {stderr_tail}",
        )
        return collected

    # 3. Timeout
    if termination_reason == "hard_timeout":
        collected.update(
            status="failed",
            failure_type="agy_execution_timeout",
            error="Antigravity CLI did not produce a final result before the hard timeout",
        )
        return collected

    # 4. Process nonzero exit
    if process_exit_code != 0:
        collected.update(
            status="failed",
            failure_type="agy_execution_error",
            error=f"Antigravity CLI exited with non-zero status ({process_exit_code})",
        )
        return collected

    # 5. Agent explicit failure status
    if agent_task_status in AGY_NON_SUCCESS_STATUSES:
        collected.update(
            status="failed",
            failure_type="agy_execution_error",
            error=parsed.get("error") or f"Antigravity agent reported non-success status: {agent_task_status}",
        )
        return collected

    # 6. Canonical success
    if parsed.get("canonical"):
        if agent_task_status == "SUCCESS" and final_message.strip():
            collected["status"] = "completed"
            return collected
        collected.update(
            status="failed",
            failure_type="agy_headless_false_success" if not final_message.strip() else "agy_execution_error",
            error=(
                f"Antigravity canonical envelope returned status={agent_task_status} with empty response"
                if not final_message.strip()
                else f"Antigravity canonical envelope returned status={agent_task_status}"
            ),
        )
        return collected

    # 7. Legacy fallback: plain text or non-canonical JSON with exit_code == 0 and non-empty text
    if final_message.strip() and process_exit_code == 0:
        collected["status"] = "completed"
        return collected

    # 8. Otherwise: failed
    collected.update(
        status="failed",
        failure_type="agy_execution_error",
        error=(
            f"Antigravity CLI terminated (reason={termination_reason}, forced={forced}) "
            "without producing a usable final result"
        ),
    )
    return collected


# Backwards-compatible wrapper for the old CompletedProcess-based
# collect_minimax_result. The new lifecycle path is preferred; the old
# helper is preserved so any external test/import still works.
def _read_minimax_message_old(
    result: subprocess.CompletedProcess, result_path: Path
) -> tuple[str, str | None]:
    try:
        message = result_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        message = ""
    if message:
        return message, None
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        return "", "Could not extract the final MiniMax message from --output-last-message or JSON stdout"
    if not isinstance(payload, dict):
        return "", "MiniMax JSON output was not an object"
    if isinstance(payload.get("answer"), str):
        return payload["answer"].strip(), None
    output = payload.get("output")
    if isinstance(output, str):
        return output.strip(), None
    return "", "MiniMax JSON output did not contain a final answer"


def collect_minimax_result(
    result: subprocess.CompletedProcess, result_path: Path
) -> dict:
    stderr_tail = output_tail(result.stderr)
    stdout_tail = output_tail(result.stdout)
    collected = {
        "exit_code": result.returncode,
        "process_exit_code": result.returncode,
        "agent_task_status": None,
        "stderr": stderr_tail,
        "stderr_tail": stderr_tail,
        "stdout_tail": stdout_tail,
        "stderr_is_diagnostic": True,
        "termination_reason": "self_exit",
        "forced_exit_after_result": False,
    }
    final_message, parse_error = _read_minimax_message_old(result, result_path)
    collected["final_message"] = final_message
    if result.returncode != 0:
        collected.update(
            status="failed",
            failure_type="minimax_execution_error",
            error="MiniMax exited with a non-zero status",
        )
    elif parse_error:
        collected.update(
            status="failed",
            failure_type="result_parse_error",
            error=parse_error,
            parser_error=parse_error,
        )
    else:
        collected["status"] = "completed"
    return collected


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------


def main(job_dir: Path) -> None:
    global _ACTIVE_STATE_PATH
    state_path = job_dir / "status.json"
    state: dict = {}
    try:
        state = claim_job(job_dir)
        if state is None:
            return
        _ACTIVE_STATE_PATH = state_path

        result_path = job_dir / "result.txt"
        harness = state.get("harness", "codex")
        if harness == "codex":
            route_req = state.get("route_requested") or state.get("route_used") or "current"
            initial_route = "official" if route_req in ("official", "official_then_codeflow", "official_then_custom") else route_req
            command = build_codex_command(state, result_path, route=initial_route)
        elif harness == "minimax":
            command = build_minimax_command(state, result_path)
        elif harness == "agy":
            command = build_agy_command(state, result_path)
        else:
            raise ValueError(f"Unsupported worker harness: {harness}")
        state["native_process"] = {"argv": command, "launcher_pid": os.getpid()}
        if harness == "minimax":
            prompt_bytes = state["prompt"].encode("utf-8")
            state["native_process"].update(
                prompt_transport="stdin",
                prompt_utf8_bytes=len(prompt_bytes),
                prompt_sha256=hashlib.sha256(prompt_bytes).hexdigest(),
            )
        write_state(state_path, state)
        if load_state(state_path).get("status") == "cancelling":
            return

        if harness == "minimax":
            lifecycle = run_minimax_with_lifecycle(command, result_path, prompt=state["prompt"])
            state.update(collect_minimax_lifecycle_result(lifecycle, result_path))
        elif harness == "agy":
            # Agy has no --cwd flag; the working directory is supplied
            # at the Popen layer. result.txt is not passed to agy; the
            # lifecycle collector writes it from the captured stdout.
            lifecycle = run_agy_with_lifecycle(command, cwd=state["cwd"])
            state.update(collect_agy_lifecycle_result(lifecycle, result_path))
        else:
            # Codex path: process-local route override.
            # No runtime mutation of config.toml, no snapshot/restore.
            attempts: list[dict] = []
            route_requested = state.get("route_requested") or state.get("route_used") or "current"
            original_route = route_requested
            original_provider = _get_codex_config_provider(CODEX_CONFIG)

            stale, stale_reason = is_stale_harbor_managed_codex_config(CODEX_CONFIG)
            if stale:
                state.update(
                    status="failed",
                    failure_type="stale_harbor_managed_codex_config",
                    error=f"stale_harbor_managed_codex_config: {stale_reason}",
                    stderr_is_diagnostic=True,
                )
                write_state(state_path, state)
                return

            attempt1_route = "official" if route_requested in ("official", "official_then_codeflow", "official_then_custom") else route_requested
            command_attempt1 = build_codex_command(state, result_path, route=attempt1_route)
            child_env_attempt1 = codex_child_environment(attempt1_route)

            result = run_codex_with_lifecycle(
                command_attempt1,
                env=child_env_attempt1,
            )
            first_attempt = collect_result(result, result_path)
            first_attempt["route"] = attempt1_route
            attempts.append(first_attempt)
            state.update(first_attempt)

            diagnostic = "\n".join([state.get("stderr", ""), state.get("stdout_tail", "")])
            should_fallback = (
                route_requested in ("official_then_codeflow", "official_then_custom")
                and state.get("status") == "failed"
                and load_state(state_path).get("status") != "cancelling"
                and is_official_quota_exhausted(diagnostic)
                and (bool(os.environ.get("CODEFLOW_API_KEY")) if route_requested == "official_then_codeflow" else bool(resolve_custom_codex_route(include_secret=True).get("ok")))
            )
            if should_fallback:
                record_official_quota_exhausted(diagnostic)

                continuation_prompt = (
                    state.get("prompt", "")
                    + "\n\n"
                    + CONTINUATION_SUFFIX
                )
                command_attempt2 = build_codex_command(
                    {**state, "prompt": continuation_prompt},
                    result_path,
                    route="codeflow" if route_requested == "official_then_codeflow" else "custom",
                )
                state["native_process"] = {"argv": command_attempt2, "launcher_pid": os.getpid()}
                write_state(state_path, state)

                fallback_route = "codeflow" if route_requested == "official_then_codeflow" else "custom"
                child_env_attempt2 = codex_child_environment(fallback_route)
                result2 = run_codex_with_lifecycle(
                    command_attempt2,
                    env=child_env_attempt2,
                )
                second_attempt = collect_result(result2, result_path)
                second_attempt["route"] = fallback_route
                attempts.append(second_attempt)
                state.update(second_attempt)

            state["attempts"] = attempts
            state["original_provider"] = original_provider
            state["original_route"] = original_route
            if len(attempts) == 2:
                state["fallback_used"] = True
                state["fallback_reason"] = "official_quota_exhausted"
                state["route_used"] = fallback_route
            else:
                state["fallback_used"] = False
                state["fallback_reason"] = None
                state["route_used"] = attempt1_route

        write_state(state_path, state)
    except Exception as exc:
        try:
            state = load_state(state_path)
        except (OSError, ValueError):
            state = {}
        existing_stderr = state.get("stderr")
        existing_stdout = state.get("stdout_tail")
        stderr_tail = output_tail(state.get("stderr"))
        stdout_tail = output_tail(state.get("stdout_tail"))
        state.update(
            status="failed",
            failure_type="wrapper_error",
            process_exit_code=state.get("process_exit_code", -1),
            agent_task_status=state.get("agent_task_status"),
            error=(
                "Codex job wrapper failed"
                if state.get("harness", "codex") == "codex"
                else (
                    "MiniMax job wrapper failed"
                    if state.get("harness", "codex") == "minimax"
                    else "Antigravity job wrapper failed"
                )
            ),
            wrapper_error=f"{type(exc).__name__}: {exc}",
            stderr=stderr_tail,
            stderr_tail=stderr_tail,
            stdout_tail=stdout_tail,
            stderr_is_diagnostic=True,
        )
        write_state(state_path, state)
        # Silence unused warnings for the local fallbacks.
        _ = existing_stderr
        _ = existing_stdout
    finally:
        _ACTIVE_STATE_PATH = None
        try:
            release_job_lock(job_dir)
        except Exception:
            pass


if __name__ == "__main__":
    main(Path(sys.argv[1]))
