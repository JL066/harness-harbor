"""Diagnostics dialog for Harness Harbor."""

from __future__ import annotations

import customtkinter as ctk

from launcher.diagnostics import collect_diagnostics, format_diagnostics_markdown


class DiagnosticsDialog(ctk.CTkToplevel):
    """Modern rounded diagnostics inspection dialog."""

    def __init__(self, master=None):
        super().__init__(master)

        self.title("Harbor System Diagnostics")
        self.geometry("700x520")
        self.minsize(560, 400)

        if master:
            x = master.winfo_x() + (master.winfo_width() // 2) - 350
            y = master.winfo_y() + (master.winfo_height() // 2) - 260
            self.geometry(f"+{max(50, x)}+{max(50, y)}")

        self._init_ui()
        self.refresh_diagnostics()

    def _init_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Header Bar
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 8))
        header.grid_columnconfigure(0, weight=1)

        title_lbl = ctk.CTkLabel(
            header,
            text="Diagnostics & Runtime Report",
            font=ctk.CTkFont(family="Segoe UI Variable Display", size=15, weight="bold"),
            anchor="w",
        )
        title_lbl.grid(row=0, column=0, sticky="w")

        btn_box = ctk.CTkFrame(header, fg_color="transparent")
        btn_box.grid(row=0, column=1, sticky="e")

        self.refresh_btn = ctk.CTkButton(
            btn_box,
            text="Refresh",
            width=70,
            height=28,
            corner_radius=8,
            command=self.refresh_diagnostics,
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=12),
        )
        self.refresh_btn.pack(side="left", padx=4)

        self.copy_btn = ctk.CTkButton(
            btn_box,
            text="Copy Diagnostics",
            width=110,
            height=28,
            corner_radius=8,
            command=self._copy_diagnostics,
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=12),
        )
        self.copy_btn.pack(side="left", padx=4)

        # Main Text Box
        text_frame = ctk.CTkFrame(self, corner_radius=12)
        text_frame.grid(row=1, column=0, sticky="nsew", padx=20, pady=8)
        text_frame.grid_columnconfigure(0, weight=1)
        text_frame.grid_rowconfigure(0, weight=1)

        self.text_box = ctk.CTkTextbox(
            text_frame,
            wrap="word",
            font=ctk.CTkFont(family="Consolas", size=11),
            corner_radius=10,
        )
        self.text_box.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

        # Bottom Notice
        notice_lbl = ctk.CTkLabel(
            self,
            text="🔒 Secret-Safe: All credentials, API keys, and auth headers are automatically redacted.",
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=11),
            text_color=("#86868B", "#98989D"),
            anchor="w",
        )
        notice_lbl.grid(row=2, column=0, sticky="w", padx=24, pady=(4, 14))

    def refresh_diagnostics(self):
        diag_data = collect_diagnostics()
        md_text = format_diagnostics_markdown(diag_data)

        self.text_box.configure(state="normal")
        self.text_box.delete("1.0", "end")
        self.text_box.insert("1.0", md_text)
        self.text_box.configure(state="disabled")

    def _copy_diagnostics(self):
        self.clipboard_clear()
        self.clipboard_append(self.text_box.get("1.0", "end"))
        self.copy_btn.configure(text="Copied!")
        self.after(1500, lambda: self.copy_btn.configure(text="Copy Diagnostics"))
