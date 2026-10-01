"""Active-job authoritative inspection and exit confirmation guard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import customtkinter as ctk
from runtime_liveness import ProcessLiveness, probe_job_tree_liveness, probe_pid_liveness

from launcher.config import (
    COLOR_BTN_PRIMARY,
    COLOR_BTN_PRIMARY_HOVER,
    COLOR_BTN_SECONDARY_DARK,
    COLOR_BTN_SECONDARY_HOVER_DARK,
    COLOR_BTN_SECONDARY_HOVER_LIGHT,
    COLOR_BTN_SECONDARY_LIGHT,
    COLOR_CARD_DARK,
    COLOR_CARD_LIGHT,
    COLOR_STATUS_WARNING,
    COLOR_TEXT_MUTED_DARK,
    COLOR_TEXT_MUTED_LIGHT,
    COLOR_TEXT_PRIMARY_DARK,
    COLOR_TEXT_PRIMARY_LIGHT,
)


def get_authoritative_active_jobs(jobs_dir: Path | str | None = None) -> dict[str, Any]:
    """Inspect the authoritative job queue on disk for genuinely running and queued jobs.

    A running job is considered safe to ignore only when every recorded owner
    PID is demonstrably dead. Missing or unobservable state remains uncertain
    and therefore still blocks an unconfirmed exit.
    """
    if jobs_dir is None:
        from control_plane import JOBS_DIR
        jobs_dir = JOBS_DIR

    jobs_dir = Path(jobs_dir)
    result: dict[str, Any] = {
        "running_count": 0,
        "running_harnesses": [],
        "running_job_ids": [],
        "queued_count": 0,
        "queued_harnesses": [],
        "queued_job_ids": [],
        "uncertain_count": 0,
        "uncertain_harnesses": [],
        "uncertain_job_ids": [],
        "inspection_error": False,
    }
    if not jobs_dir.is_dir():
        return result

    from control_plane import read_json_object, queue_root_matches

    running_harnesses = set()
    running_job_ids = []
    queued_harnesses = set()
    queued_job_ids = []
    uncertain_harnesses = set()
    uncertain_job_ids = []

    try:
        state_paths = list(jobs_dir.glob("*/status.json"))
    except OSError:
        result["inspection_error"] = True
        result["uncertain_count"] = 1
        return result

    for state_path in state_paths:
        job_dir = state_path.parent
        try:
            state = read_json_object(state_path)
            if not isinstance(state, dict):
                raise ValueError("status.json is not an object")
            if not queue_root_matches(state, jobs_dir):
                continue

            status = state.get("status")
            harness = state.get("harness") or "codex"

            if status == "running":
                worker_pid = None
                ownership_uncertain = False
                lock_path = job_dir / "worker.lock"
                if lock_path.is_file():
                    try:
                        content = lock_path.read_text(encoding="utf-8").strip()
                        if content:
                            worker_pid = int(content.split()[0])
                        else:
                            ownership_uncertain = True
                    except (OSError, ValueError):
                        ownership_uncertain = True

                native = state.get("native_process") or {}
                if not isinstance(native, dict):
                    native = {}
                    ownership_uncertain = True
                owners = {
                    pid for pid in (worker_pid, state.get("worker_pid"), native.get("launcher_pid"))
                    if isinstance(pid, int) and pid > 1
                }

                owner_states = {probe_pid_liveness(pid) for pid in owners}
                if ProcessLiveness.ALIVE in owner_states:
                    running_harnesses.add(harness)
                    running_job_ids.append(job_dir.name)
                elif ownership_uncertain or not owners or ProcessLiveness.UNKNOWN in owner_states:
                    uncertain_harnesses.add(harness)
                    uncertain_job_ids.append(job_dir.name)
                else:
                    tree_state = probe_job_tree_liveness(job_dir, owners)
                    if tree_state is ProcessLiveness.ALIVE:
                        running_harnesses.add(harness)
                        running_job_ids.append(job_dir.name)
                    elif tree_state is ProcessLiveness.UNKNOWN:
                        uncertain_harnesses.add(harness)
                        uncertain_job_ids.append(job_dir.name)
            elif status == "queued":
                queued_harnesses.add(harness)
                queued_job_ids.append(job_dir.name)
        except Exception:
            result["inspection_error"] = True
            uncertain_harnesses.add("unknown")
            uncertain_job_ids.append(job_dir.name)

    result["running_count"] = len(running_job_ids)
    result["running_harnesses"] = sorted(list(running_harnesses))
    result["running_job_ids"] = sorted(running_job_ids)
    result["queued_count"] = len(queued_job_ids)
    result["queued_harnesses"] = sorted(list(queued_harnesses))
    result["queued_job_ids"] = sorted(queued_job_ids)
    result["uncertain_count"] = len(uncertain_job_ids)
    result["uncertain_harnesses"] = sorted(list(uncertain_harnesses))
    result["uncertain_job_ids"] = sorted(uncertain_job_ids)
    return result


class ExitConfirmationDialog(ctk.CTkToplevel):
    """Modal confirmation dialog warning about genuinely running jobs before exit."""

    def __init__(
        self,
        master: Any = None,
        *,
        running_count: int = 1,
        running_harnesses: list[str] | None = None,
        queued_count: int = 0,
        uncertain_count: int = 0,
        on_confirm: Callable[[], None],
        on_cancel: Callable[[], None] | None = None,
    ):
        super().__init__(master)
        self.running_count = running_count
        self.running_harnesses = running_harnesses or ["codex"]
        self.queued_count = queued_count
        self.uncertain_count = uncertain_count
        self.on_confirm = on_confirm
        self.on_cancel = on_cancel

        self.title("Confirm Exit - Active or Unverified Tasks")
        self.geometry("480x280")
        self.resizable(False, False)

        if master:
            self.transient(master)
            try:
                x = master.winfo_x() + (master.winfo_width() // 2) - 240
                y = master.winfo_y() + (master.winfo_height() // 2) - 140
                self.geometry(f"+{max(50, x)}+{max(50, y)}")
            except Exception:
                pass

        self.protocol("WM_DELETE_WINDOW", self._handle_cancel)
        self._init_ui()

        # Lift and grab focus
        self.after(50, self._setup_focus)

    def _setup_focus(self):
        try:
            self.lift()
            self.focus_force()
            self.grab_set()
            if hasattr(self, "btn_keep_running"):
                self.btn_keep_running.focus_set()
        except Exception:
            pass

    def _init_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=0, column=0, sticky="nsew", padx=24, pady=20)
        content.grid_columnconfigure(0, weight=1)

        # Warning Header
        header_frame = ctk.CTkFrame(content, fg_color="transparent")
        header_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header_frame.grid_columnconfigure(1, weight=1)

        icon_lbl = ctk.CTkLabel(
            header_frame,
            text="⚠️",
            font=ctk.CTkFont(size=24),
            width=36,
        )
        icon_lbl.grid(row=0, column=0, sticky="w", padx=(0, 8))

        title_lbl = ctk.CTkLabel(
            header_frame,
            text="Active Tasks Running",
            font=ctk.CTkFont(family="Segoe UI Variable Display", size=16, weight="bold"),
            text_color=COLOR_STATUS_WARNING,
            anchor="w",
        )
        title_lbl.grid(row=0, column=1, sticky="w")

        # Descriptive message
        if self.running_count:
            harness_text = ", ".join(self.running_harnesses)
            job_plural = "task is" if self.running_count == 1 else "tasks are"
            msg = f"{self.running_count} {job_plural} currently running ({harness_text})."
        else:
            msg = "No task could be confirmed safe to interrupt."
        if self.queued_count > 0:
            queued_plural = "task is" if self.queued_count == 1 else "tasks are"
            msg += f"\nAdditionally, {self.queued_count} {queued_plural} queued."
        if self.uncertain_count > 0:
            msg += f"\nHarbor could not verify the state of {self.uncertain_count} task(s)."

        desc_lbl = ctk.CTkLabel(
            content,
            text=msg,
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=13, weight="bold"),
            text_color=(COLOR_TEXT_PRIMARY_LIGHT, COLOR_TEXT_PRIMARY_DARK),
            anchor="w",
            justify="left",
        )
        desc_lbl.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        warning_lbl = ctk.CTkLabel(
            content,
            text="Exiting Harbor now will interrupt active tasks and terminate background worker processes.",
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=12),
            text_color=(COLOR_TEXT_MUTED_LIGHT, COLOR_TEXT_MUTED_DARK),
            anchor="w",
            justify="left",
            wraplength=430,
        )
        warning_lbl.grid(row=2, column=0, sticky="ew", pady=(0, 20))

        # Button Row: [Exit Anyway (secondary/destructive)] [Keep Harbor Running (primary/safe)]
        btn_row = ctk.CTkFrame(content, fg_color="transparent")
        btn_row.grid(row=3, column=0, sticky="ew")
        btn_row.grid_columnconfigure((0, 1), weight=1)

        self.btn_exit_anyway = ctk.CTkButton(
            btn_row,
            text="Exit Anyway",
            height=36,
            corner_radius=8,
            fg_color=(COLOR_BTN_SECONDARY_LIGHT, COLOR_BTN_SECONDARY_DARK),
            hover_color=("#FF453A", "#FF453A"),
            text_color=(COLOR_TEXT_PRIMARY_LIGHT, COLOR_TEXT_PRIMARY_DARK),
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=12, weight="bold"),
            command=self._handle_confirm,
        )
        self.btn_exit_anyway.grid(row=0, column=0, padx=(0, 6), sticky="ew")

        self.btn_keep_running = ctk.CTkButton(
            btn_row,
            text="Keep Harbor Running",
            height=36,
            corner_radius=8,
            fg_color=COLOR_BTN_PRIMARY,
            hover_color=COLOR_BTN_PRIMARY_HOVER,
            font=ctk.CTkFont(family="Segoe UI Variable Text", size=12, weight="bold"),
            command=self._handle_cancel,
        )
        self.btn_keep_running.grid(row=0, column=1, padx=(6, 0), sticky="ew")

    def _handle_cancel(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        if self.on_cancel:
            self.on_cancel()

    def _handle_confirm(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        self.on_confirm()
