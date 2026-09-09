"""Harness Harbor — optional multi-harness queue daemon with worker pools.

Per-harness concurrency defaults to 3 (codex=3, minimax=3, agy=3),
allowing up to 9 concurrent workers across all three harnesses.

The daemon runs a non-blocking scheduling loop:
1. Reaps finished / exited worker child subprocesses and releases job reservations.
2. For each harness, determines available slots based on configured concurrency limits
   and total occupied slots on disk (combining in-memory workers and other daemons).
3. Under a short-lived per-harness dispatch lock, atomically claims queued jobs
   via exclusive reservation (dispatch.lock) up to available slots.
4. Spawns asynchronous worker subprocesses (codex_job_worker.py) for reserved jobs.
5. If worker spawn fails, immediately releases the reservation to prevent slot/job starvation.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from control_plane import JOBS_DIR


SUPPORTED_HARNESSES: tuple[str, ...] = ("codex", "minimax", "agy")
DEFAULT_HARNESS: str = "codex"
DEFAULT_HARNESS_CONCURRENCY: dict[str, int] = {
    "codex": 3,
    "minimax": 3,
    "agy": 3,
}
DISPATCH_LOCK_TTL_SECONDS: float = 30.0
UNKNOWN_OWNER_LOCK_TTL_SECONDS: float = 30.0

PROJECT_ROOT = Path(__file__).resolve().parent
WORKER_SCRIPT = PROJECT_ROOT / "codex_job_worker.py"

IS_WINDOWS = sys.platform == "win32"
_WIN_CREATION_FLAGS = (
    subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
) if IS_WINDOWS else 0


class HarnessDispatchLockTimeout(Exception):
    """Raised when acquisition of a harness dispatch lock times out."""
    pass


@dataclass
class ActiveWorker:
    proc: subprocess.Popen
    job_dir: Path
    harness: str
    started_at: float
    workspace: str


def _is_pid_alive(pid: int) -> bool:
    """Check if a process ID is currently active in the operating system."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
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


WORKER_HANDOFF_GRACE_SECONDS: float = 5.0


def get_worker_lock_pid(job_dir: Path) -> int | None:
    """Read and parse the worker process ID from a job's worker.lock file."""
    lock_path = job_dir / "worker.lock"
    if not lock_path.is_file():
        return None
    try:
        content = lock_path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        tokens = content.split()
        if tokens:
            try:
                return int(tokens[0])
            except ValueError:
                return None
    except OSError:
        pass
    return None


def get_canonical_workspace(cwd: Path | str) -> str:
    """Determine the canonical workspace path for a working directory.

    If cwd is inside a Git repository or linked Git worktree (identified by climbing
    up parent directories to find a '.git' directory or '.git' worktree file),
    the top-level worktree directory is returned. Otherwise, resolved absolute cwd is returned.

    The returned path is normalized for case and path separators (critical on Windows).
    """
    path = Path(cwd).resolve()
    current = path

    git_root: Path | None = None
    while True:
        git_entry = current / ".git"
        if git_entry.exists():
            git_root = current
            break
        parent = current.parent
        if parent == current:
            break
        current = parent

    top_level = git_root if git_root is not None else path
    norm_path = os.path.normcase(os.path.normpath(str(top_level.resolve())))
    return norm_path


def get_job_cwd(job_dir: Path) -> Path:
    """Read the cwd attribute from a job's status.json, defaulting to job_dir."""
    state_path = job_dir / "status.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                raw_cwd = state.get("cwd")
                if isinstance(raw_cwd, str) and raw_cwd.strip():
                    return Path(raw_cwd)
        except (OSError, ValueError):
            pass
    return job_dir


def get_workspace_lease_path(jobs_dir: Path, canonical_ws: str) -> Path:
    """Return the path to the lease file for a given canonical workspace."""
    ws_hash = hashlib.sha256(canonical_ws.encode("utf-8")).hexdigest()[:16]
    return jobs_dir / ".workspace_leases" / f"{ws_hash}.lease"


def _clean_stale_workspace_lease_if_dead(lease_path: Path, jobs_dir: Path) -> bool:
    """Determine whether a workspace lease is stale and remove it if safe.

    Priority logic:
    1. Terminal job status (completed/failed/cancelled) -> Stale, remove immediately.
    2. worker.lock exists:
       - Worker PID is alive -> ACTIVE, NEVER delete (even if daemon PID is dead).
       - Worker PID is dead -> Stale, remove.
    3. worker.lock does not exist yet:
       - Daemon PID is alive -> ACTIVE, NEVER delete.
       - Daemon PID is dead:
         - Within handoff grace window (<= 5s) -> Protect (allow worker child to start and create worker.lock).
         - After grace window expired (> 5s) and still no live worker -> Stale, remove.
    4. Unidentifiable/corrupted lease -> remove after TTL (30s).
    """
    try:
        content = lease_path.read_text(encoding="utf-8")
        st = lease_path.stat()
    except OSError:
        return False

    daemon_pid: int | None = None
    job_id: str | None = None
    for token in content.split():
        if token.startswith("pid="):
            try:
                daemon_pid = int(token.split("=", 1)[1])
            except ValueError:
                pass
        elif token.startswith("job_id="):
            job_id = token.split("=", 1)[1]

    if job_id:
        job_dir = jobs_dir / job_id
        if not job_dir.exists():
            # Job directory does not exist; if daemon is dead, reclaim immediately
            if daemon_pid is not None:
                if _is_pid_alive(daemon_pid):
                    return False
                try:
                    lease_path.unlink(missing_ok=True)
                    return True
                except OSError:
                    return False

        state_path = job_dir / "status.json"
        if state_path.is_file():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(state, dict) and state.get("status") in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    try:
                        lease_path.unlink(missing_ok=True)
                        return True
                    except OSError:
                        return False
            except (OSError, ValueError):
                pass

        # Check worker.lock for live worker process
        if (job_dir / "worker.lock").exists():
            worker_pid = get_worker_lock_pid(job_dir)
            if worker_pid is not None:
                if _is_pid_alive(worker_pid):
                    # Live worker process is actively running: lease is ACTIVE!
                    return False
                # Worker PID is dead: safe to clean up
                try:
                    lease_path.unlink(missing_ok=True)
                    return True
                except OSError:
                    return False

        # No worker.lock yet: check daemon PID and handoff grace window
        if daemon_pid is not None:
            if _is_pid_alive(daemon_pid):
                # Daemon is alive: lease is ACTIVE during pre-spawn/dispatch
                return False
            # Daemon is dead. Check handoff grace window
            lease_age = time.time() - st.st_mtime
            if lease_age <= WORKER_HANDOFF_GRACE_SECONDS:
                # Within handoff grace: worker child may still be spinning up
                return False
            # Handoff grace expired with dead daemon and no worker.lock: safe to reclaim
            try:
                lease_path.unlink(missing_ok=True)
                return True
            except OSError:
                return False

    # Corrupted lease or unparseable job_id
    if (time.time() - st.st_mtime) > UNKNOWN_OWNER_LOCK_TTL_SECONDS:
        try:
            lease_path.unlink(missing_ok=True)
            return True
        except OSError:
            pass

    return False



def is_workspace_leased(jobs_dir: Path, canonical_ws: str) -> bool:
    """Check if a canonical workspace is currently leased."""
    lease_path = get_workspace_lease_path(jobs_dir, canonical_ws)
    if not lease_path.exists():
        return False
    if _clean_stale_workspace_lease_if_dead(lease_path, jobs_dir):
        return False
    return True


def acquire_workspace_lease(
    jobs_dir: Path, canonical_ws: str, job_dir: Path, harness: str
) -> bool:
    """Atomically acquire an exclusive workspace lease via O_CREAT | os.O_EXCL."""
    leases_dir = jobs_dir / ".workspace_leases"
    try:
        leases_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    lease_path = get_workspace_lease_path(jobs_dir, canonical_ws)
    if lease_path.exists():
        _clean_stale_workspace_lease_if_dead(lease_path, jobs_dir)

    try:
        fd = os.open(lease_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                f"pid={os.getpid()} job_id={job_dir.name} harness={harness} "
                f"created_at={time.time()} workspace={canonical_ws}\n"
            )
        return True
    except (FileExistsError, OSError):
        return False


def release_workspace_lease(
    jobs_dir: Path, canonical_ws: str, job_dir: Path | None = None
) -> None:
    """Release a workspace lease file."""
    lease_path = get_workspace_lease_path(jobs_dir, canonical_ws)
    if not lease_path.exists():
        return
    if job_dir is not None:
        try:
            content = lease_path.read_text(encoding="utf-8")
            if f"job_id={job_dir.name}" not in content:
                # Held by a different job; do not delete
                return
        except OSError:
            pass
    try:
        lease_path.unlink(missing_ok=True)
    except OSError:
        pass



def _clean_stale_harness_lock_if_dead(lock_path: Path) -> bool:
    """Inspect a harness lock file and remove it ONLY if owner PID is dead or unidentifiable after TTL."""
    try:
        content = lock_path.read_text(encoding="utf-8")
        st = lock_path.stat()
    except OSError:
        return False

    lock_pid: int | None = None
    for token in content.split():
        if token.startswith("pid="):
            try:
                lock_pid = int(token.split("=", 1)[1])
            except ValueError:
                pass

    if lock_pid is not None:
        if _is_pid_alive(lock_pid):
            # Owner PID is alive: NEVER delete regardless of age
            return False
        # Owner PID is dead: immediately clean up
        try:
            lock_path.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    # Owner PID cannot be determined (corrupted lock file)
    if (time.time() - st.st_mtime) > UNKNOWN_OWNER_LOCK_TTL_SECONDS:
        try:
            lock_path.unlink(missing_ok=True)
            return True
        except OSError:
            pass

    return False


def _is_stale_dispatch_lock(lock_path: Path, job_dir: Path) -> bool:
    """Determine whether a dispatch.lock file is stale and should be cleared.

    A dispatch lock is NEVER stale if its owner PID is alive, unless the job
    itself has already reached a terminal status.
    """
    state_path = job_dir / "status.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and state.get("status") in {
                "completed",
                "failed",
                "cancelled",
            }:
                return True
        except (OSError, ValueError):
            pass

    if (job_dir / "worker.lock").exists():
        return False

    try:
        content = lock_path.read_text(encoding="utf-8")
        st = lock_path.stat()
    except OSError:
        return False

    lock_pid: int | None = None
    for token in content.split():
        if token.startswith("pid="):
            try:
                lock_pid = int(token.split("=", 1)[1])
            except ValueError:
                pass

    if lock_pid is not None:
        if _is_pid_alive(lock_pid):
            # Owner PID is alive: NOT stale
            return False
        # Owner PID is dead: stale
        return True

    # Owner PID unknown/corrupted
    if (time.time() - st.st_mtime) > DISPATCH_LOCK_TTL_SECONDS:
        return True

    return False


def is_dispatch_locked(job_dir: Path) -> bool:
    """Check if job_dir is actively reserved by a dispatch.lock file."""
    lock_path = job_dir / "dispatch.lock"
    if not lock_path.exists():
        return False
    if _is_stale_dispatch_lock(lock_path, job_dir):
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def reserve_job(job_dir: Path) -> bool:
    """Atomically claim a job before worker spawn via exclusive dispatch.lock creation.

    Guarantees cross-process mutual exclusion across multiple concurrent daemon instances.
    """
    lock_path = job_dir / "dispatch.lock"
    if lock_path.exists() and _is_stale_dispatch_lock(lock_path, job_dir):
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()} created_at={time.time()}\n")
        return True
    except (FileExistsError, OSError):
        return False


def unreserve_job(job_dir: Path) -> None:
    """Release a job's dispatch.lock reservation."""
    lock_path = job_dir / "dispatch.lock"
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


@contextmanager
def harness_dispatch_lock(
    jobs_dir: Path, harness: str, timeout_seconds: float = 3.0
) -> Iterator[None]:
    """Short-lived cross-process critical section for allocating slots on a specific harness.

    Fails closed: raises HarnessDispatchLockTimeout if lock cannot be acquired within timeout.
    """
    lock_path = jobs_dir / f".dispatch_{harness}.lock"
    deadline = time.monotonic() + timeout_seconds
    acquired = False

    while not acquired:
        if lock_path.exists():
            _clean_stale_harness_lock_if_dead(lock_path)

        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(f"pid={os.getpid()} created_at={time.time()}\n")
            acquired = True
        except (FileExistsError, OSError):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)

    if not acquired:
        raise HarnessDispatchLockTimeout(
            f"Timed out after {timeout_seconds}s waiting for dispatch lock for harness '{harness}'"
        )

    try:
        yield
    finally:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass



def get_job_harness(job_dir: Path) -> str:
    """Read the harness attribute from a job's status.json.

    Defaults to 'codex' if missing, unreadable, or not a supported harness
    (ensuring full backwards compatibility for legacy jobs).
    """
    state_path = job_dir / "status.json"
    if not state_path.is_file():
        return DEFAULT_HARNESS
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            harness = state.get("harness")
            if isinstance(harness, str) and harness in SUPPORTED_HARNESSES:
                return harness
    except (OSError, ValueError):
        pass
    return DEFAULT_HARNESS


def queued_jobs(jobs_dir: Path = JOBS_DIR) -> list[Path]:
    """Return all queued jobs in jobs_dir, sorted FIFO by creation time and job name."""
    try:
        job_dirs = list(jobs_dir.iterdir())
    except OSError:
        return []

    queued: list[tuple[str, Path]] = []
    for job_dir in job_dirs:
        if not job_dir.is_dir() or job_dir.name.startswith("."):
            continue
        state_path = job_dir / "status.json"
        if not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            isinstance(state, dict)
            and state.get("status") == "queued"
            and not (job_dir / "worker.lock").exists()
            and not is_dispatch_locked(job_dir)
        ):
            created_at = state.get("created_at") or ""
            queued.append((created_at, job_dir))

    queued.sort(key=lambda item: (item[0], item[1].name))
    return [item[1] for item in queued]


def get_disk_active_job_ids(jobs_dir: Path = JOBS_DIR) -> dict[str, set[str]]:
    """Scan jobs_dir for active jobs per harness (worker.lock, dispatch.lock, or status == 'running').

    Returns a dict mapping harness -> set of active job_dir names (job_ids).
    """
    try:
        job_dirs = list(jobs_dir.iterdir())
    except OSError:
        return {h: set() for h in SUPPORTED_HARNESSES}

    active: dict[str, set[str]] = {h: set() for h in SUPPORTED_HARNESSES}
    for job_dir in job_dirs:
        if not job_dir.is_dir() or job_dir.name.startswith("."):
            continue
        state_path = job_dir / "status.json"
        lock_path = job_dir / "worker.lock"

        is_active = False
        if lock_path.exists() or is_dispatch_locked(job_dir):
            is_active = True
        elif state_path.is_file():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(state, dict) and state.get("status") == "running":
                    is_active = True
            except (OSError, ValueError):
                pass

        if is_active:
            harness = get_job_harness(job_dir)
            active.setdefault(harness, set()).add(job_dir.name)

    return active


def get_disk_busy_harnesses(jobs_dir: Path = JOBS_DIR) -> set[str]:
    """Scan jobs_dir for harnesses that have at least one active job on disk."""
    disk_active = get_disk_active_job_ids(jobs_dir)
    return {h for h, jobs in disk_active.items() if jobs}


class HarborScheduler:
    """Non-blocking multi-lane worker pool dispatcher for Harness Harbor jobs."""

    def __init__(
        self,
        jobs_dir: Path = JOBS_DIR,
        python_exe: str | Path | None = None,
        worker_script: str | Path | None = None,
        supported_harnesses: tuple[str, ...] = SUPPORTED_HARNESSES,
        concurrency_limits: dict[str, int] | None = None,
        dispatch_lock_timeout: float = 3.0,
    ) -> None:
        self.jobs_dir = Path(jobs_dir)
        self.python_exe = str(python_exe or sys.executable)
        self.worker_script = str(worker_script or WORKER_SCRIPT)
        self.supported_harnesses = supported_harnesses
        self.dispatch_lock_timeout = dispatch_lock_timeout
        self.concurrency_limits = (
            dict(concurrency_limits)
            if concurrency_limits is not None
            else dict(DEFAULT_HARNESS_CONCURRENCY)
        )
        self.active_workers: dict[str, dict[str, ActiveWorker]] = {
            h: {} for h in self.supported_harnesses
        }

    def reap_workers(self) -> list[tuple[str, Path, int]]:
        """Reap finished worker subprocesses and release their in-memory lane claims and workspace leases.

        Returns list of (harness, job_dir, exit_code).
        """
        finished: list[tuple[str, Path, int]] = []
        for harness, workers in list(self.active_workers.items()):
            for job_id, worker in list(workers.items()):
                exit_code = worker.proc.poll()
                if exit_code is not None:
                    print(
                        f"Finished job {worker.job_dir.name} ({harness}) with exit code {exit_code}",
                        flush=True,
                    )
                    release_workspace_lease(self.jobs_dir, worker.workspace, worker.job_dir)
                    unreserve_job(worker.job_dir)
                    state_path = worker.job_dir / "status.json"
                    if state_path.is_file():
                        try:
                            state = json.loads(state_path.read_text(encoding="utf-8"))
                            if isinstance(state, dict) and state.get("status") in {
                                "queued",
                                "running",
                            }:
                                if exit_code != 0:
                                    state.update(
                                        status="failed",
                                        failure_type="worker_process_crash",
                                        exit_code=exit_code,
                                        error=f"Worker subprocess exited with code {exit_code}",
                                    )
                                    state_path.write_text(
                                        json.dumps(state, ensure_ascii=False, indent=2)
                                        + "\n",
                                        encoding="utf-8",
                                    )
                        except Exception:
                            pass
                    finished.append((harness, worker.job_dir, exit_code))
                    del workers[job_id]
        return finished

    def get_occupied_count(self, harness: str) -> int:
        """Get total active slots for harness (in-memory workers + on-disk jobs from other daemons)."""
        disk_active = get_disk_active_job_ids(self.jobs_dir).get(harness, set())
        memory_active = set(self.active_workers.get(harness, {}).keys())
        return len(disk_active | memory_active)

    def get_busy_harnesses(self) -> set[str]:
        """Return set of harnesses that have reached their configured concurrency limit."""
        busy = set()
        for harness in self.supported_harnesses:
            limit = self.concurrency_limits.get(
                harness, DEFAULT_HARNESS_CONCURRENCY.get(harness, 1)
            )
            if self.get_occupied_count(harness) >= limit:
                busy.add(harness)
        return busy

    def spawn_worker(self, job_dir: Path, harness: str) -> subprocess.Popen:
        """Spawn a worker child subprocess asynchronously."""
        argv = [self.python_exe, self.worker_script, str(job_dir)]
        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = _WIN_CREATION_FLAGS
        return subprocess.Popen(argv, **popen_kwargs)

    def tick(self) -> list[tuple[str, Path, int]]:
        """Run one scheduler cycle: reap, check available slots per harness, and dispatch queued jobs.

        Returns list of newly spawned workers as (harness, job_dir, pid).
        """
        self.reap_workers()
        queued = queued_jobs(self.jobs_dir)
        spawned: list[tuple[str, Path, int]] = []

        for harness in self.supported_harnesses:
            limit = self.concurrency_limits.get(
                harness, DEFAULT_HARNESS_CONCURRENCY.get(harness, 1)
            )
            if limit <= 0:
                continue

            jobs_to_spawn: list[tuple[Path, str]] = []
            try:
                with harness_dispatch_lock(
                    self.jobs_dir, harness, timeout_seconds=self.dispatch_lock_timeout
                ):
                    occupied_count = self.get_occupied_count(harness)
                    available_slots = max(0, limit - occupied_count)
                    if available_slots > 0:
                        for job_dir in queued:
                            if len(jobs_to_spawn) >= available_slots:
                                break
                            if get_job_harness(job_dir) == harness:
                                ws = get_canonical_workspace(get_job_cwd(job_dir))
                                # Prevent claiming duplicate workspaces within same tick
                                if any(w == ws for _, w in jobs_to_spawn):
                                    continue
                                # Atomic job-level reservation
                                if not reserve_job(job_dir):
                                    continue
                                # Atomic workspace-level exclusive lease
                                if not acquire_workspace_lease(self.jobs_dir, ws, job_dir, harness):
                                    unreserve_job(job_dir)
                                    continue
                                jobs_to_spawn.append((job_dir, ws))
            except HarnessDispatchLockTimeout:
                # Lock could not be acquired within timeout; fail closed and skip this harness
                continue

            for target_job, ws in jobs_to_spawn:
                print(
                    f"Starting job {target_job.name} on harness '{harness}'",
                    flush=True,
                )
                try:
                    proc = self.spawn_worker(target_job, harness)
                    self.active_workers[harness][target_job.name] = ActiveWorker(
                        proc=proc,
                        job_dir=target_job,
                        harness=harness,
                        started_at=time.monotonic(),
                        workspace=ws,
                    )
                    spawned.append((harness, target_job, proc.pid))
                except Exception as exc:
                    release_workspace_lease(self.jobs_dir, ws, target_job)
                    unreserve_job(target_job)
                    print(
                        f"Failed to spawn worker for job {target_job.name} ({harness}): {exc}",
                        flush=True,
                    )

        return spawned

    def shutdown(self, timeout: float = 5.0) -> None:
        """Wait briefly for active workers on shutdown."""
        deadline = time.monotonic() + timeout
        while any(self.active_workers.values()) and time.monotonic() < deadline:
            self.reap_workers()
            if not any(workers for workers in self.active_workers.values()):
                break
            time.sleep(0.1)


def main() -> None:
    JOBS_DIR.mkdir(exist_ok=True)
    print(f"Harness Harbor daemon started: {JOBS_DIR}", flush=True)
    scheduler = HarborScheduler(jobs_dir=JOBS_DIR)
    try:
        while True:
            scheduler.tick()
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("Harness Harbor daemon stopped", flush=True)
        scheduler.shutdown(timeout=2.0)


if __name__ == "__main__":
    main()



