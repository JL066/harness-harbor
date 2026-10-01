"""Build and verify an unsigned, self-contained Windows development bundle."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

RUNTIME_HIDDEN_IMPORTS = (
    "launcher.credential_store", "launcher.harnesses", "launcher.tunnel",
    "launcher.tunnel_manager", "launcher.tunnel_profile", "launcher.ui.setup_wizard",
    "control_plane", "launcher.runtime_client", "launcher.runtime_backend", "runtime_bootstrap",
    "launcher.build_info", "launcher.exit_guard", "runtime_liveness",
)


def generate_version_info_file(output_path: Path, display_version: str) -> Path:
    from harbor_runtime import RUNTIME_VERSION

    numeric_parts = [int(part) for part in RUNTIME_VERSION.split(".")]
    if len(numeric_parts) != 3:
        raise ValueError(f"RUNTIME_VERSION must have three numeric parts: {RUNTIME_VERSION!r}")
    windows_version = (*numeric_parts, 0)
    windows_version_text = ".".join(str(part) for part in windows_version)
    version_info_content = f'''VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={windows_version},
    prodvers={windows_version},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '040904B0',
          [
            StringStruct('CompanyName', 'Harness Harbor'),
            StringStruct('FileDescription', 'Harness Harbor Launcher'),
            StringStruct('FileVersion', '{windows_version_text}'),
            StringStruct('InternalName', 'harbor_launcher'),
            StringStruct('LegalCopyright', 'Copyright (c) 2026'),
            StringStruct('OriginalFilename', 'harbor_launcher.exe'),
            StringStruct('ProductName', 'Harness Harbor'),
            StringStruct('ProductVersion', '{display_version}')
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
'''
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(version_info_content, encoding="utf-8")
    return output_path


def isolated_build_environment(directory):
    directory = Path(directory).resolve()
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE",
        "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "NUMBER_OF_PROCESSORS"}}
    for key, name in {"APPDATA": "roaming", "LOCALAPPDATA": "local", "USERPROFILE": "home",
                      "HOME": "home", "TMP": "temp", "TEMP": "temp",
                      "PYINSTALLER_CONFIG_DIR": "pyinstaller-cache"}.items():
        path = directory / name
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    windows = Path(env.get("SYSTEMROOT", "C:/Windows"))
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), str(Path(sys.base_prefix)),
                                   str(windows / "System32"), str(windows)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["HARBOR_RUNTIME_MODE"] = "packaged"
    for key, name in {"HARBOR_CODEX_EXE": "codex.exe", "HARBOR_AGY_EXE": "agy.exe",
                      "HARBOR_MINIMAX_CLI_EXE": "mcode.cmd", "HARBOR_TUNNEL_EXE": "tunnel-client.exe"}.items():
        env[key] = str(directory / "absent-cli" / name)
    return env


def build():
    if sys.platform != "win32":
        raise RuntimeError("Build this bundle on Windows")
    import customtkinter
    from launcher.build_info import derive_build_identity
    root = Path(__file__).resolve().parent
    build_meta = derive_build_identity(root)
    stamp = build_meta["build_id"]
    output = root / "dist" / ("windows-" + stamp)
    output.mkdir(parents=True, exist_ok=False)
    env = isolated_build_environment(output / "build-user")
    hooks = root / "pyinstaller_hooks"
    assets = root / "launcher" / "assets"
    icon = assets / "brand/generated/ico/harbor-system.ico"
    if not icon.exists():
        icon = assets / "brand/generated/ico/harbor.ico"
    version_file = output / "spec" / "version_info.txt"
    generate_version_info_file(version_file, build_meta["display_version"])
    common = [sys.executable, "-m", "PyInstaller", "--onedir",
              "--distpath", str(output / "components"), "--workpath", str(output / "work"),
              "--specpath", str(output / "spec"), "--paths", str(root)]
    gui = common + ["--windowed", "--name", "harbor_launcher", "--additional-hooks-dir", str(hooks),
                    "--runtime-hook", str(hooks / "rthook_tkinter_portable.py"),
                    "--add-data", str(Path(customtkinter.__file__).parent) + ";customtkinter",
                    "--add-data", str(assets) + ";launcher/assets",
                    "--version-file", str(version_file)]
    for module in ("pystray", "PIL", "customtkinter", "tkinter.font", "tkinter.filedialog",
                   "tkinter.messagebox", "tkinter.ttk", *RUNTIME_HIDDEN_IMPORTS):
        gui += ["--hidden-import", module]
    for source, destination in (("tcl8.6", "_tcl_data"), ("tk8.6", "_tk_data"), ("tcl8", "tcl8")):
        path = Path(sys.base_prefix) / "tcl" / source
        if path.is_dir():
            gui += ["--add-data", str(path) + ";" + destination]
    if icon.exists():
        gui += ["--icon", str(icon)]
    gui += [str(root / "run_launcher.py")]
    runtime = common + ["--console", "--name", "harbor-runtime", "--collect-submodules", "mcp.server",
                        "--copy-metadata", "mcp"]
    for module in ("server_legacy", "codex_job_daemon", "codex_job_worker"):
        runtime += ["--hidden-import", module]
    runtime += [str(root / "run_runtime.py")]
    print("Development build directory:", output, flush=True)
    for label, command in (("launcher", gui), ("runtime", runtime)):
        log = output / ("build-" + label + ".log")
        print("Building", label, "(log:", log, ")", flush=True)
        with log.open("x", encoding="utf-8") as stream:
            result = subprocess.run(command, cwd=root, env=env, stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            print(log.read_text(encoding="utf-8", errors="replace")[-12000:])
            raise RuntimeError(label + " PyInstaller build failed; see " + str(log))
    bundle = output / "bundle"
    shutil.copytree(output / "components" / "harbor_launcher", bundle)
    shutil.copytree(output / "components" / "harbor-runtime", bundle / "runtime")
    build_json = json.dumps(build_meta, indent=2) + "\n"
    (output / "build_info.json").write_text(build_json, encoding="utf-8")
    (bundle / "build_info.json").write_text(build_json, encoding="utf-8")
    (bundle / "runtime" / "build_info.json").write_text(build_json, encoding="utf-8")
    from windows_bundle import validate_bundle, smoke_bundle
    validate_bundle(bundle)
    smoke = smoke_bundle(bundle, output / "smoke-user")
    with (output / "smoke-results.json").open("x", encoding="utf-8") as stream:
        json.dump(smoke, stream, indent=2)
    from windows_distribution import finalize_distribution
    distribution = finalize_distribution(bundle, output, build_identity=build_meta)
    print(json.dumps(distribution, indent=2), flush=True)
    return output


if __name__ == "__main__":
    build()
