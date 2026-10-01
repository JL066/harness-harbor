"""ChatGPT Harbor — a local agent control plane for ChatGPT."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import tomllib
import uuid
from urllib.parse import urlsplit
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import IO, Any, Iterator, Literal

from launcher.credential_store import CredentialStore
from launcher.user_settings import load_user_settings
from harness_process_adapter import default_process_activity_adapter
from harness_telemetry import HarnessTelemetryProvider
from runtime_queue import (
    QueueRoot,
    describe_queue_root,
    queue_root_fingerprint,
    queue_root_matches,
    resolve_queue_root,
)


def _is_pid_alive(pid: int) -> bool:
    """Check if a process ID is currently active in the operating system."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid
            )
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            )
            ctypes.windll.kernel32.CloseHandle(handle)
            STILL_ACTIVE = 259
            return bool(exit_code.value == STILL_ACTIVE)
        except Exception:
            try:
                os.kill(pid, 0)
                return True
            except (OSError, ProcessLookupError, PermissionError) as exc:
                return isinstance(exc, PermissionError)
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, OSError):
            return False


PROJECT_ROOT = Path(__file__).resolve().parent
QUEUE_ROOT: QueueRoot = resolve_queue_root(PROJECT_ROOT)
JOBS_DIR = QUEUE_ROOT.path
CONTROL_DIR = Path(os.environ.get("HARBOR_CONTROL_DIR", str(PROJECT_ROOT / ".control"))).expanduser().resolve()
if os.environ.get("HARBOR_CONTROL_DIR") and not Path(os.environ["HARBOR_CONTROL_DIR"]).is_absolute():
    raise ValueError("HARBOR_CONTROL_DIR must be absolute")
PROJECTS_FILE = CONTROL_DIR / "projects.json"
ROUTE_STATE_FILE = CONTROL_DIR / "route-state.json"
ROUTE_LOCK_FILE = CONTROL_DIR / "route.lock"
BACKUPS_DIR = CONTROL_DIR / "backups"

CODEX_EXE = Path(os.environ.get("HARBOR_CODEX_EXE") or shutil.which("codex") or "codex.exe")
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
_LOCAL_APPDATA = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
_APPDATA = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
MINIMAX_EXE = _LOCAL_APPDATA / "Programs" / "MiniMax Code" / "MiniMax Code.exe"
MINIMAX_CLI_EXE = Path(os.environ.get("HARBOR_MINIMAX_CLI_EXE") or shutil.which("mcode") or "mcode.cmd")
MINIMAX_CONFIG = _APPDATA / "MiniMax" / "minimax-agent-cn-config.json"
CC_SWITCH_EXE = _LOCAL_APPDATA / "Programs" / "CC Switch" / "cc-switch.exe"
AGY_EXE = Path(
    os.environ.get("HARBOR_AGY_EXE")
    or shutil.which("agy.exe")
    or _LOCAL_APPDATA / "agy" / "bin" / "agy.exe"
)
if os.environ.get("HARBOR_RUNTIME_MODE") == "packaged":
    def _packaged_executable(key, name):
        value = os.environ.get(key) or shutil.which(name)
        return Path(value) if value else CONTROL_DIR / "missing-executables" / name
    CODEX_EXE = _packaged_executable("HARBOR_CODEX_EXE", "codex")
    AGY_EXE = _packaged_executable("HARBOR_AGY_EXE", "agy")
    MINIMAX_CLI_EXE = _packaged_executable("HARBOR_MINIMAX_CLI_EXE", "mcode")

AGY_DEFAULT_PRINT_TIMEOUT = "1h"
AGY_EFFORTS = {"low", "medium", "high"}
AGY_PROBE_CACHE_TTL_SECONDS = 15.0
# Generic OpenAI-compatible Codex route identifiers.  These are deliberately
# stable and contain no credential material; the key itself is supplied only
# to the spawned child environment.
CUSTOM_CODEX_PROVIDER_ID = "harbor_custom"
CUSTOM_CODEX_ENV_KEY = "HARBOR_CODEX_CUSTOM_API_KEY"
CODEX_PRIMARY_DEFAULT_MODEL = "gpt-6-sol"
CODEX_PRIMARY_DEFAULT_REASONING_EFFORT = "medium"
CODEX_WORKER_DEFAULT_MODEL = "gpt-6-luna"
CODEX_WORKER_DEFAULT_REASONING_EFFORT = "max"

SANDBOXES = {"read-only", "workspace-write"}
MAX_TEXT_BYTES = 200_000
MAX_DIRECTORY_ENTRIES = 5_000
MAX_GIT_OUTPUT = 50_000
MAX_SUBPROCESS_OUTPUT_BYTES = 2_000_000
GIT_DELIVERY_TIMEOUT_SECONDS = 30
BINARY_SNIFF_BYTES = 8_192
JOB_ID_RE = re.compile(r"^[0-9a-zA-Z_\-]+$")
POLL_THROTTLE_INTERVAL_SECONDS: float = 600.0
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
AGY_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
AGY_MODELS_BLOCKER_RE = re.compile(
    r"(?:"
    r"(?:^|\n)\s*(?:\[\s*(?:error|fatal|exception)\s*\]|(?:error|fatal|exception)\b)|"
    r"\b(?:please\s+(?:sign|log)\s*in|sign\s*in\s+(?:required|needed|to\b)|"
    r"login\s+(?:required|needed|to\b)|not\s+(?:signed|logged)\s*in|"
    r"unauthori[sz]ed|forbidden)\b|"
    r"\b(?:auth(?:entication|orization)?|credential|token|api[_\s-]*key)s?\s+"
    r"(?:failed|failure|error|required|invalid|missing|expired)\b|"
    r"\b(?:failed|invalid|missing|expired)\s+"
    r"(?:auth(?:entication|orization)?|credentials?|tokens?|api[_\s-]*keys?)\b|"
    r"\b(?:failed|unable)\s+to\s+(?:connect|fetch|reach|resolve)\b|"
    r"\bconnection\s+(?:failed|refused|reset|error|closed|timed?\s*out)\b|"
    r"\b(?:network|proxy|dns)\s+(?:error|failure|unreachable|unavailable|timeout|timed?\s*out|failed)\b|"
    r"\b(?:timed\s+out|timeout\s+(?:exceeded|error|occurred))\b"
    r")",
    re.IGNORECASE,
)
AGY_INFO_OR_BANNER_RE = re.compile(
    r"^(?:"
    r"fetching\s+available\s+models\.{0,3}|"
    r"available\s+models:?|"
    r"models?:?(?:\s+description)?|"
    r"(?:id|name|model)\s+description|"
    r"[-=*_\s]+|"
    r"\[\s*(?:info|notice|debug|log|warn|warning|tip|hint)\s*\].*|"
    r"(?:info|notice|debug|log|warn|warning|tip|hint|note)\s*[:\-].*|"
    r"(?:total(?:\s+available)?\s+models?|count)\s*[:=]?\s*\d+.*|"
    r"\d+\s+models?\s+(?:found|available).*|"
    r"(?:using|loaded)\s+(?:config|profile|endpoint|credentials?).*|"
    r"(?:checking|syncing|updating)\s+.*"
    r")$",
    re.IGNORECASE,
)

IS_WINDOWS = sys.platform == "win32"
_WIN_CREATION_FLAGS = (
    subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
) if IS_WINDOWS else 0

TEXT_EXTENSIONS = {
    ".py", ".txt", ".md", ".rst", ".json", ".jsonc", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".csv", ".tsv", ".log", ".xml", ".html", ".htm",
    ".css", ".scss", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue",
    ".java", ".kt", ".kts", ".gradle", ".c", ".h", ".cpp", ".hpp", ".cc",
    ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".bash", ".ps1", ".psm1",
    ".bat", ".cmd", ".sql", ".proto", ".graphql", ".dockerfile", ".gitignore",
    ".gitattributes", ".editorconfig", ".env", ".lock",
}
BINARY_EXTENSIONS = {
    ".exe", ".dll", ".so", ".dylib", ".pyd", ".pyc", ".class", ".jar", ".zip",
    ".tar", ".gz", ".bz2", ".7z", ".rar", ".xz", ".png", ".jpg", ".jpeg",
    ".gif", ".bmp", ".ico", ".webp", ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".sqlite", ".db", ".woff", ".woff2", ".ttf", ".otf",
    ".eot", ".mp3", ".mp4", ".avi", ".mov", ".wav",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Safe subprocess helper
# ---------------------------------------------------------------------------
#
# All Git, MiniMax capability probe, and Codex diagnostics subprocesses MUST
# go through ``run_safe_subprocess``. This helper enforces:
#
# * stdio isolation: child stdin is DEVNULL unless the caller supplies an
#   explicit bytes payload, in which case it is PIPE only for that payload.
#   Parent stdin is never inherited; stdout/stderr are PIPE and never leak to
#   the parent's MCP transport.
# * Windows process isolation: CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
#   so the child cannot accidentally attach to the parent's console or share
#   a process group.
# * Guaranteed cleanup: the child is terminated with a deterministic
#   escalation (terminate -> wait grace -> kill -> wait grace ->
#   ``taskkill /T /F`` on Windows). The helper always reaps the child before
#   returning so it cannot leak as a long-lived orphan.
# * Bounded output: stdout and stderr are decoded and truncated to
#   ``MAX_SUBPROCESS_OUTPUT_BYTES`` to keep MCP memory bounded.
#
# The function is intentionally synchronous. The MCP layer (``server_legacy``)
# offloads calls to ``run_safe_subprocess`` via ``asyncio.to_thread`` so a
# hanging child can never block the FastMCP event loop.
# ---------------------------------------------------------------------------


_GIT_NONINTERACTIVE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "GCM_INTERACTIVE": "Never",
    # No color / progress noise; never use a pager
    "GIT_TERMINAL": "0",
}


def _make_safe_popen_kwargs(
    *,
    env: dict[str, str] | None,
    cwd: str | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
        "cwd": cwd,
    }
    if IS_WINDOWS:
        kwargs["creationflags"] = _WIN_CREATION_FLAGS
    return kwargs


def spawn_runtime_child(argv, **kwargs):
    """Packaged children have verified ownership; legacy retains its contract."""
    if os.environ.get("HARBOR_RUNTIME_MODE") == "packaged":
        from harbor_platform.process import spawn_owned
        explicit_env = kwargs.get("env")
        child_env = dict(os.environ if explicit_env is None else explicit_env)
        child_env.pop("TUNNEL_RUNTIME_KEY", None)
        if explicit_env is None:
            child_env.pop(CUSTOM_CODEX_ENV_KEY, None)
        kwargs["env"] = child_env
        return spawn_owned(argv, popen_factory=subprocess.Popen, **kwargs)
    return subprocess.Popen(argv, **kwargs)


def _escalate_terminate(proc: subprocess.Popen, argv: list[str]) -> None:
    """Terminate ``proc`` and (on Windows) its entire process tree.

    Never raises. Intended to be called from a finally block or after a
    timeout, so it must always succeed at reaping or at least attempting
    every escalation step.

    On Windows, the immediate child returned by ``Popen`` is often a
    thin wrapper (e.g. ``cmd.exe`` wrapping a ``.cmd`` file) that
    reaps cleanly with ``proc.terminate()`` while its real descendant
    work (a long-running ``node.exe``) stays alive. We therefore
    always run ``taskkill /T /F`` on Windows as part of the contract,
    not only as a last resort: killing the root of the process group
    we created with ``CREATE_NEW_PROCESS_GROUP`` is the only way to
    guarantee no orphan node or shell is left behind.
    """
    if proc is None:
        return
    if os.environ.get("HARBOR_RUNTIME_MODE") == "packaged":
        from harbor_platform.process import terminate_tree
        if not terminate_tree(proc):
            raise RuntimeError("Owned process tree could not be stopped")
        return
    pid = proc.pid
    if pid is None:
        return

    # 1. SIGTERM-equivalent (TerminateProcess on Windows). On Windows
    #    the immediate Popen child may die while its descendants
    #    survive; we keep this step for the non-Windows code path
    #    where ``proc.terminate()`` is sufficient.
    if not IS_WINDOWS:
        try:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=0.5)
                return
            except subprocess.TimeoutExpired:
                pass
        except (OSError, ProcessLookupError):
            return

        # 2. SIGKILL-equivalent (still best-effort; some processes can resist)
        try:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=0.5)
                return
            except subprocess.TimeoutExpired:
                pass
        except (OSError, ProcessLookupError):
            return

    # 3. Windows: ALWAYS run ``taskkill /T /F /PID <pid>`` first, then
    #    optionally escalate. /T walks the child tree; /F forces.
    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5.0,
                check=False,
                creationflags=_WIN_CREATION_FLAGS,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        # Some locked-down Windows hosts deny taskkill even for descendants
        # owned by this process.  Fall back to the Win32 snapshot API and
        # terminate descendants deepest-first before terminating the root.
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel32.OpenProcess.restype = wintypes.HANDLE
            snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
            if snapshot not in (0, ctypes.c_void_p(-1).value):
                try:
                    entry = PROCESSENTRY32W()
                    entry.dwSize = ctypes.sizeof(entry)
                    children: dict[int, list[int]] = {}
                    ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
                    while ok:
                        children.setdefault(int(entry.th32ParentProcessID), []).append(int(entry.th32ProcessID))
                        ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))

                    ordered: list[int] = []

                    def _walk(parent: int) -> None:
                        for child in children.get(parent, []):
                            _walk(child)
                            ordered.append(child)
                    _walk(pid)
                    ordered.append(pid)
                    for target_pid in ordered:
                        handle = kernel32.OpenProcess(0x0001 | 0x00100000, False, target_pid)
                        if handle:
                            try:
                                kernel32.TerminateProcess(handle, 1)
                                kernel32.WaitForSingleObject(handle, 1000)
                            finally:
                                kernel32.CloseHandle(handle)
                finally:
                    kernel32.CloseHandle(snapshot)
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=1.0)
            return
        except subprocess.TimeoutExpired:
            pass

    # 4. Final escalation: SIGTERM (non-Windows) and SIGKILL on
    #    whichever platform is still alive.
    try:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=0.5)
            return
        except subprocess.TimeoutExpired:
            pass
    except (OSError, ProcessLookupError):
        return

    try:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=0.5)
            return
        except subprocess.TimeoutExpired:
            pass
    except (OSError, ProcessLookupError):
        return

    # 5. Last-ditch reap: if the process is already gone, ``wait`` returns
    #    immediately; if it is somehow still alive, we cannot do more here.
    try:
        proc.wait(timeout=0.2)
    except Exception:
        pass


def run_safe_subprocess(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    input: bytes | None = None,
    timeout: float | None = None,
    max_output_bytes: int = MAX_SUBPROCESS_OUTPUT_BYTES,
) -> subprocess.CompletedProcess:
    """Run ``argv`` in a child process with strict stdio isolation.

    Returns a ``subprocess.CompletedProcess`` whose ``stdout``/``stderr`` are
    decoded text (best effort, ``errors="replace"``).  Each stream is
    drained concurrently into a buffer capped at ``max_output_bytes``;
    excess bytes are discarded while the child is still running.

    Contract:
    * Child stdin is DEVNULL unless explicit bytes ``input`` is supplied, when
      it is PIPE only for delivery by a dedicated writer thread. The MCP transport pipe
      is never inherited.
    * Child stdout/stderr are PIPE and never reach the parent's stdout/stderr.
    * On Windows: ``CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP``.
    * On timeout: terminate -> wait grace -> kill -> wait grace ->
      ``taskkill /T /F`` (Windows) -> wait grace. The child is always
      reaped before the function returns; no orphan process can remain.
    * The function itself does not raise ``subprocess.TimeoutExpired``;
      it returns a CompletedProcess with ``returncode=-1`` and empty
      stdout/stderr when the child was forcibly terminated.
    """
    if not isinstance(argv, list) or not argv or any(not isinstance(a, str) for a in argv):
        raise ValueError("argv must be a list of non-empty strings")
    if input is not None and not isinstance(input, bytes):
        raise ValueError("input must be bytes or None")
    if not isinstance(max_output_bytes, int) or max_output_bytes < 0:
        raise ValueError("max_output_bytes must be a non-negative integer")

    popen_kwargs = _make_safe_popen_kwargs(env=env, cwd=cwd)
    if input is not None:
        popen_kwargs["stdin"] = subprocess.PIPE
    proc = spawn_runtime_child(argv, **popen_kwargs)

    class _BoundedReader:
        def __init__(self, stream: IO[bytes] | None):
            self.stream = stream
            self.data = bytearray()
            self.thread = threading.Thread(target=self._run, daemon=True)

        def _run(self) -> None:
            if self.stream is None:
                return
            try:
                while True:
                    chunk = self.stream.read(64 * 1024)
                    if not chunk:
                        return
                    remaining = max_output_bytes - len(self.data)
                    if remaining > 0:
                        self.data.extend(chunk[:remaining])
            except (OSError, ValueError):
                pass

    stdout_reader = _BoundedReader(proc.stdout)
    stderr_reader = _BoundedReader(proc.stderr)
    stdout_reader.thread.start()
    stderr_reader.thread.start()

    writer: threading.Thread | None = None
    if input is not None:
        def _write_input() -> None:
            try:
                if proc.stdin is not None:
                    proc.stdin.write(input)
                    proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                try:
                    if proc.stdin is not None:
                        proc.stdin.close()
                except (OSError, ValueError):
                    pass

        writer = threading.Thread(target=_write_input, daemon=True)
        writer.start()

    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _escalate_terminate(proc, list(argv))
    finally:
        # Defensive final reap; safe to call even after a clean return.
        try:
            if proc.poll() is None or os.environ.get("HARBOR_RUNTIME_MODE") == "packaged":
                _escalate_terminate(proc, list(argv))
        except Exception:
            pass

        if writer is not None:
            writer.join(timeout=0.5)
        stdout_reader.thread.join(timeout=0.5)
        stderr_reader.thread.join(timeout=0.5)
        handles = [(proc.stdin, writer)] if writer is not None else []
        handles.extend(((proc.stdout, stdout_reader.thread), (proc.stderr, stderr_reader.thread)))
        for handle, owner_thread in handles:
            try:
                # Closing a Windows pipe while another thread is blocked in
                # read/write can itself block.  The daemon thread owns it
                # until EOF in that rare last-ditch case.
                if (owner_thread is None or not owner_thread.is_alive()) and handle is not None and not handle.closed:
                    handle.close()
            except (OSError, ValueError):
                pass

    def _decode_truncate(data: bytes) -> str:
        if not data:
            return ""
        return data.decode("utf-8", errors="replace")

    return subprocess.CompletedProcess(
        args=list(argv),
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=_decode_truncate(bytes(stdout_reader.data)),
        stderr=_decode_truncate(bytes(stderr_reader.data)),
    )


def _git_noninteractive_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a noninteractive env for Git subprocesses.

    The base is a sanitized copy of ``os.environ`` (so PATH, HOME, etc. are
    preserved) with the four mandatory keys overridden. ``extra`` is merged
    on top, allowing callers to add or override specific keys.
    """
    base = {key: value for key, value in os.environ.items() if value is not None}
    base.update(_GIT_NONINTERACTIVE_ENV)
    if extra:
        base.update(extra)
    return base


def _git_argv_with_no_pager(args: list[str]) -> list[str]:
    """Return a copy of ``args`` with ``--no-pager`` injected after ``git``.

    The function does not duplicate the flag if it is already present.
    """
    if len(args) >= 2 and args[0] == "git" and args[1] != "--no-pager":
        return ["git", "--no-pager", *args[1:]]
    return list(args)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        for attempt in range(10):
            try:
                os.replace(temporary_name, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.02 * (attempt + 1))
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def write_json(path: Path, value: dict) -> None:
    _atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _validate_user_path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path must be a non-empty string")
    if "\x00" in value:
        raise ValueError("path contains a NUL byte")
    raw = value.strip()
    if raw.startswith(("\\\\?\\", "\\\\.\\", "\\\\")):
        raise ValueError("UNC and Windows device paths are not allowed")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate)
    return candidate.resolve(strict=False)


def _resolve_existing_read_path(value: str) -> Path:
    resolved = _validate_user_path(value)
    if not resolved.exists():
        raise ValueError(f"path not found: {resolved}")
    return resolved.resolve()


def _resolve_write_target(value: str) -> Path:
    candidate = _validate_user_path(value)
    if candidate.exists():
        return candidate.resolve()
    parent = candidate.parent.resolve(strict=False)
    if not parent.exists() or not parent.is_dir():
        raise ValueError(f"parent directory not found: {parent}")
    return parent / candidate.name


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def load_projects() -> dict[str, dict]:
    if not PROJECTS_FILE.is_file():
        return {}
    data = read_json_object(PROJECTS_FILE)
    projects = data.get("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("projects.json: projects must be an object")
    result: dict[str, dict] = {}
    for alias, entry in projects.items():
        if not isinstance(alias, str) or not alias or not isinstance(entry, dict):
            continue
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        try:
            resolved = _resolve_existing_read_path(raw_path)
        except ValueError:
            resolved = _validate_user_path(raw_path)
        result[alias] = {**entry, "path": str(resolved), "exists": resolved.is_dir()}
    return result


def resolve_project(alias: str) -> Path:
    projects = load_projects()
    entry = projects.get(alias)
    if entry is None:
        raise ValueError(f"unknown project alias: {alias}")
    path = Path(entry["path"])
    if not path.is_dir():
        raise ValueError(f"project path not found: {path}")
    return path.resolve()


def list_projects() -> list[dict]:
    return [{"alias": alias, **entry} for alias, entry in sorted(load_projects().items())]


def _task_cwd_roots() -> list[Path]:
    roots: list[Path] = []
    if not JOBS_DIR.is_dir():
        return roots
    for state_path in JOBS_DIR.glob("*/status.json"):
        try:
            state = read_json_object(state_path)
            cwd = state.get("cwd")
            if isinstance(cwd, str):
                root = Path(cwd).resolve(strict=False)
                if root.is_dir():
                    roots.append(root)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return roots


def write_roots() -> list[Path]:
    roots = [CONTROL_DIR.resolve()]
    for entry in load_projects().values():
        path = Path(entry["path"])
        if path.is_dir():
            roots.append(path.resolve())
    roots.extend(_task_cwd_roots())
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def authorize_write(path: Path) -> None:
    if path == CODEX_CONFIG.resolve(strict=False):
        return
    if any(_is_under(path, root) for root in write_roots()):
        return
    allowed = [str(root) for root in write_roots()] + [str(CODEX_CONFIG)]
    raise ValueError("write denied; allowed roots are registered projects, task cwd roots, "
                     f"{CONTROL_DIR}, and {CODEX_CONFIG}. Requested: {path}. Allowed: {allowed}")


def is_probably_text(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return False
    try:
        with path.open("rb") as handle:
            return b"\x00" not in handle.read(BINARY_SNIFF_BYTES)
    except OSError:
        return False


def file_read_result(path: str, start_line: int | None = None, end_line: int | None = None,
                     max_bytes: int = MAX_TEXT_BYTES) -> dict:
    try:
        target = _resolve_existing_read_path(path)
        if not target.is_file():
            return {"ok": False, "error": f"not a file: {target}"}
        if max_bytes < 1 or max_bytes > MAX_TEXT_BYTES:
            return {"ok": False, "error": f"max_bytes must be between 1 and {MAX_TEXT_BYTES}"}
        if not is_probably_text(target):
            return {"ok": False, "error": f"binary file refused: {target}"}
        content = target.read_text(encoding="utf-8", errors="replace")
        if start_line is not None or end_line is not None:
            first = 1 if start_line is None else start_line
            last = len(content.splitlines()) if end_line is None else end_line
            if first < 1 or last < first:
                return {"ok": False, "error": "line range must use positive inclusive line numbers"}
            content = "\n".join(content.splitlines()[first - 1:last])
        encoded = content.encode("utf-8")
        truncated = len(encoded) > max_bytes
        if truncated:
            content = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return {"ok": True, "path": str(target), "content": content, "truncated": truncated}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def file_stat_result(path: str) -> dict:
    try:
        target = _resolve_existing_read_path(path)
        stat = target.stat()
        return {
            "ok": True,
            "path": str(target),
            "type": "directory" if target.is_dir() else "file",
            "size": stat.st_size,
            "modified_time": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        }
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def directory_list_result(path: str, max_entries: int = 200) -> dict:
    try:
        target = _resolve_existing_read_path(path)
        if not target.is_dir():
            return {"ok": False, "error": f"not a directory: {target}"}
        if max_entries < 1 or max_entries > MAX_DIRECTORY_ENTRIES:
            return {"ok": False, "error": f"max_entries must be between 1 and {MAX_DIRECTORY_ENTRIES}"}
        entries = []
        iterator = sorted(target.iterdir(), key=lambda item: item.name.lower())
        for entry in iterator[:max_entries]:
            try:
                stat = entry.stat()
                entries.append({
                    "name": entry.name,
                    "path": str(entry.resolve(strict=False)),
                    "type": "directory" if entry.is_dir() else "file",
                    "size": stat.st_size if entry.is_file() else None,
                    "modified_time": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                })
            except OSError:
                entries.append({"name": entry.name, "path": str(entry), "type": "unreadable"})
        return {"ok": True, "path": str(target), "entries": entries, "truncated": len(iterator) > max_entries}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def _backup_if_requested(path: Path, backup: bool) -> str | None:
    if not backup or path != CODEX_CONFIG.resolve(strict=False) or not path.exists():
        return None
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUPS_DIR / f"codex-config-{datetime.now().strftime('%Y%m%dT%H%M%S%f')}.toml"
    shutil.copy2(path, backup_path)
    return str(backup_path)


def _write_text(path: str, content: str, *, overwrite: bool, backup: bool) -> dict:
    if not isinstance(content, str):
        return {"ok": False, "error": "content must be a string"}
    try:
        target = _resolve_write_target(path)
        authorize_write(target)
        exists = target.exists()
        if exists and not overwrite:
            return {"ok": False, "error": "target exists; set overwrite=true for replacement", "path": str(target)}
        if exists and not target.is_file():
            return {"ok": False, "error": f"not a regular file: {target}"}
        if exists and not is_probably_text(target):
            return {"ok": False, "error": f"binary file refused: {target}"}
        backup_path = _backup_if_requested(target, backup)
        _atomic_write_text(target, content)
        return {"ok": True, "path": str(target), "bytes_written": len(content.encode('utf-8')), "backup_path": backup_path}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def file_write_result(path: str, content: str, overwrite: bool = False, backup: bool = True) -> dict:
    return _write_text(path, content, overwrite=overwrite, backup=backup)


def file_append_result(path: str, content: str, create: bool = False, backup: bool = True) -> dict:
    try:
        target = _resolve_write_target(path)
        authorize_write(target)
        if target.exists():
            if not target.is_file() or not is_probably_text(target):
                return {"ok": False, "error": f"text file required: {target}"}
            existing = target.read_text(encoding="utf-8", errors="replace")
            return _write_text(str(target), existing + content, overwrite=True, backup=backup)
        if not create:
            return {"ok": False, "error": "target does not exist; set create=true to create it", "path": str(target)}
        return _write_text(str(target), content, overwrite=False, backup=backup)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def _run_git(args: list[str], cwd: Path, timeout: int = 30) -> dict:
    """Run a Git command via the safe subprocess helper.

    The command is executed with:
    * ``--no-pager`` injected after ``git``,
    * a noninteractive environment (``GIT_TERMINAL_PROMPT=0``,
      ``GIT_PAGER=cat``, ``PAGER=cat``, ``GCM_INTERACTIVE=Never``),
    * a hard timeout enforced by ``run_safe_subprocess`` which always
      reaps the child before returning, so no orphan ``git.exe`` can leak.

    The function never raises; it always returns a dict.
    """
    argv = _git_argv_with_no_pager(["git", *args])
    env = _git_noninteractive_env()
    try:
        completed = run_safe_subprocess(
            argv,
            cwd=str(cwd),
            env=env,
            timeout=float(timeout),
            max_output_bytes=MAX_GIT_OUTPUT,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "git executable not found on PATH", "argv": argv}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"failed to start git: {exc}", "argv": argv}
    stdout, stderr = completed.stdout or "", completed.stderr or ""
    truncated = len(stdout) >= MAX_GIT_OUTPUT or len(stderr) >= MAX_GIT_OUTPUT
    ok = completed.returncode == 0
    if not ok and not stdout and not stderr:
        # The helper returned -1 only when the child was forcibly terminated.
        if completed.returncode == -1:
            return {
                "ok": False,
                "error": f"git command terminated after exceeding {timeout} seconds",
                "argv": argv,
                "exit_code": completed.returncode,
                "truncated": truncated,
            }
    return {
        "ok": ok,
        "argv": argv,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": completed.returncode,
        "truncated": truncated,
    }


def resolve_repo(repo: str) -> Path:
    target = _resolve_existing_read_path(repo)
    if not target.is_dir():
        raise ValueError(f"not a directory: {target}")
    result = _run_git(["rev-parse", "--show-toplevel"], target)
    if not result["ok"]:
        raise ValueError(result.get("stderr") or result.get("error") or "not a Git repository")
    root = Path(result["stdout"].strip()).resolve()
    if not root.is_dir():
        raise ValueError("git returned an invalid repository root")
    return root


def git_command(repo: str, args: list[str], timeout: int = 30) -> dict:
    try:
        root = resolve_repo(repo)
        result = _run_git(args, root, timeout)
        return {"repo_root": str(root), **result}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def git_status_result(repo: str) -> dict:
    result = git_command(repo, ["status", "--short", "--branch"])
    if result.get("ok"):
        result["branch"] = result["stdout"].splitlines()[0] if result["stdout"] else ""
    return result


def git_diff_result(repo: str, staged: bool = False, paths: list[str] | None = None) -> dict:
    args = ["diff"]
    if staged:
        args.append("--cached")
    if paths:
        args.append("--")
        args.extend(_validate_git_paths(paths))
    return {"staged": staged, **git_command(repo, args, timeout=60)}


def git_branch_result(repo: str) -> dict:
    return git_command(repo, ["branch", "--show-current"])


def git_log_result(repo: str, count: int = 10) -> dict:
    if count < 1 or count > 50:
        return {"ok": False, "error": "count must be between 1 and 50"}
    return git_command(repo, ["log", f"--max-count={count}", "--oneline", "--decorate"])


def git_worktree_list_result(repo: str) -> dict:
    return git_command(repo, ["worktree", "list", "--porcelain"])


def git_rev_parse_result(repo: str) -> dict:
    return git_command(repo, ["rev-parse", "--show-toplevel", "HEAD"])


def _validate_git_paths(paths: list[str]) -> list[str]:
    if not paths:
        raise ValueError("paths must not be empty")
    clean: list[str] = []
    for value in paths:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("each path must be a non-empty string")
        windows = PureWindowsPath(value)
        if value in {".", "./", ".\\"} or value.startswith(":") or windows.is_absolute() or ".." in windows.parts:
            raise ValueError(f"path must name a repo-relative file without pathspec magic or '..': {value}")
        clean.append(value)
    return clean


def git_add_result(repo: str, paths: list[str]) -> dict:
    try:
        clean = _validate_git_paths(paths)
        root = resolve_repo(repo)
        for relative in clean:
            target = (root / relative).resolve(strict=False)
            if not _is_under(target, root):
                raise ValueError(f"path escapes repository: {relative}")
            if not target.is_file():
                raise ValueError(f"git_add accepts existing files only: {relative}")
        result = _run_git(["add", "--", *clean], root)
        return {"repo_root": str(root), **result}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def git_commit_result(repo: str, message: str) -> dict:
    if not isinstance(message, str) or not message.strip() or len(message) > 2000:
        return {"ok": False, "error": "message must be a non-empty string of at most 2000 characters"}
    try:
        root = resolve_repo(repo)
        status = _run_git(["status", "--porcelain=v1"], root)
        if not status["ok"]:
            return {"repo_root": str(root), **status}
        dirty = [line for line in status["stdout"].splitlines() if len(line) >= 2 and (line.startswith("??") or line[1] != " ")]
        if dirty:
            return {"ok": False, "error": "commit refused: worktree has unstaged or untracked changes", "repo_root": str(root), "status": status["stdout"]}
        cached = _run_git(["diff", "--cached", "--quiet"], root)
        if cached.get("exit_code") == 0:
            return {"ok": False, "error": "commit refused: no staged changes", "repo_root": str(root)}
        if cached.get("exit_code") != 1:
            return {"repo_root": str(root), **cached}
        result = _run_git(["commit", "-m", message], root, timeout=60)
        return {"repo_root": str(root), **result}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Constrained host-side Git delivery
# ---------------------------------------------------------------------------
#
# These helpers deliberately do not extend ``git_command``.  They expose a
# small, fixed operation set instead of caller-supplied Git arguments.  The
# remote argument must name an existing remote in the selected repository;
# arbitrary URLs are not accepted as tool input.  This makes the existing Git
# configuration the explicit network authority and avoids adding a generic
# host-side network executor.
# ---------------------------------------------------------------------------

_GIT_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_GIT_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_GIT_DELIVERY_REF_RE = re.compile(
    r"^refs/heads/(?!.*//)(?!.*\.\.)(?!.*(?:^|/)\.{1,2}(?:/|$))[A-Za-z0-9](?:[A-Za-z0-9._/-]*[A-Za-z0-9_-])?$"
)
_GIT_CREDENTIAL_URL_RE = re.compile(r"(?i)(https?://)[^/\s@]+@")
_GIT_CREDENTIAL_VALUE_RE = re.compile(
    r"(?i)\b(authorization|token|password|passwd|pat|api[_-]?key|secret)\b\s*([=:])\s*[^\s,;]+"
)
_GIT_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_GIT_TOKEN_SHAPE_RE = re.compile(
    r"(?i)\b(?:gh[pousr]_[a-z0-9_]{20,}|github_pat_[a-z0-9_]{20,}|glpat-[a-z0-9_-]{20,}|akia[0-9a-z]{16})\b"
)


def _redact_git_delivery_text(value: str) -> str:
    """Remove credential-shaped material from Git delivery diagnostics."""
    if not isinstance(value, str):
        return ""
    value = _GIT_CREDENTIAL_URL_RE.sub(r"\1<redacted>@", value)
    value = _GIT_BEARER_RE.sub("Bearer <redacted>", value)
    value = _GIT_CREDENTIAL_VALUE_RE.sub(r"\1\2<redacted>", value)
    return _GIT_TOKEN_SHAPE_RE.sub("<redacted>", value)


def _sanitize_git_delivery_argv(argv: list[str]) -> list[str]:
    """Return operation argv without exposing configured remote URLs."""
    return ["<configured-https-remote>" if arg.lower().startswith("https://") else arg for arg in argv]


def _git_delivery_failure(
    message: str,
    *,
    operation: str,
    dry_run: bool = False,
    pushed: bool = False,
    **extra: Any,
) -> dict:
    return {
        **extra,
        "ok": False,
        "error": _redact_git_delivery_text(message),
        "operation": operation,
        "dry_run": dry_run,
        "pushed": pushed,
    }


class _GitDeliveryError(ValueError):
    """A validation failure that preserves sanitized subprocess diagnostics."""

    def __init__(self, message: str, result: dict):
        super().__init__(message)
        self.result = result


def _validate_git_delivery_ref(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _GIT_DELIVERY_REF_RE.fullmatch(value):
        raise ValueError(f"{field} must be one explicit branch ref under refs/heads/")
    return value


def _validate_expected_remote_head(value: str) -> str:
    if not isinstance(value, str) or not _GIT_COMMIT_SHA_RE.fullmatch(value):
        raise ValueError("expected_remote_head must be a full 40-hex commit SHA; new branches are not supported")
    return value.lower()


def _validate_git_delivery_remote_name(value: str) -> str:
    if not isinstance(value, str) or not _GIT_REMOTE_NAME_RE.fullmatch(value):
        raise ValueError("remote must be an existing simple configured remote name")
    return value


def _validate_git_delivery_https_url(value: str) -> str:
    """Validate a configured remote URL without accepting URL input from MCP.

    HTTPS is the only accepted transport.  Userinfo, query/fragment data,
    literal IP addresses, localhost-style names, and non-default ports are
    rejected so local/file/SSH/scp-style transports cannot become a host-side
    escape hatch.  We do not DNS-resolve here: Git/GCM must resolve and use
    the already configured HTTPS endpoint naturally.
    """
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(ch) < 32 or ch.isspace() for ch in value):
        raise ValueError("configured remote URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("configured remote URL is invalid") from exc
    host = parsed.hostname
    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port not in (None, 443))
    ):
        raise ValueError("configured remote must use a credential-free HTTPS URL")
    lower_host = host.lower().rstrip(".")
    if (
        lower_host == "localhost"
        or lower_host.endswith(".localhost")
        or lower_host.endswith(".local")
        or re.fullmatch(r"\d+(?:\.\d+){3}", lower_host)
        or ":" in lower_host
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", lower_host)
    ):
        raise ValueError("configured remote host is not an allowed public DNS hostname")
    return value


def _git_delivery_command(args: list[str], root: Path) -> dict:
    """Run a fixed delivery Git command and redact its externally returned data."""
    raw = _run_git(args, root, timeout=GIT_DELIVERY_TIMEOUT_SECONDS)
    result = {
        "ok": bool(raw.get("ok")),
        "argv": _sanitize_git_delivery_argv(list(raw.get("argv", []))),
        "stdout": _redact_git_delivery_text(str(raw.get("stdout", ""))),
        "stderr": _redact_git_delivery_text(str(raw.get("stderr", ""))),
        "exit_code": raw.get("exit_code"),
        "truncated": bool(raw.get("truncated", False)),
    }
    if raw.get("error"):
        result["error"] = _redact_git_delivery_text(str(raw["error"]))
    return result


def _resolve_git_delivery_remote(root: Path, remote: str) -> tuple[str, dict]:
    result = _git_delivery_command(["remote", "get-url", "--push", remote], root)
    if not result["ok"]:
        raise _GitDeliveryError("configured remote could not be resolved", result)
    lines = result["stdout"].splitlines()
    if len(lines) != 1 or not lines[0].strip():
        raise ValueError("configured remote URL is invalid")
    return _validate_git_delivery_https_url(lines[0].strip()), result


def _read_exact_git_delivery_head(root: Path, remote_url: str, dst_ref: str) -> tuple[str | None, dict]:
    result = _git_delivery_command(["ls-remote", "--refs", "--exit-code", remote_url, dst_ref], root)
    if not result["ok"]:
        # Git uses exit status 2 for a clean no-match.  New branch creation is
        # intentionally unsupported in v1, so represent it as a missing head.
        if result.get("exit_code") == 2:
            return None, result
        raise _GitDeliveryError(result.get("error") or "remote ref lookup failed", result)
    lines = [line for line in result["stdout"].splitlines() if line]
    if len(lines) != 1:
        raise ValueError("remote ref lookup did not return exactly one ref")
    try:
        object_id, returned_ref = lines[0].split("\t", 1)
    except ValueError as exc:
        raise ValueError("remote ref lookup returned malformed output") from exc
    if returned_ref != dst_ref or not _GIT_COMMIT_SHA_RE.fullmatch(object_id):
        raise ValueError("remote ref lookup returned malformed output")
    return object_id.lower(), result


def _resolve_git_delivery_source(root: Path, src_ref: str) -> tuple[str, dict]:
    result = _git_delivery_command(["rev-parse", "--verify", f"{src_ref}^{{commit}}"], root)
    if not result["ok"]:
        raise _GitDeliveryError("source branch could not be resolved to a commit", result)
    object_id = result["stdout"].strip()
    if not _GIT_COMMIT_SHA_RE.fullmatch(object_id):
        raise ValueError("source branch did not resolve to a full commit SHA")
    return object_id.lower(), result


def git_ls_remote_result(repo: str, remote: str, ref: str) -> dict:
    """Read one configured remote branch ref from the normal host Git context."""
    operation = "git_ls_remote"
    try:
        root = resolve_repo(repo)
        remote = _validate_git_delivery_remote_name(remote)
        ref = _validate_git_delivery_ref(ref, field="ref")
        remote_url, _ = _resolve_git_delivery_remote(root, remote)
        before_head, command = _read_exact_git_delivery_head(root, remote_url, ref)
        if before_head is None:
            return _git_delivery_failure(
                "remote branch does not exist; new branches are not supported",
                operation=operation,
                repo_root=str(root), remote=remote, ref=ref,
                before_head=None, after_head=None, **command,
            )
        return {
            "ok": True, "operation": operation, "dry_run": False, "pushed": False,
            "repo_root": str(root), "remote": remote, "ref": ref,
            "before_head": before_head, "after_head": before_head, **command,
        }
    except _GitDeliveryError as exc:
        return _git_delivery_failure(str(exc), operation=operation, **exc.result)
    except (OSError, ValueError) as exc:
        return _git_delivery_failure(str(exc), operation=operation)


def _prepare_git_push(repo: str, remote: str, src_ref: str, dst_ref: str, expected_remote_head: str, *, operation: str) -> tuple[Path, str, str, str, str, str, dict] | dict:
    try:
        root = resolve_repo(repo)
        remote = _validate_git_delivery_remote_name(remote)
        src_ref = _validate_git_delivery_ref(src_ref, field="src_ref")
        dst_ref = _validate_git_delivery_ref(dst_ref, field="dst_ref")
        expected_remote_head = _validate_expected_remote_head(expected_remote_head)
        remote_url, _ = _resolve_git_delivery_remote(root, remote)
        before_head, lookup = _read_exact_git_delivery_head(root, remote_url, dst_ref)
        if before_head != expected_remote_head:
            return _git_delivery_failure(
                "remote destination head differs from expected_remote_head (concurrent drift or new branch)",
                operation=operation,
                repo_root=str(root), remote=remote, src_ref=src_ref, dst_ref=dst_ref,
                expected_remote_head=expected_remote_head, before_head=before_head, after_head=before_head,
                **lookup,
            )
        source_head, _ = _resolve_git_delivery_source(root, src_ref)
        return root, remote, remote_url, src_ref, dst_ref, source_head, {
            "expected_remote_head": expected_remote_head,
            "before_head": before_head,
        }
    except _GitDeliveryError as exc:
        return _git_delivery_failure(str(exc), operation=operation, **exc.result)
    except (OSError, ValueError) as exc:
        return _git_delivery_failure(str(exc), operation=operation)


def git_push_dry_run_result(repo: str, remote: str, src_ref: str, dst_ref: str, expected_remote_head: str) -> dict:
    """Safely dry-run exactly one non-force branch update to a configured HTTPS remote."""
    operation = "git_push_dry_run"
    prepared = _prepare_git_push(repo, remote, src_ref, dst_ref, expected_remote_head, operation=operation)
    if isinstance(prepared, dict):
        return prepared
    root, remote, remote_url, src_ref, dst_ref, source_head, metadata = prepared
    command = _git_delivery_command(["push", "--dry-run", remote_url, f"{src_ref}:{dst_ref}"], root)
    if not command["ok"]:
        return _git_delivery_failure(
            command.get("error") or "git push --dry-run failed",
            operation=operation, dry_run=True, repo_root=str(root), remote=remote,
            src_ref=src_ref, dst_ref=dst_ref, source_head=source_head, after_head=metadata["before_head"],
            **metadata, **command,
        )
    return {
        "ok": True, "operation": operation, "dry_run": True, "pushed": False,
        "repo_root": str(root), "remote": remote, "src_ref": src_ref, "dst_ref": dst_ref,
        "source_head": source_head, "after_head": metadata["before_head"], **metadata, **command,
    }


def git_push_ref_result(repo: str, remote: str, src_ref: str, dst_ref: str, expected_remote_head: str) -> dict:
    """Push one fast-forward branch update after two fresh exact-head checks."""
    operation = "git_push_ref"
    prepared = _prepare_git_push(repo, remote, src_ref, dst_ref, expected_remote_head, operation=operation)
    if isinstance(prepared, dict):
        return prepared
    root, remote, remote_url, src_ref, dst_ref, source_head, metadata = prepared
    dry_run = _git_delivery_command(["push", "--dry-run", remote_url, f"{src_ref}:{dst_ref}"], root)
    if not dry_run["ok"]:
        return _git_delivery_failure(
            dry_run.get("error") or "git push --dry-run failed",
            operation=operation, dry_run=True, repo_root=str(root), remote=remote,
            src_ref=src_ref, dst_ref=dst_ref, source_head=source_head, after_head=metadata["before_head"],
            **metadata, **dry_run,
        )
    # Bound review-to-write TOCTOU: re-resolve the configured endpoint and
    # re-read the exact destination immediately before the real non-force push.
    try:
        fresh_remote_url, _ = _resolve_git_delivery_remote(root, remote)
        if fresh_remote_url != remote_url:
            raise ValueError("configured remote URL changed during delivery")
        fresh_head, fresh_lookup = _read_exact_git_delivery_head(root, fresh_remote_url, dst_ref)
        if fresh_head != metadata["expected_remote_head"]:
            return _git_delivery_failure(
                "remote destination head differs from expected_remote_head immediately before push",
                operation=operation, dry_run=True, repo_root=str(root), remote=remote,
                src_ref=src_ref, dst_ref=dst_ref, source_head=source_head,
                expected_remote_head=metadata["expected_remote_head"], before_head=fresh_head, after_head=fresh_head,
                **fresh_lookup,
            )
    except _GitDeliveryError as exc:
        return _git_delivery_failure(str(exc), operation=operation, dry_run=True, **exc.result)
    except (OSError, ValueError) as exc:
        return _git_delivery_failure(str(exc), operation=operation, dry_run=True)
    command = _git_delivery_command(["push", fresh_remote_url, f"{src_ref}:{dst_ref}"], root)
    if not command["ok"]:
        return _git_delivery_failure(
            command.get("error") or "git push failed",
            operation=operation, dry_run=True, repo_root=str(root), remote=remote,
            src_ref=src_ref, dst_ref=dst_ref, source_head=source_head, after_head=fresh_head,
            **metadata, **command,
        )
    try:
        after_head, verification = _read_exact_git_delivery_head(root, fresh_remote_url, dst_ref)
    except _GitDeliveryError as exc:
        return _git_delivery_failure(
            str(exc), operation=operation, dry_run=True, pushed=True, repo_root=str(root), remote=remote,
            src_ref=src_ref, dst_ref=dst_ref, source_head=source_head, after_head=None, **metadata, **exc.result,
        )
    except (OSError, ValueError) as exc:
        return _git_delivery_failure(
            str(exc), operation=operation, dry_run=True, pushed=True, repo_root=str(root), remote=remote,
            src_ref=src_ref, dst_ref=dst_ref, source_head=source_head, after_head=None, **metadata,
        )
    if after_head != source_head:
        return _git_delivery_failure(
            "push completed but post-push remote head verification did not match source_head",
            operation=operation, dry_run=True, pushed=True, repo_root=str(root), remote=remote,
            src_ref=src_ref, dst_ref=dst_ref, source_head=source_head, after_head=after_head,
            **metadata, **verification,
        )
    return {
        "ok": True, "operation": operation, "dry_run": True, "pushed": True,
        "repo_root": str(root), "remote": remote, "src_ref": src_ref, "dst_ref": dst_ref,
        "source_head": source_head, "after_head": after_head, **metadata, **command,
    }


def _load_route_state() -> dict:
    if not ROUTE_STATE_FILE.is_file():
        return {}
    try:
        return read_json_object(ROUTE_STATE_FILE)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def record_official_quota_exhausted(detail: str, unavailable_until: str | None = None) -> None:
    state = _load_route_state()
    state["official"] = {
        "status": "quota_exhausted",
        "recorded_at": utc_now(),
        "unavailable_until": unavailable_until,
        "detail": detail[-1000:],
    }
    write_json(ROUTE_STATE_FILE, state)


def is_official_quota_exhausted(text: str) -> bool:
    """Return true only for explicit official Codex quota/usage exhaustion, not 429/rate limits."""
    lower = (text or "").lower()
    if not lower or "rate limit" in lower or "retry-after" in lower or "retry after" in lower:
        return False
    if re.search(r"\b429\b", lower) and not re.search(r"(?:quota|usage)[^\n]{0,50}(?:exhausted|exceeded|limit|reached)", lower):
        return False
    official = "openai" in lower or "codex" in lower
    exhausted = re.search(r"(?:quota|usage(?:\s+limit)?|plan\s+limit)[^\n]{0,80}(?:exhausted|exceeded|reached|used\s+up)", lower)
    return bool(official and exhausted)


def _get_codex_config_provider(path: Path | None = None) -> str:
    """Return the current provider name in the Codex config, or 'unknown'."""
    target_path = path or CODEX_CONFIG
    try:
        text = target_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "unknown"
    try:
        parsed = tomllib.loads(text)
        mp = parsed.get("model_provider")
        if mp == "custom":
            custom = parsed.get("providers", {}).get("custom", {})
            if custom.get("base_url") == "https://codeflow.asia/v1":
                return "codeflow"
            return "custom"
        elif mp == "openai":
            return "official"
        elif mp:
            return str(mp)
    except Exception:
        pass
    if 'model_provider = "custom"' in text and "https://codeflow.asia/v1" in text:
        return "codeflow"
    return "official"


def is_stale_harbor_managed_codex_config(path: Path | None = None) -> tuple[bool, str | None]:
    """Check if the Codex config is a stale Harbor-managed minimal config or left in an unsafe state."""
    target_path = path or CODEX_CONFIG
    state = _load_route_state()
    if state.get("config_unsafe"):
        restore_err = state.get("config_restore_error")
        err_msg = restore_err.get("error", "unknown error") if isinstance(restore_err, dict) else str(restore_err)
        return True, f"Codex config is marked unsafe due to restore failure: {err_msg}"

    try:
        if target_path.is_file():
            text = target_path.read_text(encoding="utf-8", errors="replace")
            if "managed by MCP control plane fallback machinery" in text:
                return True, "Codex config contains legacy temporary management header but no active route lock/snapshot context is active"
    except OSError:
        pass
    return False, None


def codex_route_status() -> dict:
    provider = _get_codex_config_provider()
    stale, stale_reason = is_stale_harbor_managed_codex_config()
    blocker = None
    has_codeflow_key = bool(os.environ.get("CODEFLOW_API_KEY"))

    if stale:
        blocker = f"stale_harbor_managed_codex_config: {stale_reason}"
        fallback_available = False
    elif not has_codeflow_key:
        blocker = "missing_codeflow_api_key: CODEFLOW_API_KEY environment variable is not set"
        fallback_available = False
    else:
        fallback_available = True

    custom = _custom_codex_route_definition(include_secret=False)
    return {
        "current_provider": provider,
        "process_local_override_supported": True,
        "automatic_fallback_available": fallback_available,
        "codeflow_credential_configured": has_codeflow_key,
        "blocker": blocker,
        "state": _load_route_state().get("official"),
        "custom_route_enabled": custom.get("enabled", False),
        "custom_route_configured": custom.get("ok", False),
        "custom_route_blocker": custom.get("blocker"),
    }


def _custom_codex_route_definition(*, include_secret: bool = False) -> dict[str, Any]:
    """Resolve persisted generic Codex custom-route settings.

    The returned mapping is safe to record unless ``include_secret`` is true;
    callers must keep the latter strictly local to child-process creation.
    """
    try:
        settings = load_user_settings()
        custom = settings.codex.custom
    except Exception:
        return {"ok": False, "enabled": False, "blocker": "custom_route_settings_unavailable"}
    if not custom.enabled:
        return {"ok": False, "enabled": False, "blocker": "custom_route_disabled"}
    profile_name = (custom.profile_name or "").strip()
    base_url = (custom.base_url or "").strip()
    credential_ref = (custom.credential_ref or "").strip()
    if not profile_name:
        return {"ok": False, "enabled": True, "blocker": "custom_route_missing_profile_name"}
    if not re.match(r"^https?://[^\s]+$", base_url, re.IGNORECASE):
        return {"ok": False, "enabled": True, "blocker": "custom_route_invalid_base_url"}
    if not credential_ref:
        return {"ok": False, "enabled": True, "blocker": "custom_route_missing_credential_ref"}
    result: dict[str, Any] = {
        "ok": True,
        "enabled": True,
        "provider_id": CUSTOM_CODEX_PROVIDER_ID,
        "provider_name": profile_name,
        "base_url": base_url,
        "wire_api": "responses",
        "env_key": CUSTOM_CODEX_ENV_KEY,
        "default_model": (custom.default_model or "").strip(),
        "credential_ref": credential_ref,
    }
    if include_secret:
        try:
            secret = (os.environ.get(CUSTOM_CODEX_ENV_KEY) if os.environ.get("HARBOR_RUNTIME_MODE") == "packaged"
                      else CredentialStore().read(credential_ref))
        except Exception:
            return {"ok": False, "enabled": True, "blocker": "custom_route_credential_unavailable"}
        if not isinstance(secret, str) or not secret.strip():
            return {"ok": False, "enabled": True, "blocker": "custom_route_missing_credential"}
        result["api_key"] = secret
    return result


def resolve_custom_codex_route(*, include_secret: bool = False) -> dict[str, Any]:
    """Public testable wrapper for custom route resolution."""
    return _custom_codex_route_definition(include_secret=include_secret)


def codex_child_environment(route: str) -> dict[str, str] | None:
    """Return a child-only environment for a Codex route.

    Legacy routes inherit the existing environment unchanged.  Custom routes
    receive a copied environment with the vault secret injected under the
    deterministic provider env key; the parent environment is never mutated.
    """
    if route != "custom":
        return None
    resolved = _custom_codex_route_definition(include_secret=True)
    if not resolved.get("ok"):
        raise ValueError(str(resolved.get("blocker", "custom_route_invalid")))
    env = {k: v for k, v in os.environ.items() if v is not None}
    env[CUSTOM_CODEX_ENV_KEY] = resolved["api_key"]
    return env



def _minimax_cli_candidates() -> list[Path]:
    candidates = [MINIMAX_CLI_EXE]
    if os.environ.get("HARBOR_RUNTIME_MODE") == "packaged" and os.environ.get("HARBOR_MINIMAX_CLI_EXE"):
        return candidates
    for name in ("mcode.cmd", "mcode"):
        discovered = shutil.which(name)
        if discovered:
            candidates.append(Path(discovered))
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved not in unique:
            unique.append(resolved)
    return unique


def _run_minimax_probe(command: list[str]) -> subprocess.CompletedProcess:
    """Run a MiniMax CLI probe command via the safe subprocess helper.

    Probes are short-lived (``--version``, ``--help``, ``exec --help``) but
    they MUST go through the safe helper to keep the MCP transport isolated
    from any console / credential prompt the CLI might attempt to read.
    """
    if not isinstance(command, list) or not command:
        raise ValueError("command must be a non-empty list")
    # Cap the timeout so a stuck probe cannot pin the calling thread.
    return run_safe_subprocess(
        command,
        cwd=None,
        env=None,
        timeout=5.0,
        max_output_bytes=MAX_SUBPROCESS_OUTPUT_BYTES,
    )


def _minimax_direct_probe_command(executable: Path, args: list[str]) -> list[str] | None:
    if executable.suffix.lower() not in {".cmd", ".bat"}:
        return None
    try:
        launcher = executable.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    node_match = re.search(r'"([^"]*node\.exe)"', launcher, re.IGNORECASE)
    node = Path(node_match.group(1)) if node_match else None
    package_root = executable.parent / "node_modules" / "@minimax-ai" / "code"
    try:
        cli_source = (package_root / "cli.js").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    main_match = re.search(r'import\("\./chunks/(main-[^"/]+\.js)"\)', cli_source)
    main_chunk = package_root / "chunks" / main_match.group(1) if main_match else None
    if node is None or not node.is_file() or main_chunk is None or not main_chunk.is_file():
        return None
    main_uri = main_chunk.resolve().as_uri()
    script = (
        "process.argv.splice(1,0,'mcode'); "
        f"const {{ runTuiCli }} = await import({json.dumps(main_uri)}); "
        "await runTuiCli();"
    )
    return [str(node), "--input-type=module", "--eval", script, "--", *args]


def _probe_minimax_cli(executable: Path, args: list[str]) -> subprocess.CompletedProcess:
    result = _run_minimax_probe([str(executable), *args])
    diagnostic = _probe_text(result)
    if result.returncode == 0 or "EPERM" not in diagnostic or ".mcode-active" not in diagnostic:
        return result
    fallback = _minimax_direct_probe_command(executable, args)
    return _run_minimax_probe(fallback) if fallback else result


def _probe_text(result: subprocess.CompletedProcess) -> str:
    return "\n".join(
        value for value in (result.stdout, result.stderr) if isinstance(value, str)
    )


def _minimax_cli_status() -> dict:
    executable = next((candidate for candidate in _minimax_cli_candidates() if candidate.is_file()), None)
    reported_executable = str(executable or MINIMAX_CLI_EXE)
    base = {
        "name": "minimax",
        "available": False,
        "executable": reported_executable,
        "executable_exists": executable is not None,
        "config_path": str(MINIMAX_CONFIG),
        "config_exists": MINIMAX_CONFIG.is_file(),
        "supports_async": False,
        "version": None,
        "capabilities": {},
        "noninteractive_command": None,
        "session_behavior": "mcode exec reads the exact UTF-8 prompt from stdin for one headless task through the existing local job queue.",
        "output_behavior": "mcode exec --output-format json emits a machine-readable ExecResult; --output-last-message writes the final answer.",
    }
    if executable is None:
        base["blocker"] = f"MiniMax CLI not found at {MINIMAX_CLI_EXE} or on PATH. GUI automation is intentionally not used."
        return base

    probes: dict[str, subprocess.CompletedProcess] = {}
    probe_errors: list[str] = []
    for label, args in (("version", ["--version"]), ("help", ["--help"]), ("exec_help", ["exec", "--help"])):
        try:
            result = _probe_minimax_cli(executable, args)
            probes[label] = result
            if result.returncode != 0:
                detail = _probe_text(result).strip()[-500:]
                probe_errors.append(f"{label} exited {result.returncode}{': ' + detail if detail else ''}")
        except (OSError, subprocess.SubprocessError) as exc:
            probe_errors.append(f"{label} probe failed: {type(exc).__name__}: {exc}")

    version_text = _probe_text(probes["version"]).strip() if "version" in probes else ""
    version_match = re.search(r"\b\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\b", version_text)
    version = version_match.group(0) if version_match else (version_text or None)
    exec_help = _probe_text(probes["exec_help"]) if "exec_help" in probes else ""
    capabilities = {
        "exec": "Usage: mcode exec" in exec_help or "exec [options]" in exec_help,
        "cwd": "--cwd" in exec_help,
        "model": "--model" in exec_help,
        "permission": "--permission" in exec_help,
        "output_format": "--output-format" in exec_help,
        "output_format_json": bool(re.search(r"--output-format[\s\S]{0,160}\bjson\b", exec_help)),
        "output_format_stream_json": bool(re.search(r"--output-format[\s\S]{0,160}\bstream-json\b", exec_help)),
        "output_last_message": "--output-last-message" in exec_help,
        "stdin_input": "--input" in exec_help,
        "reasoning_effort": False,
        "sandbox": False,
    }
    required = ("exec", "cwd", "output_format", "output_format_json", "output_last_message", "stdin_input")
    missing = [name for name in required if not capabilities[name]]
    base.update(version=version, capabilities=capabilities)
    if probe_errors or missing:
        details = probe_errors + ([f"missing verified exec capabilities: {', '.join(missing)}"] if missing else [])
        base["blocker"] = "MiniMax CLI capability probes did not pass: " + "; ".join(details)
        return base

    base.update(
        available=True,
        supports_async=True,
        noninteractive_command=[str(executable), "exec"],
        parameter_mappings={
            "prompt": "UTF-8 stdin via --input -",
            "model": "--model <provider/model>",
            "sandbox": None,
            "reasoning_effort": None,
        },
        blocker=None,
    )
    return base


_AGY_PROBE_LOCK = threading.Lock()
_AGY_PROBE_CACHE: tuple[tuple[Any, ...], float, dict] | None = None


def _agy_probe_context() -> tuple[Path, str, dict[str, str], tuple[Any, ...]]:
    """Capture the executable and process context shared by all agy probes.

    Passing an explicit environment and cwd makes the three subprocesses use
    one identity/profile/proxy snapshot even when the caller is running in an
    executor thread.  The fingerprint invalidates the short-lived cache when
    any inherited environment value or executable identity changes without
    retaining or exposing environment values in the cache key.
    """
    executable = Path(AGY_EXE)
    cwd = os.getcwd()
    environment = {key: value for key, value in os.environ.items() if value is not None}
    environment_fingerprint = hashlib.sha256(
        json.dumps(sorted(environment.items()), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    try:
        stat = executable.stat()
        executable_identity: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        executable_identity = None
    cache_key = (str(executable), executable_identity, cwd, environment_fingerprint)
    return executable, cwd, environment, cache_key


def _run_agy_probe(
    command: list[str],
    *,
    cwd: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess:
    """Run an agy CLI probe command via the safe subprocess helper.

    Probes are short-lived (``--version``, ``--help``, ``models``) but they
    MUST go through the safe helper to keep the MCP transport isolated from
    any console / credential prompt the CLI might attempt to read.
    """
    if not isinstance(command, list) or not command:
        raise ValueError("command must be a non-empty list")
    return run_safe_subprocess(
        command,
        cwd=cwd,
        env=env,
        timeout=15.0,
        max_output_bytes=MAX_SUBPROCESS_OUTPUT_BYTES,
    )


def _parse_agy_model_enumeration(result: subprocess.CompletedProcess) -> tuple[list[str], bool]:
    """Extract a strict model enumeration from an ``agy models`` result.

    Model IDs are parsed exclusively from stdout; stderr is reserved for
    diagnostics and harmless progress/info banners and must never be parsed
    as model IDs. Known and generic info/banner lines in stdout (such as
    progress banners, table headings, and info tags) are ignored and do
    not contaminate the model enumeration or invalidate structure.
    """
    stdout_text = result.stdout if isinstance(result.stdout, str) else ""
    if not stdout_text.strip():
        return [], False

    models: list[str] = []
    structurally_valid = True

    try:
        decoded = json.loads(stdout_text)
    except (TypeError, ValueError):
        pass
    else:
        if isinstance(decoded, dict):
            decoded = decoded.get("models")
        if isinstance(decoded, list) and decoded:
            for item in decoded:
                model_id = item.get("id") if isinstance(item, dict) else item
                if not isinstance(model_id, str) or not AGY_MODEL_ID_RE.fullmatch(model_id):
                    structurally_valid = False
                    continue
                if model_id not in models:
                    models.append(model_id)
            return models, bool(models and structurally_valid)
        return [], False

    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if not line or AGY_INFO_OR_BANNER_RE.fullmatch(line):
            continue
        # The documented text format may be a tabular or a
        # whitespace-separated ``id description`` row.
        model_id = line.split(None, 1)[0]
        if not AGY_MODEL_ID_RE.fullmatch(model_id):
            structurally_valid = False
            continue
        if model_id not in models:
            models.append(model_id)

    return models, bool(models and structurally_valid)


def _is_valid_agy_model_catalogue(
    result: subprocess.CompletedProcess,
    *,
    models: list[str],
    structurally_valid: bool,
) -> bool:
    """Verify that ``agy models`` emitted a valid, unblocked Gemini-ready catalogue.

    Harbor AGY executes Gemini models only.  The capability probe therefore
    requires:
      1. A non-empty, structurally valid model enumeration.
      2. At least one ``gemini-*`` model present in the catalogue.
      3. No authentication, login, network, or explicit error diagnostics.

    When these semantic conditions hold, the capability is accepted even if
    the CLI emitted an abnormal non-zero exit code.  Genuine login, auth,
    network, or execution blockers remain strictly fail-closed.
    """
    output = _probe_text(result)
    return (
        bool(models)
        and structurally_valid
        and any(model_id.startswith("gemini-") for model_id in models)
        and not bool(AGY_MODELS_BLOCKER_RE.search(output))
    )


def _probe_agy_capabilities(
    executable: Path,
    *,
    cwd: str,
    env: dict[str, str],
) -> dict:
    """Return the canonical agy harness status record.

    The shape mirrors ``_minimax_cli_status`` so the listing/registry
    surface is consistent. Agy is treated as a peer lifecycle-supervised
    harness: a single ``agy`` invocation via ``--print=<prompt>`` is supervised by the
    worker, not by the MCP transport, so ``supports_async`` is True
    once the CLI is verified.
    """
    reported_executable = str(executable)
    base = {
        "name": "agy",
        "available": False,
        "executable": reported_executable,
        "executable_exists": executable.is_file(),
        "config_path": None,
        "config_exists": False,
        "supports_async": False,
        "supports_reasoning_effort": True,
        "version": None,
        "capabilities": {},
        "models": [],
        "workspace_capability": {
            "verified": False,
            "status": "unverified",
            "reason": (
                "CLI/help/models probes do not verify workspace tool permissions in "
                "headless execution."
            ),
        },
        "noninteractive_command": None,
        "session_behavior": (
            "agy runs one headless single-shot turn via --print=<prompt>; the worker "
            "supervises the lifecycle and reads the final result from "
            "captured stdout."
        ),
        "output_behavior": (
            "agy --output-format json emits a single JSON object on stdout "
            "at the end of the turn; the worker writes result.txt from it."
        ),
        "route": None,
    }
    if not executable.is_file():
        base["blocker"] = (
            f"Antigravity CLI not found at {executable}. Install Antigravity "
            "or update AGY_EXE to the correct path."
        )
        return base

    probes: dict[str, subprocess.CompletedProcess] = {}
    probe_errors: list[str] = []
    for label, args in (
        ("version", ["--version"]),
        ("help", ["--help"]),
        ("models", ["models"]),
    ):
        try:
            result = _run_agy_probe(
                [str(executable), *args],
                cwd=cwd,
                env=env,
            )
            probes[label] = result
            # ``agy models`` is assessed below after its output has been
            # structurally validated.  Version/help remain conventional
            # fail-closed capability probes.
            if label != "models" and result.returncode != 0:
                detail = _probe_text(result).strip()[-500:]
                probe_errors.append(
                    f"{label} exited {result.returncode}{': ' + detail if detail else ''}"
                )
        except (OSError, subprocess.SubprocessError) as exc:
            probe_errors.append(f"{label} probe failed: {type(exc).__name__}: {exc}")

    version_text = _probe_text(probes["version"]).strip() if "version" in probes else ""
    version_match = re.search(r"\b\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\b", version_text)
    version = version_match.group(0) if version_match else (version_text or None)

    help_text = _probe_text(probes["help"]) if "help" in probes else ""
    models_result = probes.get("models")
    models, models_structurally_valid = (
        _parse_agy_model_enumeration(models_result)
        if models_result is not None
        else ([], False)
    )
    if models_result is None:
        probe_errors.append("models probe returned no result")
    elif _is_valid_agy_model_catalogue(
        models_result,
        models=models,
        structurally_valid=models_structurally_valid,
    ):
        pass
    else:
        detail = _probe_text(models_result).strip()[-500:]
        if models_result.returncode != 0:
            probe_errors.append(
                f"models exited {models_result.returncode}{': ' + detail if detail else ''}"
            )
        else:
            probe_errors.append(
                f"models returned incomplete or blocked output{': ' + detail if detail else ''}"
            )
        models = []
    capabilities = {
        "print": bool(re.search(r"(?:^|\s)(?:-p|--print|--prompt)(?:\s|\b)", help_text)),
        "print_interactive": bool(re.search(r"(?:^|\s)(?:-i|--prompt-interactive)(?:\s|\b)", help_text)),
        "dangerously_skip_permissions": "--dangerously-skip-permissions" in help_text,
        "output_format": "--output-format" in help_text,
        "output_format_json": bool(re.search(r"--output-format[\s\S]{0,160}\bjson\b", help_text)),
        "output_format_stream_json": bool(re.search(r"--output-format[\s\S]{0,160}\bstream-json\b", help_text)),
        "model": "--model" in help_text,
        "effort": "--effort" in help_text,
        "effort_low_medium_high": bool(
            re.search(r"--effort.{0,120}low.{0,3}\|.{0,3}medium.{0,3}\|.{0,3}high", help_text)
        ),
        "print_timeout": "--print-timeout" in help_text,
        "sandbox": "--sandbox" in help_text,
    }
    base.update(version=version, capabilities=capabilities, models=models)

    if probe_errors:
        base["blocker"] = "Antigravity CLI capability probes did not pass: " + "; ".join(probe_errors)
        return base

    required = ("print", "dangerously_skip_permissions", "output_format", "model", "effort", "print_timeout")
    missing = [name for name in required if not capabilities[name]]
    if missing:
        base["blocker"] = "Antigravity CLI capability probes did not pass: missing verified exec capabilities: " + ", ".join(missing)
        return base

    base.update(
        available=True,
        supports_async=True,
        noninteractive_command=[
            str(executable),
            "--dangerously-skip-permissions",
            "--output-format",
            "json",
            "--print-timeout",
            AGY_DEFAULT_PRINT_TIMEOUT,
            "--print=<prompt>",
        ],
        parameter_mappings={
            "model": "--model <id>",
            "sandbox": "sandbox is a single boolean in agy; read-only is rejected (workspace-write is the only verified mapping).",
            "reasoning_effort": "--effort <low|medium|high>",
        },
        blocker=None,
    )
    return base


def _agy_cli_status() -> dict:
    """Return the canonical, context-bound agy capability probe result.

    Status and task preflight intentionally share this short-lived snapshot.
    This prevents a second immediate ``agy models`` invocation from disagreeing
    with the capability result that was just reported, while context changes
    and expired snapshots still force a fresh, fail-closed probe.
    """
    global _AGY_PROBE_CACHE
    executable, cwd, environment, cache_key = _agy_probe_context()
    with _AGY_PROBE_LOCK:
        now = time.monotonic()
        if _AGY_PROBE_CACHE is not None:
            cached_key, cached_at, cached_status = _AGY_PROBE_CACHE
            if (
                cached_key == cache_key
                and now - cached_at <= AGY_PROBE_CACHE_TTL_SECONDS
            ):
                return copy.deepcopy(cached_status)
        status = _probe_agy_capabilities(executable, cwd=cwd, env=environment)
        _AGY_PROBE_CACHE = (cache_key, time.monotonic(), copy.deepcopy(status))
        return copy.deepcopy(status)


def harnesses() -> list[dict]:
    return [
        {
            "name": "codex",
            "available": CODEX_EXE.is_file(),
            "executable": str(CODEX_EXE),
            "supports_async": True,
            "supports_reasoning_effort": True,
            "route": codex_route_status(),
        },
        _minimax_cli_status(),
        _agy_cli_status(),
    ]


def harness_status(name: str) -> dict:
    if name == "codex":
        return {
            "ok": True,
            "name": "codex",
            "available": CODEX_EXE.is_file(),
            "executable": str(CODEX_EXE),
            "supports_async": True,
            "supports_reasoning_effort": True,
            "route": codex_route_status(),
        }
    if name == "minimax":
        return {"ok": True, **_minimax_cli_status()}
    if name == "agy":
        return {"ok": True, **_agy_cli_status()}
    return {"ok": False, "error": f"unknown harness: {name}"}


def queue_diagnostics(jobs_dir: Path | None = None) -> dict[str, str]:
    """Return secret-free queue identity diagnostics."""
    path = Path(jobs_dir or JOBS_DIR)
    source = (
        QUEUE_ROOT.source
        if queue_root_fingerprint(path) == QUEUE_ROOT.fingerprint
        else "runtime_override"
    )
    return describe_queue_root(path, source).as_dict()


def _normalise_codex_rate_limits(payload: object) -> dict:
    """Map the documented app-server response to telemetry windows.

    Only backend-provided percentages are emitted.  Invalid or incomplete
    entries are omitted rather than guessed, and the raw response is never
    retained because it is not a telemetry contract.
    """
    result = payload if isinstance(payload, dict) else {}
    rate_limits = result.get("rateLimits") if isinstance(result.get("rateLimits"), dict) else {}
    selected_bucket: str | None = None
    by_limit = result.get("rateLimitsByLimitId") if isinstance(result.get("rateLimitsByLimitId"), dict) else {}
    if not rate_limits and by_limit:
        for bucket, item in by_limit.items():
            if isinstance(item, dict):
                rate_limits = item
                selected_bucket = str(bucket)
                break
    windows: list[dict] = []

    def label(minutes: object, fallback: str) -> str:
        if not isinstance(minutes, (int, float)) or minutes <= 0:
            return fallback
        total = int(minutes)
        if total % 10080 == 0:
            return "Weekly" if total == 10080 else f"{total // 10080}w"
        if total % 1440 == 0:
            return f"{total // 1440}d"
        if total % 60 == 0:
            return f"{total // 60}h"
        return f"{total}m"

    def add(window: object, bucket: object, fallback: str) -> None:
        if not isinstance(window, dict) or not isinstance(window.get("usedPercent"), (int, float)):
            return
        reset = window.get("resetsAt")
        windows.append({
            "label": label(window.get("windowDurationMins"), fallback),
            "bucket": str(bucket or "default"),
            "used_percent": window["usedPercent"],
            "window_minutes": window.get("windowDurationMins"),
            "resets_at": reset if isinstance(reset, (int, float)) else None,
        })

    if isinstance(rate_limits, dict):
        bucket = rate_limits.get("limitId") or selected_bucket
        add(rate_limits.get("primary"), bucket, "Primary")
        add(rate_limits.get("secondary"), bucket, "Secondary")
    for bucket, limit in by_limit.items():
        if not isinstance(limit, dict):
            continue
        if selected_bucket is not None and str(bucket) == selected_bucket:
            continue
        add(limit.get("primary"), bucket, "Primary")
        add(limit.get("secondary"), bucket, "Secondary")
    if not windows:
        return {
            "state": "unavailable",
            "source": "Codex app-server account/rateLimits/read",
            "error": "Codex app-server returned no quota windows",
        }
    return {
        "state": "available",
        "source": "Codex app-server account/rateLimits/read",
        "windows": windows,
        "quota_scope": "codex_account",
    }


def _codex_rate_limits_snapshot() -> dict:
    """Use Harbor's sole authoritative Codex app-server status path.

    The request is short-lived, read-only JSON-RPC over stdio.  It shares the
    configured Codex executable with the harness registry and intentionally
    exposes fixed, secret-free failure messages only.
    """
    if not CODEX_EXE.is_file():
        return {"state": "unavailable", "source": "Codex app-server account/rateLimits/read", "error": "Codex CLI is not installed"}
    requests = (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "harbor-telemetry", "version": "1"}, "capabilities": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}},
    )
    payload = "\n".join(json.dumps(item) for item in requests) + "\n"
    try:
        completed = run_safe_subprocess(
            [str(CODEX_EXE), "app-server", "--listen", "stdio://"],
            input=payload.encode("utf-8"),
            timeout=6,
        )
    except (OSError, subprocess.SubprocessError):
        return {"state": "unavailable", "source": "Codex app-server account/rateLimits/read", "error": "Codex app-server could not be started"}
    for line in (completed.stdout or "").splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict) and message.get("id") == 2 and isinstance(message.get("result"), dict):
            return _normalise_codex_rate_limits(message["result"])
    return {"state": "unavailable", "source": "Codex app-server account/rateLimits/read", "error": "Codex app-server returned no quota response"}


def _harness_job_activity(jobs_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    jobs_dir = jobs_dir or JOBS_DIR
    counts = {name: {"running": 0, "queued": 0, "running_job_ids": [], "source": "Harbor job queue"} for name in ("codex", "minimax", "agy")}
    if not jobs_dir.is_dir():
        return counts
    for path in jobs_dir.glob("*/status.json"):
        try:
            state = read_json_object(path)
        except (OSError, ValueError, json.JSONDecodeError):
            for row in counts.values():
                row["error"] = "Some job states could not be read"
            continue
        name = state.get("harness")
        status = state.get("status")
        if not queue_root_matches(state, jobs_dir):
            for row in counts.values():
                row["error"] = "Job queue identity mismatch"
            continue
        if name in counts:
            timestamp = state.get("updated_at") or state.get("created_at")
            if isinstance(timestamp, str):
                try:
                    timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    if timestamp.tzinfo is not None:
                        normalized = timestamp.astimezone(timezone.utc).isoformat()
                        counts[name]["latest_activity_at"] = max(normalized, counts[name].get("latest_activity_at", ""))
                except ValueError:
                    pass
        if name in counts and status in {"queued", "running"}:
            counts[name][status] += 1
            if status == "running":
                counts[name]["running_job_ids"].append(path.parent.name)
    for row in counts.values():
        row["running_job_ids"].sort()
    return counts

_HARNESS_TELEMETRY_PROVIDER: HarnessTelemetryProvider | None = None


def harness_telemetry_snapshot(*, force_refresh: bool = False) -> dict:
    """Return the versioned unified read-only harness telemetry snapshot."""
    global _HARNESS_TELEMETRY_PROVIDER
    if _HARNESS_TELEMETRY_PROVIDER is None:
        def _invalidate_agy_cache() -> None:
            """Reset the short-lived AGY capability probe cache.

            Telemetry and canonical AGY status must derive from the same
            *current* canonical probe/context.  ``agy_status()`` /
            ``harness_status("agy")`` and the telemetry provider both read
            ``_AGY_PROBE_CACHE``; the cache key only covers
            ``(executable, executable identity, cwd, env fingerprint)``,
            so a real AGY capability flip driven by auth, credentials, or
            network state can leave a stale failing result in the cache
            that the canonical path has since moved past.  Forcing a
            fresh probe from the telemetry snapshot eliminates that
            asymmetry without altering what canonical observes.
            """
            global _AGY_PROBE_CACHE
            _AGY_PROBE_CACHE = None
        _HARNESS_TELEMETRY_PROVIDER = HarnessTelemetryProvider(
            status_provider=harness_status,
            codex_quota_provider=_codex_rate_limits_snapshot,
            job_activity_provider=_harness_job_activity,
            process_adapter=default_process_activity_adapter(run_safe_subprocess),
            agy_cache_invalidator=_invalidate_agy_cache,
        )
    return _HARNESS_TELEMETRY_PROVIDER.snapshot(force_refresh=force_refresh)


def resolve_task_cwd(project: str | None, cwd: str | None) -> tuple[Path, str | None]:
    if bool(project) == bool(cwd):
        raise ValueError("provide exactly one of project or cwd")
    if project:
        return resolve_project(project), project
    workdir = _resolve_existing_read_path(cwd or "")
    if not workdir.is_dir():
        raise ValueError(f"working directory not found: {workdir}")
    return workdir, None


def start_task(*, harness: Literal["codex", "minimax", "agy"], prompt: str, project: str | None,
               cwd: str | None, model: str | None, sandbox: str, reasoning_effort: str | None,
               route: str = "current", codex_role: str = "worker") -> dict:
    if harness not in {"codex", "minimax", "agy"}:
        return {"ok": False, "error": f"unsupported harness: {harness}"}
    if not isinstance(prompt, str) or not prompt.strip():
        return {"ok": False, "error": "prompt must be a non-empty string"}
    if sandbox not in SANDBOXES:
        return {"ok": False, "error": f"unsupported sandbox: {sandbox}"}
    if route not in {"current", "official", "codeflow", "official_then_codeflow", "custom", "official_then_custom"}:
        return {"ok": False, "error": f"unsupported route: {route}"}
    try:
        workdir, project_alias = resolve_task_cwd(project, cwd)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if harness == "minimax":
        if route != "current":
            return {"ok": False, "error": "MiniMax tasks only support route=current; Codex route switching is not applied."}
        if reasoning_effort is not None:
            return {"ok": False, "error": "MiniMax CLI does not expose a verified reasoning_effort flag."}
        if sandbox != "workspace-write":
            return {"ok": False, "error": "MiniMax CLI does not expose a verified read-only sandbox mapping; use sandbox=workspace-write."}
        minimax_status = harness_status("minimax")
        if not minimax_status["available"]:
            return {"ok": False, "error": minimax_status["blocker"]}
        minimax_sandbox_requested = sandbox
        minimax_sandbox_effective = "unenforced"
        minimax_sandbox_enforced = False
    if harness == "agy":
        if route != "current":
            return {"ok": False, "error": "Antigravity tasks only support route=current; Codex route switching is not applied."}
        if sandbox != "workspace-write":
            return {"ok": False, "error": "Antigravity CLI does not expose a verified read-only sandbox mapping; use sandbox=workspace-write."}
        if reasoning_effort is not None and reasoning_effort not in AGY_EFFORTS:
            return {"ok": False, "error": f"Antigravity reasoning_effort must be one of {sorted(AGY_EFFORTS)} or unset."}
        agy_status = harness_status("agy")
        if not agy_status["available"]:
            return {"ok": False, "error": agy_status["blocker"]}
        if not isinstance(model, str) or not model.startswith("gemini-"):
            return {
                "ok": False,
                "error": (
                    f"Antigravity harness is Gemini-only: model must start with 'gemini-'; "
                    f"rejected non-Gemini model {model!r}. Non-Gemini models (Claude, GPT-OSS, etc.) "
                    f"cannot be executed via AGY."
                ),
            }
    if harness == "codex":
        if not CODEX_EXE.is_file():
            return {"ok": False, "error": f"Codex executable not found: {CODEX_EXE}"}
        stale, stale_reason = is_stale_harbor_managed_codex_config()
        if stale:
            return {
                "ok": False,
                "error": f"stale_harbor_managed_codex_config: {stale_reason}",
                "blocker": "stale_harbor_managed_codex_config",
            }
        if route in ("codeflow", "official_then_codeflow") and not os.environ.get("CODEFLOW_API_KEY"):
            return {
                "ok": False,
                "error": "missing_codeflow_api_key: CODEFLOW_API_KEY environment variable is not set for Code Flow route/fallback.",
                "blocker": "missing_codeflow_api_key",
                "route": route,
                "route_status": codex_route_status(),
            }
        if route in ("custom", "official_then_custom"):
            custom_route = _custom_codex_route_definition(include_secret=True)
            if not custom_route.get("ok"):
                blocker = custom_route.get("blocker", "custom_route_invalid")
                return {
                    "ok": False,
                    "error": f"{blocker}: custom Codex route is not configured",
                    "blocker": blocker,
                    "route": route,
                    "route_status": codex_route_status(),
                }
    job_id = uuid.uuid4().hex
    job_dir = JOBS_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    state = {
        "job_id": job_id,
        "harness": harness,
        "status": "queued",
        "prompt": prompt,
        "cwd": str(workdir),
        "project": project_alias,
        "model": model,
        "sandbox": sandbox,
        "reasoning_effort": reasoning_effort,
        "route_requested": route,
        "route_used": ("codeflow" if route == "codeflow" else
                        "custom" if route == "custom" else
                        "official" if route in ("official", "official_then_codeflow", "official_then_custom") else "current"),
        "native_process": None,
        "minimax_executable": minimax_status["executable"] if harness == "minimax" else None,
        "agy_executable": agy_status["executable"] if harness == "agy" else None,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "queue_root_fingerprint": queue_root_fingerprint(JOBS_DIR),
        "codex_role": ("primary" if codex_role == "primary" else "worker") if harness == "codex" else None,
    }
    if harness == "minimax":
        state["sandbox_requested"] = minimax_sandbox_requested
        state["sandbox_effective"] = minimax_sandbox_effective
        state["sandbox_enforced"] = minimax_sandbox_enforced
    write_json(job_dir / "status.json", state)
    response = {"ok": True, "job_id": job_id, "status": "queued", "harness": harness, "cwd": str(workdir), "project": project_alias}
    if harness == "minimax":
        response["parameter_handling"] = {
            "prompt": "UTF-8 stdin via --input -",
            "model": "mapped to --model" if model else "not requested",
            "sandbox": "unenforced",
            "sandbox_requested": minimax_sandbox_requested,
            "sandbox_effective": minimax_sandbox_effective,
            "sandbox_enforced": minimax_sandbox_enforced,
            "reasoning_effort": "not supported",
        }
        response["sandbox_requested"] = minimax_sandbox_requested
        response["sandbox_effective"] = minimax_sandbox_effective
        response["sandbox_enforced"] = minimax_sandbox_enforced
    if harness == "agy":
        response["parameter_handling"] = {
            "model": "mapped to --model" if model else "not requested",
            "sandbox": "not mapped; agy uses --dangerously-skip-permissions by default",
            "reasoning_effort": "mapped to --effort" if reasoning_effort else "not requested",
            "cwd": "not passed via flag; the worker sets Popen(cwd=...)",
            "result_path": "not passed via flag; the worker writes result.txt from captured stdout",
        }
    return response


class JobPollLockError(RuntimeError):
    """Raised when per-job poll lock cannot be acquired within the timeout."""


_JOB_POLL_THREAD_LOCKS: dict[str, threading.Lock] = {}
_JOB_POLL_IN_MEMORY_META: dict[str, dict] = {}
_JOB_POLL_META_LOCK = threading.Lock()


def _get_job_thread_lock(job_key: str) -> threading.Lock:
    norm_key = os.path.normcase(os.path.abspath(job_key))
    with _JOB_POLL_META_LOCK:
        lock = _JOB_POLL_THREAD_LOCKS.get(norm_key)
        if lock is None:
            lock = threading.Lock()
            _JOB_POLL_THREAD_LOCKS[norm_key] = lock
        return lock


@contextmanager
def _job_poll_lock(job_dir: Path, timeout: float = 5.0) -> Iterator[None]:
    norm_key = os.path.normcase(os.path.abspath(str(job_dir)))
    thread_lock = _get_job_thread_lock(norm_key)
    acquired_thread = thread_lock.acquire(timeout=timeout)
    if not acquired_thread:
        raise JobPollLockError(
            f"Timed out after {timeout:.1f}s waiting for in-process poll lock for job {job_dir.name}"
        )

    lock_file = job_dir / "poll.lock"
    start_time = time.monotonic()
    file_locked = False
    try:
        while time.monotonic() - start_time < timeout:
            try:
                fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, f"{os.getpid()} {time.time()}\n".encode("utf-8"))
                finally:
                    os.close(fd)
                file_locked = True
                break
            except FileExistsError:
                # PID-aware stale lock recovery
                try:
                    content = lock_file.read_text(encoding="utf-8").strip()
                    parts = content.split()
                    pid = None
                    lock_ts = None
                    if parts and parts[0].isdigit():
                        pid = int(parts[0])
                    if len(parts) >= 2:
                        try:
                            lock_ts = float(parts[1])
                        except ValueError:
                            pass

                    if pid is not None:
                        if not _is_pid_alive(pid):
                            # Process is dead. Only unlink if lock has existed at least a brief grace period (5s)
                            mtime = lock_file.stat().st_mtime
                            if abs(time.time() - mtime) > 5.0 or (lock_ts is not None and abs(time.time() - lock_ts) > 5.0):
                                try:
                                    lock_file.unlink(missing_ok=True)
                                except OSError:
                                    pass
                        # If process is alive, NEVER delete the lock file!
                    else:
                        # Corrupt lock file: fail-closed, delete only if severely stale (> 120s)
                        mtime = lock_file.stat().st_mtime
                        if abs(time.time() - mtime) > 120.0 and (time.monotonic() - start_time > 1.0):
                            try:
                                lock_file.unlink(missing_ok=True)
                            except OSError:
                                pass
                except OSError:
                    pass
                time.sleep(0.005)

        if not file_locked:
            raise JobPollLockError(
                f"Timed out after {timeout:.1f}s waiting for file poll lock for job {job_dir.name}"
            )

        yield
    finally:
        if file_locked:
            try:
                lock_file.unlink(missing_ok=True)
            except OSError:
                pass
        thread_lock.release()


def poll_task(
    job_id: str,
    immediate: bool = False,
    jobs_dir: Path | None = None,
    lock_timeout: float = 5.0,
) -> dict:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        return {"ok": False, "error": "invalid job_id"}
    base_jobs_dir = jobs_dir or JOBS_DIR
    job_dir = base_jobs_dir / job_id
    if not job_dir.is_dir():
        state_path = job_dir / "status.json"
        if not state_path.is_file():
            return {"ok": False, "error": f"Unknown job_id: {job_id}"}

    norm_key = os.path.normcase(os.path.abspath(str(job_dir)))

    try:
        with _job_poll_lock(job_dir, timeout=lock_timeout):
            poll_meta_path = job_dir / "poll_meta.json"
            state_path = job_dir / "status.json"
            disk_meta: dict | None = None
            disk_corrupted = False

            if poll_meta_path.is_file():
                try:
                    data = read_json_object(poll_meta_path)
                    if isinstance(data, dict):
                        disk_meta = data
                    else:
                        disk_corrupted = True
                except (OSError, ValueError, json.JSONDecodeError):
                    disk_corrupted = True

            with _JOB_POLL_META_LOCK:
                in_mem = _JOB_POLL_IN_MEMORY_META.get(norm_key)
                in_mem_meta = dict(in_mem) if isinstance(in_mem, dict) else None

            # If metadata on disk is corrupted and caller did not explicitly request immediate poll: fail-closed!
            if disk_corrupted and not immediate:
                now_ts = time.time()
                if in_mem_meta and isinstance(in_mem_meta.get("last_polled_ts"), (int, float)):
                    elapsed = now_ts - in_mem_meta["last_polled_ts"]
                    remaining = math.ceil(max(0.0, POLL_THROTTLE_INTERVAL_SECONDS - elapsed))
                    next_allowed = in_mem_meta.get("next_allowed_at") or datetime.fromtimestamp(
                        in_mem_meta["last_polled_ts"] + POLL_THROTTLE_INTERVAL_SECONDS, timezone.utc
                    ).isoformat()
                    return {
                        "ok": False,
                        "error": f"Corrupted poll metadata on disk for job {job_id}; fail-closed",
                        "poll_throttled": True,
                        "metadata_error": True,
                        "retry_after_seconds": remaining,
                        "next_allowed_at": next_allowed,
                        **in_mem_meta.get("snapshot", {}),
                    }
                else:
                    return {
                        "ok": False,
                        "error": f"Corrupted poll metadata for job {job_id}; fail-closed",
                        "poll_throttled": True,
                        "metadata_error": True,
                        "retry_after_seconds": int(POLL_THROTTLE_INTERVAL_SECONDS),
                        "next_allowed_at": datetime.fromtimestamp(
                            now_ts + POLL_THROTTLE_INTERVAL_SECONDS, timezone.utc
                        ).isoformat(),
                    }

            # Select effective metadata: prefer disk_meta, fallback to in_mem_meta
            meta = disk_meta if disk_meta is not None else (in_mem_meta or {})
            cached_snapshot = meta.get("snapshot")
            last_polled_ts = meta.get("last_polled_ts")

            if isinstance(cached_snapshot, dict) and not queue_root_matches(cached_snapshot, base_jobs_dir):
                return {
                    "ok": False,
                    "error": "job queue root mismatch; refusing cached foreign queue state",
                    "queue_root_mismatch": True,
                    **queue_diagnostics(base_jobs_dir),
                }

            # If a terminal state was already observed, return cached result directly without reading disk
            if isinstance(cached_snapshot, dict) and cached_snapshot.get("status") in TERMINAL_STATUSES:
                return {"ok": True, **cached_snapshot, "poll_throttled": False}

            now_ts = time.time()
            now_iso = utc_now()

            if isinstance(last_polled_ts, (int, float)):
                elapsed = now_ts - last_polled_ts
            else:
                elapsed = None

            is_throttled = (
                not immediate
                and isinstance(cached_snapshot, dict)
                and elapsed is not None
                and elapsed < POLL_THROTTLE_INTERVAL_SECONDS
            )

            if is_throttled:
                remaining = math.ceil(max(0.0, POLL_THROTTLE_INTERVAL_SECONDS - (elapsed if elapsed is not None else 0.0)))
                next_allowed_ts = (last_polled_ts if last_polled_ts is not None else now_ts) + POLL_THROTTLE_INTERVAL_SECONDS
                next_allowed_at = meta.get("next_allowed_at") or datetime.fromtimestamp(next_allowed_ts, timezone.utc).isoformat()
                return {
                    "ok": True,
                    **cached_snapshot,
                    "poll_throttled": True,
                    "retry_after_seconds": remaining,
                    "next_allowed_at": next_allowed_at,
                    "last_polled_at": meta.get("last_polled_at"),
                }

            # Actual poll: read status.json from disk
            if not state_path.is_file():
                return {"ok": False, "error": f"Unknown job_id: {job_id}"}
            try:
                state = read_json_object(state_path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                return {"ok": False, "error": f"Could not read job state: {exc}"}
            if not queue_root_matches(state, base_jobs_dir):
                return {
                    "ok": False,
                    "error": "job queue root mismatch; refusing to poll foreign queue state",
                    "queue_root_mismatch": True,
                    **queue_diagnostics(base_jobs_dir),
                }

            status = state.get("status")
            if status in TERMINAL_STATUSES:
                new_meta = {
                    "last_polled_at": now_iso,
                    "last_polled_ts": now_ts,
                    "snapshot": state,
                    "next_allowed_at": None,
                }
                with _JOB_POLL_META_LOCK:
                    _JOB_POLL_IN_MEMORY_META[norm_key] = dict(new_meta)

                meta_persisted = True
                try:
                    write_json(poll_meta_path, new_meta)
                except OSError as exc:
                    meta_persisted = False
                    with _JOB_POLL_META_LOCK:
                        _JOB_POLL_IN_MEMORY_META[norm_key]["persistence_error"] = str(exc)

                resp = {
                    "ok": True,
                    **state,
                    "poll_throttled": False,
                    "last_polled_at": now_iso,
                }
                if not meta_persisted:
                    resp["meta_persisted"] = False
                return resp

            # Non-terminal state (queued/running): record snapshot and 10-minute cooldown
            next_allowed_ts = now_ts + POLL_THROTTLE_INTERVAL_SECONDS
            next_allowed_at = datetime.fromtimestamp(next_allowed_ts, timezone.utc).isoformat()
            new_meta = {
                "last_polled_at": now_iso,
                "last_polled_ts": now_ts,
                "snapshot": state,
                "next_allowed_at": next_allowed_at,
            }
            with _JOB_POLL_META_LOCK:
                _JOB_POLL_IN_MEMORY_META[norm_key] = dict(new_meta)

            meta_persisted = True
            try:
                write_json(poll_meta_path, new_meta)
            except OSError as exc:
                meta_persisted = False
                with _JOB_POLL_META_LOCK:
                    _JOB_POLL_IN_MEMORY_META[norm_key]["persistence_error"] = str(exc)

            resp = {
                "ok": True,
                **state,
                "poll_throttled": False,
                "last_polled_at": now_iso,
                "next_allowed_at": next_allowed_at,
            }
            if not meta_persisted:
                resp["meta_persisted"] = False
            return resp

    except JobPollLockError as exc:
        now_ts = time.time()
        now_iso = utc_now()
        poll_meta_path = job_dir / "poll_meta.json"
        meta = {}
        if poll_meta_path.is_file():
            try:
                data = read_json_object(poll_meta_path)
                if isinstance(data, dict):
                    meta = data
            except (OSError, ValueError, json.JSONDecodeError):
                meta = {}
        if not meta:
            with _JOB_POLL_META_LOCK:
                in_mem = _JOB_POLL_IN_MEMORY_META.get(norm_key)
                if isinstance(in_mem, dict):
                    meta = in_mem

        cached_snapshot = meta.get("snapshot") if isinstance(meta.get("snapshot"), dict) else {}
        last_polled_ts = meta.get("last_polled_ts")

        if isinstance(last_polled_ts, (int, float)):
            elapsed = now_ts - last_polled_ts
            remaining = math.ceil(max(0.0, POLL_THROTTLE_INTERVAL_SECONDS - elapsed))
            retry_after_seconds = remaining
            next_allowed_at = meta.get("next_allowed_at") or datetime.fromtimestamp(
                last_polled_ts + POLL_THROTTLE_INTERVAL_SECONDS, timezone.utc
            ).isoformat()
        else:
            retry_after_seconds = int(POLL_THROTTLE_INTERVAL_SECONDS)
            next_allowed_at = datetime.fromtimestamp(
                now_ts + POLL_THROTTLE_INTERVAL_SECONDS, timezone.utc
            ).isoformat()

        response = {
            "ok": False,
            "error": f"Job poll lock is busy: {exc}",
            "poll_throttled": True,
            "lock_busy": True,
            "retry_after_seconds": retry_after_seconds,
            "next_allowed_at": next_allowed_at,
            **cached_snapshot,
        }
        if meta.get("last_polled_at"):
            response["last_polled_at"] = meta.get("last_polled_at")
        return response


def cancel_task(job_id: str, jobs_dir: Path | None = None) -> dict:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        return {"ok": False, "error": "invalid job_id"}
    job_dir = (jobs_dir or JOBS_DIR) / job_id
    state_path = job_dir / "status.json"
    if not state_path.is_file():
        return {"ok": False, "error": f"unknown job_id: {job_id}"}
    if (job_dir / "worker.lock").exists():
        return {"ok": False, "error": "task already claimed by worker and cannot be safely cancelled", "status": "running"}
    try:
        state = read_json_object(state_path)
        if not queue_root_matches(state, jobs_dir or JOBS_DIR):
            return {
                "ok": False,
                "error": "job queue root mismatch; refusing to cancel foreign queue state",
                "queue_root_mismatch": True,
                **queue_diagnostics(jobs_dir or JOBS_DIR),
            }
        if state.get("status") != "queued":
            return {"ok": False, "error": f"task is not queued: {state.get('status')}", "status": state.get("status")}
        state.update(status="cancelled", cancelled_at=utc_now(), updated_at=utc_now())
        write_json(state_path, state)
        return {"ok": True, "job_id": job_id, "status": "cancelled"}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def is_effective_openai_provider(route: str | None = None, config_path: Path | None = None) -> bool:
    """Return True if the effective Codex route resolves to OpenAI."""
    effective_route = route or "current"
    if effective_route in ("official", "official_then_codeflow", "official_then_custom"):
        return True
    if effective_route in ("codeflow", "custom"):
        return False
    if effective_route == "current":
        provider = _get_codex_config_provider(config_path)
        return provider in ("official", "openai")
    return False


def resolve_codex_model_and_effort(
    *,
    role: str = "worker",
    route: str = "current",
    model: str | None = None,
    reasoning_effort: str | None = None,
    config_path: Path | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the effective model and reasoning_effort for a Codex execution.

    Policy:
    1) Primary/main Codex default (codex_run, codex_start):
       model = gpt-6-sol; reasoning_effort = medium
    2) Delegated Codex worker default (task_start):
       model = gpt-6-luna; reasoning_effort = max
    3) Explicit caller-provided model/reasoning_effort always wins.
    4) Defaults apply ONLY when effective provider is OpenAI:
       - route=official
       - official attempt of official_then_* routes
       - route=current when current configured provider resolves to openai
    5) Explicit model only: preserve explicit model; do not guess effort.
    6) Explicit reasoning_effort only: preserve it; choose role default model
       if route/provider is OpenAI and model omitted.
    7) Non-OpenAI routes (custom, codeflow, or current with non-openai provider):
       no GPT-6 injection. Custom route preserves its default_model.
    """
    explicit_model = model if (isinstance(model, str) and model.strip()) else None
    explicit_effort = reasoning_effort if (isinstance(reasoning_effort, str) and reasoning_effort.strip()) else None

    openai_target = is_effective_openai_provider(route, config_path=config_path)

    if openai_target:
        if role == "primary":
            role_model = CODEX_PRIMARY_DEFAULT_MODEL
            role_effort = CODEX_PRIMARY_DEFAULT_REASONING_EFFORT
        else:
            role_model = CODEX_WORKER_DEFAULT_MODEL
            role_effort = CODEX_WORKER_DEFAULT_REASONING_EFFORT

        if explicit_model is not None and explicit_effort is not None:
            return explicit_model, explicit_effort
        elif explicit_model is not None and explicit_effort is None:
            return explicit_model, None
        elif explicit_model is None and explicit_effort is not None:
            return role_model, explicit_effort
        else:
            return role_model, role_effort
    else:
        if explicit_model is not None:
            eff_model = explicit_model
        elif route == "custom":
            eff_model = _custom_codex_route_definition(include_secret=False).get("default_model") or None
        else:
            eff_model = None

        return eff_model, explicit_effort


def build_codex_command(state: dict, result_path: Path, route: str | None = None) -> list[str]:
    command = [
        str(CODEX_EXE), "exec", "--color", "never", "--sandbox", state["sandbox"],
        "-C", state["cwd"], "-o", str(result_path),
    ]
    effective_route = route or state.get("route") or state.get("route_requested") or "current"
    if effective_route in ("official", "official_then_codeflow", "official_then_custom"):
        command.extend(["-c", 'model_provider="openai"'])
    elif effective_route == "codeflow":
        command.extend([
            "-c", 'model_provider="harbor_codeflow"',
            "-c", 'model_providers.harbor_codeflow.name="Harbor Code Flow"',
            "-c", 'model_providers.harbor_codeflow.base_url="https://codeflow.asia/v1"',
            "-c", 'model_providers.harbor_codeflow.wire_api="responses"',
            "-c", 'model_providers.harbor_codeflow.env_key="CODEFLOW_API_KEY"',
        ])
    elif effective_route == "custom":
        custom = _custom_codex_route_definition(include_secret=False)
        if not custom.get("ok"):
            raise ValueError(str(custom.get("blocker", "custom_route_invalid")))
        provider_id = custom["provider_id"]
        def _toml_string(value: str) -> str:
            # JSON strings are valid TOML basic strings and safely escape any
            # user-provided quotes, slashes, or control characters.
            return json.dumps(str(value), ensure_ascii=False)
        command.extend([
            "-c", f"model_provider={_toml_string(provider_id)}",
            "-c", f"model_providers.{provider_id}.name={_toml_string(custom['provider_name'])}",
            "-c", f"model_providers.{provider_id}.base_url={_toml_string(custom['base_url'])}",
            "-c", f"model_providers.{provider_id}.wire_api={_toml_string(custom['wire_api'])}",
            "-c", f"model_providers.{provider_id}.env_key={_toml_string(custom['env_key'])}",
        ])
    codex_role = state.get("codex_role") or "worker"
    effective_model, effective_effort = resolve_codex_model_and_effort(
        role=codex_role,
        route=effective_route,
        model=state.get("model"),
        reasoning_effort=state.get("reasoning_effort"),
    )
    if effective_model:
        command.extend(["--model", effective_model])
    if effective_effort:
        command.extend(["-c", f'model_reasoning_effort="{effective_effort}"'])
    command.append(state["prompt"])
    return command


def build_minimax_command(state: dict, result_path: Path) -> list[str]:
    command = [
        state["minimax_executable"] if state.get("minimax_executable") else str(MINIMAX_CLI_EXE),
        "exec",
        "--cwd", state["cwd"],
        "--output-format", "json",
        "--output-last-message", str(result_path),
        "--input", "-",
    ]
    if state.get("model"):
        command.extend(["--model", state["model"]])
    return command


def build_agy_command(state: dict, result_path: Path) -> list[str]:
    """Build the argv list for an agy headless turn.

    Agy has no ``--cwd`` and no ``-o`` (result path) flag, so:
    * the working directory is supplied by the worker via ``Popen(cwd=...)``;
    * ``result_path`` is **not** passed to agy; the worker writes it
      itself from the captured stdout.

    Permission and formatting flags must precede the prompt flag, and the prompt
    is passed using the non-colliding ``--print=<prompt>`` form to prevent
    token swallowing:
    ``agy --dangerously-skip-permissions --output-format json
    --print-timeout 1h [...] --print="<prompt>"``
    """
    executable = state.get("agy_executable") or str(AGY_EXE)
    command: list[str] = [
        executable,
        "--dangerously-skip-permissions",
        "--output-format", "json",
        "--print-timeout", AGY_DEFAULT_PRINT_TIMEOUT,
    ]
    if state.get("model"):
        command.extend(["--model", state["model"]])
    if state.get("reasoning_effort"):
        command.extend(["--effort", state["reasoning_effort"]])
    command.append(f"--print={state['prompt']}")
    # result_path is intentionally unused here. The worker writes it.
    _ = result_path
    return command


def claim_job(job_dir: Path) -> dict | None:
    lock_path = job_dir / "worker.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()} {utc_now()}\n")
    except FileExistsError:
        return None
    state = read_json_object(job_dir / "status.json")
    if state.get("status") != "queued":
        return None
    state.update(status="running", started_at=utc_now(), updated_at=utc_now())
    write_json(job_dir / "status.json", state)
    return state


@contextmanager
def route_lock(lock_path: Path = ROUTE_LOCK_FILE, timeout_seconds: float = 5.0) -> Iterator[None]:
    """Cross-process lock for any future guarded global Codex config mutation."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for Codex route lock")
            time.sleep(0.05)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()} created_at={utc_now()}\n")
        yield
    finally:
        lock_path.unlink(missing_ok=True)


class ConfigSnapshot:
    """Snapshot/restore primitive used only with an explicit safe config mutation path."""

    def __init__(self, path: Path):
        self.path = path
        self.existed = path.exists()
        self.content = path.read_bytes() if self.existed else b""
        self.before_sha256 = hashlib.sha256(self.content).hexdigest()

    def restore(self) -> None:
        if self.existed:
            fd, temp = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".restore", dir=self.path.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(self.content)
                os.replace(temp, self.path)
            except Exception:
                Path(temp).unlink(missing_ok=True)
                raise
        else:
            self.path.unlink(missing_ok=True)

    def unchanged(self) -> bool:
        if self.path.exists() != self.existed:
            return False
        current = self.path.read_bytes() if self.existed else b""
        return hashlib.sha256(current).hexdigest() == self.before_sha256
