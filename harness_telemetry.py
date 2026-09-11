"""Unified, read-only harness telemetry domain model.

This module has no executable probing, subprocess calls, credentials, or
platform assumptions.  Core supplies authoritative harness/quota readers and
a process observation adapter; the launcher consumes its snapshot unchanged.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from harness_process_adapter import ProcessActivityAdapter

HARNESS_NAMES = ("codex", "minimax", "agy")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(now: datetime) -> str:
    return now.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_error(value: object, fallback: str) -> str:
    """Do not allow provider diagnostics (which can contain credentials) out."""
    return fallback if value else fallback


def unavailable_quota(source: str, error: str, *, account_binding: str | None = None) -> dict[str, Any]:
    return {
        "state": "unavailable",
        "windows": [],
        "source": source,
        "fetched_at": None,
        "freshness": "unknown",
        "stale": False,
        "error": error,
        "account_binding": account_binding,
        "quota_scope": None,
    }


class HarnessTelemetryProvider:
    """In-memory cache and normalizer for a complete harness snapshot."""

    def __init__(
        self,
        *,
        status_provider: Callable[[str], Mapping[str, Any]],
        codex_quota_provider: Callable[[], Mapping[str, Any]],
        job_activity_provider: Callable[[], Mapping[str, Mapping[str, Any]]],
        process_adapter: ProcessActivityAdapter,
        refresh_seconds: int = 300,
        clock: Callable[[], datetime] = _utc_now,
        agy_cache_invalidator: Callable[[], None] | None = None,
    ) -> None:
        self._status_provider = status_provider
        self._codex_quota_provider = codex_quota_provider
        self._job_activity_provider = job_activity_provider
        self._process_adapter = process_adapter
        self._refresh_seconds = max(15, int(refresh_seconds))
        self._clock = clock
        # The canonical AGY capability result is shared with ``agy_status()``
        # / ``harness_status("agy")`` via a short-lived context-keyed cache
        # in the control plane.  Telemetry and canonical must derive from the
        # same *current* canonical probe/context; an injectable invalidator
        # lets the wiring layer guarantee the snapshot never serves a stale
        # failing result that the canonical path would not also serve at the
        # same instant.  It is optional so unit tests with synthetic status
        # providers remain unaffected.
        self._agy_cache_invalidator = agy_cache_invalidator
        self._quota_cache: dict[str, dict[str, Any]] = {}
        self._last_attempt: dict[str, datetime] = {}

    def snapshot(self, *, force_refresh: bool = False) -> dict[str, Any]:
        now = self._clock()
        if self._agy_cache_invalidator is not None:
            # Invalidate any short-lived AGY capability cache so the
            # snapshot reads the same current canonical probe the canonical
            # path would observe at this instant.  A stale failing result
            # from an earlier probe must never leak into telemetry while a
            # fresh ``agy_status()`` / ``harness_status("agy")`` call would
            # already report the current healthy state.
            self._agy_cache_invalidator()
        statuses = {name: self._safe_status(name) for name in HARNESS_NAMES}
        activities = self._activities()
        quotas = {
            "codex": self._cached_quota("codex", self._codex_quota_provider, now, force_refresh),
            "minimax": self._cached_quota(
                "minimax",
                lambda: unavailable_quota(
                    "MiniMax CLI status",
                    "no verified account-bound, read-only MiniMax quota source",
                    account_binding="unavailable",
                ),
                now,
                force_refresh,
            ),
            "agy": self._cached_quota(
                "agy",
                lambda: unavailable_quota(
                    "AGY CLI status",
                    "AGY exposes no authoritative read-only machine-readable quota source",
                ),
                now,
                force_refresh,
            ),
        }
        return {
            "schema_version": 1,
            "fetched_at": _timestamp(now),
            "harnesses": [
                self._harness_snapshot(name, statuses[name], activities[name], quotas[name])
                for name in HARNESS_NAMES
            ],
        }

    def _safe_status(self, name: str) -> dict[str, Any]:
        try:
            raw = self._status_provider(name)
        except Exception:
            raw = {}
        data = dict(raw) if isinstance(raw, Mapping) else {}
        available = bool(data.get("available"))
        installed = bool(data.get("executable_exists", available))
        return {
            "available": available,
            "installed": installed,
            "version": data.get("version") if isinstance(data.get("version"), str) else None,
            # CLI probe output can be embedded in a legacy blocker.  Preserve
            # the blocked state without exporting an arbitrary diagnostic.
            "blocker": "Harness capability status is blocked" if data.get("blocker") else None,
            "models": list(data.get("models")) if isinstance(data.get("models"), Sequence) and not isinstance(data.get("models"), (str, bytes)) else [],
        }

    def _activities(self) -> dict[str, dict[str, Any]]:
        try:
            jobs = self._job_activity_provider()
        except Exception:
            jobs = {}
        try:
            processes = self._process_adapter.observe(HARNESS_NAMES)
        except Exception:
            processes = {}
        result: dict[str, dict[str, Any]] = {}
        for name in HARNESS_NAMES:
            job = dict(jobs.get(name, {})) if isinstance(jobs, Mapping) else {}
            process = dict(processes.get(name, {})) if isinstance(processes, Mapping) else {}
            running = int(job.get("running", 0) or 0)
            queued = int(job.get("queued", 0) or 0)
            count = int(process.get("process_count", 0) or 0)
            process_error = process.get("error") if isinstance(process.get("error"), str) else None
            if running:
                state = "running"
            elif queued:
                state = "queued"
            elif count:
                state = "active"
            elif process_error:
                state = "unknown"
            else:
                state = "idle"
            sources = [value for value in (job.get("source"), process.get("source")) if isinstance(value, str)]
            result[name] = {
                "state": state,
                "running_jobs": running,
                "running_job_ids": list(job.get("running_job_ids", [])),
                "queued_jobs": queued,
                "process_count": count,
                "source": " + ".join(dict.fromkeys(sources)) or "source unavailable",
                "error": process_error or (job.get("error") if isinstance(job.get("error"), str) else None),
            }
        return result

    def _cached_quota(
        self, name: str, provider: Callable[[], Mapping[str, Any]], now: datetime, force_refresh: bool
    ) -> dict[str, Any]:
        last = self._last_attempt.get(name)
        if not force_refresh and last and (now - last).total_seconds() < self._refresh_seconds and name in self._quota_cache:
            return deepcopy(self._quota_cache[name])
        self._last_attempt[name] = now
        try:
            raw = provider()
        except Exception:
            raw = {"state": "unavailable", "source": f"{name} quota provider", "error": "quota source failed"}
        normalized = self._normalise_quota(raw, now)
        previous = self._quota_cache.get(name)
        if normalized["state"] == "available" or previous is None:
            self._quota_cache[name] = normalized
        else:
            # Retain only a previous successful snapshot; a first failure is
            # unavailable/unknown, never a fabricated stale quota value.
            stale = deepcopy(previous)
            stale.update(freshness="stale", stale=True, error=normalized["error"])
            self._quota_cache[name] = stale
        return deepcopy(self._quota_cache[name])

    def _normalise_quota(self, raw: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        data = dict(raw) if isinstance(raw, Mapping) else {}
        source = str(data.get("source") or "quota source unavailable")
        if data.get("state") != "available":
            return unavailable_quota(
                source,
                _safe_error(data.get("error"), "authoritative quota is unavailable"),
                account_binding=data.get("account_binding") if isinstance(data.get("account_binding"), str) else None,
            )
        windows: list[dict[str, Any]] = []
        for raw_window in data.get("windows", []):
            if not isinstance(raw_window, Mapping):
                continue
            used = raw_window.get("used_percent")
            if not isinstance(used, (int, float)):
                continue
            resets_at = raw_window.get("resets_at")
            resets_epoch = int(resets_at) if isinstance(resets_at, (int, float)) else None
            if resets_epoch and resets_epoch > 10_000_000_000:
                resets_epoch //= 1000
            window = {
                "label": str(raw_window.get("label") or "Window"),
                "bucket": str(raw_window.get("bucket") or "default"),
                "used_percent": max(0, min(100, int(round(used)))),
                "window_minutes": int(raw_window["window_minutes"]) if isinstance(raw_window.get("window_minutes"), (int, float)) else None,
                "resets_at": _timestamp(datetime.fromtimestamp(resets_epoch, timezone.utc)) if resets_epoch else None,
                "resets_in_seconds": max(0, resets_epoch - int(now.timestamp())) if resets_epoch else None,
            }
            windows.append(window)
        if not windows:
            return unavailable_quota(source, "authoritative quota source returned no quota windows")
        return {
            "state": "available",
            "windows": windows,
            "source": source,
            "fetched_at": _timestamp(now),
            "freshness": "fresh",
            "stale": False,
            "error": None,
            "account_binding": data.get("account_binding") if isinstance(data.get("account_binding"), str) else None,
            "quota_scope": data.get("quota_scope") if isinstance(data.get("quota_scope"), str) else None,
        }

    @staticmethod
    def _harness_snapshot(name: str, status: Mapping[str, Any], activity: Mapping[str, Any], quota: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "name": name,
            "installed": bool(status["installed"]),
            "available": bool(status["available"]),
            "version": status["version"],
            "blocker": status["blocker"],
            "models": list(status["models"]),
            "source": "Harbor Core harness registry",
            "activity": dict(activity),
            "quota": dict(quota),
        }


__all__ = ["HARNESS_NAMES", "HarnessTelemetryProvider", "unavailable_quota"]
