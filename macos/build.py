"""Build a local self-contained app; never installs, publishes or notarizes it."""
import argparse
import hashlib
import os
from pathlib import Path
import plistlib
import platform
import re
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def release_version() -> str:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from harbor_runtime import RUNTIME_VERSION
    from launcher import __version__ as launcher_version

    if launcher_version != RUNTIME_VERSION:
        raise RuntimeError(
            f"Launcher/runtime version drift: {launcher_version!r} != {RUNTIME_VERSION!r}"
        )
    return launcher_version


def macos_release_name(version: str | None = None, arch: str | None = None) -> str:
    machine = (arch or platform.machine()).lower()
    arch = {"aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)
    return f"Harness-Harbor-v{version or release_version()}-macos-{arch}.dmg"


def write_sha256sums(artifacts: tuple[Path, ...], target: Path) -> Path:
    lines = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in sorted(artifacts, key=lambda p: p.name)]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def minimum_macos(runtime):
    versions = [(13, 0)]  # MenuBarExtra and SMAppService
    seen = set()
    for path in runtime.rglob("*"):
        if not path.is_file() or path.resolve() in seen:
            continue
        seen.add(path.resolve())
        with path.open("rb") as stream:
            if stream.read(4) not in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf"):
                continue
        result = subprocess.run(["otool", "-l", str(path)], capture_output=True, text=True, check=True)
        for version in re.findall(r"\bminos\s+(\d+(?:\.\d+)+)", result.stdout):
            versions.append(tuple(map(int, version.split("."))))
    return ".".join(map(str, max(versions)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dmg", action="store_true")
    parser.add_argument("--signing-identity", help="Developer ID identity already installed by the release operator")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("Build on macOS using the target CPU architecture")
    version = release_version()
    # Unique output makes every build recoverable and avoids overwriting artifacts.
    output = ROOT / "dist" / time.strftime("macos-%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=False)
    env = {**os.environ, "PYINSTALLER_CONFIG_DIR": str(ROOT / "build/pyinstaller-cache"),
           "CLANG_MODULE_CACHE_PATH": str(ROOT / "build/clang-cache")}
    command = [sys.executable, "-m", "PyInstaller", "--onedir", "--name", "harbor-runtime",
               "--distpath", str(output), "--workpath", str(output / "work"),
               "--specpath", str(output / "spec"), "--collect-submodules", "mcp.server",
               "--hidden-import", "server_legacy", "--hidden-import", "codex_job_daemon",
               "--hidden-import", "codex_job_worker", "--copy-metadata", "mcp", str(ROOT / "run_runtime.py")]
    if args.signing_identity:
        command += ["--codesign-identity", args.signing_identity]
    subprocess.run(command, cwd=ROOT, env=env, check=True)
    scratch = ROOT / "build/swift"
    subprocess.run(["swift", "build", "--package-path", str(ROOT / "macos"), "--scratch-path", str(scratch), "-c", "release",
                    "-Xswiftc", "-file-prefix-map", "-Xswiftc", f"{ROOT}=.",
                    "-Xswiftc", "-debug-prefix-map", "-Xswiftc", f"{ROOT}=."], env=env, check=True)
    app = output / "Harness Harbor.app"
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources").mkdir()
    shutil.copy2(scratch / "release/HarnessHarbor", contents / "MacOS/Harness Harbor")
    shutil.copytree(output / "harbor-runtime", contents / "Resources/HarborRuntime", symlinks=True)
    iconset = output / "Harbor.iconset"
    iconset.mkdir()
    assets = ROOT / "launcher/assets/brand/generated/png"
    for size in (16, 32, 128, 256, 512):
        shutil.copy2(assets / f"harbor-{size}.png", iconset / f"icon_{size}x{size}.png")
        shutil.copy2(assets / f"harbor-{size * 2}.png", iconset / f"icon_{size}x{size}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(contents / "Resources/Harbor.icns")], check=True)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleExecutable": "Harness Harbor", "CFBundleIdentifier": "com.jl066.harness-harbor",
                      "CFBundleName": "Harness Harbor", "CFBundleDisplayName": "Harness Harbor",
                      "CFBundleIconFile": "Harbor.icns",
                      "CFBundlePackageType": "APPL", "CFBundleShortVersionString": version,
                      "CFBundleVersion": "1", "LSMinimumSystemVersion": minimum_macos(contents / "Resources/HarborRuntime"),
                      "NSHighResolutionCapable": True, "NSPrincipalClass": "NSApplication"}, handle)
    signing = ["codesign", "--sign", args.signing_identity or "-"]
    if args.signing_identity:
        signing += ["--options", "runtime", "--timestamp", "--entitlements", str(ROOT / "macos/entitlements.plist")]
    subprocess.run([*signing, str(app)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    runtime = contents / "Resources/HarborRuntime/harbor-runtime"
    subprocess.run([str(runtime), "version"], env={"PATH": "/usr/bin:/bin"}, cwd=output, check=True)
    if args.dmg:
        stage = output / "dmg-root"
        stage.mkdir()
        shutil.copytree(app, stage / app.name, symlinks=True)
        (stage / "Applications").symlink_to("/Applications", target_is_directory=True)
        dmg = output / macos_release_name(version)
        subprocess.run(["hdiutil", "create", "-volname", "Harness Harbor", "-srcfolder", str(stage),
                        "-format", "UDZO", str(dmg)], check=True)
        write_sha256sums((dmg,), output / "SHA256SUMS.txt")
    print(f"Development application: {app}")
    print("Ad-hoc signed only. Not notarized; not a public release.")


if __name__ == "__main__":
    main()
