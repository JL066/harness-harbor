"""Windows System Tray integration for Harness Harbor Launcher."""

from __future__ import annotations

import threading
from typing import Callable

import pystray
from PIL import Image, ImageDraw


from launcher.config import BRAND_TRAY_ICON


def get_tray_icon_image() -> Image.Image:
    """Load authentic brand icon for system tray (Tiny master)."""
    if BRAND_TRAY_ICON.exists():
        try:
            return Image.open(BRAND_TRAY_ICON).convert("RGBA")
        except Exception:
            pass

    # Fallback to simple circle
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, 30, 30], fill="#0071E3")
    return img


class HarborTrayManager:
    """Manages system tray lifecycle and context actions."""

    def __init__(
        self,
        on_open: Callable[[], None],
        on_restart: Callable[[], None],
        on_stop: Callable[[], None],
        on_exit: Callable[[], None],
    ):
        self.on_open = on_open
        self.on_restart = on_restart
        self.on_stop = on_stop
        self.on_exit = on_exit

        self.icon: pystray.Icon | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        """Start system tray in a background thread."""
        img = get_tray_icon_image()
        menu = pystray.Menu(
            pystray.MenuItem("Open Harbor Launcher", lambda icon, item: self.on_open(), default=True),
            pystray.MenuItem("Restart Harbor", lambda icon, item: self.on_restart()),
            pystray.MenuItem("Stop Harbor", lambda icon, item: self.on_stop()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit Launcher", lambda icon, item: self._handle_exit()),
        )
        self.icon = pystray.Icon(
            name="Harness-Harbor-Launcher",
            icon=img,
            title="Harness Harbor",
            menu=menu,
        )
        self._thread = threading.Thread(target=self.icon.run, daemon=True)
        self._thread.start()

    def _handle_exit(self):
        if self.icon:
            self.icon.stop()
        self.on_exit()

    def stop(self):
        if self.icon:
            self.icon.stop()
            self.icon = None
