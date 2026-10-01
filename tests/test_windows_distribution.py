from pathlib import Path
import hashlib
import json
import sys
from types import SimpleNamespace
import zipfile

import pytest

from windows_distribution import finalize_distribution
from harbor_runtime import RUNTIME_VERSION

pytestmark = pytest.mark.windows_ci


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    root = tmp_path / "bundle"
    (root / "runtime").mkdir(parents=True)
    (root / "harbor_launcher.exe").write_bytes(b"gui")
    (root / "runtime" / "harbor-runtime.exe").write_bytes(b"sidecar")
    monkeypatch.setitem(sys.modules, "windows_bundle", SimpleNamespace(validate_bundle=lambda path: {}))
    return root


def test_manifest_archive_and_checksum_agree(bundle):
    result = finalize_distribution(bundle, bundle.parent)
    manifest = json.loads((bundle / "bundle-manifest.json").read_text(encoding="utf-8"))
    assert manifest["channel"] == "development" and manifest["signed"] is False
    assert manifest["files_sha256"]["runtime/harbor-runtime.exe"] == hashlib.sha256(b"sidecar").hexdigest()
    archive = Path(result["archive"])
    assert archive.name == f"HarnessHarbor-Windows-v{RUNTIME_VERSION}.zip"
    assert Path(result["checksum"]).read_text().split()[0] == hashlib.sha256(archive.read_bytes()).hexdigest()
    with zipfile.ZipFile(archive) as stream:
        assert "HarnessHarbor/runtime/harbor-runtime.exe" in stream.namelist()
        assert stream.read("HarnessHarbor/harbor_launcher.exe") == b"gui"


def test_repeat_never_overwrites_distribution(bundle):
    result = finalize_distribution(bundle, bundle.parent)
    before = Path(result["archive"]).read_bytes()
    with pytest.raises(FileExistsError):
        finalize_distribution(bundle, bundle.parent)
    assert Path(result["archive"]).read_bytes() == before


def test_bundle_must_belong_to_output(bundle):
    with pytest.raises(ValueError):
        finalize_distribution(bundle, bundle.parent / "other")
    assert not (bundle / "bundle-manifest.json").exists()
