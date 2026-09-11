"""Explicit release-operator step; uses an existing Keychain notary profile.

Does not install certificates, change keychains, fetch credentials or publish.
The input app must already be Developer ID signed by build.py.
"""
import argparse
from pathlib import Path
import platform
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("app", type=Path)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--notary-profile", required=True)
    args = parser.parse_args()
    app = args.app.resolve()
    if not app.is_dir() or app.suffix != ".app":
        parser.error("Expected the signed Harness Harbor.app directory")
    archive = app.parent / "Harness-Harbor-notary.zip"
    dmg = app.parent / f"Harness-Harbor-1.1.0-{platform.machine()}.dmg"
    if archive.exists() or dmg.exists():
        parser.error("Release outputs already exist; use a new build directory")
    def run(*command):
        subprocess.run(list(command), check=True)
    def submit(path):
        import json
        response = subprocess.run(["xcrun", "notarytool", "submit", str(path), "--keychain-profile",
                                  args.notary_profile, "--wait", "--output-format", "json"],
                                  capture_output=True, text=True, check=True)
        if json.loads(response.stdout).get("status") != "Accepted":
            raise RuntimeError("Notarization was not accepted; inspect the Apple submission log")
    run("codesign", "--verify", "--deep", "--strict", str(app))
    run("ditto", "-c", "-k", "--keepParent", str(app), str(archive))
    submit(archive)
    run("xcrun", "stapler", "staple", str(app))
    run("spctl", "--assess", "--type", "execute", "--verbose", str(app))
    import shutil
    stage = app.parent / "notarized-dmg-root"
    stage.mkdir()
    shutil.copytree(app, stage / app.name, symlinks=True)
    (stage / "Applications").symlink_to("/Applications", target_is_directory=True)
    run("hdiutil", "create", "-volname", "Harness Harbor", "-srcfolder", str(stage), "-format", "UDZO", str(dmg))
    run("codesign", "--sign", args.identity, "--timestamp", str(dmg))
    submit(dmg)
    run("xcrun", "stapler", "staple", str(dmg))
    run("xcrun", "stapler", "validate", str(dmg))
    print(dmg)


if __name__ == "__main__":
    main()
