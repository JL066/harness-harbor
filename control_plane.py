"""Harness Harbor — a local harness control plane for upstream supervisors."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import copy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Iterator, Literal, Mapping
from urllib.parse import urlsplit

from harness_process_adapter import default_process_activity_adapter
from harness_telemetry import HarnessTelemetryProvider
from runtime_queue import QueueRoot, describe_queue_root, queue_root_matches, resolve_queue_root
from harbor_platform import process as platform_process


def _is_pid_alive(pid: int) -> bool:
    from harbor_platform.process import is_alive
    return is_alive(pid)


PROJECT_ROOT = Path(__file__).resolve().parent
QUEUE_ROOT: QueueRoot = resolve_queue_root(PROJECT_ROOT)
JOBS_DIR = QUEUE_ROOT.path
CONTROL_DIR = Path(os.environ.get("HARBOR_CONTROL_DIR") or (
    str(Path(os.environ["HARBOR_STATE_DIR"]) / "control")
    if os.environ.get("HARBOR_STATE_DIR") else str(PROJECT_ROOT / ".control")))
if not CONTROL_DIR.is_absolute():
    raise ValueError("HARBOR_CONTROL_DIR must be an absolute path")
PROJECTS_FILE = CONTROL_DIR / "projects.json"
BACKUPS_DIR = CONTROL_DIR / "backups"

CODEX_EXE = Path(os.environ.get("HARBOR_CODEX_EXE") or shutil.which("codex") or ("codex.exe" if os.name == "nt" else "codex"))
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
CODEX_ROUTES = frozenset({"current", "official", "custom", "official_then_custom"})
CODEX_DEFAULT_MODEL = "gpt-5.6-sol"
CODEX_DEFAULT_REASONING_EFFORT = "medium"
CODEX_CUSTOM_BASE_URL_ENV = "HARBOR_CODEX_CUSTOM_BASE_URL"
CODEX_CUSTOM_API_KEY_ENV = "HARBOR_CODEX_CUSTOM_API_KEY"
CODEX_CUSTOM_MODEL_ENV = "HARBOR_CODEX_CUSTOM_MODEL"
CODEX_CUSTOM_PROVIDER_ID = "harbor_custom"
CODEX_CUSTOM_PROVIDER_NAME = "Harbor Custom"
CODEX_CUSTOM_WIRE_API = "responses"
CODEX_ROUTE_FAILURES = frozenset({"subscription_quota_exhausted"})
_CODEX_SECRET_DIAGNOSTIC_PATTERNS = (
    re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|auth(?:entication)?[_-]?token|password|secret)\b\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}\b"),
)
MINIMAX_CLI_EXE = Path(os.environ.get("HARBOR_MINIMAX_CLI_EXE") or shutil.which("mcode") or ("mcode.cmd" if os.name == "nt" else "mcode"))
MINIMAX_CONFIG = Path(os.environ.get("HARBOR_MINIMAX_CONFIG") or Path.home() / ".minimax-code" / "config.json")
AGY_EXE = Path(os.environ.get("HARBOR_AGY_EXE") or shutil.which("agy") or ("agy.exe" if os.name == "nt" else "agy"))
AGY_DEFAULT_PRINT_TIMEOUT = "5m"
AGY_EFFORTS = {"low", "medium", "high"}
AGY_DANGEROUS_PERMISSIONS_ENV = "HARBOR_AGY_DANGEROUSLY_SKIP_PERMISSIONS"
AGY_PROBE_CACHE_TTL_SECONDS = 15.0
AGY_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
AGY_PROBE_BLOCKER_RE = re.compile(r"\b(?:error|fatal|unauthori[sz]ed|forbidden|sign\s*in|required|timed?\s*out|failed)\b", re.I)
# Stderr is a diagnostic channel, so its model-row parser must be narrower
# than the historical stdout parser.  A model id emitted by AGY has a
# provider/model shape (for example ``gemini-3.8-flash-high`` or
# ``openai/gpt-oss-120b-medium``); a bare diagnostic word must not become a
# model merely because it happens to match AGY_MODEL_ID_RE.
AGY_STDERR_MODEL_ID_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9]*(?:[-/][A-Za-z0-9][A-Za-z0-9._/-]*)+$"
)
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
    r"\b(?:timed\s+out|timeout\s+(?:exceeded|error|occurred))\b|"
    # Generic / library-level error diagnostics such as ``Unexpected error
    # occurred`` or ``An error was thrown`` carry no recoverable signal
    # about a valid model catalogue — they must fail closed regardless of
    # any catalogue AGY may have emitted on stdout.
    r"\b(?:unexpected|an?|internal|unknown|critical|fatal|generic|some)\s+"
    r"error\s+(?:occurred|happened|was\s+thrown|has\s+occurred|encountered|raised)\b|"
    r"\berror\s+(?:occurred|happened|was\s+thrown|has\s+occurred|encountered|raised)\b|"
    # DNS / hostname resolution failures invalidate any catalogue emitted
    # in the same probe, even when stdout already contains one.
    r"\b(?:dns|host|hostname|server|domain(?:\s+name)?)\s+"
    r"resolution\s+(?:failed|failure|error|timeout|timed?\s*out)\b"
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


def agy_dangerous_permissions_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether AGY's dangerous permission bypass was explicitly enabled.

    The public default is deliberately off. Unknown values are treated as off
    so a typo cannot silently enable the bypass.
    """
    env = os.environ if environ is None else environ
    return env.get(AGY_DANGEROUS_PERMISSIONS_ENV, "").strip().lower() in {
        "1", "true", "yes", "on"
    }

SANDBOXES = {"read-only", "workspace-write"}
MAX_TEXT_BYTES = 200_000
MAX_DIRECTORY_ENTRIES = 5_000
MAX_GIT_OUTPUT = 50_000
GIT_DELIVERY_TIMEOUT_SECONDS = 30
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


def codex_custom_route_config(
    environ: Mapping[str, str] | None = None,
    *,
    include_secret: bool = False,
) -> dict[str, str]:
    """Return the validated, generic process-local custom provider settings."""
    env = os.environ if environ is None else environ
    base_url = env.get(CODEX_CUSTOM_BASE_URL_ENV, "").strip()
    api_key = env.get(CODEX_CUSTOM_API_KEY_ENV, "").strip()
    if not base_url or not api_key:
        raise ValueError(
            "custom route requires HARBOR_CODEX_CUSTOM_BASE_URL and "
            "HARBOR_CODEX_CUSTOM_API_KEY"
        )
    if any(ord(char) < 0x20 or char.isspace() for char in base_url) or any(
        ord(char) < 0x20 for char in api_key
    ):
        raise ValueError("custom route configuration contains invalid control characters")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("HARBOR_CODEX_CUSTOM_BASE_URL must be an http(s) URL without credentials or query data")
    config = {
        "provider_id": CODEX_CUSTOM_PROVIDER_ID,
        "provider_name": CODEX_CUSTOM_PROVIDER_NAME,
        "base_url": base_url,
        "wire_api": CODEX_CUSTOM_WIRE_API,
        "env_key": CODEX_CUSTOM_API_KEY_ENV,
    }
    default_model = env.get(CODEX_CUSTOM_MODEL_ENV, "").strip()
    if default_model:
        config["default_model"] = default_model
    if include_secret:
        config["api_key"] = api_key
    return config


def codex_route_attempts(route: str) -> list[str]:
    """Return the ordered route attempts for a validated request."""
    if route == "official_then_custom":
        return ["official", "custom"]
    if route in CODEX_ROUTES:
        return [route]
    raise ValueError(f"unsupported Codex route: {route}")


def validate_codex_route(route: str) -> list[str]:
    """Validate route names and all configuration needed by their attempts."""
    routes = codex_route_attempts(route)
    for attempt_route in routes:
        if attempt_route == "custom":
            codex_custom_route_config()
    return routes


def is_official_quota_exhausted(text: str) -> bool:
    """Recognize only explicit OpenAI/Codex subscription usage exhaustion."""
    lower = (text or "").lower()
    if not lower or re.search(r"\b429\b|rate[- ]limit|too\s+many\s+requests|retry[- ]after", lower):
        return False
    if not re.search(r"\b(?:openai|codex)\b", lower):
        return False
    exhaustion = (
        r"\b(?:subscription\s+)?(?:quota|usage(?:\s+limit)?|plan\s+limit|subscription\s+limit)\b"
        r"[^\n]{0,80}\b(?:exhausted|exceeded|reached|depleted|used\s+up)\b"
        r"|\b(?:exhausted|exceeded|depleted|used\s+up)\b[^\n]{0,80}"
        r"\b(?:quota|usage(?:\s+limit)?|plan\s+limit|subscription\s+limit)\b"
    )
    return bool(re.search(exhaustion, lower))


def classify_codex_route_failure(result: subprocess.CompletedProcess) -> str | None:
    """Classify only failures safe for the v0.1 official-to-custom fallback."""
    if result.returncode == 0:
        return None
    text = "\n".join(
        value for value in (result.stdout, result.stderr) if isinstance(value, str)
    )
    return "subscription_quota_exhausted" if is_official_quota_exhausted(text) else None


def sanitize_codex_diagnostic(
    value: object,
    limit: int = 4000,
    *,
    redact_values: tuple[str, ...] = (),
) -> str:
    """Bound Codex diagnostics and redact common credential-shaped values."""
    text = value[-limit:].strip() if isinstance(value, str) else ""
    for index, sensitive_value in enumerate(redact_values):
        if sensitive_value:
            replacement = "[REDACTED_BASE_URL]" if index == 0 else "[REDACTED]"
            text = text.replace(sensitive_value, replacement)
    for pattern in _CODEX_SECRET_DIAGNOSTIC_PATTERNS:
        text = pattern.sub(
            lambda match: (
                f"{match.group(1)}[REDACTED]"
                if match.lastindex else "[REDACTED]"
            ),
            text,
        )
    return text


def sanitize_codex_argv(argv: list[str], route: str = "current") -> list[str]:
    """Return a persisted-safe representation of a Codex command."""
    redact_values: tuple[str, ...] = ()
    if route == "custom":
        try:
            redact_values = (codex_custom_route_config()["base_url"],)
        except ValueError:
            pass
    return [sanitize_codex_diagnostic(value, redact_values=redact_values) for value in argv]


def codex_route_redaction_values(route: str) -> tuple[str, ...]:
    """Return transient custom route values that must not reach observability."""
    if route != "custom":
        return ()
    config = codex_custom_route_config(include_secret=True)
    return (config["base_url"], config["api_key"])


def codex_process_environment(route: str) -> dict[str, str] | None:
    """Build a child-only environment for a custom Codex route."""
    if route != "custom":
        return None
    config = codex_custom_route_config(include_secret=True)
    child_env = os.environ.copy()
    child_env[config["env_key"]] = config["api_key"]
    return child_env


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
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _posix_descendant_pids(root_pid: int) -> list[int]:
    """Return a best-effort bottom-up snapshot of a POSIX process tree."""
    # ponytail: one ps snapshot can miss a child born after it; process-group
    # isolation is the upgrade path if launcher validation needs stronger guarantees.
    if IS_WINDOWS:
        return []
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        try:
            pid, parent_pid = (int(fields[0]), int(fields[1]))
        except ValueError:
            continue
        children.setdefault(parent_pid, []).append(pid)

    descendants: list[int] = []
    pending = [root_pid]
    seen = {root_pid}
    while pending:
        parent_pid = pending.pop()
        for pid in children.get(parent_pid, ()):
            if pid in seen:
                continue
            seen.add(pid)
            descendants.append(pid)
            pending.append(pid)
    descendants.reverse()
    return descendants


def _signal_pids(pids: list[int], signum: signal.Signals) -> None:
    for pid in pids:
        try:
            os.kill(pid, signum)
        except (OSError, ProcessLookupError):
            pass


def _escalate_terminate(proc: subprocess.Popen, argv: list[str]) -> None:
    """Terminate ``proc`` and its descendants.

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
    if isinstance(getattr(proc, "_harbor_identity", None), dict) or not IS_WINDOWS and getattr(proc, "_harbor_pgid", None) == pid:
        platform_process.terminate_tree(proc)
        return

    # 1. SIGTERM-equivalent (TerminateProcess on Windows). On Windows
    #    the immediate Popen child may die while its descendants
    #    survive; we keep this step for the non-Windows code path
    #    where ``proc.terminate()`` is sufficient.
    descendants = _posix_descendant_pids(pid)
    if not IS_WINDOWS:
        _signal_pids(descendants, signal.SIGTERM)
        try:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
        except (OSError, ProcessLookupError):
            pass

        # 2. SIGKILL-equivalent (still best-effort; some processes can resist)
        _signal_pids(descendants, signal.SIGKILL)
        try:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
        except (OSError, ProcessLookupError):
            pass

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
    input: bytes | None = None,
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
    if input is not None and not isinstance(input, bytes):
        raise ValueError("input must be bytes or None")

    popen_kwargs = _make_safe_popen_kwargs(env=env, cwd=cwd)
    if input is not None:
        popen_kwargs["stdin"] = subprocess.PIPE
    proc = platform_process.spawn_owned(argv, **popen_kwargs)

    stdout_bytes = bytearray()
    stderr_bytes = bytearray()
    def _drain(stream, target):
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    return
                if len(target) < max_output_bytes:
                    target.extend(chunk[: max_output_bytes - len(target)])
        except (OSError, ValueError):
            return
    stdout_stream = getattr(proc, "stdout", None)
    stderr_stream = getattr(proc, "stderr", None)
    out_thread = threading.Thread(target=_drain, args=(stdout_stream, stdout_bytes), daemon=True)
    err_thread = threading.Thread(target=_drain, args=(stderr_stream, stderr_bytes), daemon=True)
    out_thread.start(); err_thread.start()
    if input is not None and proc.stdin is not None:
        try:
            proc.stdin.write(input)
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _escalate_terminate(proc, list(argv))
    finally:
        # Defensive final reap; safe to call even after a clean return.
        try:
            if proc.poll() is None:
                _escalate_terminate(proc, list(argv))
                proc.wait(timeout=0.5)
        except Exception:
            pass
        if isinstance(getattr(proc, "_harbor_identity", None), dict) or not IS_WINDOWS and getattr(proc, "_harbor_pgid", None) == proc.pid:
            platform_process.terminate_tree(proc)
    out_thread.join(timeout=1.0)
    err_thread.join(timeout=1.0)
    for stream in (stdout_stream, stderr_stream):
        try:
            if stream is not None and not stream.closed:
                stream.close()
        except (OSError, ValueError):
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
        stdout=_decode_truncate(bytes(stdout_bytes)),
        stderr=_decode_truncate(bytes(stderr_bytes)),
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
    return git_command(repo, ["rev-parse", "--abbrev-ref", "HEAD"])


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
    if not isinstance(value, str):
        return ""
    value = _GIT_CREDENTIAL_URL_RE.sub(r"\1<redacted>@", value)
    value = _GIT_BEARER_RE.sub("Bearer <redacted>", value)
    value = _GIT_CREDENTIAL_VALUE_RE.sub(r"\1\2<redacted>", value)
    return _GIT_TOKEN_SHAPE_RE.sub("<redacted>", value)


def _sanitize_git_delivery_argv(argv: list[str]) -> list[str]:
    return ["<configured-https-remote>" if arg.lower().startswith("https://") else arg for arg in argv]


def _git_delivery_failure(message: str, *, operation: str, dry_run: bool = False, pushed: bool = False, **extra: Any) -> dict:
    return {**extra, "ok": False, "error": _redact_git_delivery_text(message), "operation": operation, "dry_run": dry_run, "pushed": pushed}


class _GitDeliveryError(ValueError):
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
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(ch) < 32 or ch.isspace() for ch in value):
        raise ValueError("configured remote URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("configured remote URL is invalid") from exc
    host = parsed.hostname
    lower_host = host.lower().rstrip(".") if host else ""
    if (parsed.scheme.lower() != "https" or not host or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or port not in (None, 443)
            or lower_host == "localhost" or lower_host.endswith(".localhost") or lower_host.endswith(".local")
            or re.fullmatch(r"\d+(?:\.\d+){3}", lower_host) or ":" in lower_host
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", lower_host)):
        raise ValueError("configured remote must use a credential-free HTTPS URL")
    return value


def _git_delivery_command(args: list[str], root: Path) -> dict:
    raw = _run_git(args, root, timeout=GIT_DELIVERY_TIMEOUT_SECONDS)
    result = {"ok": bool(raw.get("ok")), "argv": _sanitize_git_delivery_argv(list(raw.get("argv", []))),
              "stdout": _redact_git_delivery_text(str(raw.get("stdout", ""))),
              "stderr": _redact_git_delivery_text(str(raw.get("stderr", ""))), "exit_code": raw.get("exit_code"),
              "truncated": bool(raw.get("truncated", False))}
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
    operation = "git_ls_remote"
    try:
        root = resolve_repo(repo); remote = _validate_git_delivery_remote_name(remote); ref = _validate_git_delivery_ref(ref, field="ref")
        remote_url, _ = _resolve_git_delivery_remote(root, remote); before_head, command = _read_exact_git_delivery_head(root, remote_url, ref)
        if before_head is None:
            return _git_delivery_failure("remote branch does not exist; new branches are not supported", operation=operation, repo_root=str(root), remote=remote, ref=ref, before_head=None, after_head=None, **command)
        return {"ok": True, "operation": operation, "dry_run": False, "pushed": False, "repo_root": str(root), "remote": remote, "ref": ref, "before_head": before_head, "after_head": before_head, **command}
    except _GitDeliveryError as exc:
        return _git_delivery_failure(str(exc), operation=operation, **exc.result)
    except (OSError, ValueError) as exc:
        return _git_delivery_failure(str(exc), operation=operation)


def _prepare_git_push(repo: str, remote: str, src_ref: str, dst_ref: str, expected_remote_head: str, *, operation: str):
    try:
        root = resolve_repo(repo); remote = _validate_git_delivery_remote_name(remote); src_ref = _validate_git_delivery_ref(src_ref, field="src_ref"); dst_ref = _validate_git_delivery_ref(dst_ref, field="dst_ref"); expected_remote_head = _validate_expected_remote_head(expected_remote_head)
        remote_url, _ = _resolve_git_delivery_remote(root, remote); before_head, lookup = _read_exact_git_delivery_head(root, remote_url, dst_ref)
        if before_head != expected_remote_head:
            return _git_delivery_failure("remote destination head differs from expected_remote_head (concurrent drift or new branch)", operation=operation, repo_root=str(root), remote=remote, src_ref=src_ref, dst_ref=dst_ref, expected_remote_head=expected_remote_head, before_head=before_head, after_head=before_head, **lookup)
        source_head, _ = _resolve_git_delivery_source(root, src_ref)
        return root, remote, remote_url, src_ref, dst_ref, source_head, {"expected_remote_head": expected_remote_head, "before_head": before_head}
    except _GitDeliveryError as exc:
        return _git_delivery_failure(str(exc), operation=operation, **exc.result)
    except (OSError, ValueError) as exc:
        return _git_delivery_failure(str(exc), operation=operation)


def git_push_dry_run_result(repo: str, remote: str, src_ref: str, dst_ref: str, expected_remote_head: str) -> dict:
    operation = "git_push_dry_run"; prepared = _prepare_git_push(repo, remote, src_ref, dst_ref, expected_remote_head, operation=operation)
    if isinstance(prepared, dict): return prepared
    root, remote, remote_url, src_ref, dst_ref, source_head, metadata = prepared
    command = _git_delivery_command(["push", "--dry-run", remote_url, f"{src_ref}:{dst_ref}"], root)
    if not command["ok"]:
        return _git_delivery_failure(command.get("error") or "git push --dry-run failed", operation=operation, dry_run=True, repo_root=str(root), remote=remote, src_ref=src_ref, dst_ref=dst_ref, source_head=source_head, after_head=metadata["before_head"], **metadata, **command)
    return {"ok": True, "operation": operation, "dry_run": True, "pushed": False, "repo_root": str(root), "remote": remote, "src_ref": src_ref, "dst_ref": dst_ref, "source_head": source_head, "after_head": metadata["before_head"], **metadata, **command}


def git_push_ref_result(repo: str, remote: str, src_ref: str, dst_ref: str, expected_remote_head: str) -> dict:
    operation = "git_push_ref"; prepared = _prepare_git_push(repo, remote, src_ref, dst_ref, expected_remote_head, operation=operation)
    if isinstance(prepared, dict): return prepared
    root, remote, remote_url, src_ref, dst_ref, source_head, metadata = prepared
    dry_run = _git_delivery_command(["push", "--dry-run", remote_url, f"{src_ref}:{dst_ref}"], root)
    if not dry_run["ok"]:
        return _git_delivery_failure(dry_run.get("error") or "git push --dry-run failed", operation=operation, dry_run=True, repo_root=str(root), remote=remote, src_ref=src_ref, dst_ref=dst_ref, source_head=source_head, after_head=metadata["before_head"], **metadata, **dry_run)
    try:
        fresh_remote_url, _ = _resolve_git_delivery_remote(root, remote)
        if fresh_remote_url != remote_url: raise ValueError("configured remote URL changed during delivery")
        fresh_head, fresh_lookup = _read_exact_git_delivery_head(root, fresh_remote_url, dst_ref)
        if fresh_head != metadata["expected_remote_head"]:
            return _git_delivery_failure("remote destination head differs from expected_remote_head immediately before push", operation=operation, dry_run=True, repo_root=str(root), remote=remote, src_ref=src_ref, dst_ref=dst_ref, source_head=source_head, expected_remote_head=metadata["expected_remote_head"], before_head=fresh_head, after_head=fresh_head, **fresh_lookup)
    except _GitDeliveryError as exc:
        return _git_delivery_failure(str(exc), operation=operation, dry_run=True, **exc.result)
    except (OSError, ValueError):
        return _git_delivery_failure("pre-push remote validation failed", operation=operation, dry_run=True)
    command = _git_delivery_command(["push", fresh_remote_url, f"{src_ref}:{dst_ref}"], root)
    if not command["ok"]:
        return _git_delivery_failure(command.get("error") or "git push failed", operation=operation, dry_run=False, repo_root=str(root), remote=remote, src_ref=src_ref, dst_ref=dst_ref, source_head=source_head, after_head=fresh_head, **metadata, **command)
    try: after_head, verification = _read_exact_git_delivery_head(root, fresh_remote_url, dst_ref)
    except (_GitDeliveryError, OSError, ValueError) as exc:
        details = exc.result if isinstance(exc, _GitDeliveryError) else {}
        return _git_delivery_failure(str(exc), operation=operation, dry_run=False, pushed=True, repo_root=str(root), remote=remote, src_ref=src_ref, dst_ref=dst_ref, source_head=source_head, after_head=None, **metadata, **details)
    if after_head != source_head:
        return _git_delivery_failure("push completed but post-push remote head verification did not match source_head", operation=operation, dry_run=False, pushed=True, repo_root=str(root), remote=remote, src_ref=src_ref, dst_ref=dst_ref, source_head=source_head, after_head=after_head, **metadata, **verification)
    return {"ok": True, "operation": operation, "dry_run": False, "pushed": True, "repo_root": str(root), "remote": remote, "src_ref": src_ref, "dst_ref": dst_ref, "source_head": source_head, "after_head": after_head, **metadata, **command}


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


def _run_agy_probe(command: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
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
        timeout=30.0 if command[1:] == ["models"] else 5.0,
        max_output_bytes=MAX_SUBPROCESS_OUTPUT_BYTES,
    )


def _parse_agy_models(result: subprocess.CompletedProcess) -> tuple[list[str], bool]:
    """Parse a model probe without treating banners or diagnostics as models."""
    text = _probe_text(result)
    if result.returncode != 0 or AGY_PROBE_BLOCKER_RE.search(text):
        return [], False
    models: list[str] = []
    for line in text.splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        token = token.strip("`[](),")
        if AGY_MODEL_ID_RE.fullmatch(token) and (token.startswith("gemini-") or "/" in token):
            if token not in models:
                models.append(token)
    return models, bool(models)


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


def _parse_agy_stderr_model_enumeration(
    result: subprocess.CompletedProcess,
) -> tuple[list[str], bool]:
    """Extract only a strict model catalogue accidentally written to stderr.

    Unlike stdout, stderr is primarily diagnostic output.  Every non-banner
    line must therefore be either a complete model id with a provider-like
    separator or a model row whose first field is such an id.  Any unknown
    diagnostic line invalidates the fallback catalogue.
    """
    stderr_text = result.stderr if isinstance(result.stderr, str) else ""
    if not stderr_text.strip():
        return [], False

    models: list[str] = []
    structurally_valid = True
    for raw_line in stderr_text.splitlines():
        line = raw_line.strip()
        if not line or AGY_INFO_OR_BANNER_RE.fullmatch(line):
            continue
        model_id = line.split(None, 1)[0]
        if not AGY_STDERR_MODEL_ID_RE.fullmatch(model_id):
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

    The public capability probe requires:
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


_AGY_PROBE_LOCK = threading.Lock()
_AGY_PROBE_CACHE: tuple[tuple[str, str, str, bool], float, dict] | None = None


def _agy_cli_status() -> dict:
    """Return the canonical agy harness status record.

    The shape mirrors ``_minimax_cli_status`` so the listing/registry
    surface is consistent. Agy is treated as a peer lifecycle-supervised
    harness: a single ``agy --print`` invocation is supervised by the
    worker, not by the MCP transport, so ``supports_async`` is True
    once the CLI is verified.
    """
    global _AGY_PROBE_CACHE
    executable = AGY_EXE
    cwd = str(Path.cwd())
    env = {k: v for k, v in os.environ.items() if v is not None}
    dangerous_permissions_enabled = agy_dangerous_permissions_enabled(env)
    cache_key = (
        str(executable),
        cwd,
        hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest(),
        dangerous_permissions_enabled,
    )
    with _AGY_PROBE_LOCK:
        cached = _AGY_PROBE_CACHE
        if cached and cached[0] == cache_key and time.monotonic() - cached[1] <= AGY_PROBE_CACHE_TTL_SECONDS:
            return copy.deepcopy(cached[2])
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
        "dangerously_skip_permissions_enabled": dangerous_permissions_enabled,
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
            result = _run_agy_probe([str(executable), *args], cwd=cwd, env=env)
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
    models_result = probes.get("models")
    models, models_structurally_valid = (
        _parse_agy_model_enumeration(models_result)
        if models_result is not None
        else ([], False)
    )
    if models_result is not None and not (
        models and models_structurally_valid
    ):
        # AGY 1.1.27 can write the otherwise valid catalogue to stderr while
        # reporting progress there as well.  Keep stdout authoritative when
        # it yields a valid catalogue; only then consider the strict stderr
        # fallback, which treats unknown diagnostic lines as structural noise.
        stderr_models, stderr_structurally_valid = _parse_agy_stderr_model_enumeration(
            models_result
        )
        if stderr_models and stderr_structurally_valid:
            models, models_structurally_valid = stderr_models, stderr_structurally_valid
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
    base["models"] = models
    base.update(version=version, capabilities=capabilities)

    if probe_errors:
        base["blocker"] = "Antigravity CLI capability probes did not pass: " + "; ".join(probe_errors)
        with _AGY_PROBE_LOCK: _AGY_PROBE_CACHE = (cache_key, time.monotonic(), copy.deepcopy(base))
        return base

    required = ("print", "dangerously_skip_permissions", "output_format", "model", "effort", "print_timeout")
    missing = [name for name in required if not capabilities[name]]
    if missing:
        base["blocker"] = "Antigravity CLI capability probes did not pass: missing verified exec capabilities: " + ", ".join(missing)
        with _AGY_PROBE_LOCK: _AGY_PROBE_CACHE = (cache_key, time.monotonic(), copy.deepcopy(base))
        return base

    base.update(
        available=True,
        supports_async=True,
        noninteractive_command=[str(executable), "--print"],
        parameter_mappings={
            "model": "--model <id>",
            "sandbox": "sandbox is a single boolean in agy; read-only is rejected (workspace-write is the only verified mapping).",
            "reasoning_effort": "--effort <low|medium|high>",
        },
        blocker=None,
    )
    if dangerous_permissions_enabled:
        base["noninteractive_command"].append("--dangerously-skip-permissions")
    base["noninteractive_command"].extend(
        ["--output-format", "json", "--print-timeout", AGY_DEFAULT_PRINT_TIMEOUT]
    )
    with _AGY_PROBE_LOCK: _AGY_PROBE_CACHE = (cache_key, time.monotonic(), copy.deepcopy(base))
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


_HARNESS_TELEMETRY_PROVIDER: HarnessTelemetryProvider | None = None


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


def harness_telemetry_snapshot(*, force_refresh: bool = False) -> dict:
    """Return a bounded, read-only snapshot shared by core integrations."""
    global _HARNESS_TELEMETRY_PROVIDER
    if _HARNESS_TELEMETRY_PROVIDER is None:
        def invalidate_agy_cache() -> None:
            global _AGY_PROBE_CACHE
            _AGY_PROBE_CACHE = None
        _HARNESS_TELEMETRY_PROVIDER = HarnessTelemetryProvider(
            status_provider=harness_status,
            codex_quota_provider=lambda: {"state": "unavailable", "source": "Codex CLI status", "error": "no authoritative local quota source"},
            job_activity_provider=_harness_job_activity,
            process_adapter=default_process_activity_adapter(run_safe_subprocess),
            agy_cache_invalidator=invalidate_agy_cache,
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


def codex_model_options(model: str | None, reasoning_effort: str | None) -> tuple[str, str | None]:
    model = model if model is not None else CODEX_DEFAULT_MODEL
    if reasoning_effort is None and model == CODEX_DEFAULT_MODEL:
        reasoning_effort = CODEX_DEFAULT_REASONING_EFFORT
    return model, reasoning_effort


def start_task(*, harness: Literal["codex", "minimax", "agy"], prompt: str, project: str | None,
               cwd: str | None, model: str | None, sandbox: str, reasoning_effort: str | None,
               route: str | None = None) -> dict:
    if route is None:
        route = os.environ.get("HARBOR_CODEX_DEFAULT_ROUTE", "current") if harness == "codex" else "current"
    if harness not in {"codex", "minimax", "agy"}:
        return {"ok": False, "error": f"unsupported harness: {harness}"}
    if not isinstance(prompt, str) or not prompt.strip():
        return {"ok": False, "error": "prompt must be a non-empty string"}
    if sandbox not in SANDBOXES:
        return {"ok": False, "error": f"unsupported sandbox: {sandbox}"}
    if harness == "codex":
        model, reasoning_effort = codex_model_options(model, reasoning_effort)
        if route not in CODEX_ROUTES:
            return {"ok": False, "error": f"Codex route must be one of {sorted(CODEX_ROUTES)}."}
        try:
            validate_codex_route(route)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    elif route != "current":
        return {"ok": False, "error": f"{harness} tasks only support route=current."}
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
        dangerous_permissions_enabled = agy_dangerous_permissions_enabled()
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
        "route_used": (
            codex_route_attempts(route)[0] if harness == "codex" else "current"
        ),
        "fallback_used": False,
        "fallback_reason": None,
        "attempts": [],
        "queue_root_fingerprint": describe_queue_root(JOBS_DIR, QUEUE_ROOT.source).fingerprint,
        "native_process": None,
        "minimax_executable": minimax_status["executable"] if harness == "minimax" else None,
        "agy_executable": str(AGY_EXE) if harness == "agy" else None,
        "agy_dangerously_skip_permissions": (
            dangerous_permissions_enabled if harness == "agy" else False
        ),
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    write_json(job_dir / "status.json", state)
    response = {"ok": True, "job_id": job_id, "status": "queued", "harness": harness, "cwd": str(workdir), "project": project_alias,
                "queue_root": describe_queue_root(JOBS_DIR, QUEUE_ROOT.source).as_dict()}
    if harness == "minimax":
        response["parameter_handling"] = {
            "model": "mapped to --model" if model else "not requested",
            "sandbox": "not mapped; MiniMax uses its configured/default permission policy",
            "reasoning_effort": "not supported",
        }
    if harness == "agy":
        response["parameter_handling"] = {
            "model": "mapped to --model" if model else "not requested",
            "sandbox": "workspace-write is required; dangerous permission bypass is opt-in",
            "dangerously_skip_permissions": (
                f"enabled via {AGY_DANGEROUS_PERMISSIONS_ENV}"
                if dangerous_permissions_enabled
                else f"disabled by default; set {AGY_DANGEROUS_PERMISSIONS_ENV}=1 to enable"
            ),
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
    state_path = job_dir / "status.json"
    # Local reads never probe a harness. Terminal truth must precede cached
    # snapshots, corrupt poll metadata, and even a busy cooldown metadata lock.
    def local_state():
        try:
            state = read_json_object(state_path)
        except FileNotFoundError:
            return None, {"ok": False, "error": f"Unknown job_id: {job_id}",
                          "queue_root": describe_queue_root(base_jobs_dir, "reader").as_dict()}
        except (OSError, ValueError) as exc:
            return None, {"ok": False, "error": f"Could not read job state: {exc}"}
        if not queue_root_matches(state, base_jobs_dir):
            return None, {"ok": False, "error": "job belongs to a different Harbor queue root",
                          "queue_root": describe_queue_root(base_jobs_dir, "reader").as_dict()}
        if state.get("status") in TERMINAL_STATUSES:
            return state, {**state, "ok": True, "poll_throttled": False, "last_polled_at": utc_now()}
        if state.get("status") not in {"queued", "running"}:
            return None, {"ok": False, "error": "invalid local job status"}
        return state, None

    state, response = local_state()
    if response is not None:
        return response

    norm_key = os.path.normcase(os.path.abspath(str(job_dir)))

    try:
        with _job_poll_lock(job_dir, timeout=lock_timeout):
            # A worker can finish while this reader waits for metadata ownership.
            state, response = local_state()
            if response is not None:
                return response
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

            now_ts = time.time()
            now_iso = utc_now()

            if isinstance(last_polled_ts, (int, float)):
                elapsed = now_ts - last_polled_ts
            else:
                elapsed = None

            is_throttled = (
                not immediate
                and isinstance(cached_snapshot, dict)
                and cached_snapshot.get("status") in {"queued", "running"}
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
        state, response = local_state()
        if response is not None:
            return response
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
        return {"ok": False, "error": f"unknown job_id: {job_id}", "queue_root": QUEUE_ROOT.as_dict()}
    try:
        state = read_json_object(state_path)
        if not queue_root_matches(state, JOBS_DIR):
            return {"ok": False, "error": "job belongs to a different Harbor queue root",
                    "queue_root": describe_queue_root(JOBS_DIR, "reader").as_dict()}
        if (job_dir / "worker.lock").exists():
            return {"ok": False, "error": "task already claimed by worker and cannot be safely cancelled", "status": "running"}
        if state.get("status") != "queued":
            return {"ok": False, "error": f"task is not queued: {state.get('status')}", "status": state.get("status")}
        state.update(status="cancelled", cancelled_at=utc_now(), updated_at=utc_now())
        write_json(state_path, state)
        return {"ok": True, "job_id": job_id, "status": "cancelled"}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def build_codex_command(
    state: dict,
    result_path: Path,
    *,
    route: str | None = None,
) -> list[str]:
    selected_route = route or state.get("route_used") or state.get("route_requested", "current")
    if selected_route == "official_then_custom":
        selected_route = codex_route_attempts(selected_route)[0]
    command = [
        str(CODEX_EXE), "exec", "--color", "never", "--sandbox", state["sandbox"],
        "-C", state["cwd"], "-o", str(result_path),
    ]
    if selected_route == "official":
        command.extend(["-c", 'model_provider="openai"'])
    elif selected_route == "custom":
        custom = codex_custom_route_config()

        def toml_string(value: str) -> str:
            # JSON strings are valid TOML basic strings and safely escape any
            # user-provided quotes, slashes, or control characters.
            return json.dumps(value, ensure_ascii=False)

        provider_id = custom["provider_id"]
        command.extend([
            "-c", f"model_provider={toml_string(provider_id)}",
            "-c", f"model_providers.{provider_id}.name={toml_string(custom['provider_name'])}",
            "-c", f"model_providers.{provider_id}.base_url={toml_string(custom['base_url'])}",
            "-c", f"model_providers.{provider_id}.wire_api={toml_string(custom['wire_api'])}",
            "-c", f"model_providers.{provider_id}.env_key={toml_string(custom['env_key'])}",
        ])
    effective_model, effective_effort = codex_model_options(state.get("model"), state.get("reasoning_effort"))
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
    ]
    if state.get("model"):
        command.extend(["--model", state["model"]])
    command.extend(["--input", "-"])
    return command


def build_agy_command(state: dict, result_path: Path) -> list[str]:
    """Build the argv list for an agy headless turn.

    Agy has no ``--cwd`` and no ``-o`` (result path) flag, so:
    * the working directory is supplied by the worker via ``Popen(cwd=...)``;
    * ``result_path`` is **not** passed to agy; the worker writes it
      itself from the captured stdout.

    The base flags are the supported non-interactive argv. The dangerous
    permission bypass is included only when the job explicitly records that
    ``HARBOR_AGY_DANGEROUSLY_SKIP_PERMISSIONS`` was enabled.
    """
    executable = state.get("agy_executable") or str(AGY_EXE)
    command: list[str] = [executable]
    if state.get("agy_dangerously_skip_permissions", agy_dangerous_permissions_enabled()):
        command.append("--dangerously-skip-permissions")
    command.extend(["--output-format", "json", "--print-timeout", AGY_DEFAULT_PRINT_TIMEOUT])
    if state.get("model"):
        command.extend(["--model", state["model"]])
    if state.get("reasoning_effort"):
        command.extend(["--effort", state["reasoning_effort"]])
    # The prompt must be a value of the ``-p`` flag; a bare positional
    # would be rejected by agy.
    command.append("--print=" + state["prompt"])
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
    state.update(status="running", worker_pid=os.getpid(), started_at=utc_now(), updated_at=utc_now())
    write_json(job_dir / "status.json", state)
    return state
