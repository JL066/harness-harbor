"""Apple/macOS-inspired UI components for Harness Harbor Launcher."""

from __future__ import annotations

import tkinter as tk
import customtkinter as ctk

from launcher.config import (
    COLOR_CARD_DARK,
    COLOR_CARD_LIGHT,
    COLOR_STATUS_FAILED,
    COLOR_STATUS_HEALTHY,
    COLOR_STATUS_RUNNING,
    COLOR_STATUS_STARTING,
    COLOR_STATUS_STOPPED,
    COLOR_STATUS_WARNING,
    COLOR_TEXT_MUTED_DARK,
    COLOR_TEXT_MUTED_LIGHT,
    COLOR_TEXT_PRIMARY_DARK,
    COLOR_TEXT_PRIMARY_LIGHT,
)


class StatusBadge(ctk.CTkFrame):
    """Rounded pill badge displaying status text with soft tint."""

    def __init__(self, master, status: str = "Stopped", color: str = COLOR_STATUS_STOPPED, **kwargs):
        super().__init__(
            master,
            corner_radius=8,
            fg_color=(self._lighten_color(color, 0.15), self._darken_color(color, 0.2)),
            **kwargs,
        )
        self.label = ctk.CTkLabel(
            self,
            text=status,
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=12, weight="bold"),
            text_color=color,
            padx=10,
            pady=3,
        )
        self.label.pack()

    def update_status(self, status: str, color: str):
        self.configure(fg_color=(self._lighten_color(color, 0.15), self._darken_color(color, 0.2)))
        self.label.configure(text=status, text_color=color)

    @staticmethod
    def _lighten_color(hex_color: str, factor: float) -> str:
        try:
            hex_color = hex_color.lstrip("#")
            r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
            r = int(r + (255 - r) * (1 - factor))
            g = int(g + (255 - g) * (1 - factor))
            b = int(b + (255 - b) * (1 - factor))
            return f"#{min(255, r):02x}{min(255, g):02x}{min(255, b):02x}"
        except Exception:
            return "#E5E5EA"

    @staticmethod
    def _darken_color(hex_color: str, factor: float) -> str:
        try:
            hex_color = hex_color.lstrip("#")
            r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
            r = int(r * factor)
            g = int(g * factor)
            b = int(b * factor)
            return f"#{max(0, r):02x}{max(0, g):02x}{max(0, b):02x}"
        except Exception:
            return "#2C2C2E"


class StatusCard(ctk.CTkFrame):
    """Rounded card representing an individual component's state."""

    def __init__(
        self,
        master,
        title: str,
        initial_status: str = "Stopped",
        initial_detail: str = "Checking...",
        initial_color: str = COLOR_STATUS_STOPPED,
        **kwargs,
    ):
        super().__init__(
            master,
            corner_radius=14,
            fg_color=(COLOR_CARD_LIGHT, COLOR_CARD_DARK),
            border_width=1,
            border_color=("#E5E5EA", "#38383A"),
            **kwargs,
        )

        self.grid_columnconfigure(1, weight=1)

        # Left: Indicator dot canvas
        self.dot_canvas = tk.Canvas(self, width=14, height=14, highlightthickness=0, bg=self._get_canvas_bg())
        self.dot_canvas.grid(row=0, column=0, rowspan=2, padx=(16, 12), pady=14, sticky="w")
        self.dot_id = self.dot_canvas.create_oval(2, 2, 12, 12, fill=initial_color, outline="")

        # Middle: Title and sub-detail
        self.title_label = ctk.CTkLabel(
            self,
            text=title,
            font=ctk.CTkFont(family="Segoe UI Variable Display", size=13, weight="bold"),
            text_color=(COLOR_TEXT_PRIMARY_LIGHT, COLOR_TEXT_PRIMARY_DARK),
            anchor="w",
        )
        self.title_label.grid(row=0, column=1, sticky="w", padx=0, pady=(12, 0))

        self.detail_label = ctk.CTkLabel(
            self,
            text=initial_detail,
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=11),
            text_color=(COLOR_TEXT_MUTED_LIGHT, COLOR_TEXT_MUTED_DARK),
            anchor="w",
        )
        self.detail_label.grid(row=1, column=1, sticky="w", padx=0, pady=(0, 12))

        # Right: Rounded Badge
        self.badge = StatusBadge(self, status=initial_status, color=initial_color)
        self.badge.grid(row=0, column=2, rowspan=2, padx=(8, 16), pady=12, sticky="e")

    def _get_canvas_bg(self) -> str:
        mode = ctk.get_appearance_mode()
        return COLOR_CARD_LIGHT if mode == "Light" else COLOR_CARD_DARK

    def set_status(self, status: str, detail: str, color: str):
        self.dot_canvas.configure(bg=self._get_canvas_bg())
        self.dot_canvas.itemconfig(self.dot_id, fill=color)
        self.detail_label.configure(text=detail)
        self.badge.update_status(status, color)
