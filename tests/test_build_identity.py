"""Unit tests for build identity, version derivation, and UI version display (launcher/build_info.py)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from harbor_runtime import PROTOCOL_VERSION, RUNTIME_VERSION
from launcher import __version__ as launcher_version
from launcher.build_info import (
    derive_build_identity,
    get_build_info,
    get_display_version,
)
from launcher.diagnostics import collect_diagnostics, format_diagnostics_markdown
from build_exe import generate_version_info_file


def test_package_version_matches_runtime_version():
    assert launcher_version == RUNTIME_VERSION == "1.1.5"


def test_derive_build_identity_keeps_public_version_simple_and_builds_distinct():
    meta1 = derive_build_identity(
        timestamp="20260927T100000Z",
        commit="aaaa111",
        dirty=False,
    )
    meta2 = derive_build_identity(
        timestamp="20260927T110000Z",
        commit="bbbb222",
        dirty=True,
        dirty_hash="c3d4e5f6",
    )

    assert meta1["build_id"] != meta2["build_id"]
    assert meta1["display_version"] == meta2["display_version"] == RUNTIME_VERSION
    assert meta1["product_version"] == meta2["product_version"] == RUNTIME_VERSION
    assert meta1["footer_string"] == meta2["footer_string"] == f"v{RUNTIME_VERSION}"
    assert meta1["archive_filename"] == meta2["archive_filename"] == f"HarnessHarbor-Windows-v{RUNTIME_VERSION}.zip"

    assert meta1["build_id"] == "20260927T100000Z-aaaa111"

    assert meta2["build_id"] == "20260927T110000Z-bbbb222-dirty-c3d4e5f6"
    assert meta2["dirty_hash"] == "c3d4e5f6"


def test_get_build_info_from_packaged_file(tmp_path, monkeypatch):
    packaged_meta = {
        "runtime_version": RUNTIME_VERSION,
        "protocol_version": 1,
        "build_id": "20260927T123456Z-fd39926-dirty-99887766",
        "display_version": RUNTIME_VERSION,
        "product_version": RUNTIME_VERSION,
        "footer_string": f"v{RUNTIME_VERSION}",
        "archive_filename": f"HarnessHarbor-Windows-v{RUNTIME_VERSION}.zip",
    }
    meta_file = tmp_path / "build_info.json"
    meta_file.write_text(json.dumps(packaged_meta), encoding="utf-8")

    monkeypatch.setattr("launcher.build_info.find_packaged_metadata_file", lambda: meta_file)

    info = get_build_info(force_refresh=True)
    assert info["build_id"] == packaged_meta["build_id"]
    assert info["display_version"] == packaged_meta["display_version"]
    assert info["footer_string"] == packaged_meta["footer_string"]
    assert get_display_version() == packaged_meta["display_version"]


def test_get_build_info_dev_source_fallback(monkeypatch):
    monkeypatch.setattr("launcher.build_info.find_packaged_metadata_file", lambda: None)

    info = get_build_info(force_refresh=True)
    assert info["build_id"] == "source"
    assert info["display_version"] == RUNTIME_VERSION
    assert info["footer_string"] == f"v{RUNTIME_VERSION}"


def test_diagnostics_includes_build_identity(monkeypatch):
    monkeypatch.setattr(
        "launcher.build_info.get_build_info",
        lambda force_refresh=False: {
            "runtime_version": RUNTIME_VERSION,
            "protocol_version": 1,
            "build_id": "test-build-123",
            "display_version": RUNTIME_VERSION,
            "footer_string": f"v{RUNTIME_VERSION}",
        },
    )

    diag = collect_diagnostics()
    assert diag.get("product_version") == RUNTIME_VERSION
    assert diag.get("build_id") == "test-build-123"

    md = format_diagnostics_markdown(diag)
    assert f"- **Harbor Version**: `{RUNTIME_VERSION}` (build: `test-build-123`)" in md


def test_app_version_label_in_footer(monkeypatch):
    from launcher.ui.app import HarborLauncherApp

    monkeypatch.setattr(
        "launcher.build_info.get_build_info",
        lambda force_refresh=False: {
            "footer_string": f"v{RUNTIME_VERSION}",
        },
    )

    app = HarborLauncherApp.__new__(HarborLauncherApp)
    lbl_text = app._build_version_label()
    assert lbl_text == f"v{RUNTIME_VERSION}"


def test_windows_version_info_uses_public_runtime_version(tmp_path):
    version_file = generate_version_info_file(tmp_path / "version_info.txt", RUNTIME_VERSION)
    content = version_file.read_text(encoding="utf-8")

    numeric_version = tuple(int(part) for part in RUNTIME_VERSION.split(".")) + (0,)
    dotted_version = ".".join(str(part) for part in numeric_version)
    assert f"filevers={numeric_version}" in content
    assert f"prodvers={numeric_version}" in content
    assert f"StringStruct('FileVersion', '{dotted_version}')" in content
    assert f"StringStruct('ProductVersion', '{RUNTIME_VERSION}')" in content
