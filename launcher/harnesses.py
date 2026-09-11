"""Launcher adapter for Harbor's authoritative harness capability registry.

The launcher deliberately does not probe executables itself.  The default
provider imports Harbor's existing ``control_plane`` registry lazily; tests and
embedders can inject a provider with the same small callable contract.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

SUPPORTED_HARNESSES = (
    ("codex", "Codex CLI"),
    ("minimax", "MiniMax CLI"),
    ("agy", "Antigravity/AGY"),
)


def _default_status_provider(name: str) -> Mapping[str, Any]:
    # Lazy import keeps the launcher importable when Harbor's control plane is
    # unavailable, and avoids maintaining a second probing implementation.
    from control_plane import harness_status

    return harness_status(name)


def _default_telemetry_provider() -> Mapping[str, Any]:
    """Read the one Core snapshot; launcher never probes harnesses itself."""
    from control_plane import harness_telemetry_snapshot

    return harness_telemetry_snapshot()


def _status(provider: Callable[[str], Mapping[str, Any]], name: str) -> dict[str, Any]:
    try:
        raw = provider(name)
    except Exception as exc:
        # A missing or failing optional harness must never fail launcher setup.
        # Without a record from the authoritative status we have no way to
        # distinguish a missing executable from a transient probe failure, so
        # the safest label is ``Not installed``.
        return {
            "name": name,
            "display_name": dict(SUPPORTED_HARNESSES)[name],
            "status": "Not installed",
            "available": False,
            "detail": "Not detected",
            "raw": {"error": f"{type(exc).__name__}: {exc}"},
        }
    data = dict(raw) if isinstance(raw, Mapping) else {}
    installed = bool(data.get("available", False))
    if installed:
        return {
            "name": name,
            "display_name": dict(SUPPORTED_HARNESSES)[name],
            "status": "Installed",
            "available": True,
            "detail": data.get("version") or "Ready",
            "raw": data,
        }
    # The authoritative status distinguishes a missing executable from one
    # that is installed but blocked (e.g. probe failure, missing capabilities).
    # A transient probe error must not be mislabeled as ``Not installed``;
    # the blocker message gives the user a real reason to investigate.
    if data.get("executable_exists") is False:
        return {
            "name": name,
            "display_name": dict(SUPPORTED_HARNESSES)[name],
            "status": "Not installed",
            "available": False,
            "detail": "Not detected",
            "raw": data,
        }
    if data.get("blocker"):
        return {
            "name": name,
            "display_name": dict(SUPPORTED_HARNESSES)[name],
            "status": "Blocked",
            "available": False,
            "detail": data.get("blocker"),
            "raw": data,
        }
    return {
        "name": name,
        "display_name": dict(SUPPORTED_HARNESSES)[name],
        "status": "Not installed",
        "available": False,
        "detail": "Not detected",
        "raw": data,
    }


def _telemetry_rows(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Translate the Core snapshot to the Batch 3 launcher card contract."""
    records = snapshot.get("harnesses") if isinstance(snapshot, Mapping) else None
    by_name = {
        item.get("name"): item for item in records
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    } if isinstance(records, Sequence) and not isinstance(records, (str, bytes)) else {}
    rows: list[dict[str, Any]] = []
    for name, display_name in SUPPORTED_HARNESSES:
        record = by_name.get(name, {})
        raw = {
            "available": record.get("available", False),
            "executable_exists": record.get("installed", False),
            "version": record.get("version"),
            "blocker": record.get("blocker"),
            "models": record.get("models", []),
            "telemetry": dict(record),
        }
        row = _status(lambda _name, data=raw: data, name)
        activity = record.get("activity") if isinstance(record, Mapping) else None
        quota = record.get("quota") if isinstance(record, Mapping) else None
        detail = row["detail"]
        # Queue activity is layered on by the fast shared snapshot, never by a
        # capability response that may finish after a newer task observation.
        quota_summary = _quota_summary(quota)
        if quota_summary:
            detail = f"{detail} — {quota_summary}"
        if isinstance(quota, Mapping) and quota.get("stale"):
            detail += " — telemetry stale"
        row["detail"] = detail
        row["raw"] = raw
        rows.append(row)
    return rows


def _quota_summary(quota: object) -> str:
    """Format only valid, authoritative normalized quota windows for a card.

    Core owns quota acquisition and normalization.  The launcher deliberately
    renders nothing for unavailable, malformed, or empty quota records rather
    than deriving a value from activity, errors, or provider-specific fields.
    """
    if not isinstance(quota, Mapping) or quota.get("state") != "available":
        return ""
    windows = quota.get("windows")
    if not isinstance(windows, Sequence) or isinstance(windows, (str, bytes)):
        return ""
    summaries: list[str] = []
    for window in windows:
        if not isinstance(window, Mapping):
            continue
        label = window.get("label")
        used_percent = window.get("used_percent")
        if not isinstance(label, str) or not label.strip() or not isinstance(used_percent, (int, float)):
            continue
        if isinstance(used_percent, float) and not used_percent.is_integer():
            continue
        value = int(used_percent)
        if not 0 <= value <= 100:
            continue
        summaries.append(f"Quota {label.strip()}: {value}% used")
    return "; ".join(summaries)


def list_harnesses(
    provider: Callable[[str], Mapping[str, Any]] | None = None,
    *,
    telemetry_snapshot: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return stable Batch 3 UI rows from the unified Core snapshot.

    ``provider`` remains supported for tests and existing embedders using the
    former per-harness registry callback contract.
    """
    if provider is not None:
        return [_status(provider, name) for name, _ in SUPPORTED_HARNESSES]
    snapshot = telemetry_snapshot
    if snapshot is None:
        try:
            snapshot = _default_telemetry_provider()
        except Exception:
            return [_status(_default_status_provider, name) for name, _ in SUPPORTED_HARNESSES]
    return _telemetry_rows(snapshot)


def agy_models(
    provider: Callable[[str], Mapping[str, Any]] | None = None,
    *,
    telemetry_snapshot: Mapping[str, Any] | None = None,
) -> list[str]:
    """Read current AGY model IDs from the authoritative status response."""
    record = list_harnesses(provider, telemetry_snapshot=telemetry_snapshot)[2]["raw"]
    candidates = record.get("models")
    if candidates is None and isinstance(record.get("capabilities"), Mapping):
        candidates = record["capabilities"].get("models")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        return []
    result: list[str] = []
    for item in candidates:
        value = item.get("id") if isinstance(item, Mapping) else item
        if isinstance(value, str) and value and value not in result:
            result.append(value)
    return result


__all__ = ["SUPPORTED_HARNESSES", "agy_models", "list_harnesses"]
