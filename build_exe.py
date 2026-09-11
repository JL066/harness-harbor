"""PyInstaller build script for Harness Harbor Launcher."""

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

# These modules are loaded indirectly by the UI at runtime.  Keep the list
# explicit so a frozen release has the same setup, credential, tunnel,
# harness, and custom-Codex route behavior as a source checkout.
RUNTIME_HIDDEN_IMPORTS = (
    "launcher.credential_store",
    "launcher.harnesses",
    "launcher.tunnel",
    "launcher.tunnel_manager",
    "launcher.tunnel_profile",
    "launcher.ui.setup_wizard",
    "control_plane",
)


def release_version() -> str:
    from harbor_runtime import RUNTIME_VERSION
    from launcher import __version__ as launcher_version

    if launcher_version != RUNTIME_VERSION:
        raise RuntimeError(
            f"Launcher/runtime version drift: {launcher_version!r} != {RUNTIME_VERSION!r}"
        )
    return launcher_version


def windows_release_name(version: str | None = None) -> str:
    return f"Harness-Harbor-v{version or release_version()}-windows-x64.zip"


def write_sha256sums(artifacts: tuple[Path, ...], target: Path) -> Path:
    lines = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in sorted(artifacts, key=lambda p: p.name)]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def package_windows_release(root_dir: Path) -> tuple[Path, Path]:
    source = root_dir / "dist" / "harbor_launcher"
    archive = root_dir / "dist" / windows_release_name()
    if not source.is_dir():
        raise FileNotFoundError(f"PyInstaller output directory is missing: {source}")
    if archive.exists():
        raise FileExistsError(f"Refusing to overwrite release artifact: {archive}")
    with ZipFile(archive, "w", ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                member = Path("harbor_launcher") / path.relative_to(source)
                info = ZipInfo(str(member).replace("\\", "/"), (1980, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                bundle.writestr(info, path.read_bytes())
    checksums = write_sha256sums((archive,), root_dir / "dist" / "SHA256SUMS.txt")
    return archive, checksums


def build(release: bool = False):
    root_dir = Path(__file__).resolve().parent
    try:
        import customtkinter
    except ImportError as exc:
        raise RuntimeError("Launcher packaging requires requirements-launcher.txt") from exc
    ctk_path = Path(customtkinter.__file__).resolve().parent

    print(f"Building Harbor Launcher from {root_dir}")
    print(f"CustomTkinter path: {ctk_path}")

    # Separator for --add-data is ';' on Windows
    add_data_arg = f"{ctk_path};customtkinter"
    assets_path = root_dir / "launcher" / "assets"
    hooks_path = root_dir / "pyinstaller_hooks"
    tcl_root = Path(sys.base_prefix) / "tcl"
    ico_path = assets_path / "brand" / "generated" / "ico" / "harbor-system.ico"
    if not ico_path.exists():
        ico_path = assets_path / "brand" / "generated" / "ico" / "harbor.ico"

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",             # onedir is faster and more reliable on Windows
        "--windowed",           # no terminal console
        "--name",
        "harbor_launcher",
        "--add-data",
        add_data_arg,
        "--add-data",
        f"{assets_path};launcher/assets",
        "--additional-hooks-dir",
        str(hooks_path),
        "--runtime-hook",
        str(hooks_path / "rthook_tkinter_portable.py"),
    ]
    for module in (
        "pystray", "PIL", "customtkinter", "tkinter.font", "tkinter.filedialog",
        "tkinter.messagebox", "tkinter.ttk", *RUNTIME_HIDDEN_IMPORTS,
    ):
        cmd.extend(["--hidden-import", module])
    for source, destination in (
        (tcl_root / "tcl8.6", "_tcl_data"),
        (tcl_root / "tk8.6", "_tk_data"),
        (tcl_root / "tcl8", "tcl8"),
    ):
        if source.is_dir():
            cmd.extend(["--add-data", f"{source};{destination}"])

    if ico_path.exists():
        cmd.extend(["--icon", str(ico_path)])

    cmd.append(str(root_dir / "run_launcher.py"))

    print("Running command:", " ".join(cmd))
    res = subprocess.run(cmd, cwd=str(root_dir))
    if res.returncode != 0:
        print("PyInstaller build failed!")
        sys.exit(res.returncode)

    print("\nBuild succeeded!")
    dist_exe = root_dir / "dist" / "harbor_launcher" / "harbor_launcher.exe"
    print(f"Standalone executable located at: {dist_exe}")
    if release:
        archive, checksums = package_windows_release(root_dir)
        print(f"Release archive: {archive}")
        print(f"Checksums: {checksums}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", action="store_true", help="Package the onedir output as a versioned release zip")
    build(release=parser.parse_args().release)
