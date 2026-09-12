"""POSIX process ownership. Never discover kill targets by executable name."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid


def process_identity(pid):
    if type(pid) is not int or pid <= 1:
        return None
    if sys.platform == "win32":
        from .windows_process import process_identity as identify
        return identify(pid)
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "pid=,pgid=,lstart=,command="],
                            capture_output=True, timeout=2, text=True)
    fields = result.stdout.strip().split(None, 7)
    if result.returncode or len(fields) < 8:
        return None
    return {"pid": pid, "pgid": int(fields[1]), "started_at": " ".join(fields[2:7]),
            "argv_hash": hashlib.sha256(fields[7].encode()).hexdigest()}


def settled_identity(pid):
    # macOS Python launchers can exec their framework binary after Popen returns.
    previous = process_identity(pid)
    for _ in range(10):
        time.sleep(0.02)
        current = process_identity(pid)
        if current == previous:
            return current
        previous = current
    return None


def is_alive(pid):
    if type(pid) is not int or pid <= 0:
        return False
    if sys.platform == "win32":
        from .windows_process import is_alive as alive
        return alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True  # Observation failure must not release ownership.


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin can report EPERM for an empty group during final reap. Query
        # membership before treating that as a live inaccessible process.
        result = subprocess.run(["/bin/ps", "-axo", "pgid=,stat="], capture_output=True, text=True, timeout=2)
        if result.returncode:
            return True  # observation failure is never proof of termination
        return any(len(parts := line.split()) == 2 and parts[0] == str(pgid) and not parts[1].startswith("Z")
                   for line in result.stdout.splitlines())


def terminate_tree(proc, grace=0.5):
    """Accept only a retained Popen handle for a session we started.

    A reaped leader may leave children in its group. The caller retains the
    handle and records isolation at spawn, so cleanup still targets that group.
    Recovery from disk must separately verify process_identity before calling.
    """
    if sys.platform == "win32":
        from .windows_process import terminate_owned_tree
        identity = getattr(proc, "_harbor_identity", None) or getattr(proc, "identity", None)
        gone = terminate_owned_tree(identity, grace)
        if gone:
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                return False
            registry = getattr(proc, "_harbor_registry", None)
            if isinstance(registry, Path):
                registry.unlink(missing_ok=True)
        return gone
    pid = proc.pid
    if type(pid) is not int or pid <= 1 or pid == os.getpgrp():
        return False
    try:
        isolated = os.getpgid(pid) == pid
    except ProcessLookupError:
        isolated = getattr(proc, "_harbor_pgid", None) == pid
    if not isolated:
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            if not group_alive(pid):
                break
            return False
        end = time.monotonic() + grace
        while time.monotonic() < end:
            proc.poll()
            if not group_alive(pid):
                break
            time.sleep(0.025)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        return False
    gone = not group_alive(pid)
    registry = getattr(proc, "_harbor_registry", None)
    if gone and isinstance(registry, Path):
        registry.unlink(missing_ok=True)
    return gone


def track(proc):
    if type(proc.pid) is int:
        if sys.platform != "win32":
            proc._harbor_pgid = proc.pid
        registry = os.environ.get("HARBOR_PROCESS_REGISTRY")
        if registry:
            identity = getattr(proc, "_harbor_identity", None) or settled_identity(proc.pid)
            if identity:
                root = Path(registry)
                if not root.is_absolute():
                    terminate_tree(proc)
                    raise ValueError("Process registry must be absolute")
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                target = root / f"{proc.pid}.json"
                temp = root / f".{uuid.uuid4().hex}.tmp"
                try:
                    with temp.open("x") as out:
                        os.chmod(temp, 0o600)
                        json.dump({"instance_id": os.environ.get("HARBOR_RUNTIME_INSTANCE_ID"), "identity": identity}, out)
                        out.flush()
                        os.fsync(out.fileno())
                    os.replace(temp, target)
                    proc._harbor_registry = target
                except BaseException:
                    terminate_tree(proc)
                    raise
    return proc


def spawn_owned(argv, *, popen_factory=None, **kwargs):
    if sys.platform == "win32":
        from .windows_process import spawn_owned as spawn
        return track(spawn(argv, popen_factory=popen_factory, **kwargs))
    kwargs["start_new_session"] = True
    return track((popen_factory or subprocess.Popen)(argv, **kwargs))


def descendants(identity):
    if not isinstance(identity, dict):
        return None
    if sys.platform == "win32":
        from .windows_process import descendants as observe
        return observe(identity)
    if not identity or type(identity.get("pgid")) is not int:
        return None
    result = subprocess.run(["/bin/ps", "-axo", "pid=,pgid=,stat="], capture_output=True, text=True, timeout=2)
    if result.returncode:
        return None
    return [int(parts[0]) for line in result.stdout.splitlines()
            if len(parts := line.split()) == 3 and parts[1] == str(identity["pgid"]) and not parts[2].startswith("Z")]


def owned_tree_alive(proc):
    if sys.platform == "win32":
        members = descendants(getattr(proc, "_harbor_identity", None) or getattr(proc, "identity", None))
        return members is None or bool(members)
    pgid = getattr(proc, "_harbor_pgid", None)
    return group_alive(pgid) if type(pgid) is int else proc.poll() is None


def owner_identity_valid(identity):
    if not isinstance(identity, dict) or type(identity.get("pid")) is not int:
        return False
    current = process_identity(identity["pid"])
    if sys.platform == "win32":
        return current == identity
    return current == identity and identity.get("pgid") == identity["pid"]


def recover_owned(identity):
    if is_alive(identity["pid"]):
        if not owner_identity_valid(identity):
            raise RuntimeError("Component identity mismatch; refusing process control")
        return RecoveredProcess(identity)
    members = descendants(identity)
    if members is None:
        raise RuntimeError("Component ownership could not be verified")
    if members:
        if sys.platform == "win32":
            return RecoveredProcess(identity)  # Named job retains identity after leader exit.
        raise RuntimeError("Orphan group identity cannot be verified")
    return None


def wait_owned_tree_exit(proc, timeout=5):
    end = time.monotonic() + timeout
    while owned_tree_alive(proc) and time.monotonic() < end:
        time.sleep(0.025)
    return not owned_tree_alive(proc)


terminate_owned_tree = terminate_tree


class RecoveredProcess:
    """A manifest identity is verified again immediately before group control."""
    def __init__(self, identity):
        self.identity = identity
        self.pid = identity["pid"]
        self._harbor_pgid = self.pid
        self.returncode = None

    def poll(self):
        if process_identity(self.pid) != self.identity:
            self.returncode = 0
        return self.returncode

    def wait(self, timeout):
        end = time.monotonic() + timeout
        while self.poll() is None:
            if time.monotonic() >= end:
                raise subprocess.TimeoutExpired("harbor-runtime", timeout)
            time.sleep(0.025)
        return self.returncode
