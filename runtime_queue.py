"""Canonical runtime queue root resolution for every Harbor component."""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

JOBS_DIR_ENV = "HARBOR_JOBS_DIR"


@dataclass(frozen=True)
class QueueRoot:
    path: Path
    source: str
    canonical_path: str
    fingerprint: str

    def as_dict(self) -> dict[str, str]:
        return {
            "jobs_dir": str(self.path),
            "source": self.source,
            "canonical_path": self.canonical_path,
            "fingerprint": self.fingerprint,
        }


def _canonicalize(path: Path) -> Path:
    """Return a stable absolute path without requiring it to exist."""
    return Path(os.path.realpath(os.path.abspath(os.fspath(path))))


def queue_root_fingerprint(path: Path) -> str:
    canonical_path = os.path.normcase(str(_canonicalize(path)))
    return hashlib.sha256(canonical_path.encode("utf-8")).hexdigest()[:16]


def describe_queue_root(path: Path, source: str) -> QueueRoot:
    resolved = _canonicalize(path)
    return QueueRoot(
        resolved,
        source,
        os.path.normcase(str(resolved)),
        queue_root_fingerprint(resolved),
    )


def resolve_queue_root(
    project_root: Path,
    environ: Mapping[str, str] | None = None,
) -> QueueRoot:
    """Resolve the one queue identity used by producers, readers, and workers."""
    env = os.environ if environ is None else environ
    configured = env.get(JOBS_DIR_ENV)
    if configured is not None and configured.strip():
        candidate = Path(configured.strip()).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"{JOBS_DIR_ENV} must be an absolute path: {configured!r}")
        source = "environment"
    elif env.get("HARBOR_STATE_DIR", "").strip():
        root = Path(env["HARBOR_STATE_DIR"]).expanduser()
        if not root.is_absolute():
            raise ValueError("HARBOR_STATE_DIR must be an absolute path")
        candidate = root / "jobs"
        source = "state_environment"
    elif sys.platform == "darwin":
        # Direct stdio/daemon entrypoints and the packaged App must agree.
        from harbor_platform.paths import PlatformPaths
        candidate = PlatformPaths(project_root, env).jobs_dir()
        source = "application_support"
    else:
        candidate = Path(project_root) / ".jobs"
        source = "project_fallback"

    return describe_queue_root(candidate, source)


def queue_root_matches(state: Mapping[str, object], root: QueueRoot | Path) -> bool:
    """Accept legacy jobs, but reject jobs explicitly created for another root."""
    recorded = state.get("queue_root_fingerprint")
    expected = root.fingerprint if isinstance(root, QueueRoot) else queue_root_fingerprint(root)
    return recorded is None or recorded == expected
