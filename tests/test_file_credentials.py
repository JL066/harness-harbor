"""Checks for the macOS private file credential backend."""

from __future__ import annotations

import os
import stat
import sys

import pytest

import launcher.credential_store as credential_store
from launcher.credential_store import (
    CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY,
    CredentialOperationError,
    MacOSFileCredentialBackend,
    get_default_backend,
)


@pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "getuid"),
    reason="requires POSIX ownership and permission checks",
)
def test_file_backend_is_lazy_private_and_atomic(tmp_path):
    backend = MacOSFileCredentialBackend(tmp_path / "settings" / "credentials")
    target = CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY
    settings_dir = tmp_path / "settings"
    credentials_dir = settings_dir / "credentials"
    credential_path = credentials_dir / "tunnel-runtime-key.txt"

    assert backend.read(target) is None
    assert not backend.exists(target)
    assert not backend.delete(target)
    assert not settings_dir.exists()

    backend.store(target, "private-value")
    assert backend.read(target) == "private-value"
    assert stat.S_IMODE(credentials_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(credential_path.stat().st_mode) == 0o600
    assert not list(credentials_dir.glob("*.tmp"))

    backend.store(target, "updated-value")
    assert backend.read(target) == "updated-value"
    assert backend.delete(target)
    assert backend.read(target) is None


@pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "getuid"),
    reason="requires POSIX ownership and permission checks",
)
def test_file_backend_rejects_unsafe_files(tmp_path):
    backend = MacOSFileCredentialBackend(tmp_path / "credentials")
    target = CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY
    with pytest.raises(ValueError):
        backend.store(target, " \t\n")
    backend.store(target, "private-value")
    path = tmp_path / "credentials" / "tunnel-runtime-key.txt"

    path.chmod(0o644)
    with pytest.raises(CredentialOperationError):
        backend.read(target)

    path.chmod(0o600)
    hardlink = tmp_path / "hardlink"
    os.link(path, hardlink)
    with pytest.raises(CredentialOperationError):
        backend.read(target)
    hardlink.unlink()

    path.write_bytes(b"x" * (MacOSFileCredentialBackend._MAX_SECRET_BYTES + 1))
    with pytest.raises(CredentialOperationError):
        backend.read(target)

    path.write_bytes(b" \n")
    with pytest.raises(CredentialOperationError):
        backend.read(target)

    path.unlink()
    path.symlink_to(tmp_path / "outside")
    with pytest.raises(CredentialOperationError):
        backend.exists(target)


def test_macos_default_backend_is_private_file_backend(monkeypatch):
    monkeypatch.setattr(credential_store.sys, "platform", "darwin")
    assert isinstance(get_default_backend(), MacOSFileCredentialBackend)
