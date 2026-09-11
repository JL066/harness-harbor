from __future__ import annotations

import hashlib
from pathlib import Path
from zipfile import ZipFile

from build_exe import package_windows_release, release_version, windows_release_name
from harbor_runtime import RUNTIME_VERSION
from launcher import __version__ as launcher_version
from macos.build import macos_release_name, release_version as macos_release_version


ROOT = Path(__file__).resolve().parents[1]


def test_packaging_uses_one_shared_version_and_release_names():
    assert release_version() == macos_release_version() == launcher_version == RUNTIME_VERSION
    assert windows_release_name("2.3.4") == "Harness-Harbor-v2.3.4-windows-x64.zip"
    assert macos_release_name("2.3.4", "arm64") == "Harness-Harbor-v2.3.4-macos-arm64.dmg"


def test_windows_onedir_archive_and_checksum_are_deterministic(tmp_path: Path):
    source = tmp_path / "dist" / "harbor_launcher"
    (source / "nested").mkdir(parents=True)
    (source / "harbor_launcher.exe").write_bytes(b"exe")
    (source / "nested" / "runtime.dll").write_bytes(b"dll")

    archive, checksums = package_windows_release(tmp_path)

    with ZipFile(archive) as bundle:
        assert bundle.namelist() == ["harbor_launcher/harbor_launcher.exe", "harbor_launcher/nested/runtime.dll"]
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in bundle.infolist())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert checksums.read_text(encoding="utf-8") == f"{digest}  {archive.name}\n"


def test_release_workflows_upload_artifacts_without_publishing():
    windows = (ROOT / ".github/workflows/windows-build.yml").read_text(encoding="utf-8")
    macos = (ROOT / ".github/workflows/macos-build.yml").read_text(encoding="utf-8")
    assert "python build_exe.py --release" in windows
    assert "actions/upload-artifact@v4" in windows and "actions/upload-artifact@v4" in macos
    assert "action-gh-release" not in windows
    assert "softprops" not in windows
    assert "gh release" not in windows
    assert "Harness-Harbor-v*-macos-*.dmg" in macos


def test_release_artifacts_are_ignored():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("*.app", "*.dmg", "*.exe", "*.msi"):
        assert pattern in gitignore
