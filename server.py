import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

from control_plane import (
    CODEX_ROUTE_FAILURES,
    build_codex_command,
    classify_codex_route_failure,
    codex_route_attempts,
    codex_process_environment,
    codex_route_redaction_values,
    sanitize_codex_diagnostic,
    validate_codex_route,
)


CODEX_EXE = Path(os.environ.get("HARBOR_CODEX_EXE") or shutil.which("codex") or ("codex.exe" if os.name == "nt" else "codex"))
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"

mcp = FastMCP("Harness Harbor")


@mcp.tool()
def codex_status() -> dict:
    """Check the exact Codex CLI used by this MCP server."""
    if not CODEX_EXE.is_file():
        return {
            "ok": False,
            "error": "Codex executable not found",
            "codex_exe": str(CODEX_EXE),
        }

    try:
        result = subprocess.run(
            [str(CODEX_EXE), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "codex_exe": str(CODEX_EXE),
        }

    return {
        "ok": result.returncode == 0,
        "codex_exe": str(CODEX_EXE),
        "version": result.stdout.strip(),
        "config_path": str(CODEX_CONFIG),
        "config_exists": CODEX_CONFIG.is_file(),
    }


@mcp.tool()
def codex_run(
    prompt: str,
    cwd: str,
    model: str | None = None,
    sandbox: Literal["read-only", "workspace-write"] = "workspace-write",
    route: Literal["current", "official", "custom", "official_then_custom"] = "current",
) -> dict:
    """Run Codex non-interactively in a specific working directory."""
    workdir = Path(cwd).expanduser().resolve()

    if not CODEX_EXE.is_file():
        return {"ok": False, "error": f"Codex executable not found: {CODEX_EXE}"}

    if not workdir.is_dir():
        return {"ok": False, "error": f"Working directory not found: {workdir}"}

    fd, output_path = tempfile.mkstemp(prefix="codex-mcp-", suffix=".txt")
    os.close(fd)

    route_state = {
        "cwd": str(workdir), "sandbox": sandbox, "model": model,
        "prompt": prompt,
    }

    try:
        validate_codex_route(route)
        result = None
        for index, attempt_route in enumerate(codex_route_attempts(route)):
            cmd = build_codex_command(route_state, Path(output_path), route=attempt_route)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
                env=codex_process_environment(attempt_route),
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
        return {
            "ok": False,
            "error": str(exc),
            "cwd": str(workdir),
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "Codex timed out after 600 seconds",
            "cwd": str(workdir),
        }

    finally:
        try:
            Path(output_path).unlink(missing_ok=True)
        except OSError:
            pass

if __name__ == "__main__":
    mcp.run()
