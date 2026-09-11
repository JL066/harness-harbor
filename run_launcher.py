"""Entrypoint script for Harness Harbor Launcher."""

import sys
from pathlib import Path

# Ensure root package is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from launcher.ui.app import HarborLauncherApp


def main():
    app = HarborLauncherApp()
    app.mainloop()


if __name__ == "__main__":
    main()
