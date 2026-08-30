"""Codex / MiniMax / Antigravity job worker.

Each invocation handles exactly one job directory. The worker is
short-lived: it claims the job (creates ``worker.lock``), runs the
harness subprocess to completion (or to a forced termination), writes
back the final state, and always releases the lock in ``finally``.

Three execution models are supported:

* **Codex** — runs the Codex CLI via a one-shot blocking
  ``subprocess.run``. Codex exits cleanly on completion.
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
    - A hard total timeout (default 30 min) bounds the entire job.
    - All of these terminations reclaim the process tree and never
      leak child processes.
* **Antigravity (agy)** — runs ``agy --print`` through a
  lifecycle-aware supervisor that mirrors the MiniMax shape but
  watches the bounded stdout tail (not a result file) for the final
  JSON line. The worker writes ``result.txt`` from the captured
  stdout. The cwd is supplied at the Popen layer because agy has
  no ``--cwd`` flag. The hard total timeout is identical to MiniMax
  (30 min). Forced exits after a stable result line are reported
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

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO

from control_plane import (
    _escalate_terminate,
    build_agy_command,
    build_codex_command,
    build_minimax_command,
    claim_job,
    read_json_object,
    utc_now,
    write_json,
)


OUTPUT_TAIL_CHARS = 4000

# Lifecycle defaults. Tests override these via parameters.
MINIMAX_POLL_INTERVAL = 0.2            # 200ms between poll()/result checks
MINIMAX_RESULT_SETTLE_GRACE = 0.2      # result.txt must be stable this long
MINIMAX_EXIT_GRACE = 3.0               # CLI gets this long to exit after result
MINIMAX_TOTAL_TIMEOUT = 30 * 60.0      # 30 min hard upper bound
READER_BUFFER_BYTES = 4 * 1024 * 1024  # 4 MiB per stream before tail truncation


# ---------------------------------------------------------------------------
# state helpers
# ---------------------------------------------------------------------------


def write_state(path: Path, state: dict) -> None:
    state["updated_at"] = utc_now()
    write_json(path, state)


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
# Codex execution (unchanged shape; blocking run is fine for Codex)
# ---------------------------------------------------------------------------


def collect_result(result: subprocess.CompletedProcess, result_path: Path) -> dict:
    stderr_tail = output_tail(result.stderr)
    stdout_tail = output_tail(result.stdout)
    collected = {
        "exit_code": result.returncode,
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
    if result.returncode == 0:
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
    with ``errors="replace"`` to handle large or partially-UTF-8 output.

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
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        )

    try:
        proc = subprocess.Popen(command, **popen_kwargs)
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

    try:
        while True:
            now = time.monotonic()
            exit_code = proc.poll()
            if exit_code is not None:
                termination_reason = "self_exit"
                break

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
            if proc.poll() is None:
                _escalate_terminate(proc, list(command))
                exit_code = proc.returncode
                forced_exit = True
                if termination_reason == "self_exit":
                    termination_reason = "self_exit"
        except Exception:  # noqa: BLE001
            pass
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
    return {
        "exit_code": exit_code,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "termination_reason": termination_reason,
        "forced_exit": forced_exit,
        "result_completed_at": result_completed_at,
        "result_text": first_stable_text,
    }


def collect_minimax_lifecycle_result(
    lifecycle: dict, result_path: Path
) -> dict:
    """Translate a MiniMax lifecycle report into the worker state delta.

    Semantics:
    * ``grace_expired_after_result`` with a valid result.txt -> ``completed``,
      ``forced_exit_after_result=True``.
    * ``hard_timeout`` without a result -> ``failed``,
      ``failure_type=minimax_execution_timeout``.
    * ``self_exit`` with a result -> ``completed``.
    * ``self_exit`` without a result -> ``failed``,
      ``failure_type=minimax_execution_error``.
    * ``failed_to_start`` -> ``failed``,
      ``failure_type=minimax_execution_error``.
    """
    stderr_tail = output_tail(lifecycle.get("stderr", ""))
    stdout_tail = output_tail(lifecycle.get("stdout", ""))
    collected: dict = {
        "exit_code": lifecycle.get("exit_code", -1),
        "stderr": stderr_tail,
        "stderr_tail": stderr_tail,
        "stdout_tail": stdout_tail,
        "stderr_is_diagnostic": True,
        "termination_reason": lifecycle.get("termination_reason", "unknown"),
        "forced_exit_after_result": False,
    }

    final_message, parse_error = _read_minimax_message(result_path)
    # If we never got a stable result from the lifecycle watch, also
    # try the post-hoc read once.
    if not final_message and lifecycle.get("result_text"):
        final_message = lifecycle["result_text"]
    collected["final_message"] = final_message
    reason = lifecycle.get("termination_reason", "self_exit")
    forced = bool(lifecycle.get("forced_exit", False))

    if reason == "grace_expired_after_result" and final_message and not parse_error:
        collected["status"] = "completed"
        collected["forced_exit_after_result"] = True
        return collected
    if reason == "self_exit" and final_message and not parse_error:
        collected["status"] = "completed"
        return collected
    if reason == "hard_timeout":
        collected["status"] = "failed"
        collected["failure_type"] = "minimax_execution_timeout"
        collected["error"] = (
            "MiniMax CLI did not write a usable result before the hard timeout"
        )
        return collected
    if reason == "failed_to_start":
        collected["status"] = "failed"
        collected["failure_type"] = "minimax_execution_error"
        collected["error"] = (
            f"MiniMax CLI could not be started: {lifecycle.get('stderr', '')}"
        )
        return collected
    # self_exit but no usable result, or forced exit without result
    collected["status"] = "failed"
    if reason == "self_exit":
        collected["failure_type"] = "minimax_execution_error"
    else:
        collected["failure_type"] = "minimax_execution_error"
    collected["error"] = parse_error or (
        f"MiniMax CLI terminated (reason={reason}, forced={forced}) without producing a result"
    )
    return collected


# ---------------------------------------------------------------------------
# Antigravity (agy) lifecycle execution
# ---------------------------------------------------------------------------
#
# The agy CLI runs as a one-shot ``agy --print`` process. Unlike Codex
# (which has its own blocking subprocess.run wrapper) and unlike MiniMax
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
# Hard total timeout defaults to 30 min, identical to the minimax
# harness, so the daemon-level abort budget stays consistent across
# harnesses.
# ---------------------------------------------------------------------------


AGY_RESULT_SETTLE_GRACE = 0.2      # final JSON line must be stable this long
AGY_EXIT_GRACE = 3.0               # CLI gets this long to exit after result
AGY_TOTAL_TIMEOUT = 30 * 60.0      # 30 min hard upper bound


def _extract_agy_result_message(stdout_text: str) -> str:
    """Locate the last JSON object on stdout and return its ``response`` / ``result`` field.

    Agy with ``--output-format json`` emits a JSON object on stdout at the end
    of the turn with shape:
    ``{"conversation_id": "...", "status": "SUCCESS", "response": "..."}``

    We accept the following fields in priority order:
    1. ``{"response": "..."}`` — canonical AGY JSON output
    2. ``{"result": "..."}``   — alt
    3. ``{"text": "..."}``     — alt
    4. ``{"content": "..."}``  — alt
    5. ``{"answer": "..."}``   — alt (matches minimax's shape)
    6. ``{"output": "..."}``   — alt
    7. ``{"message": "..."}``  — alt
    8. A plain string line is returned as-is (best effort)

    If multiple JSON objects appear, the last one wins. If no JSON
    object can be parsed, the last non-empty stdout line is returned
    verbatim so the caller's ``final_message`` is still populated.
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
        for key in ("response", "result", "text", "content", "answer", "output", "message"):
            value = last_json_obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # JSON but no recognized field: return the raw JSON so the
        # caller can still see *something* useful.
        return last_json_raw or ""
    # No JSON; fall back to the last non-empty line.
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
    flag (verified by the capability probe contract).

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
        proc = subprocess.Popen(command, **popen_kwargs)
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

    try:
        while True:
            now = time.monotonic()
            exit_code = proc.poll()
            if exit_code is not None:
                termination_reason = "self_exit"
                break

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
            if proc.poll() is None:
                _escalate_terminate(proc, list(command))
                exit_code = proc.returncode
                forced_exit = True
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

    Semantics (mirror ``collect_minimax_lifecycle_result``):

    * ``grace_expired_after_result`` with a usable final message -> ``completed``,
      ``forced_exit_after_result=True``.
    * ``self_exit`` with a usable final message -> ``completed``.
    * ``hard_timeout`` without a final message -> ``failed``,
      ``failure_type=agy_execution_timeout``.
    * ``failed_to_start`` -> ``failed``, ``failure_type=agy_execution_error``.
    * Otherwise -> ``failed``, ``failure_type=agy_execution_error`` with a
      diagnostic ``error`` string.

    The function also writes ``result.txt`` from the captured stdout so
    downstream consumers that read result.txt keep working.
    """
    stderr_tail = output_tail(lifecycle.get("stderr", ""))
    stdout_full = lifecycle.get("stdout", "") or ""
    stdout_tail = output_tail(stdout_full)
    collected: dict = {
        "exit_code": lifecycle.get("exit_code", -1),
        "stderr": stderr_tail,
        "stderr_tail": stderr_tail,
        "stdout_tail": stdout_tail,
        "stderr_is_diagnostic": True,
        "termination_reason": lifecycle.get("termination_reason", "unknown"),
        "forced_exit_after_result": False,
    }

    final_message = _extract_agy_result_message(stdout_full)
    # If the lifecycle's pre-computed result_text captured the tail but
    # the post-hoc extractor found nothing (e.g. agy emitted a JSON
    # object whose key we did not recognize), keep the lifecycle's
    # pre-computed value as a last-ditch fallback.
    if not final_message and lifecycle.get("result_text"):
        final_message = lifecycle["result_text"]

    # Always write result.txt so the existing collect_result-style
    # downstream code path can still read it.
    try:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(final_message or "", encoding="utf-8")
    except OSError:
        pass

    collected["final_message"] = final_message
    reason = lifecycle.get("termination_reason", "self_exit")
    forced = bool(lifecycle.get("forced_exit", False))

    if reason == "grace_expired_after_result" and final_message:
        collected["status"] = "completed"
        collected["forced_exit_after_result"] = True
        return collected
    if reason == "self_exit" and final_message:
        collected["status"] = "completed"
        return collected
    if reason == "hard_timeout":
        collected["status"] = "failed"
        collected["failure_type"] = "agy_execution_timeout"
        collected["error"] = (
            "Antigravity CLI did not produce a final result before the hard timeout"
        )
        return collected
    if reason == "failed_to_start":
        collected["status"] = "failed"
        collected["failure_type"] = "agy_execution_error"
        collected["error"] = (
            f"Antigravity CLI could not be started: {lifecycle.get('stderr', '')}"
        )
        return collected
    # self_exit / grace_expired_after_result / unknown without a usable message
    collected["status"] = "failed"
    collected["failure_type"] = "agy_execution_error"
    collected["error"] = (
        f"Antigravity CLI terminated (reason={reason}, forced={forced}) "
        "without producing a final result"
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
    state_path = job_dir / "status.json"
    state: dict = {}
    try:
        state = claim_job(job_dir)
        if state is None:
            return

        result_path = job_dir / "result.txt"
        harness = state.get("harness", "codex")
        if harness == "codex":
            command = build_codex_command(state, result_path)
        elif harness == "minimax":
            command = build_minimax_command(state, result_path)
        elif harness == "agy":
            command = build_agy_command(state, result_path)
        else:
            raise ValueError(f"Unsupported worker harness: {harness}")
        state["native_process"] = {"argv": command, "launcher_pid": os.getpid()}
        write_state(state_path, state)

        if harness == "minimax":
            lifecycle = run_minimax_with_lifecycle(command, result_path)
            state.update(collect_minimax_lifecycle_result(lifecycle, result_path))
        elif harness == "agy":
            # Agy has no --cwd flag; the working directory is supplied
            # at the Popen layer. result.txt is not passed to agy; the
            # lifecycle collector writes it from the captured stdout.
            lifecycle = run_agy_with_lifecycle(command, cwd=state["cwd"])
            state.update(collect_agy_lifecycle_result(lifecycle, result_path))
        else:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            state.update(collect_result(result, result_path))
            state["route_used"] = "current"

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
        try:
            release_job_lock(job_dir)
        except Exception:
            pass


if __name__ == "__main__":
    main(Path(sys.argv[1]))
