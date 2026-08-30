import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP


CODEX_EXE = Path(os.environ.get("HARBOR_CODEX_EXE") or shutil.which("codex") or ("codex.exe" if os.name == "nt" else "codex"))
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"

mcp = FastMCP("ChatGPT Harbor")


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
) -> dict:
    """Run Codex non-interactively in a specific working directory."""
    workdir = Path(cwd).expanduser().resolve()

    if not CODEX_EXE.is_file():
        return {"ok": False, "error": f"Codex executable not found: {CODEX_EXE}"}

    if not workdir.is_dir():
        return {"ok": False, "error": f"Working directory not found: {workdir}"}

    fd, output_path = tempfile.mkstemp(prefix="codex-mcp-", suffix=".txt")
    os.close(fd)

    cmd = [
        str(CODEX_EXE),
        "exec",
        "--color",
        "never",
        "--sandbox",
        sandbox,
        "-C",
        str(workdir),
        "-o",
        output_path,
    ]

    if model:
        cmd.extend(["--model", model])

    cmd.append(prompt)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )

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
            "final_message": final_message,
            "stderr": result.stderr[-4000:].strip(),
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
