"""Job-scoped stop coordination. No caller-supplied process identifiers."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harbor_platform import process
from runtime_queue import queue_root_matches

GRACE_SECONDS = 10
TERMINAL = {"completed", "failed", "cancelled"}


def _now():
    return datetime.now(timezone.utc)


def _error(code, stage, **extra):
    return {"ok": False, "operation": "task_stop", "stage": stage,
            "error_kind": code, "error_code": code, "process_signal_attempted": False,
            "job_state_changed": False, "requires_new_preview": code in {"ownership_changed", "process_tree_changed"}, **extra}


def _load(job_id, jobs_dir):
    from control_plane import JOB_ID_RE, read_json_object
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        return None, None
    path = Path(jobs_dir) / job_id / "status.json"
    if not path.is_file():
        return path, None
    state = read_json_object(path)
    if not queue_root_matches(state, jobs_dir):
        return path, None
    return path, state


def _tree(identity):
    """Return (verified, members); None members means uncertain observation.

    A dead POSIX group leader with surviving members is deliberately unverifiable:
    its group number alone is not authority to signal a potentially reused group.
    """
    if not isinstance(identity, dict) or type(identity.get("pid")) is not int:
        return False, None
    try:
        members = process.descendants(identity)
        if sys.platform == "win32" and not members:
            from harbor_platform.windows_process import inspect_job
            status, found = inspect_job(identity)
            # A vanished Job Object handle is not proof that children exited.
            members = found if status == "active" else None
        if members is None:
            return False, None
        leader = process.process_identity(identity["pid"])
        if sys.platform == "win32" and members == []:
            # A retained handle can keep both an empty owned Job Object and
            # its exited leader's identity observable. Verify death separately.
            if (leader is None or leader == identity) and not process.is_alive(identity["pid"]):
                return True, []
            return False, None
        if (leader != identity and members) or (leader == identity and not members):
            return False, None
        observed = []
        for pid in sorted(set(members)):
            current = process.process_identity(pid)
            if current is None:
                return False, None
            observed.append(current)
        return True, observed
    except (OSError, ValueError, subprocess.SubprocessError):
        return False, None


def snapshot(state, jobs_dir):
    owner = state.get("stop_ownership")
    if not isinstance(owner, dict) or owner.get("job_id") != state.get("job_id") or owner.get("harness") != state.get("harness") or owner.get("queue_root") != str(Path(jobs_dir).resolve()):
        return False, [], None
    trees = []
    for key in ("worker", "native"):
        identity = owner.get(key)
        if identity is None:
            continue
        verified, members = _tree(identity)
        if (not verified and key in {"worker", "native"}
                and (owner.get("native") is None or owner.get("native_exit_verified"))
                and sys.platform == "win32" and not process.is_alive(identity["pid"])):
            from harbor_platform.windows_process import inspect_job
            status, _ = inspect_job(identity)
            if status == "confirmed_missing":
                verified, members = True, []
        if not verified:
            return False, [], None
        trees.append({"kind": key, "identity": identity, "members": members})
    if owner.get("worker") is None:
        return False, [], None
    payload = {"job_id": state["job_id"], "generation": owner.get("generation"),
               "queue_root": owner["queue_root"], "trees": trees}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return True, trees, digest


def preview(job_id, jobs_dir):
    try:
        _, state = _load(job_id, jobs_dir)
    except (OSError, ValueError, json.JSONDecodeError):
        state = None
    if state is None:
        return {**_error("job_not_found", "preview"), "ownership_verified": False}
    verified, trees, fingerprint = snapshot(state, jobs_dir)
    stop = state.get("stop") or {}
    deadline = stop.get("grace_deadline")
    elapsed = False
    if isinstance(deadline, str):
        try:
            elapsed = _now() >= datetime.fromisoformat(deadline)
        except ValueError:
            pass
    status = state.get("status")
    return {"ok": True, "operation": "task_stop_preview", "job_id": job_id,
            "status": status, "ownership_verified": verified, "owned_tree": trees if verified else [],
            "ownership_fingerprint": fingerprint if verified else None,
            "graceful_eligible": status in {"queued", "running"},
            "force_eligible": status == "cancelling" and elapsed and verified and any(t["members"] for t in trees),
            "stop": stop}


def _release(job_dir, jobs_dir):
    from codex_job_daemon import get_canonical_workspace, get_job_cwd, release_workspace_lease, unreserve_job
    (job_dir / "worker.lock").unlink(missing_ok=True)
    release_workspace_lease(Path(jobs_dir), get_canonical_workspace(get_job_cwd(job_dir)), job_dir)
    unreserve_job(job_dir)


def reconcile(job_id, jobs_dir):
    """Finalize only after a verified, empty owned tree; never signal here."""
    from control_plane import write_json, utc_now, _job_poll_lock
    job_dir = Path(jobs_dir) / job_id
    with _job_poll_lock(job_dir):
        path, state = _load(job_id, jobs_dir)
        if state is None or state.get("status") != "cancelling":
            return False
        verified, trees, _ = snapshot(state, jobs_dir)
        if not verified or any(t["members"] for t in trees):
            return False
        state.update(status="cancelled", cancelled_at=utc_now(), updated_at=utc_now())
        state.setdefault("stop", {})["tree_exit_verified_at"] = utc_now()
        write_json(path, state)
    _release(job_dir, jobs_dir)
    return True


def stop(job_id, jobs_dir, mode="graceful", ownership_fingerprint=None, reason=None):
    from control_plane import _job_poll_lock, cancel_task, read_json_object, write_json, utc_now
    if mode not in {"graceful", "force"} or (reason is not None and (not isinstance(reason, str) or len(reason) > 1000)):
        return _error("invalid_request", "validate")
    path, state = _load(job_id, jobs_dir)
    if state is None:
        return _error("job_not_found", "load")
    job_dir = path.parent
    try:
        with _job_poll_lock(job_dir):
            state = read_json_object(path)
            if not queue_root_matches(state, jobs_dir):
                return _error("ownership_unverified", "load")
            status = state.get("status")
            if status in TERMINAL:
                return _error("job_already_terminal", "state", status=status)
            if mode == "graceful":
                if status == "queued":
                    result = cancel_task(job_id, jobs_dir)
                    return {"operation": "task_stop", "mode": mode, **result}
                if status == "cancelling":
                    return {"ok": True, "operation": "task_stop", "status": status, "job_state_changed": False, "stop": state.get("stop")}
                if status != "running":
                    return _error("invalid_state", "state", status=status)
                now = _now()
                state["stop"] = {"requested_at": now.isoformat(), "requested_mode": "graceful",
                                 "reason": reason, "requested_by": "mcp", "grace_deadline": (now + timedelta(seconds=GRACE_SECONDS)).isoformat()}
                state.update(status="cancelling", updated_at=utc_now())
                write_json(path, state)
                return {"ok": True, "operation": "task_stop", "mode": mode, "status": "cancelling",
                        "job_state_changed": True, "process_signal_attempted": False, "stop": state["stop"]}
            if status != "cancelling":
                return _error("force_not_yet_allowed", "state", status=status)
            if not ownership_fingerprint or not isinstance(ownership_fingerprint, str):
                return _error("ownership_unverified", "fingerprint")
            view = preview(job_id, jobs_dir)
            if not view["ownership_verified"]:
                return _error("ownership_unverified", "verify", ownership_verified=False)
            if ownership_fingerprint != view["ownership_fingerprint"]:
                return _error("ownership_changed", "verify", ownership_verified=True)
            if not view["force_eligible"]:
                return _error("force_not_yet_allowed", "deadline", ownership_verified=True)
            # A Windows worker Job Object may also contain the native process.
            # Stop the narrower native job while its owning worker still holds
            # the handle, then account for its verified members disappearing
            # from the worker job's next observation.
            trees = sorted(view["owned_tree"], key=lambda tree: tree["kind"] != "native")
            # Reobserve before each signal. Any drift fails closed; never use a stale PID.
            state.setdefault("stop", {})["force_requested_at"] = utc_now()
            state["stop"]["output_complete"] = False
            write_json(path, state)
            attempted = False
            terminated_pids = set()
            for tree in (t for t in trees if t["members"]):
                current = preview(job_id, jobs_dir)
                expected = {t["kind"]: t for t in trees}
                observed = {t["kind"]: t for t in current.get("owned_tree", [])}
                if not current["ownership_verified"] or any(
                    observed.get(kind, {}).get("identity") != original["identity"]
                    or (observed[kind]["members"] != [m for m in original["members"] if m["pid"] not in terminated_pids])
                    for kind, original in expected.items()
                ):
                    return {**_error("process_tree_changed", "terminate"), "process_signal_attempted": attempted, "job_state_changed": True}
                if not observed[tree["kind"]]["members"]:
                    continue
                if tree["kind"] == "worker" and state["stop_ownership"].get("native"):
                    native = observed.get("native")
                    if native is not None and not native["members"] and not state["stop_ownership"].get("native_exit_verified"):
                        # Once the worker dies its last handle to an already
                        # empty native job may close. Persist the verified
                        # emptiness while that handle still exists.
                        state["stop_ownership"]["native_exit_verified"] = True
                        write_json(path, state)
                try:
                    recovered = process.recover_owned(tree["identity"])
                except RuntimeError:
                    return {**_error("ownership_changed", "terminate"), "process_signal_attempted": attempted, "job_state_changed": True}
                if recovered is None:
                    return {**_error("process_tree_changed", "terminate"), "process_signal_attempted": attempted, "job_state_changed": True}
                attempted = True
                try:
                    gone = process.terminate_tree(recovered, grace=1)
                except (OSError, RuntimeError):
                    gone = False
                if not gone:
                    return {**_error("force_stop_failed", "terminate"), "process_signal_attempted": True, "job_state_changed": True}
                terminated_pids.update(m["pid"] for m in tree["members"])
                if tree["kind"] == "native":
                    state["stop_ownership"]["native_exit_verified"] = True
                    write_json(path, state)
            result = {"ok": True, "operation": "task_stop", "mode": "force", "status": "cancelling",
                      "process_signal_attempted": attempted, "job_state_changed": True, "tree_exit_verified": False}
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        return _error("state_persist_failed", "state", error=str(exc))
    try:
        if reconcile(job_id, jobs_dir):
            result.update(status="cancelled", tree_exit_verified=True)
    except (OSError, RuntimeError, ValueError):
        return {**_error("tree_exit_unverified", "reconcile"),
                "process_signal_attempted": result["process_signal_attempted"],
                "job_state_changed": True, "tree_exit_verified": False}
    return result


def request_native_stop(proc, state_path):
    """Worker-side cooperative signal; a surviving tree remains cancelling."""
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
        if state.get("status") != "cancelling" or proc.poll() is not None:
            return False
        identity = (state.get("stop_ownership") or {}).get("native")
        if identity is None or identity != (getattr(proc, "_harbor_identity", None) or process.process_identity(proc.pid)):
            return False
        if not process.owner_identity_valid(identity):
            return False
        if sys.platform == "win32":
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except OSError:
                proc.terminate()  # leader only; no immediate Job Object kill
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
