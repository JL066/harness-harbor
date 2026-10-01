"""Harbor build identity, metadata resolution, and version formatting."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from harbor_runtime import PROTOCOL_VERSION, RUNTIME_VERSION

_CACHED_BUILD_INFO: dict[str, Any] | None = None


def _get_git_commit(root_dir: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root_dir),
            capture_output=True,
            text=True,
            timeout=2.0,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _get_git_dirty_info(root_dir: Path) -> tuple[bool, str]:
    """Check if git working tree is dirty and compute a deterministic hash."""
    try:
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(root_dir),
            capture_output=True,
            text=True,
            timeout=3.0,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if status_res.returncode == 0:
            output = status_res.stdout.strip()
            if not output:
                return False, ""
            diff_res = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=str(root_dir),
                capture_output=True,
                timeout=3.0,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            raw = diff_res.stdout if diff_res.returncode == 0 and diff_res.stdout else output.encode("utf-8")
            digest = hashlib.sha256(raw).hexdigest()[:8]
            return True, digest
    except Exception:
        pass
    return False, ""


def derive_build_identity(
    root_dir: Path | str | None = None,
    *,
    timestamp: str | None = None,
    commit: str | None = None,
    dirty: bool | None = None,
    dirty_hash: str | None = None,
) -> dict[str, Any]:
    """Derive unique build identity and display metadata.

    Safe for reproducible synthetic tests as well as canonical build-time execution.
    """
    root = Path(root_dir or Path(__file__).resolve().parent.parent)

    if timestamp is None:
        utc_now = datetime.now(timezone.utc)
        ts_str = utc_now.strftime("%Y%m%dT%H%M%SZ")
        iso_str = utc_now.isoformat()
    else:
        ts_str = timestamp
        iso_str = timestamp

    commit_str = commit or _get_git_commit(root)

    if dirty is None:
        is_dirty, d_hash = _get_git_dirty_info(root)
    else:
        is_dirty = bool(dirty)
        d_hash = dirty_hash or ("dirty" if is_dirty else "")

    if is_dirty:
        dirty_suffix = f"dirty-{d_hash}" if d_hash else "dirty"
        build_id = f"{ts_str}-{commit_str}-{dirty_suffix}"
    else:
        build_id = f"{ts_str}-{commit_str}"

    display_version = RUNTIME_VERSION
    footer_string = f"v{RUNTIME_VERSION}"
    archive_filename = f"HarnessHarbor-Windows-v{RUNTIME_VERSION}.zip"

    return {
        "runtime_version": RUNTIME_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "build_id": build_id,
        "commit": commit_str,
        "timestamp": iso_str,
        "timestamp_tag": ts_str,
        "is_dirty": is_dirty,
        "dirty_hash": d_hash if is_dirty else None,
        "display_version": display_version,
        "product_version": display_version,
        "footer_string": footer_string,
        "archive_filename": archive_filename,
    }


def find_packaged_metadata_file() -> Path | None:
    """Find build_info.json in packaged locations."""
    candidates = []

    bundle_root = os.environ.get("HARBOR_BUNDLE_ROOT")
    if bundle_root:
        candidates.append(Path(bundle_root) / "build_info.json")

    try:
        candidates.append(Path(sys.executable).parent / "build_info.json")
    except Exception:
        pass

    try:
        candidates.append(Path(__file__).resolve().parent.parent / "build_info.json")
    except Exception:
        pass

    for path in candidates:
        if path.is_file():
            return path
    return None


def get_build_info(force_refresh: bool = False) -> dict[str, Any]:
    """Return authoritative build information at runtime.

    In packaged bundles, reads from build_info.json.
    In dev/source checkouts, derives the fallback from ``RUNTIME_VERSION``.
    """
    global _CACHED_BUILD_INFO
    if _CACHED_BUILD_INFO is not None and not force_refresh:
        return _CACHED_BUILD_INFO

    meta_file = find_packaged_metadata_file()
    if meta_file:
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("display_version"):
                _CACHED_BUILD_INFO = data
                return data
        except Exception:
            pass

    # Source / dev fallback
    root = Path(__file__).resolve().parent.parent
    commit = _get_git_commit(root)
    source_display = RUNTIME_VERSION
    source_footer = f"v{RUNTIME_VERSION}"
    info = {
        "runtime_version": RUNTIME_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "build_id": "source",
        "commit": commit,
        "timestamp": None,
        "is_dirty": False,
        "dirty_hash": None,
        "display_version": source_display,
        "product_version": source_display,
        "footer_string": source_footer,
        "archive_filename": f"HarnessHarbor-Windows-v{RUNTIME_VERSION}.zip",
    }
    _CACHED_BUILD_INFO = info
    return info


def get_display_version() -> str:
    """Return the display/product version string."""
    return str(get_build_info().get("display_version") or RUNTIME_VERSION)
