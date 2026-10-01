import os
from pathlib import Path
from pathlib import PureWindowsPath
import shlex
import shutil
import subprocess
import sys


def serialize_command(argv):
    """Serialize the tunnel profile command string for its shell-like parser.

    This is a tunnel configuration string, not a CreateProcess command line.
    Windows absolute paths therefore use POSIX separators before shell quoting;
    macOS retains the existing ``shlex.join`` behavior.
    """
    if sys.platform != "win32":
        return shlex.join(argv)
    normalized = [
        PureWindowsPath(value).as_posix() if PureWindowsPath(value).is_absolute() else value
        for value in argv
    ]
    return shlex.join(normalized)


def executable_candidates(name):
    detected = shutil.which(name)
    roots = [Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/usr/bin"),
             Path.home() / ".local/bin", Path.home() / ".npm-global/bin",
             Path.home() / ".volta/bin"]
    roots += sorted((Path.home() / ".nvm/versions/node").glob("*/bin"), reverse=True)
    return ([Path(detected)] if detected else []) + [root / name for root in roots]


def resolve_executable(name, override=""):
    candidates = [Path(override).expanduser()] if override else executable_candidates(name)
    for path in candidates:
        if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
            return str(path.absolute())  # preserve symlink name for multicall launchers
    return None


def child_path(executables=()):
    roots = [str(Path(p).parent) for p in executables if p]
    roots += ["/opt/homebrew/bin", "/usr/local/bin", str(Path.home() / ".local/bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    roots += os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(dict.fromkeys(p for p in roots if p and Path(p).is_absolute()))
