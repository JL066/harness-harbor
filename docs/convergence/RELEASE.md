# Release artifact contract

The release version is read from `launcher.__version__` and
`harbor_runtime.RUNTIME_VERSION`. The packaging scripts fail before building if
those values drift. The current shared version is `1.1.0`.

## macOS

Run the existing native build from an Apple Silicon macOS host:

```sh
python macos/build.py --dmg
```

The script keeps the timestamped `dist/macos-YYYYMMDD-HHMMSS/` output layout,
the existing Swift/PyInstaller bundle, and the existing ad-hoc signing policy.
With `--dmg`, it writes:

```text
Harness-Harbor-v1.1.0-macos-arm64.dmg
SHA256SUMS.txt
```

The checksum file contains one `sha256  filename` line for the DMG. The
workflow uploads the DMG and checksum file as Actions artifacts; it does not
publish or create a release.

## Windows

Run the existing PyInstaller onedir build with the release flag on a Windows
host:

```powershell
python build_exe.py --release
```

PyInstaller still produces `dist/harbor_launcher/harbor_launcher.exe`. Because
the build is onedir, the release artifact is a deterministic folder archive:

```text
Harness-Harbor-v1.1.0-windows-x64.zip
SHA256SUMS.txt
```

The archive contains the complete `harbor_launcher/` directory, including the
executable and its PyInstaller dependencies. This preserves the existing launcher
packaging; it does not establish a standalone shared Core installation. Its entries use sorted paths and fixed
timestamps so repeated packaging of the same onedir output produces the same
archive bytes. The workflow runs the shared and Windows launcher pytest
suites, then uploads the archive and checksum file as an Actions artifact.

`.app`, `.dmg`, `.exe`, and `.msi` outputs are ignored by Git. Release
artifacts remain local or in CI artifact storage until an operator performs a
separate, explicitly authorized publication step.

## Independent platform releases

Use `macos-v<version>` and `windows-v<version>` tags; previews append
`-preview.N` and are GitHub prereleases. Shared runtime version stays `1.1.0`.
Only macOS DMG/checksums belong in a macOS Release. Windows binaries require
separate native acceptance; do not imply they passed from Mac results.
