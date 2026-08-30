"""ChatGPT Harbor — a local agent control plane for ChatGPT."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Iterator, Literal


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
JOBS_DIR = PROJECT_ROOT / ".jobs"
CONTROL_DIR = PROJECT_ROOT / ".control"
PROJECTS_FILE = CONTROL_DIR / "projects.json"
BACKUPS_DIR = CONTROL_DIR / "backups"

CODEX_EXE = Path(os.environ.get("HARBOR_CODEX_EXE") or shutil.which("codex") or ("codex.exe" if os.name == "nt" else "codex"))
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
MINIMAX_EXE = Path(os.environ.get("HARBOR_MINIMAX_EXE") or shutil.which("minimax") or ("minimax.exe" if os.name == "nt" else "minimax"))
MINIMAX_CLI_EXE = Path(os.environ.get("HARBOR_MINIMAX_CLI_EXE") or shutil.which("mcode") or ("mcode.cmd" if os.name == "nt" else "mcode"))
MINIMAX_CONFIG = Path(os.environ.get("HARBOR_MINIMAX_CONFIG") or Path.home() / ".minimax-code" / "config.json")
AGY_EXE = Path(os.environ.get("HARBOR_AGY_EXE") or shutil.which("agy") or ("agy.exe" if os.name == "nt" else "agy"))
AGY_DEFAULT_PRINT_TIMEOUT = "5m"
AGY_EFFORTS = {"low", "medium", "high"}

SANDBOXES = {"read-only", "workspace-write"}
MAX_TEXT_BYTES = 200_000
MAX_DIRECTORY_ENTRIES = 5_000
MAX_GIT_OUTPUT = 50_000
MAX_SUBPROCESS_OUTPUT_BYTES = 2_000_000
BINARY_SNIFF_BYTES = 8_192
JOB_ID_RE = re.compile(r"^[0-9a-zA-Z_\-]+$")
POLL_THROTTLE_INTERVAL_SECONDS: float = 600.0
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})

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
# * stdio isolation: child stdin is DEVNULL (never inherits the MCP transport
#   pipe), stdout/stderr are PIPE, and child stdout/stderr never leak to the
#   parent's MCP transport.
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
                timeout=2.0,
                check=False,
                creationflags=_WIN_CREATION_FLAGS,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
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
    timeout: float | None = None,
    max_output_bytes: int = MAX_SUBPROCESS_OUTPUT_BYTES,
) -> subprocess.CompletedProcess:
    """Run ``argv`` in a child process with strict stdio isolation.

    Returns a ``subprocess.CompletedProcess`` whose ``stdout``/``stderr`` are
    decoded text (best effort, ``errors="replace"``) and truncated to
    ``max_output_bytes`` per stream.

    Contract:
    * Child stdin is DEVNULL (MCP transport pipe is never inherited).
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

    popen_kwargs = _make_safe_popen_kwargs(env=env, cwd=cwd)
    proc = subprocess.Popen(argv, **popen_kwargs)

    stdout_bytes = b""
    stderr_bytes = b""
    try:
        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _escalate_terminate(proc, list(argv))
            try:
                stdout_bytes, stderr_bytes = proc.communicate(timeout=0.5)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                stdout_bytes = b""
                stderr_bytes = b""
    finally:
        # Defensive final reap; safe to call even after a clean return.
        try:
            if proc.poll() is None:
                _escalate_terminate(proc, list(argv))
                try:
                    proc.communicate(timeout=0.5)
                except Exception:
                    pass
        except Exception:
            pass

    def _decode_truncate(data: bytes) -> str:
        if not data:
            return ""
        if len(data) > max_output_bytes:
            data = data[:max_output_bytes]
        return data.decode("utf-8", errors="replace")

    return subprocess.CompletedProcess(
        args=list(argv),
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=_decode_truncate(stdout_bytes),
        stderr=_decode_truncate(stderr_bytes),
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
        os.replace(temporary_name, path)
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


def _minimax_cli_candidates() -> list[Path]:
    candidates = [MINIMAX_CLI_EXE]
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
        "session_behavior": "mcode exec runs one headless task through the existing local job queue.",
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
        "reasoning_effort": False,
        "sandbox": False,
    }
    required = ("exec", "cwd", "output_format", "output_format_json", "output_last_message")
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
            "model": "--model <provider/model>",
            "sandbox": None,
            "reasoning_effort": None,
        },
        blocker=None,
    )
    return base


def _run_agy_probe(command: list[str]) -> subprocess.CompletedProcess:
    """Run an agy CLI probe command via the safe subprocess helper.

    Probes are short-lived (``--version``, ``--help``, ``models``) but they
    MUST go through the safe helper to keep the MCP transport isolated from
    any console / credential prompt the CLI might attempt to read.
    """
    if not isinstance(command, list) or not command:
        raise ValueError("command must be a non-empty list")
    return run_safe_subprocess(
        command,
        cwd=None,
        env=None,
        timeout=5.0,
        max_output_bytes=MAX_SUBPROCESS_OUTPUT_BYTES,
    )


def _agy_cli_status() -> dict:
    """Return the canonical agy harness status record.

    The shape mirrors ``_minimax_cli_status`` so the listing/registry
    surface is consistent. Agy is treated as a peer lifecycle-supervised
    harness: a single ``agy --print`` invocation is supervised by the
    worker, not by the MCP transport, so ``supports_async`` is True
    once the CLI is verified.
    """
    executable = AGY_EXE
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
        "noninteractive_command": None,
        "session_behavior": (
            "agy --print runs one headless single-shot turn; the worker "
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
            f"Antigravity CLI not found at {AGY_EXE}. Install Antigravity "
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
            result = _run_agy_probe([str(executable), *args])
            probes[label] = result
            if result.returncode != 0:
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
    base.update(version=version, capabilities=capabilities)

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
            "--print",
            "--dangerously-skip-permissions",
            "--output-format",
            "json",
            "--print-timeout",
            AGY_DEFAULT_PRINT_TIMEOUT,
        ],
        parameter_mappings={
            "model": "--model <id>",
            "sandbox": "sandbox is a single boolean in agy; read-only is rejected (workspace-write is the only verified mapping).",
            "reasoning_effort": "--effort <low|medium|high>",
        },
        blocker=None,
    )
    return base


def harnesses() -> list[dict]:
    return [
        {
            "name": "codex",
            "available": CODEX_EXE.is_file(),
            "executable": str(CODEX_EXE),
            "supports_async": True,
            "supports_reasoning_effort": True,
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
        }
    if name == "minimax":
        return {"ok": True, **_minimax_cli_status()}
    if name == "agy":
        return {"ok": True, **_agy_cli_status()}
    return {"ok": False, "error": f"unknown harness: {name}"}


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
               route: str = "current") -> dict:
    if harness not in {"codex", "minimax", "agy"}:
        return {"ok": False, "error": f"unsupported harness: {harness}"}
    if not isinstance(prompt, str) or not prompt.strip():
        return {"ok": False, "error": "prompt must be a non-empty string"}
    if sandbox not in SANDBOXES:
        return {"ok": False, "error": f"unsupported sandbox: {sandbox}"}
    if route != "current":
        return {"ok": False, "error": f"only route=current is supported (received: {route})"}
    try:
        workdir, project_alias = resolve_task_cwd(project, cwd)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if harness == "minimax":
        if route != "current":
            return {"ok": False, "error": "MiniMax tasks only support route=current."}
        if reasoning_effort is not None:
            return {"ok": False, "error": "MiniMax CLI does not expose a verified reasoning_effort flag."}
        if sandbox != "workspace-write":
            return {"ok": False, "error": "MiniMax CLI does not expose a verified read-only sandbox mapping; use sandbox=workspace-write."}
        minimax_status = harness_status("minimax")
        if not minimax_status["available"]:
            return {"ok": False, "error": minimax_status["blocker"]}
    if harness == "agy":
        if route != "current":
            return {"ok": False, "error": "Antigravity tasks only support route=current."}
        if sandbox != "workspace-write":
            return {"ok": False, "error": "Antigravity CLI does not expose a verified read-only sandbox mapping; use sandbox=workspace-write."}
        if reasoning_effort is not None and reasoning_effort not in AGY_EFFORTS:
            return {"ok": False, "error": f"Antigravity reasoning_effort must be one of {sorted(AGY_EFFORTS)} or unset."}
        agy_status = harness_status("agy")
        if not agy_status["available"]:
            return {"ok": False, "error": agy_status["blocker"]}
    if not CODEX_EXE.is_file() and harness == "codex":
        return {"ok": False, "error": f"Codex executable not found: {CODEX_EXE}"}
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
        "route_used": "current",
        "native_process": None,
        "minimax_executable": minimax_status["executable"] if harness == "minimax" else None,
        "agy_executable": str(AGY_EXE) if harness == "agy" else None,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    write_json(job_dir / "status.json", state)
    response = {"ok": True, "job_id": job_id, "status": "queued", "harness": harness, "cwd": str(workdir), "project": project_alias}
    if harness == "minimax":
        response["parameter_handling"] = {
            "model": "mapped to --model" if model else "not requested",
            "sandbox": "not mapped; MiniMax uses its configured/default permission policy",
            "reasoning_effort": "not supported",
        }
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


def cancel_task(job_id: str) -> dict:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        return {"ok": False, "error": "invalid job_id"}
    job_dir = JOBS_DIR / job_id
    state_path = job_dir / "status.json"
    if not state_path.is_file():
        return {"ok": False, "error": f"unknown job_id: {job_id}"}
    if (job_dir / "worker.lock").exists():
        return {"ok": False, "error": "task already claimed by worker and cannot be safely cancelled", "status": "running"}
    try:
        state = read_json_object(state_path)
        if state.get("status") != "queued":
            return {"ok": False, "error": f"task is not queued: {state.get('status')}", "status": state.get("status")}
        state.update(status="cancelled", cancelled_at=utc_now(), updated_at=utc_now())
        write_json(state_path, state)
        return {"ok": True, "job_id": job_id, "status": "cancelled"}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def build_codex_command(state: dict, result_path: Path) -> list[str]:
    command = [
        str(CODEX_EXE), "exec", "--color", "never", "--sandbox", state["sandbox"],
        "-C", state["cwd"], "-o", str(result_path),
    ]
    if state.get("model"):
        command.extend(["--model", state["model"]])
    if state.get("reasoning_effort"):
        command.extend(["-c", f'model_reasoning_effort="{state["reasoning_effort"]}"'])
    command.append(state["prompt"])
    return command


def build_minimax_command(state: dict, result_path: Path) -> list[str]:
    command = [
        state["minimax_executable"] if state.get("minimax_executable") else str(MINIMAX_CLI_EXE),
        "exec",
        "--cwd", state["cwd"],
        "--output-format", "json",
        "--output-last-message", str(result_path),
    ]
    if state.get("model"):
        command.extend(["--model", state["model"]])
    command.append(state["prompt"])
    return command


def build_agy_command(state: dict, result_path: Path) -> list[str]:
    """Build the argv list for an agy headless turn.

    Agy has no ``--cwd`` and no ``-o`` (result path) flag, so:
    * the working directory is supplied by the worker via ``Popen(cwd=...)``;
    * ``result_path`` is **not** passed to agy; the worker writes it
      itself from the captured stdout.

    The base flags are the supported non-interactive argv:
    ``agy --print --dangerously-skip-permissions --output-format json
    --print-timeout 5m [...] -p "<prompt>"``
    """
    executable = state.get("agy_executable") or str(AGY_EXE)
    command: list[str] = [
        executable,
        "--print",
        "--dangerously-skip-permissions",
        "--output-format", "json",
        "--print-timeout", AGY_DEFAULT_PRINT_TIMEOUT,
    ]
    if state.get("model"):
        command.extend(["--model", state["model"]])
    if state.get("reasoning_effort"):
        command.extend(["--effort", state["reasoning_effort"]])
    # The prompt must be a value of the ``-p`` flag; a bare positional
    # would be rejected by agy.
    command.extend(["-p", state["prompt"]])
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
