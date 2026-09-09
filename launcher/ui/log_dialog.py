"""Log viewer dialog for Harness Harbor."""

from __future__ import annotations

import tkinter as tk
import customtkinter as ctk

from launcher.config import DAEMON_LOG, TUNNEL_LOG
from launcher.log_reader import read_log_tail


class LogDialog(ctk.CTkToplevel):
    """Modern rounded log viewer dialog."""

    def __init__(self, master=None):
        super().__init__(master)

        self.title("Harbor Runtime Logs")
        self.geometry("780x560")
        self.minsize(640, 420)

        # Center on parent window if possible
        if master:
            x = master.winfo_x() + (master.winfo_width() // 2) - 390
            y = master.winfo_y() + (master.winfo_height() // 2) - 280
            self.geometry(f"+{max(50, x)}+{max(50, y)}")

        self.current_log_type = "tunnel"
        self.max_lines = 300
        self.auto_refresh_job = None

        self._init_ui()
        self.refresh_log()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _init_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Top Control Bar
        top_frame = ctk.CTkFrame(self, fg_color="transparent")
        top_frame.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        top_frame.grid_columnconfigure(2, weight=1)

        # Log selector segmented button
        self.log_seg = ctk.CTkSegmentedButton(
            top_frame,
            values=["Tunnel Supervisor", "Job Daemon"],
            command=self._on_log_type_change,
            corner_radius=8,
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=12, weight="bold"),
        )
        self.log_seg.set("Tunnel Supervisor")
        self.log_seg.grid(row=0, column=0, sticky="w")

        # Lines selector
        self.lines_seg = ctk.CTkSegmentedButton(
            top_frame,
            values=["100", "300", "500"],
            command=self._on_lines_change,
            corner_radius=8,
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=12),
        )
        self.lines_seg.set("300")
        self.lines_seg.grid(row=0, column=1, padx=(12, 0), sticky="w")

        # Actions on right
        btn_frame = ctk.CTkFrame(top_frame, fg_color="transparent")
        btn_frame.grid(row=0, column=3, sticky="e")

        self.refresh_btn = ctk.CTkButton(
            btn_frame,
            text="Refresh",
            width=70,
            height=28,
            corner_radius=8,
            command=self.refresh_log,
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=12),
        )
        self.refresh_btn.pack(side="left", padx=4)

        self.copy_btn = ctk.CTkButton(
            btn_frame,
            text="Copy Tail",
            width=80,
            height=28,
            corner_radius=8,
            command=self._copy_log,
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=12),
        )
        self.copy_btn.pack(side="left", padx=4)

        # Text Area (Monospace)
        text_frame = ctk.CTkFrame(self, corner_radius=12)
        text_frame.grid(row=1, column=0, sticky="nsew", padx=16, pady=8)
        text_frame.grid_columnconfigure(0, weight=1)
        text_frame.grid_rowconfigure(0, weight=1)

        self.text_box = ctk.CTkTextbox(
            text_frame,
            wrap="none",
            font=ctk.CTkFont(family="Consolas", size=11),
            corner_radius=10,
        )
        self.text_box.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

        # Bottom Info Bar
        self.info_label = ctk.CTkLabel(
            self,
            text="Encoding: ... | Lines: 0",
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=11),
            text_color=("#86868B", "#98989D"),
            anchor="w",
        )
        self.info_label.grid(row=2, column=0, sticky="w", padx=20, pady=(4, 12))

    def _on_log_type_change(self, val: str):
        self.current_log_type = "tunnel" if val == "Tunnel Supervisor" else "daemon"
        self.refresh_log()

    def _on_lines_change(self, val: str):
        self.max_lines = int(val)
        self.refresh_log()

    def refresh_log(self):
        target_path = TUNNEL_LOG if self.current_log_type == "tunnel" else DAEMON_LOG
        lines, encoding = read_log_tail(target_path, max_lines=self.max_lines)

        self.text_box.configure(state="normal")
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", "\n".join(lines))
        self.text_box.see("end")
        self.text_box.configure(state="disabled")

        self.info_label.configure(
            text=f"Log: {target_path.name} | Detected Encoding: {encoding} | Lines Shown: {len(lines)}"
        )

    def _copy_log(self):
        self.clipboard_clear()
        self.clipboard_append(self.text_box.get("1.0", "end"))
        self.copy_btn.configure(text="Copied!")
        self.after(1500, lambda: self.copy_btn.configure(text="Copy Tail"))

    def _on_close(self):
        if self.auto_refresh_job:
            self.after_cancel(self.auto_refresh_job)
        self.destroy()
