"""Windows owned trees use Job Objects, including after the leader exits.

Create suspended, assign the job, then resume: descendants cannot escape the
ownership boundary during Python/CLI startup. No executable-name kill discovery.
"""
import ctypes
from ctypes import wintypes as w
import subprocess
import time
import weakref


def api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcess": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        "CloseHandle": ([w.HANDLE], w.BOOL),
        "GetExitCodeProcess": ([w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
        "GetProcessTimes": ([w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4, w.BOOL),
        "CreateJobObjectW": ([w.LPVOID, w.LPCWSTR], w.HANDLE),
        "OpenJobObjectW": ([w.DWORD, w.BOOL, w.LPCWSTR], w.HANDLE),
        "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
        "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
        "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, w.LPVOID], w.BOOL),
        "CreateToolhelp32Snapshot": ([w.DWORD, w.DWORD], w.HANDLE),
        "Thread32First": ([w.HANDLE, w.LPVOID], w.BOOL),
        "Thread32Next": ([w.HANDLE, w.LPVOID], w.BOOL),
        "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        "ResumeThread": ([w.HANDLE], w.DWORD),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel


def is_alive(pid):
    kernel = api()
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() != 87  # access denied/observation failure is not death
    try:
        code = w.DWORD()
        return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
    finally:
        kernel.CloseHandle(handle)


def process_identity(pid):
    kernel = api()
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        times = [w.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        started = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return {"pid": pid, "started_at": str(started), "job_name": f"Local\\HarnessHarbor-{pid}-{started}"}
    finally:
        kernel.CloseHandle(handle)


class ThreadEntry(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("thread", w.DWORD),
                ("owner", w.DWORD), ("base", w.LONG), ("delta", w.LONG), ("flags", w.DWORD)]


def spawn_owned(argv, *, popen_factory=None, **kwargs):
    kernel = api()
    kwargs.pop("start_new_session", None)
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x4 | 0x08000200
    proc = (popen_factory or subprocess.Popen)(argv, **kwargs)
    job = None
    assigned = False
    try:
        identity = process_identity(proc.pid)
        if identity is None:
            raise OSError("Could not establish process identity")
        ctypes.set_last_error(0)
        job = kernel.CreateJobObjectW(None, identity["job_name"])
        if not job or ctypes.get_last_error() == 183:
            raise OSError("Process ownership name is already in use")
        if not kernel.AssignProcessToJobObject(job, int(proc._handle)):
            raise OSError("Could not establish process tree ownership")
        assigned = True
        snapshot = kernel.CreateToolhelp32Snapshot(4, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise OSError("Could not inspect suspended process")
        resumed = False
        try:
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            more = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.owner == proc.pid:
                    thread = kernel.OpenThread(2, False, entry.thread)
                    if not thread:
                        raise OSError("Could not resume owned process")
                    try:
                        if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise OSError("Could not resume owned process")
                        resumed = True
                    finally:
                        kernel.CloseHandle(thread)
                more = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        finally:
            kernel.CloseHandle(snapshot)
        if not resumed:
            raise OSError("Owned process has no suspended thread")
        proc._harbor_identity = identity
        # Keep the named job open for the retained Popen lifetime, including after
        # its leader exits. Closing the last handle loses name-based observation.
        proc._harbor_job_finalizer = weakref.finalize(proc, kernel.CloseHandle, job)
        job = None  # Ownership transferred; failure cleanup must not close it.
        return proc
    except BaseException:
        if assigned:
            kernel.TerminateJobObject(job, 1)
        proc.kill()
        proc.wait(timeout=5)
        raise
    finally:
        if job:
            kernel.CloseHandle(job)


def descendants(identity):
    """Return all job member PIDs; None means observation failed."""
    if not isinstance(identity, dict) or type(identity.get("pid")) is not int or not str(identity.get("started_at", "")).isdigit():
        return None
    if identity.get("job_name") != f"Local\\HarnessHarbor-{identity['pid']}-{identity['started_at']}":
        return None
    kernel = api()
    handle = kernel.OpenJobObjectW(4, False, identity["job_name"])
    if not handle:
        return None  # A missing name is not proof that every descendant exited.
    try:
        # ponytail: bound ownership observations to 4096 processes; fail closed above it.
        class Members(ctypes.Structure):
            _fields_ = [("assigned", w.DWORD), ("count", w.DWORD), ("pids", ctypes.c_size_t * 4096)]
        members = Members()
        if not kernel.QueryInformationJobObject(handle, 3, ctypes.byref(members), ctypes.sizeof(members), None):
            return None
        if members.assigned > members.count or members.count > 4096:
            return None
        return list(members.pids[:members.count])
    finally:
        kernel.CloseHandle(handle)


def terminate_owned_tree(identity, grace):
    members = descendants(identity)
    if members == []:
        return True
    if members is None:
        return False
    kernel = api()
    handle = kernel.OpenJobObjectW(8, False, identity["job_name"])
    if not handle:
        return descendants(identity) == []
    try:
        if not kernel.TerminateJobObject(handle, 1):
            return False
        end = time.monotonic() + grace
        while time.monotonic() < end:
            if descendants(identity) == []:
                return True
            time.sleep(0.025)
        return descendants(identity) == []
    finally:
        kernel.CloseHandle(handle)
