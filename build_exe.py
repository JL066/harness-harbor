"""PyInstaller build script for Harness Harbor Launcher."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

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


def build():
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


if __name__ == "__main__":
    build()
