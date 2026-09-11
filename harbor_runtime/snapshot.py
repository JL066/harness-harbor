"""Fast queue truth layered over slow capability observations for both shells."""
from datetime import datetime, timezone
import time

from runtime_queue import queue_root_fingerprint

HARNESS_NAMES = ("codex", "minimax", "agy")


def harness_snapshot(jobs_dir, telemetry_rows=(), *, connected=True, now=None):
    from control_plane import _harness_job_activity
    now = time.time() if now is None else now
    observed_at = datetime.fromtimestamp(now, timezone.utc).isoformat()
    try:
        activity = _harness_job_activity(jobs_dir) if connected else {}
    except (OSError, ValueError):
        activity = {name: {"error": "Job queue could not be read"} for name in HARNESS_NAMES}
    by_name = {row["name"]: row for row in telemetry_rows}
    rows = []
    for name in HARNESS_NAMES:
        row = dict(by_name.get(name, {"name": name, "available": False, "status": "Unavailable", "summary": "Capability probe pending"}))
        current = activity.get(name, {})
        error = current.get("error")
        fresh = connected and not error
        ids = current.get("running_job_ids", []) if fresh else []
        state = "disconnected" if not connected else "stale" if error else "running" if ids else "idle"
        row.update(running_jobs=len(ids), running_job_ids=ids, activity_fresh=fresh,
                   activity_state=state, queue_read_error=error, activity_observed_at=observed_at,
                   latest_activity_at=current.get("latest_activity_at"),
                   queue_fingerprint=queue_root_fingerprint(jobs_dir))
        summary = row.get("summary", row.get("detail", ""))
        row["summary"] = summary
        row["detail"] = summary + " — " + (f"Running {len(ids)}: " + ", ".join(ids) if ids else state.title())
        rows.append(row)
    return rows
