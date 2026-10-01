"""Entrypoint script for Harness Harbor Launcher."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the checkout/package root is importable before the dependency-free
# bootstrap runs.  The launcher package itself is imported only after that
# boundary succeeds.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime_bootstrap import bootstrap


def _show_bootstrap_error(error: Exception) -> None:
    message = str(error).strip() or "Packaged runtime bootstrap failed."
    try:
        import tkinter.messagebox as messagebox
        messagebox.showerror("Harness Harbor", message)
    except Exception:
        print(f"Harness Harbor startup failed: {message}", file=sys.stderr)


def main():
    try:
        bootstrap()
    except Exception as exc:
        _show_bootstrap_error(exc)
        return 1

    # Keep this import below bootstrap: packaged startup must fail closed and
    # must never construct the UI or a backend after a failed boundary check.
    from launcher.ui.app import HarborLauncherApp

    app = HarborLauncherApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
