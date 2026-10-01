"""Conservative process-liveness probes shared by Harbor recovery guards."""

from __future__ import annotations

import ctypes
from enum import Enum
import errno
import json
import os
from pathlib import Path
import sys


class ProcessLiveness(str, Enum):
    """Result of a process probe where observation failure is explicit."""

    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


def probe_pid_liveness(pid: int) -> ProcessLiveness:
    """Return alive/dead only when the operating system proves that state."""
    if type(pid) is not int or pid <= 1:
        return ProcessLiveness.UNKNOWN

    if sys.platform == "win32":
        try:
            kernel = ctypes.windll.kernel32
            kernel.SetLastError(0)
            handle = kernel.OpenProcess(0x1000 | 0x00100000, False, pid)
            if not handle:
                error = ctypes.get_last_error() or kernel.GetLastError()
                if error == 87:  # ERROR_INVALID_PARAMETER: PID does not exist.
                    return ProcessLiveness.DEAD
                return ProcessLiveness.UNKNOWN

            try:
                exit_code = ctypes.c_ulong()
                if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return ProcessLiveness.UNKNOWN
                return ProcessLiveness.ALIVE if exit_code.value == 259 else ProcessLiveness.DEAD
            finally:
                kernel.CloseHandle(handle)
        except Exception:
            return _probe_with_signal(pid)

    return _probe_with_signal(pid)


def _probe_with_signal(pid: int) -> ProcessLiveness:
    try:
        os.kill(pid, 0)
        return ProcessLiveness.ALIVE
    except ProcessLookupError:
        return ProcessLiveness.DEAD
    except PermissionError:
        return ProcessLiveness.ALIVE
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return ProcessLiveness.DEAD
        if exc.errno == errno.EPERM:
            return ProcessLiveness.ALIVE
        return ProcessLiveness.UNKNOWN


def probe_job_tree_liveness(job_dir: Path, owners: set[int] = ()) -> ProcessLiveness:
    """Probe persisted process identity and descendants without guessing on errors."""
    from harbor_platform.process import descendants, group_alive

    identity_path = Path(job_dir) / "worker.identity.json"
    if identity_path.exists():
        try:
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
            members = descendants(identity)
            if members is None:
                return ProcessLiveness.UNKNOWN
            return ProcessLiveness.ALIVE if members else ProcessLiveness.DEAD
        except (OSError, ValueError, TypeError):
            return ProcessLiveness.UNKNOWN

    if sys.platform == "win32":
        if not owners:
            return ProcessLiveness.UNKNOWN
        try:
            from ctypes import wintypes as w

            class PROCESSENTRY32(ctypes.Structure):
                _fields_ = [
                    ("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ProcessID", w.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", w.DWORD),
                    ("cntThreads", w.DWORD), ("th32ParentProcessID", w.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", w.DWORD),
                    ("szExeFile", ctypes.c_char * 260),
                ]

            kernel = ctypes.windll.kernel32
            handle = kernel.CreateToolhelp32Snapshot(2, 0)
            if handle == -1 or not handle:
                return ProcessLiveness.UNKNOWN
            entry = PROCESSENTRY32()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
            unknown_child = False
            try:
                if not kernel.Process32First(handle, ctypes.byref(entry)):
                    return ProcessLiveness.UNKNOWN
                while True:
                    if entry.th32ParentProcessID in owners:
                        child_state = probe_pid_liveness(entry.th32ProcessID)
                        if child_state is ProcessLiveness.ALIVE:
                            return ProcessLiveness.ALIVE
                        unknown_child = unknown_child or child_state is ProcessLiveness.UNKNOWN
                    if not kernel.Process32Next(handle, ctypes.byref(entry)):
                        break
            finally:
                kernel.CloseHandle(handle)
            return ProcessLiveness.UNKNOWN if unknown_child else ProcessLiveness.DEAD
        except Exception:
            return ProcessLiveness.UNKNOWN

    try:
        return (
            ProcessLiveness.ALIVE
            if any(group_alive(pid) for pid in owners if type(pid) is int and pid > 1)
            else ProcessLiveness.DEAD
        )
    except Exception:
        return ProcessLiveness.UNKNOWN
