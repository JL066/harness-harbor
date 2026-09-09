# -*- mode: python ; coding: utf-8 -*-
"""Portable PyInstaller specification for the public Harbor Launcher build."""

from pathlib import Path
import sys

import customtkinter


ROOT_DIR = Path(SPECPATH).resolve()
ASSETS_DIR = ROOT_DIR / "launcher" / "assets"
CUSTOMTKINTER_DIR = Path(customtkinter.__file__).resolve().parent
HOOKS_DIR = ROOT_DIR / "pyinstaller_hooks"
TCL_ROOT = Path(sys.base_prefix) / "tcl"
ICON_PATH = ASSETS_DIR / "brand" / "generated" / "ico" / "harbor-system.ico"
if not ICON_PATH.exists():
    ICON_PATH = ASSETS_DIR / "brand" / "generated" / "ico" / "harbor.ico"

# See build_exe.py.  These imports cover modules reached through lazy imports
# after the GUI is running, including the custom Codex route in control_plane.
RUNTIME_HIDDEN_IMPORTS = [
    "launcher.credential_store",
    "launcher.harnesses",
    "launcher.tunnel",
    "launcher.tunnel_manager",
    "launcher.tunnel_profile",
    "launcher.ui.setup_wizard",
    "control_plane",
]
GUI_HIDDEN_IMPORTS = ["tkinter.font", "tkinter.filedialog", "tkinter.messagebox", "tkinter.ttk"]
DATA_FILES = [(str(CUSTOMTKINTER_DIR), "customtkinter"), (str(ASSETS_DIR), "launcher/assets")]
for source, destination in ((TCL_ROOT / "tcl8.6", "_tcl_data"), (TCL_ROOT / "tk8.6", "_tk_data"), (TCL_ROOT / "tcl8", "tcl8")):
    if source.is_dir():
        DATA_FILES.append((str(source), destination))


a = Analysis(
    [str(ROOT_DIR / "run_launcher.py")],
    pathex=[str(ROOT_DIR)],
    binaries=[],
    datas=DATA_FILES,
    hiddenimports=["pystray", "PIL", "customtkinter", *GUI_HIDDEN_IMPORTS, *RUNTIME_HIDDEN_IMPORTS],
    hookspath=[str(HOOKS_DIR)],
    hooksconfig={},
    runtime_hooks=[str(HOOKS_DIR / "rthook_tkinter_portable.py")],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="harbor_launcher",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(ICON_PATH)] if ICON_PATH.exists() else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="harbor_launcher",
)
