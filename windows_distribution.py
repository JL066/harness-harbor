"""Create an unsigned development archive after bundle validation and smoke."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import zipfile

from harbor_runtime import PROTOCOL_VERSION, RUNTIME_VERSION


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def finalize_distribution(bundle, output, build_identity: dict | None = None):
    """Never overwrite an earlier artifact; caller supplies a unique build root."""
    from windows_bundle import validate_bundle
    bundle, output = Path(bundle).resolve(), Path(output).resolve()
    if bundle.parent != output:
        raise ValueError("Bundle must be a direct child of its unique output directory")
    validate_bundle(bundle)
    manifest_path = bundle / "bundle-manifest.json"

    build_info_path = bundle / "build_info.json"
    if build_identity is None and build_info_path.is_file():
        try:
            build_identity = json.loads(build_info_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    if build_identity and build_identity.get("archive_filename"):
        archive_name = build_identity["archive_filename"]
    else:
        archive_name = f"HarnessHarbor-Windows-v{RUNTIME_VERSION}.zip"

    archive = output / archive_name
    checksum = archive.with_suffix(".zip.sha256")
    if any(p.exists() for p in (manifest_path, archive, checksum)):
        raise FileExistsError("Distribution already exists; use a new build directory")
    inventory = {p.relative_to(bundle).as_posix(): file_hash(p)
                 for p in sorted(bundle.rglob("*")) if p.is_file()}
    manifest = {
        "format_version": 1,
        "runtime_version": RUNTIME_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "channel": "development",
        "signed": False,
        "installation_scope": "per-user",
        "entrypoints": {"launcher": "harbor_launcher.exe", "runtime": "runtime/harbor-runtime.exe"},
        "mutable_locations": {"configuration": "%APPDATA%/Harness Harbor",
                              "state": "%LOCALAPPDATA%/Harness Harbor"},
        "external_cli_dependencies": ["codex", "agy", "mcode", "tunnel-client"],
        "files_sha256": inventory,
    }
    if build_identity:
        manifest.update({
            "build_id": build_identity.get("build_id"),
            "display_version": build_identity.get("display_version"),
            "product_version": build_identity.get("product_version"),
            "commit": build_identity.get("commit"),
            "is_dirty": build_identity.get("is_dirty"),
            "build_timestamp": build_identity.get("timestamp"),
        })
    with manifest_path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=True, indent=2)
        stream.write("\n")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as stream:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                stream.write(path, "HarnessHarbor/" + path.relative_to(bundle).as_posix())
    with checksum.open("x", encoding="utf-8") as stream:
        stream.write(file_hash(archive) + "  " + archive.name + "\n")
    return {"archive": str(archive), "checksum": str(checksum), "files": len(inventory)}
