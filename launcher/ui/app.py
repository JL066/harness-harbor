"""Main Application Window for Harness Harbor Launcher."""

from __future__ import annotations

import datetime
import threading
import time
import tkinter as tk
import customtkinter as ctk

from launcher.autostart import is_autostart_enabled, set_autostart_enabled
from launcher.config import (
    COLOR_BG_DARK,
    COLOR_BG_LIGHT,
    COLOR_BTN_DANGER,
    COLOR_BTN_DANGER_HOVER,
    COLOR_BTN_PRIMARY,
    COLOR_BTN_PRIMARY_HOVER,
    COLOR_BTN_SECONDARY_DARK,
    COLOR_BTN_SECONDARY_HOVER_DARK,
    COLOR_BTN_SECONDARY_HOVER_LIGHT,
    COLOR_BTN_SECONDARY_LIGHT,
    COLOR_CARD_DARK,
    COLOR_CARD_LIGHT,
    COLOR_STATUS_HEALTHY,
    COLOR_STATUS_RESTARTING,
    COLOR_STATUS_RUNNING,
    COLOR_STATUS_STARTING,
    COLOR_STATUS_STOPPED,
    COLOR_STATUS_WARNING,
    COLOR_TEXT_MUTED_DARK,
    COLOR_TEXT_MUTED_LIGHT,
    COLOR_TEXT_PRIMARY_DARK,
    COLOR_TEXT_PRIMARY_LIGHT,
    POLL_INTERVAL_SECONDS,
    PRODUCTION_PATH,
    BRAND_HEADER_ICON,
    BRAND_ICO_PATH,
    BRAND_SYSTEM_ICO_PATH,
)
from PIL import Image
from launcher.health_checker import HarborHealthSnapshot, get_harbor_health
from launcher.harnesses import agy_models, list_harnesses
from control_plane import harness_telemetry_snapshot
from launcher.lifecycle import restart_harbor, start_harbor, stop_harbor
from launcher.ui.components import StatusCard
from launcher.ui.diag_dialog import DiagnosticsDialog
from launcher.ui.log_dialog import LogDialog
from launcher.ui.tray import HarborTrayManager
from launcher.ui.setup_wizard import SettingsController, SetupWizard, first_run_status
from launcher.credential_store import CredentialStore


class HarborLauncherApp(ctk.CTk):
    """Apple-inspired utility panel for Harness Harbor."""

    def __init__(self):
        super().__init__()

        # Appearance configuration
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.title("Harness Harbor")
        self.geometry("470x680")
        self.minsize(440, 620)
        self.resizable(False, False)

        # Center on screen
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"+{max(50, (sw - 470) // 2)}+{max(50, (sh - 680) // 2)}")

        # Set Windows Window & Taskbar Icon (use transparent system icon)
        ico_target = BRAND_SYSTEM_ICO_PATH if BRAND_SYSTEM_ICO_PATH.exists() else BRAND_ICO_PATH
        if ico_target.exists():
            try:
                self.iconbitmap(str(ico_target))
            except Exception:
                pass

        # State variables
        self.is_busy = False
        self.current_health: HarborHealthSnapshot | None = None
        self._poll_active = True
        self.log_dialog: LogDialog | None = None
        self.diag_dialog: DiagnosticsDialog | None = None
        self.harness_cards: dict[str, StatusCard] = {}
        self.agy_model_menu = None
        self.settings_dialog = None
        self._settings_controller = None

        self._build_ui()

        # Initialize System Tray
        self.tray = HarborTrayManager(
            on_open=self._tray_open,
            on_restart=self._tray_restart,
            on_stop=self._tray_stop,
            on_exit=self._tray_exit,
        )
        self.tray.start()

        # Intercept window close
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)

        # Start background health monitor
        self._schedule_health_poll(immediate=True)
        # Setup is deferred until the window is realized so it behaves well on
        # narrow displays and never interrupts construction of the launcher.
        self.after(50, self._maybe_open_setup)

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)

        # Main scrollable or static container with smooth padding
        main_frame = ctk.CTkFrame(self, fg_color="transparent")
        main_frame.grid(row=0, column=0, sticky="nsew", padx=24, pady=20)
        main_frame.grid_columnconfigure(0, weight=1)

        # -------------------------------------------------------------------
        # Header: Icon + Title + Production Badge
        # -------------------------------------------------------------------
        header_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        header_frame.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        header_frame.grid_columnconfigure(1, weight=1)

        # Header Brand Mark (Harbor Lighthouse Rounded Tile)
        if BRAND_HEADER_ICON.exists():
            try:
                raw_im = Image.open(BRAND_HEADER_ICON)
                self.brand_img = ctk.CTkImage(light_image=raw_im, dark_image=raw_im, size=(48, 48))
                tile_frame = ctk.CTkFrame(
                    header_frame,
                    width=52,
                    height=52,
                    corner_radius=14,
                    fg_color="transparent",
                )
                tile_frame.grid(row=0, column=0, rowspan=2, padx=(0, 14), pady=2, sticky="w")
                tile_frame.grid_propagate(False)

                logo_lbl = ctk.CTkLabel(
                    tile_frame,
                    text="",
                    image=self.brand_img,
                )
                logo_lbl.place(relx=0.5, rely=0.5, anchor="center")
            except Exception:
                logo_lbl = ctk.CTkLabel(
                    header_frame,
                    text="⚓",
                    font=ctk.CTkFont(size=28),
                    width=36,
                )
                logo_lbl.grid(row=0, column=0, rowspan=2, padx=(0, 12), sticky="w")
        else:
            logo_lbl = ctk.CTkLabel(
                header_frame,
                text="⚓",
                font=ctk.CTkFont(size=28),
                width=36,
            )
            logo_lbl.grid(row=0, column=0, rowspan=2, padx=(0, 12), sticky="w")

        title_lbl = ctk.CTkLabel(
            header_frame,
            text="Harness Harbor",
            font=ctk.CTkFont(family="Segoe UI Variable Display", size=19, weight="bold"),
            text_color=(COLOR_TEXT_PRIMARY_LIGHT, COLOR_TEXT_PRIMARY_DARK),
            anchor="w",
        )
        title_lbl.grid(row=0, column=1, sticky="w")

        sub_lbl = ctk.CTkLabel(
            header_frame,
            text="Windows Control Panel",
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=12),
            text_color=(COLOR_TEXT_MUTED_LIGHT, COLOR_TEXT_MUTED_DARK),
            anchor="w",
        )
        sub_lbl.grid(row=1, column=1, sticky="w")

        # Environment Badge
        env_badge = ctk.CTkFrame(
            header_frame,
            corner_radius=8,
            fg_color=("#E5E5EA", "#3A3A3C"),
        )
        env_badge.grid(row=0, column=2, rowspan=2, sticky="e")
        env_lbl = ctk.CTkLabel(
            env_badge,
            text="Production",
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=11, weight="bold"),
            text_color=(COLOR_TEXT_MUTED_LIGHT, COLOR_TEXT_MUTED_DARK),
            padx=10,
            pady=3,
        )
        env_lbl.pack()

        # -------------------------------------------------------------------
        # Section Label: STATUS
        # -------------------------------------------------------------------
        sec_lbl = ctk.CTkLabel(
            main_frame,
            text="STATUS",
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=11, weight="bold"),
            text_color=(COLOR_TEXT_MUTED_LIGHT, COLOR_TEXT_MUTED_DARK),
            anchor="w",
        )
        sec_lbl.grid(row=1, column=0, sticky="w", padx=4, pady=(0, 6))

        # -------------------------------------------------------------------
        # Status Cards Container
        # -------------------------------------------------------------------
        self.card_tunnel = StatusCard(main_frame, title="Tunnel")
        self.card_tunnel.grid(row=2, column=0, sticky="ew", pady=4)

        self.card_mcp = StatusCard(main_frame, title="Harbor MCP")
        self.card_mcp.grid(row=3, column=0, sticky="ew", pady=4)

        self.card_daemon = StatusCard(main_frame, title="Job Daemon")
        self.card_daemon.grid(row=4, column=0, sticky="ew", pady=4)

        # Supported Harnesses (status comes from Harbor's registry API).
        harness_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        harness_frame.grid(row=5, column=0, sticky="ew", pady=(10, 2))
        harness_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(harness_frame, text="SUPPORTED HARNESSES", anchor="w",
                     font=ctk.CTkFont(family="Segoe UI Variable Text", size=11, weight="bold"),
                     text_color=(COLOR_TEXT_MUTED_LIGHT, COLOR_TEXT_MUTED_DARK)).grid(row=0, column=0, sticky="w", padx=4, pady=(0, 4))
        for row, (name, title) in enumerate((("codex", "Codex CLI"), ("minimax", "MiniMax CLI"), ("agy", "Antigravity/AGY")), 1):
            card = StatusCard(harness_frame, title=title, initial_detail="Checking...")
            card.grid(row=row, column=0, sticky="ew", pady=2)
            self.harness_cards[name] = card
        self.agy_model_menu = ctk.CTkOptionMenu(harness_frame, values=["No models detected"], width=180,
                                                height=28, corner_radius=8)
        self.agy_model_menu.grid(row=4, column=0, sticky="e", pady=(4, 0))

        # -------------------------------------------------------------------
        # Overall Summary Banner
        # -------------------------------------------------------------------
        self.banner_frame = ctk.CTkFrame(
            main_frame,
            corner_radius=12,
            fg_color=(COLOR_CARD_LIGHT, COLOR_CARD_DARK),
            border_width=1,
            border_color=("#E5E5EA", "#38383A"),
        )
        self.banner_frame.grid(row=6, column=0, sticky="ew", pady=(14, 16))
        self.banner_frame.grid_columnconfigure(0, weight=1)

        self.summary_title = ctk.CTkLabel(
            self.banner_frame,
            text="Checking status...",
            font=ctk.CTkFont(family="Segoe UI Variable Display", size=14, weight="bold"),
            text_color=COLOR_STATUS_STARTING,
        )
        self.summary_title.pack(pady=(10, 2))

        self.summary_sub = ctk.CTkLabel(
            self.banner_frame,
            text="Initializing health probe...",
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=11),
            text_color=(COLOR_TEXT_MUTED_LIGHT, COLOR_TEXT_MUTED_DARK),
        )
        self.summary_sub.pack(pady=(0, 10))

        # -------------------------------------------------------------------
        # Primary Action Buttons (Start / Restart / Stop)
        # -------------------------------------------------------------------
        btn_row = ctk.CTkFrame(main_frame, fg_color="transparent")
        btn_row.grid(row=7, column=0, sticky="ew", pady=(0, 12))
        btn_row.grid_columnconfigure((0, 1, 2), weight=1)

        self.btn_start = ctk.CTkButton(
            btn_row,
            text="Start",
            height=38,
            corner_radius=10,
            fg_color=COLOR_BTN_PRIMARY,
            hover_color=COLOR_BTN_PRIMARY_HOVER,
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=13, weight="bold"),
            command=self._handle_start,
        )
        self.btn_start.grid(row=0, column=0, padx=(0, 6), sticky="ew")

        self.btn_restart = ctk.CTkButton(
            btn_row,
            text="Restart",
            height=38,
            corner_radius=10,
            fg_color=(COLOR_BTN_SECONDARY_LIGHT, COLOR_BTN_SECONDARY_DARK),
            hover_color=(COLOR_BTN_SECONDARY_HOVER_LIGHT, COLOR_BTN_SECONDARY_HOVER_DARK),
            text_color=(COLOR_TEXT_PRIMARY_LIGHT, COLOR_TEXT_PRIMARY_DARK),
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=13, weight="bold"),
            command=self._handle_restart,
        )
        self.btn_restart.grid(row=0, column=1, padx=6, sticky="ew")

        self.btn_stop = ctk.CTkButton(
            btn_row,
            text="Stop",
            height=38,
            corner_radius=10,
            fg_color=(COLOR_BTN_SECONDARY_LIGHT, COLOR_BTN_SECONDARY_DARK),
            hover_color=(COLOR_BTN_DANGER_HOVER, COLOR_BTN_DANGER_HOVER),
            text_color=(COLOR_TEXT_PRIMARY_LIGHT, COLOR_TEXT_PRIMARY_DARK),
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=13, weight="bold"),
            command=self._handle_stop,
        )
        self.btn_stop.grid(row=0, column=2, padx=(6, 0), sticky="ew")

        # -------------------------------------------------------------------
        # Secondary Links (View Logs & Diagnostics)
        # -------------------------------------------------------------------
        links_row = ctk.CTkFrame(main_frame, fg_color="transparent")
        links_row.grid(row=8, column=0, sticky="ew", pady=(0, 16))
        links_row.grid_columnconfigure((0, 1), weight=1)

        self.btn_logs = ctk.CTkButton(
            links_row,
            text="📄  View Logs",
            height=32,
            corner_radius=8,
            fg_color="transparent",
            hover_color=("#E5E5EA", "#3A3A3C"),
            text_color=COLOR_BTN_PRIMARY,
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=12, weight="bold"),
            command=self._open_logs,
        )
        self.btn_logs.grid(row=0, column=0, padx=4, sticky="ew")

        self.btn_diag = ctk.CTkButton(
            links_row,
            text="🔍  Diagnostics",
            height=32,
            corner_radius=8,
            fg_color="transparent",
            hover_color=("#E5E5EA", "#3A3A3C"),
            text_color=COLOR_BTN_PRIMARY,
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=12, weight="bold"),
            command=self._open_diagnostics,
        )
        self.btn_diag.grid(row=0, column=1, padx=4, sticky="ew")

        # -------------------------------------------------------------------
        # Divider Line
        # -------------------------------------------------------------------
        sep = ctk.CTkFrame(main_frame, height=1, fg_color=("#E5E5EA", "#38383A"))
        sep.grid(row=9, column=0, sticky="ew", pady=(0, 12))

        # -------------------------------------------------------------------
        # Footer & Settings (Autostart + Production Path)
        # -------------------------------------------------------------------
        footer_frame = ctk.CTkFrame(main_frame, fg_color="transparent")
        footer_frame.grid(row=10, column=0, sticky="ew")
        footer_frame.grid_columnconfigure(0, weight=1)

        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        self.autostart_cb = ctk.CTkCheckBox(
            footer_frame,
            text="Start Harbor Launcher with Windows",
            variable=self.autostart_var,
            command=self._on_autostart_toggled,
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=11),
            corner_radius=5,
        )
        self.autostart_cb.grid(row=0, column=0, sticky="w", pady=(0, 6))

        self.btn_settings = ctk.CTkButton(
            footer_frame,
            text="Settings",
            height=30,
            corner_radius=8,
            fg_color="transparent",
            hover_color=("#E5E5EA", "#3A3A3C"),
            text_color=COLOR_BTN_PRIMARY,
            command=self._open_settings,
        )
        self.btn_settings.grid(row=0, column=1, sticky="e", pady=(0, 6))

        path_lbl = ctk.CTkLabel(
            footer_frame,
            text=f"Production Path: {PRODUCTION_PATH}",
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=10),
            text_color=(COLOR_TEXT_MUTED_LIGHT, COLOR_TEXT_MUTED_DARK),
            anchor="w",
        )
        path_lbl.grid(row=1, column=0, sticky="w")

    # -----------------------------------------------------------------------
    # Background Health Polling
    # -----------------------------------------------------------------------

    def _schedule_health_poll(self, immediate: bool = False):
        if not self._poll_active:
            return

        delay_ms = 100 if immediate else int(POLL_INTERVAL_SECONDS * 1000)
        self.after(delay_ms, self._run_health_poll_async)

    def _run_health_poll_async(self):
        if self.is_busy:
            self._schedule_health_poll(immediate=False)
            return

        def worker():
            try:
                snapshot = get_harbor_health()
                self.after(0, self._apply_health_snapshot, snapshot)
                telemetry = harness_telemetry_snapshot()
                harness_statuses = list_harnesses(telemetry_snapshot=telemetry)
                models = agy_models(telemetry_snapshot=telemetry)
                self.after(0, self._apply_harness_statuses, harness_statuses, models)
            except Exception as e:
                pass
            finally:
                self.after(0, lambda: self._schedule_health_poll(immediate=False))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_harness_statuses(self, statuses, models):
        """Apply registry results without making probing part of UI setup."""
        colors = {"Installed": COLOR_STATUS_HEALTHY, "Not installed": COLOR_STATUS_STOPPED}
        for record in statuses:
            card = self.harness_cards.get(record["name"])
            if card:
                status = record["status"]
                card.set_status(status, record["detail"], colors.get(status, COLOR_STATUS_WARNING))
        if self.agy_model_menu is not None:
            values = models or ["No models detected"]
            self.agy_model_menu.configure(values=values)
            if self.agy_model_menu.get() not in values:
                self.agy_model_menu.set(values[0])

    def _apply_health_snapshot(self, snap: HarborHealthSnapshot):
        self.current_health = snap

        # Update cards
        self.card_tunnel.set_status(snap.tunnel.status, snap.tunnel.detail, snap.tunnel.color)
        self.card_mcp.set_status(snap.mcp.status, snap.mcp.detail, snap.mcp.color)
        self.card_daemon.set_status(snap.daemon.status, snap.daemon.detail, snap.daemon.color)

        # Update summary banner
        self.summary_title.configure(text=snap.overall_status, text_color=snap.overall_color)
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        self.summary_sub.configure(text=f"Last check: {now_str}")

        # Update action buttons state if not busy
        if not self.is_busy:
            is_running = snap.tunnel.status == "Healthy" and snap.daemon.status == "Running"
            is_stopped = snap.tunnel.status == "Stopped" and snap.daemon.status == "Stopped"

            if is_running:
                self.btn_start.configure(state="disabled", text="Running")
                self.btn_restart.configure(state="normal")
                self.btn_stop.configure(state="normal", text_color=COLOR_BTN_DANGER)
            elif is_stopped:
                self.btn_start.configure(state="normal", text="Start")
                self.btn_restart.configure(state="disabled")
                self.btn_stop.configure(state="disabled", text_color=(COLOR_TEXT_PRIMARY_LIGHT, COLOR_TEXT_PRIMARY_DARK))
            else:
                self.btn_start.configure(state="normal", text="Start")
                self.btn_restart.configure(state="normal")
                self.btn_stop.configure(state="normal", text_color=COLOR_BTN_DANGER)

    # -----------------------------------------------------------------------
    # Lifecycle Action Handlers
    # -----------------------------------------------------------------------

    def _set_busy(self, action_title: str, subtext: str):
        self.is_busy = True
        self.summary_title.configure(text=action_title, text_color=COLOR_STATUS_STARTING)
        self.summary_sub.configure(text=subtext)
        self.btn_start.configure(state="disabled")
        self.btn_restart.configure(state="disabled")
        self.btn_stop.configure(state="disabled")

    def _clear_busy(self):
        self.is_busy = False
        self._schedule_health_poll(immediate=True)

    def _handle_start(self):
        if self.is_busy:
            return
        self._set_busy("Starting Harbor...", "Spawning supervisors and probing health...")

        def worker():
            ok, msg = start_harbor(progress_cb=lambda s: self.after(0, lambda: self.summary_sub.configure(text=s)))
            self.after(500, self._clear_busy)

        threading.Thread(target=worker, daemon=True).start()

    def _handle_restart(self):
        if self.is_busy:
            return
        self._set_busy("Restarting Harbor...", "Stopping existing runtime...")

        def worker():
            ok, msg = restart_harbor(progress_cb=lambda s: self.after(0, lambda: self.summary_sub.configure(text=s)))
            self.after(500, self._clear_busy)

        threading.Thread(target=worker, daemon=True).start()

    def _handle_stop(self):
        if self.is_busy:
            return
        self._set_busy("Stopping Harbor...", "Stopping supervisors and child process tree...")

        def worker():
            ok, msg = stop_harbor(progress_cb=lambda s: self.after(0, lambda: self.summary_sub.configure(text=s)))
            self.after(500, self._clear_busy)

        threading.Thread(target=worker, daemon=True).start()

    # -----------------------------------------------------------------------
    # Secondary Dialogs
    # -----------------------------------------------------------------------

    def _open_logs(self):
        if self.log_dialog is None or not self.log_dialog.winfo_exists():
            self.log_dialog = LogDialog(self)
        else:
            self.log_dialog.lift()
            self.log_dialog.focus()

    def _open_diagnostics(self):
        if self.diag_dialog is None or not self.diag_dialog.winfo_exists():
            self.diag_dialog = DiagnosticsDialog(self)
        else:
            self.diag_dialog.lift()
            self.diag_dialog.focus()

    def _get_settings_controller(self):
        if self._settings_controller is None:
            try:
                self._settings_controller = SettingsController(credential_store=CredentialStore())
            except Exception:
                # Keep the failure visible in the wizard; no plaintext fallback.
                self._settings_controller = None
        return self._settings_controller

    def _maybe_open_setup(self):
        try:
            status = first_run_status(credential_store=self._get_settings_controller().store if self._get_settings_controller() else None)
        except Exception:
            status = None
        if status is not None and status.required:
            self._open_settings(setup=True, reason=status.reason)

    def _open_settings(self, setup=False, reason=""):
        if self.settings_dialog is not None and self.settings_dialog.winfo_exists():
            self.settings_dialog.lift(); self.settings_dialog.focus_force(); return
        controller = self._get_settings_controller()
        if controller is None:
            # Constructing a controller requires a secure backend.  Surface a
            # concise error and fail closed instead of offering unsafe storage.
            import tkinter.messagebox as mb
            mb.showerror("Secure credential store unavailable", "Harbor cannot save settings until the OS secure credential store is available.")
            return
        self.settings_dialog = SetupWizard(self, controller=controller, on_applied=self._settings_applied, setup=setup)
        if reason:
            self.settings_dialog.error.configure(text=reason)

    def _settings_applied(self):
        # Saving settings never restarts Harbor or tunnel-client.  A health
        # poll reflects the new configuration and the user can explicitly use
        # Start/Restart when a runtime restart is desired.
        self.summary_sub.configure(text="Settings saved. Restart Harbor if required by the changes.")
        self._schedule_health_poll(immediate=True)

    def _on_autostart_toggled(self):
        enabled = self.autostart_var.get()
        set_autostart_enabled(enabled)

    # -----------------------------------------------------------------------
    # Tray Integration & Window Close Behavior
    # -----------------------------------------------------------------------

    def _on_window_close(self):
        """Minimize to system tray on window close."""
        self.withdraw()

    def _tray_open(self):
        """Restore window from tray."""
        self.after(0, self._restore_window)

    def _restore_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _tray_restart(self):
        self.after(0, self._handle_restart)

    def _tray_stop(self):
        self.after(0, self._handle_stop)

    def _tray_exit(self):
        """Exit launcher application only (Harbor runtime continues running)."""
        self._poll_active = False
        self.tray.stop()
        self.after(0, self.destroy)
