import asyncio
import os
import re
import subprocess
import shutil
import tempfile
import json
import time
import uuid
import fnmatch
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from control_plane import (
    cancel_task,
    build_codex_command,
    classify_codex_route_failure,
    codex_process_environment,
    codex_route_attempts,
    codex_route_redaction_values,
    CODEX_ROUTE_FAILURES,
    directory_list_result,
    file_append_result,
    file_read_result,
    file_stat_result,
    file_write_result,
    git_add_result,
    git_branch_result,
    git_commit_result,
    git_diff_result,
    git_log_result,
    git_ls_remote_result,
    git_push_dry_run_result,
    git_push_ref_result,
    git_rev_parse_result,
    git_status_result,
    git_worktree_list_result,
    harness_status as control_harness_status,
    harnesses,
    list_projects,
    poll_task,
    resolve_project,
    run_safe_subprocess,
    sanitize_codex_diagnostic,
    start_task,
    validate_codex_route,
    QUEUE_ROOT,
    harness_telemetry_snapshot,
)
from host_diagnostics import (
    dns_resolve as diag_dns_resolve,
    firewall_query as diag_firewall_query,
    http_probe as diag_http_probe,
    network_interfaces as diag_network_interfaces,
    port_listeners as diag_port_listeners,
    process_inspect as diag_process_inspect,
    tcp_connect_probe as diag_tcp_connect_probe,
    tls_inspect as diag_tls_inspect,
)


CODEX_EXE = Path(os.environ.get("HARBOR_CODEX_EXE") or shutil.which("codex") or ("codex.exe" if os.name == "nt" else "codex"))
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
JOBS_DIR = QUEUE_ROOT.path
SANDBOXES = {"read-only", "workspace-write"}

MAX_LIST_ENTRIES_HARD_LIMIT = 5000
MAX_READ_LINES_PER_CALL = 2000
MAX_READ_CHARS = 200_000
BINARY_SNIFF_BYTES = 8192
GIT_DIFF_MAX_CHARS = 50_000
TEXT_FILE_EXTENSIONS = {
    ".py", ".txt", ".md", ".rst", ".json", ".jsonc", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".csv", ".tsv", ".log", ".xml", ".html", ".htm",
    ".css", ".scss", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue",
    ".java", ".kt", ".kts", ".gradle", ".c", ".h", ".cpp", ".hpp", ".cc",
    ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".bash", ".ps1", ".psm1",
    ".bat", ".cmd", ".sql", ".proto", ".graphql", ".dockerfile",
    ".gitignore", ".gitattributes", ".editorconfig", ".env", ".lock",
}
BINARY_FILE_EXTENSIONS = {
    ".exe", ".dll", ".so", ".dylib", ".pyd", ".pyc", ".class", ".jar",
    ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".xz", ".png", ".jpg",
    ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf", ".doc", ".docx",
    ".xls", ".xlsx", ".ppt", ".pptx", ".sqlite", ".db", ".woff", ".woff2",
    ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".avi", ".mov", ".wav",
}

mcp = FastMCP("Harness Harbor")


def _resolve_existing_path(path: str) -> Path:
    """Resolve a user-supplied path, raising ValueError for bad input."""
    if not path or not path.strip():
        raise ValueError("path must be a non-empty string")
    resolved = Path(path).expanduser().resolve()
    blocked_roots = (
        Path("C:\\Windows"),
        Path("C:\\Program Files"),
        Path("C:\\Program Files (x86)"),
    )
    for root in blocked_roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        raise ValueError(f"Access to system directory is not allowed: {root}")
    return resolved


def _is_probably_text(path: Path) -> bool:
    """Heuristic: known text extensions are text; otherwise sniff for NUL bytes."""
    suffix = path.suffix.lower()
    if suffix in BINARY_FILE_EXTENSIONS:
        return False
    if suffix in TEXT_FILE_EXTENSIONS or suffix == "":
        pass
    try:
        with path.open("rb") as fh:
            chunk = fh.read(BINARY_SNIFF_BYTES)
    except OSError:
        return False
    return b"\x00" not in chunk


def _read_git_output(args: list[str], cwd: Path, timeout: int = 30) -> tuple[bool, str]:
    """Deprecated: unsafe Git helper, retained only for backward source compat.

    All callers should use ``control_plane._run_git`` (which is safe by
    construction) directly. This stub intentionally raises so any leftover
    use surfaces during import-time smoke checks rather than at runtime.
    """
    raise RuntimeError(
        "_read_git_output is unsafe and has been retired; use control_plane._run_git"
    )


@mcp.tool()
async def codex_status() -> dict:
    """Inspect the local Codex CLI installation and configuration status.

    This tool does not execute a Codex task.
    Use it only for health checks, diagnostics, or confirming which Codex CLI/configuration is available.
    """
    if not CODEX_EXE.is_file():
        return {
            "ok": False,
            "error": "Codex executable not found",
            "codex_exe": str(CODEX_EXE),
        }

    def _run() -> "subprocess.CompletedProcess":
        return run_safe_subprocess(
            [str(CODEX_EXE), "--version"],
            cwd=None,
            env=None,
            timeout=15.0,
        )

    result = await asyncio.to_thread(_run)
    return {
        "ok": result.returncode == 0,
        "codex_exe": str(CODEX_EXE),
        "version": (result.stdout or "").strip(),
        "config_path": str(CODEX_CONFIG),
        "config_exists": CODEX_CONFIG.is_file(),
    }


@mcp.tool()
async def agy_status() -> dict:
    """Inspect the local Antigravity (agy) CLI installation and capability status.

    This tool does not execute an agy task. It probes ``agy --version``,
    ``agy --help``, and ``agy models`` via the same safe subprocess
    contract used for the Codex and MiniMax harnesses.

    The returned record has the same shape as the other harness status
    tools (see ``harness_list`` for the full harness registry).
    """
    return await asyncio.to_thread(control_harness_status, "agy")


@mcp.tool()
async def codex_run(
    prompt: str,
    cwd: str,
    model: str | None = None,
    sandbox: Literal["read-only", "workspace-write"] = "workspace-write",
    reasoning_effort: str | None = None,
    route: Literal["current", "official", "custom", "official_then_custom"] = "current",
) -> dict:
    """Run a Codex task synchronously and wait for the final result.

    Use ONLY for very short tasks expected to finish quickly.

    Do NOT use this as a substitute for codex_start.

    For normal coding, repository analysis, debugging, edits, research, or any task that may take more than a few seconds, prefer codex_start followed by codex_poll. Long synchronous calls may exceed a remote tunnel transport response deadline and fail with a timeout or 502 error.
    """
    workdir = Path(cwd).expanduser().resolve()

    if not CODEX_EXE.is_file():
        return {"ok": False, "error": f"Codex executable not found: {CODEX_EXE}"}

    if not workdir.is_dir():
        return {"ok": False, "error": f"Working directory not found: {workdir}"}

    fd, output_path = tempfile.mkstemp(prefix="codex-mcp-", suffix=".txt")
    os.close(fd)

    route_state = {
        "cwd": str(workdir), "sandbox": sandbox, "model": model,
        "reasoning_effort": reasoning_effort, "prompt": prompt,
    }

    try:
        validate_codex_route(route)
        result = None
        for index, attempt_route in enumerate(codex_route_attempts(route)):
            cmd = build_codex_command(route_state, Path(output_path), route=attempt_route)
            child_env = codex_process_environment(attempt_route)
            result = await asyncio.to_thread(
                lambda: run_safe_subprocess(cmd, cwd=None, env=child_env, timeout=600.0)
            )
            classification = classify_codex_route_failure(result)
            if not (
                route == "official_then_custom"
                and index == 0
                and classification in CODEX_ROUTE_FAILURES
            ):
                break
        assert result is not None

        final_message = ""
        try:
            final_message = Path(output_path).read_text(
                encoding="utf-8",
                errors="replace",
            ).strip()
        except OSError:
            pass

        return {
            "ok": result.returncode == 0,
            "exit_code": result.returncode,
            "final_message": sanitize_codex_diagnostic(
                final_message,
                redact_values=codex_route_redaction_values(attempt_route),
            ),
            "stderr": sanitize_codex_diagnostic(
                result.stderr,
                redact_values=codex_route_redaction_values(attempt_route),
            ),
            "cwd": str(workdir),
        }

    except ValueError as exc:
        return {"ok": False, "error": str(exc), "cwd": str(workdir)}

    finally:
        try:
            Path(output_path).unlink(missing_ok=True)
        except OSError:
            pass


@mcp.tool()
async def codex_start(
    prompt: str,
    cwd: str,
    model: str | None = None,
    sandbox: Literal["read-only", "workspace-write"] = "workspace-write",
    reasoning_effort: str | None = None,
    route: Literal["current", "official", "custom", "official_then_custom"] = "current",
) -> dict:
    """Start a Codex task asynchronously.

    This compatibility tool retains the original Codex-only API. For project
    aliases or another harness, use task_start. Jobs created here are unified
    task records and are still read with codex_poll.
    """
    return await asyncio.to_thread(
        start_task,
        harness="codex",
        prompt=prompt,
        project=None,
        cwd=cwd,
        model=model,
        sandbox=sandbox,
        reasoning_effort=reasoning_effort,
        route=route,
    )


@mcp.tool()
def codex_poll(job_id: str, immediate: bool = False) -> dict:
    """Check the status and result of a Codex job previously created by codex_start.

    If status is "queued" or "running", the job is still executing.
    By default, polling queued/running jobs is rate-limited to at most once every 10 minutes per job_id.
    Within the 10-minute cooldown window, normal polls return a cached status snapshot without reading disk.

    Only pass immediate=True when the current user prompt explicitly and unambiguously requests an immediate
    status check for this specific job. Supervisors, cron/scheduled tasks, loops, or autonomous model decisions
    MUST NOT set immediate=True to bypass the cooldown.
    When receiving poll_throttled: true or lock_busy: true, automated Supervisors and agents MUST NOT call poll again
    before the returned retry_after_seconds or next_allowed_at.

    If status is "completed", return the final Codex message.
    If status is "failed", return the failure information.
    Terminal jobs (completed/failed/cancelled) are never throttled.

    Do not start a new Codex job just because an existing job is still queued or running.
    """
    return poll_task(job_id, immediate=immediate, jobs_dir=JOBS_DIR)


@mcp.tool()
def list_directory(
    path: str,
    recursive: bool = False,
    max_entries: int = 200,
) -> dict:
    """List the contents of a directory on this Windows machine.

    READ-ONLY inspection tool. Use it to explore the workspace (see which
    files and folders exist, their sizes and modification times) without
    starting a Codex task.

    Args:
        path: Absolute or relative directory path to list.
        recursive: If true, walk subdirectories recursively. Prefer false
            for a quick overview; use true only when you need deep structure.
        max_entries: Maximum number of entries to return (default 200,
            hard cap 5000). Results are marked "truncated": true when more
            entries exist.

    Returns {"ok": true, "path", "entries", "truncated"} where each entry
    has name, type ("file"|"directory"), size and modified_time for files.
    On failure returns {"ok": false, "error"}.
    """
    try:
        target = _resolve_existing_path(path)
        if not target.is_dir():
            return {"ok": False, "error": f"Not a directory or not found: {target}"}

        max_entries = min(int(max_entries), MAX_LIST_ENTRIES_HARD_LIMIT)
        if max_entries <= 0:
            max_entries = 200

        entries: list[dict] = []
        truncated = False

        def _add_entry(p: Path) -> bool:
            nonlocal truncated
            if len(entries) >= max_entries:
                truncated = True
                return False
            try:
                is_dir = p.is_dir()
                entry: dict = {"name": str(p), "type": "directory" if is_dir else "file"}
                if not is_dir:
                    stat = p.stat()
                    entry["size"] = stat.st_size
                    entry["modified_time"] = time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)
                    )
                else:
                    entry["modified_time"] = time.strftime(
                        "%Y-%m-%d %H:%M:%S",
                        time.localtime(p.stat().st_mtime),
                    )
                entries.append(entry)
                return True
            except OSError as exc:
                entries.append({"name": str(p), "type": "unknown", "error": str(exc)})
                return True

        if recursive:
            for root_str, dir_names, file_names in os.walk(target):
                root = Path(root_str)
                stop = False
                for d in sorted(dir_names):
                    if not _add_entry(root / d):
                        stop = True
                        break
                if stop:
                    break
                for f in sorted(file_names):
                    if not _add_entry(root / f):
                        stop = True
                        break
                if stop:
                    break
        else:
            try:
                children = sorted(target.iterdir(), key=lambda p: p.name)
            except OSError as exc:
                return {"ok": False, "error": f"Cannot list directory: {exc}"}
            for child in children:
                if not _add_entry(child):
                    break

        return {
            "ok": True,
            "path": str(target),
            "entries": entries,
            "truncated": truncated,
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except PermissionError:
        return {"ok": False, "error": f"Permission denied: {path}"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def read_file(
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
) -> dict:
    """Read the text content of a file from this Windows workspace.

    READ-ONLY inspection tool. Use it to inspect source code, config files,
    logs or documents directly instead of invoking Codex for pure reading.

    Supports reading large files in segments via line ranges. A single call
    reads at most 2000 lines / 200000 characters; if the requested range is
    larger the response is truncated and includes total_lines so you can
    request the next segment.

    Binary files (images, archives, executables...) are rejected with an
    error telling you to use another approach.

    Args:
        path: File path to read.
        start_line: First line to read (1-based, default 1).
        end_line: Last line to read inclusive (1-based); omit/None to read
            to the end of the file (subject to per-call limits).

    Returns {"ok": true, "path", "content", "start_line", "end_line",
    "total_lines", "truncated"} or {"ok": false, "error"}.
    """
    try:
        target = _resolve_existing_path(path)
        if not target.is_file():
            return {"ok": False, "error": f"File not found: {target}"}
        if start_line < 1:
            return {"ok": False, "error": "start_line must be >= 1"}
        if end_line is not None and end_line < start_line:
            return {"ok": False, "error": "end_line must be >= start_line"}

        if not _is_probably_text(target):
            return {
                "ok": False,
                "error": (
                    f"File appears to be binary: {target}. "
                    "This tool only reads text files."
                ),
            }

        with target.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()

        total_lines = len(lines)
        requested_end = end_line if end_line is not None else total_lines
        effective_end = min(
            requested_end,
            total_lines,
            start_line + MAX_READ_LINES_PER_CALL - 1,
        )
        effective_start = start_line

        selected = lines[effective_start - 1 : effective_end]
        truncated_by_chars = False
        content_parts: list[str] = []
        char_count = 0
        kept_lines = 0
        for line in selected:
            char_count += len(line)
            if char_count > MAX_READ_CHARS:
                truncated_by_chars = True
                break
            content_parts.append(line)
            kept_lines += 1

        content = "".join(content_parts)
        actual_end = effective_start - 1 + kept_lines
        truncated = truncated_by_chars or actual_end < effective_end

        return {
            "ok": True,
            "path": str(target),
            "content": content,
            "start_line": effective_start,
            "end_line": actual_end,
            "total_lines": total_lines,
            "truncated": truncated,
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except PermissionError:
        return {"ok": False, "error": f"Permission denied: {path}"}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def search_files(
    pattern: str,
    path: str,
    glob: str = "*",
    max_results: int = 100,
) -> dict:
    """Search for a text pattern inside files under a directory.

    READ-ONLY inspection tool. Use it to find where a function, class,
    string or configuration value appears in the Windows workspace without
    invoking Codex. Equivalent to grep over text files.

    Only text files are searched (binary files are skipped automatically).
    Matching is case-insensitive regular expression; invalid regex falls
    back to literal substring matching.

    Args:
        pattern: Text or regex to search for (case-insensitive).
        path: Root directory to search in.
        glob: Filename filter, e.g. "*.py", "*.ts". Default "*" matches all.
        max_results: Maximum number of matches returned (default 100).
            Response includes "truncated": true when there were more.

    Each result contains file, line_number and a trimmed line excerpt.
    Returns {"ok": true, "matches", "files_searched", "truncated"} or
    {"ok": false, "error"}.
    """
    try:
        target = _resolve_existing_path(path)
        if not target.is_dir():
            return {"ok": False, "error": f"Not a directory or not found: {target}"}
        if not pattern:
            return {"ok": False, "error": "pattern must be a non-empty string"}

        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(pattern), re.IGNORECASE)

        max_results = max(1, min(int(max_results), 1000))
        matches: list[dict] = []
        files_searched = 0
        truncated = False

        for root_str, dir_names, file_names in os.walk(target):
            dir_names[:] = [
                d for d in dir_names
                if d not in {
                    ".git", ".hg", ".svn", "__pycache__", "node_modules",
                    ".venv", ".venv-legacy", "venv", "dist", "build",
                }
            ]
            root = Path(root_str)
            for file_name in sorted(file_names):
                if glob != "*" and not fnmatch.fnmatch(file_name.lower(), glob.lower()):
                    continue
                file_path = root / file_name
                if not _is_probably_text(file_path):
                    continue
                try:
                    with file_path.open("r", encoding="utf-8", errors="replace") as fh:
                        for line_no, line in enumerate(fh, start=1):
                            if regex.search(line):
                                matches.append({
                                    "file": str(file_path),
                                    "line_number": line_no,
                                    "excerpt": line.strip()[:300],
                                })
                                if len(matches) > max_results:
                                    matches.pop()
                                    truncated = True
                                    raise StopIteration
                except StopIteration:
                    break
                except OSError:
                    continue
                finally:
                    files_searched += 1
            if truncated:
                break

        return {
            "ok": True,
            "pattern": pattern,
            "root": str(target),
            "matches": matches,
            "files_searched": files_searched,
            "truncated": truncated,
        }
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
async def git_status(path: str) -> dict:
    """Read Git status for a repository or directory inside it without modifying it.

    Offloaded to a worker thread so a stuck ``git.exe`` cannot block the
    FastMCP event loop or wedge the MCP stdio transport.
    """
    result = await asyncio.to_thread(git_status_result, path)
    if not result.get("ok"):
        return result
    changes = []
    for line in result.get("stdout", "").splitlines()[1:]:
        if not line:
            continue
        changes.append({"status": line[:2], "file": line[3:] if len(line) > 3 else ""})
    return {**result, "branch": result.get("branch", ""), "changes": changes}


@mcp.tool()
async def git_diff(
    path: str,
    staged: bool = False,
    paths: list[str] | None = None,
) -> dict:
    """Read unstaged or staged Git diff; optional paths must be repo-relative.

    Offloaded to a worker thread.
    """
    result = await asyncio.to_thread(git_diff_result, path, staged=staged, paths=paths)
    if not result.get("ok"):
        return result
    diff = result.get("stdout", "")
    return {**result, "diff": diff, "empty": not bool(diff.strip())}


@mcp.tool()
async def harness_list() -> dict:
    """List installed task harnesses and their verified local capabilities.

    Triggers MiniMax CLI capability probes (subprocess); offloaded.
    """
    return await asyncio.to_thread(lambda: {"ok": True, "harnesses": harnesses()})


@mcp.tool()
async def harness_status(harness: str) -> dict:
    """Inspect one harness; MiniMax remains unavailable unless a real CLI is installed.

    Triggers MiniMax CLI capability probes; offloaded.
    """
    return await asyncio.to_thread(control_harness_status, harness)


@mcp.tool()
async def harness_telemetry(force_refresh: bool = False) -> dict:
    """Return bounded read-only harness status, queue, process, and quota telemetry."""
    return await asyncio.to_thread(harness_telemetry_snapshot, force_refresh=force_refresh)


@mcp.tool()
def project_list() -> dict:
    """List aliases from .control/projects.json. This is an editable JSON registry, not a database."""
    try:
        return {"ok": True, "projects": list_projects()}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def project_resolve(project: str) -> dict:
    """Resolve one project alias to its canonical existing working directory."""
    try:
        return {"ok": True, "project": project, "cwd": str(resolve_project(project))}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
async def task_start(
    harness: Literal["codex", "minimax", "agy"],
    prompt: str,
    project: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
    sandbox: Literal["read-only", "workspace-write"] = "workspace-write",
    reasoning_effort: str | None = None,
    route: Literal["current", "official", "custom", "official_then_custom"] = "current",
) -> dict:
    """Queue a unified async task using exactly one project alias or explicit cwd.

    Harness/model choice remains caller-controlled. MiniMax uses its verified
    headless `mcode exec` interface. Codex supports the four public route
    values; MiniMax and AGY accept only `current`.

    Offloaded to a worker thread because it may run a MiniMax capability probe.
    """
    return await asyncio.to_thread(
        start_task,
        harness=harness,
        prompt=prompt,
        project=project,
        cwd=cwd,
        model=model,
        sandbox=sandbox,
        reasoning_effort=reasoning_effort,
        route=route,
    )


@mcp.tool()
def task_poll(job_id: str, immediate: bool = False) -> dict:
    """Read a unified task record. Poll a queued/running task with the same job_id.

    By default, polling non-terminal jobs (queued/running) is rate-limited to at most once every 10 minutes per job_id.
    Within the 10-minute cooldown window, normal polls return a cached status snapshot without reading disk.

    Only pass immediate=True when the current user prompt explicitly and unambiguously requests an immediate
    status check for this specific job. Supervisors, cron/scheduled tasks, loops, or autonomous model decisions
    MUST NOT set immediate=True to bypass the cooldown.
    When receiving poll_throttled: true or lock_busy: true, automated Supervisors and agents MUST NOT call poll again
    before the returned retry_after_seconds or next_allowed_at.

    Terminal jobs (completed/failed/cancelled) are never throttled.
    """
    return poll_task(job_id, immediate=immediate, jobs_dir=JOBS_DIR)


@mcp.tool()
def task_cancel(job_id: str) -> dict:
    """Cancel only an unclaimed queued task; running tasks are intentionally not killed."""
    return cancel_task(job_id)


@mcp.tool()
def file_read(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    max_bytes: int = 200000,
) -> dict:
    """Read a UTF-8 text file. Device/UNC paths and probable binary files are refused."""
    return file_read_result(path, start_line=start_line, end_line=end_line, max_bytes=max_bytes)


@mcp.tool()
def file_stat(path: str) -> dict:
    """Return safe metadata for one existing local file or directory."""
    return file_stat_result(path)


@mcp.tool()
def directory_list(path: str, max_entries: int = 200) -> dict:
    """List one directory without recursive shell access."""
    return directory_list_result(path, max_entries=max_entries)


@mcp.tool()
def file_write(path: str, content: str, overwrite: bool = False, backup: bool = True) -> dict:
    """Atomically write a whole text file in an authorized root; replacement requires overwrite=true."""
    return file_write_result(path, content, overwrite=overwrite, backup=backup)


@mcp.tool()
def file_append(path: str, content: str, create: bool = False, backup: bool = True) -> dict:
    """Append text by atomic replacement; creating a missing file requires create=true."""
    return file_append_result(path, content, create=create, backup=backup)


@mcp.tool()
async def git_branch(repo: str) -> dict:
    """Read the current branch of a repository. Offloaded to a worker thread."""
    return await asyncio.to_thread(git_branch_result, repo)


@mcp.tool()
async def git_log(repo: str, count: int = 10) -> dict:
    """Read a bounded recent Git log (1-50 commits). Offloaded to a worker thread."""
    return await asyncio.to_thread(git_log_result, repo, count=count)


@mcp.tool()
async def git_worktree_list(repo: str) -> dict:
    """Read Git worktree metadata without modifying it. Offloaded to a worker thread."""
    return await asyncio.to_thread(git_worktree_list_result, repo)


@mcp.tool()
async def git_rev_parse(repo: str) -> dict:
    """Read repository root and current HEAD object id. Offloaded to a worker thread."""
    return await asyncio.to_thread(git_rev_parse_result, repo)


@mcp.tool()
async def git_add(repo: str, paths: list[str]) -> dict:
    """Stage explicit repo-relative paths only. It cannot reset, clean, or push.

    Offloaded to a worker thread.
    """
    return await asyncio.to_thread(git_add_result, repo, paths)


@mcp.tool()
async def git_commit(repo: str, message: str) -> dict:
    """Commit staged changes only when no unstaged or untracked changes remain.

    Offloaded to a worker thread.
    """
    return await asyncio.to_thread(git_commit_result, repo, message)


@mcp.tool()
async def git_ls_remote(repo: str, remote: str, ref: str) -> dict:
    """Read one existing branch ref from a configured HTTPS remote."""
    return await asyncio.to_thread(git_ls_remote_result, repo, remote, ref)


@mcp.tool()
async def git_push_dry_run(repo: str, remote: str, src_ref: str, dst_ref: str, expected_remote_head: str) -> dict:
    """Dry-run exactly one non-force existing-branch update with an exact-head precondition."""
    return await asyncio.to_thread(
        git_push_dry_run_result, repo, remote, src_ref, dst_ref, expected_remote_head,
    )


@mcp.tool()
async def git_push_ref(repo: str, remote: str, src_ref: str, dst_ref: str, expected_remote_head: str) -> dict:
    """Push one non-force branch update after dry-run, drift checks, and verification."""
    return await asyncio.to_thread(
        git_push_ref_result, repo, remote, src_ref, dst_ref, expected_remote_head,
    )


@mcp.tool()
async def port_listeners(port: int, protocol: Literal["tcp", "udp"] = "tcp") -> dict:
    """Read current listeners and owning PIDs for one TCP or UDP port."""
    return await asyncio.to_thread(diag_port_listeners, port, protocol)


@mcp.tool()
async def process_inspect(pid: int) -> dict:
    """Read safe metadata for one process by PID; no process mutation is performed."""
    return await asyncio.to_thread(diag_process_inspect, pid)


@mcp.tool()
async def http_probe(url: str, method: Literal["HEAD", "GET"] = "HEAD", timeout_seconds: int = 5) -> dict:
    """Perform a bounded, SSRF-restricted read-only HTTP probe."""
    return await asyncio.to_thread(diag_http_probe, url, method=method, timeout_seconds=timeout_seconds)


@mcp.tool()
async def tls_inspect(host: str, port: int = 443, timeout_seconds: int = 5) -> dict:
    """Inspect TLS certificate metadata for an approved local/LAN target."""
    return await asyncio.to_thread(diag_tls_inspect, host, port=port, timeout_seconds=timeout_seconds)


@mcp.tool()
async def firewall_query(port: int | None = None, protocol: Literal["tcp", "udp"] | None = None, executable: str | None = None) -> dict:
    """Read-only query of matching Windows Defender Firewall rules."""
    return await asyncio.to_thread(diag_firewall_query, port=port, protocol=protocol, executable=executable)


@mcp.tool()
async def network_interfaces() -> dict:
    """Read local network interface metadata."""
    return await asyncio.to_thread(diag_network_interfaces)


@mcp.tool()
async def tcp_connect_probe(host: str, port: int, timeout_seconds: int = 3) -> dict:
    """Test TCP connectivity without sending application data."""
    return await asyncio.to_thread(diag_tcp_connect_probe, host, port, timeout_seconds=timeout_seconds)


@mcp.tool()
async def dns_resolve(hostname: str) -> dict:
    """Resolve a hostname to A/AAAA records with bounded timeout."""
    return await asyncio.to_thread(diag_dns_resolve, hostname)


if __name__ == "__main__":
    mcp.run()
