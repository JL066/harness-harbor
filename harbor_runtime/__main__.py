import json
from pathlib import Path
import signal
import sys


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "version"
    from .protocol import hello, encode
    if mode == "version":
        print(json.dumps(hello()))
        return
    if mode not in {"bridge", "doctor", "mcp", "daemon", "worker"}:
        raise SystemExit("Usage: harbor-runtime bridge|mcp|daemon|doctor|version")
    from .config import configure
    paths, settings, found = configure()
    if mode in {"daemon", "worker"}:
        def stop(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
    if mode == "bridge":
        from .bridge import serve
        serve(paths, settings, found)
    elif mode == "doctor":
        from .lifecycle import doctor
        sys.stdout.buffer.write(encode(doctor(paths, found)))
    elif mode == "mcp":
        from server_legacy import mcp
        mcp.run(transport="stdio")
    elif mode == "daemon":
        paths.jobs_dir().mkdir(parents=True, exist_ok=True)
        from codex_job_daemon import main as run
        run()
    else:
        if len(sys.argv) != 3:
            raise SystemExit("Worker requires one job directory")
        from codex_job_worker import main as run
        run(Path(sys.argv[2]))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Imported libraries may put secrets in exception reprs.
        print("Harbor runtime failed. Check configuration and component status.", file=sys.stderr)
        raise SystemExit(1) from None
