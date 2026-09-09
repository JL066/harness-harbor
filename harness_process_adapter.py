"""Platform-specific, read-only process observations for harness telemetry.

The telemetry domain consumes only the small ``observe`` result shape below.
Keeping the Windows/CIM command here prevents OS process discovery from
leaking into its snapshot/cache logic (and keeps command lines out of all
telemetry results).
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol


class ProcessActivityAdapter(Protocol):
    """Read-only process observation boundary used by harness telemetry."""

    def observe(self, harnesses: Sequence[str]) -> Mapping[str, Mapping[str, Any]]: ...


class UnavailableProcessActivityAdapter:
    """Portable fail-soft adapter used when a platform probe is unavailable."""

    def observe(self, harnesses: Sequence[str]) -> Mapping[str, Mapping[str, Any]]:
        return {
            name: {
                "process_count": 0,
                "source": "process adapter unavailable",
                "error": "platform process discovery is unavailable",
            }
            for name in harnesses
        }


class WindowsProcessActivityAdapter:
    """Narrow Windows process observer.

    Command lines are used only transiently to identify the MiniMax CLI; they
    are never returned, logged, or included in any error.  This is deliberately
    an observation-only command with no process-control capability.
    """

    _SCRIPT = (
        "$ErrorActionPreference='Stop'; "
        "Get-CimInstance Win32_Process -Filter \"Name='codex.exe' OR Name='agy.exe' OR Name='node.exe'\" | "
        "Select-Object Name,CommandLine | ConvertTo-Json -Compress"
    )

    def __init__(self, runner: Callable[..., subprocess.CompletedProcess]) -> None:
        self._runner = runner

    def observe(self, harnesses: Sequence[str]) -> Mapping[str, Mapping[str, Any]]:
        if sys.platform != "win32":
            return UnavailableProcessActivityAdapter().observe(harnesses)
        try:
            result = self._runner(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", self._SCRIPT],
                timeout=5,
            )
            if result.returncode != 0:
                raise OSError("process query failed")
            rows = json.loads(result.stdout or "[]")
        except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
            return {
                name: {
                    "process_count": 0,
                    "source": "Windows process adapter",
                    "error": "Windows process discovery failed",
                }
                for name in harnesses
            }
        if isinstance(rows, Mapping):
            rows = [rows]
        counts = {name: 0 for name in harnesses}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, Mapping):
                continue
            image = str(row.get("Name") or "").lower()
            command = str(row.get("CommandLine") or "").lower().replace("/", "\\")
            if image == "codex.exe" and " app-server " not in f" {command} ":
                counts["codex"] = counts.get("codex", 0) + 1
            elif image == "agy.exe":
                counts["agy"] = counts.get("agy", 0) + 1
            elif image == "node.exe" and (
                "@minimax-ai\\code\\cli.js" in command or ".minimax-code\\node_modules" in command
            ):
                counts["minimax"] = counts.get("minimax", 0) + 1
        return {
            name: {"process_count": counts.get(name, 0), "source": "Windows process adapter", "error": None}
            for name in harnesses
        }


def default_process_activity_adapter(
    runner: Callable[..., subprocess.CompletedProcess],
) -> ProcessActivityAdapter:
    return WindowsProcessActivityAdapter(runner) if sys.platform == "win32" else UnavailableProcessActivityAdapter()


__all__ = [
    "ProcessActivityAdapter",
    "UnavailableProcessActivityAdapter",
    "WindowsProcessActivityAdapter",
    "default_process_activity_adapter",
]
