"""First-run setup and reusable settings surface.

The controller in this module is intentionally UI-agnostic so validation and
atomicity can be tested without a display.  Tk widgets only collect values and
delegate persistence to :class:`SettingsController`.
"""
from __future__ import annotations

import sys
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path

import customtkinter as ctk

from launcher.credential_store import (
    CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY,
    CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY,
    CredentialStore,
)
from launcher.config import MCP_SCRIPT_NAME, PRODUCTION_PATH, TUNNEL_PROFILE_DIR, TUNNEL_PROFILE_NAME
from launcher.harnesses import agy_models, list_harnesses
from launcher.tunnel import TunnelProfileManager
from launcher.user_settings import (
    CodexCustomSettings,
    CodexSettings,
    ConnectionSettings,
    SettingsCorruptionError,
    SettingsError,
    UserSettings,
    get_user_settings_path,
    load_user_settings,
    save_user_settings,
)

from harbor_runtime.config import ROUTES as ROUTE_CHOICES, parse_settings, validate_settings, requires_custom
ROUTE_LABELS = {
    "current": "Current Codex configuration",
    "official": "Official OpenAI",
    "custom": "Custom OpenAI-compatible provider",
    "official_then_custom": "Official, then custom on quota exhaustion",
}


@dataclass(frozen=True)
class FirstRunStatus:
    required: bool
    reason: str = ""
    corrupted: bool = False


def legacy_installation_detected(
    *,
    production_path: Path | str = PRODUCTION_PATH,
    tunnel_profile_dir: Path | str = TUNNEL_PROFILE_DIR,
    tunnel_profile_name: str = TUNNEL_PROFILE_NAME,
) -> bool:
    """Return whether the pre-settings Harbor installation has credible evidence.

    This is deliberately read-only and only checks the two established legacy
    artifacts.  It does not parse profiles or consult the secure credential
    store, and all paths are injectable for deterministic tests.
    """
    runtime = Path(production_path).expanduser() / MCP_SCRIPT_NAME
    profile = Path(tunnel_profile_dir).expanduser() / f"{tunnel_profile_name}.yaml"
    try:
        return runtime.is_file() and profile.is_file()
    except OSError:
        return False


def canonical_route(value: str) -> str:
    """Normalize either a route key or its visible UI label to one route key."""
    text = str(value).strip()
    if text in ROUTE_CHOICES:
        return text
    return next((key for key, label in ROUTE_LABELS.items() if label == text), "current")


def first_run_status(
    settings: UserSettings | None = None,
    credential_store: CredentialStore | None = None,
    *,
    settings_path: Path | str | None = None,
    legacy_detector=None,
) -> FirstRunStatus:
    """Return whether required setup is incomplete, without changing files."""
    if settings is None:
        try:
            candidate = Path(settings_path).expanduser() if settings_path is not None else get_user_settings_path()
            absent = not candidate.exists()
            detector = legacy_detector if legacy_detector is not None else legacy_installation_detected
            legacy = detector() if absent else False
        except Exception:
            # Any path/detector failure fails closed into the normal setup
            # validation below; never skip setup on uncertain evidence.
            absent = False
            legacy = False
        if absent and legacy:
            # Existing legacy users continue to the normal launcher.  No
            # settings are synthesized and no secret is queried or migrated.
            return FirstRunStatus(False)
    try:
        current = settings if settings is not None else load_user_settings(settings_path)
    except SettingsError as exc:
        return FirstRunStatus(True, "Existing settings are corrupted; repair is required.", True)
    try:
        store = credential_store or CredentialStore()
        validate_settings(current.to_dict(), require_connection=True, credentials={
            "tunnel": store.exists(current.connection.credential_ref),
            "custom": store.exists(current.codex.custom.credential_ref),
        })
    except (ValueError, SettingsError) as exc:
        return FirstRunStatus(True, str(exc))
    except Exception:
        return FirstRunStatus(True, "Secure credential store is unavailable.")
    return FirstRunStatus(False)


def is_first_run(
    settings: UserSettings | None = None,
    credential_store: CredentialStore | None = None,
    *,
    settings_path: Path | str | None = None,
    legacy_detector=None,
) -> bool:
    return first_run_status(
        settings, credential_store, settings_path=settings_path, legacy_detector=legacy_detector
    ).required


needs_setup = is_first_run


def validate_draft(draft: dict) -> list[str]:
    """Compatibility entrypoint; runtime owns business validation."""
    try:
        validate_settings(draft, require_connection=True)
    except (ValueError, SettingsError) as exc:
        return [str(exc)]
    return []


class SettingsController:
    """Stage and atomically apply settings plus optional secret changes."""

    def __init__(self, *, credential_store: CredentialStore | None = None, settings_path: Path | str | None = None, profile_path: Path | str | None = None):
        self.store = credential_store or CredentialStore()
        self.settings_path = settings_path
        self.profile_path = Path(profile_path) if profile_path is not None else None

    def _preflight_secret(self, ref: str, value: str | None, clear: bool, *, required: bool, label: str) -> str | None:
        """Read and validate the post-apply state before any writes occur."""
        if value is not None and clear:
            raise ValueError(f"{label} cannot be replaced and cleared at the same time.")
        if sys.platform == "darwin" and value is None and not clear:
            if required and not self.store.exists(ref):
                raise ValueError(f"{label} is required and must remain configured.")
            return None  # No mutation to roll back; do not read an unused secret.
        try:
            present = self.store.exists(ref)
            existing = self.store.read(ref) if present else None
        except Exception as exc:
            raise ValueError("Secure credential store is unavailable; changes were not applied.") from exc
        candidate = value if value is not None else (None if clear else existing)
        if required and (not isinstance(candidate, str) or not candidate.strip()):
            raise ValueError(f"{label} is required and must remain configured.")
        return existing if present else None

    def apply(self, draft: dict, *, tunnel_runtime_key: str | None = None,
              custom_api_key: str | None = None, clear_tunnel_key: bool = False,
              clear_custom_key: bool = False) -> UserSettings:
        errors = validate_draft(draft)
        if errors:
            raise ValueError(" ".join(errors))
        old = load_user_settings(self.settings_path)
        # Merge edits into the loaded document so omitted platform fields survive.
        merged = old.to_dict()
        for section in ("connection", "codex"):
            values = dict(draft.get(section, {}))
            if section == "codex" and "custom" in values:
                merged[section]["custom"].update(values.pop("custom"))
            merged[section].update(values)
        staged = UserSettings.from_dict(validate_settings(merged, require_connection=True))
        tunnel_ref = staged.connection.credential_ref or CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY
        custom_ref = staged.codex.custom.credential_ref or CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY
        custom_required = requires_custom(staged.to_dict())
        # Validate both credential transitions before rendering or writing a
        # profile, settings file, or secure-store value.
        old_secrets: dict[str, str | None] = {
            tunnel_ref: self._preflight_secret(
                tunnel_ref, tunnel_runtime_key, clear_tunnel_key,
                required=True, label="Tunnel Runtime Key",
            ),
            custom_ref: self._preflight_secret(
                custom_ref, custom_api_key, clear_custom_key,
                required=custom_required, label="Custom API key",
            ),
        }
        # Generate the managed profile only through Batch 2's abstraction.  It
        # is a per-user profile and this does not start/restart tunnel-client.
        profile_manager = TunnelProfileManager(staged, profile_path=self.profile_path or (Path(self.settings_path).parent / f"{staged.connection.profile_name}.yaml" if self.settings_path is not None else None))
        profile_target = profile_manager.profile_path.expanduser()
        profile_backup = profile_target.read_bytes() if profile_target.exists() else None
        profile_written = False
        changed: list[str] = []
        try:
            profile_manager.write_profile()
            profile_written = True
            for ref, value, clear in ((tunnel_ref, tunnel_runtime_key, clear_tunnel_key), (custom_ref, custom_api_key, clear_custom_key)):
                if value is not None:
                    self.store.store(ref, value)
                    changed.append(ref)
                elif clear:
                    self.store.delete(ref)
                    changed.append(ref)
            save_user_settings(staged, self.settings_path)
        except Exception:
            # Best-effort secret rollback; original settings file was not
            # replaced unless save_user_settings completed.
            for ref in reversed(changed):
                try:
                    previous = old_secrets.get(ref)
                    if previous is None:
                        self.store.delete(ref)
                    else:
                        self.store.store(ref, previous)
                except Exception:
                    pass
            if profile_written:
                try:
                    if profile_backup is None:
                        profile_target.unlink(missing_ok=True)
                    else:
                        profile_target.write_bytes(profile_backup)
                except Exception:
                    pass
            raise
        return staged

    def secret_state(self) -> dict[str, bool]:
        settings = load_user_settings(self.settings_path)
        return {
            "tunnel_runtime_key": self.store.exists(settings.connection.credential_ref or CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY),
            "custom_api_key": self.store.exists(settings.codex.custom.credential_ref or CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY),
        }


class SetupWizard(ctk.CTkToplevel):
    """Compact, keyboard-usable four-step setup/settings window."""

    def __init__(self, master, *, controller: SettingsController | None = None, on_applied=None, setup: bool = True):
        super().__init__(master)
        self.title("Harness Harbor Setup" if setup else "Harness Harbor Settings")
        self.geometry("520x620")
        self.minsize(440, 500)
        self.controller = controller or SettingsController()
        self.on_applied = on_applied
        try:
            loaded = load_user_settings(self.controller.settings_path)
            self.draft = loaded.to_dict()
        except Exception:
            self.draft = UserSettings().to_dict()
        self.draft["codex"]["routing_mode"] = self.draft["codex"].get("routing_mode", "current")
        self._secret_values = {"tunnel": None, "custom": None}
        self._clear_values = {"tunnel": tk.BooleanVar(value=False), "custom": tk.BooleanVar(value=False)}
        try:
            state = self.controller.secret_state()
        except Exception:
            state = {"tunnel_runtime_key": False, "custom_api_key": False}
        self._secret_state = state
        self._step = tk.IntVar(value=0)
        self._build()

    def _build(self):
        self.grid_columnconfigure(0, weight=1); self.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(self, text="Setup Wizard" if not self.title().endswith("Settings") else "Settings", font=ctk.CTkFont(size=20, weight="bold")).grid(row=0, column=0, padx=20, pady=16, sticky="w")
        self.body = ctk.CTkScrollableFrame(self); self.body.grid(row=1, column=0, padx=16, pady=4, sticky="nsew")
        nav = ctk.CTkFrame(self, fg_color="transparent"); nav.grid(row=2, column=0, padx=16, pady=12, sticky="ew"); nav.grid_columnconfigure(0, weight=1)
        self.error = ctk.CTkLabel(nav, text="", text_color="#d33", anchor="w"); self.error.grid(row=0, column=0, sticky="w")
        self.next_btn = ctk.CTkButton(nav, text="Next", command=self._next); self.next_btn.grid(row=0, column=1, padx=4)
        ctk.CTkButton(nav, text="Cancel", fg_color="transparent", command=self.destroy).grid(row=0, column=2)
        self._render_step()

    def _field(self, label, value, key, secret=False, parent=None):
        parent = parent or self.body
        ctk.CTkLabel(parent, text=label, anchor="w").pack(fill="x", pady=(8, 2))
        var = tk.StringVar(value=value if not secret else "")
        entry = ctk.CTkEntry(parent, textvariable=var, show="•" if secret else "")
        entry.pack(fill="x"); setattr(self, f"_var_{key}", var); return entry

    def _render_step(self):
        for child in self.body.winfo_children(): child.destroy()
        step = self._step.get(); self.next_btn.configure(text="Apply" if step == 3 else "Next")
        if step == 0:
            ctk.CTkLabel(self.body, text="Harbor / Tunnel connection", font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w")
            conn = self.draft["connection"]
            self._field("Tunnel ID", conn.get("tunnel_id", ""), "tunnel_id")
            self._field("Control-plane / base URL", conn.get("base_url", ""), "base_url")
            self._field("Managed profile name", conn.get("profile_name", "harness-harbor"), "profile_name")
            state = "configured" if self._secret_state.get("tunnel_runtime_key") else "not configured"
            self._field(f"Tunnel Runtime Key ({state}; leave blank to keep)", "", "tunnel_secret", True)
            ctk.CTkCheckBox(self.body, text="Explicitly delete the stored Tunnel Runtime Key", variable=self._clear_values["tunnel"]).pack(anchor="w", pady=4)
        elif step == 1:
            ctk.CTkLabel(self.body, text="Harness status", font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w")
            for rec in list_harnesses(): ctk.CTkLabel(self.body, text=f"{rec['display_name']}: {rec['status']} — {rec['detail']}", anchor="w").pack(fill="x", pady=3)
            models = agy_models(); ctk.CTkLabel(self.body, text="AGY models: " + (", ".join(models) if models else "No models detected"), anchor="w", wraplength=450).pack(fill="x", pady=8)
        elif step == 2:
            ctk.CTkLabel(self.body, text="Codex routing", font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w")
            current_route = self.draft["codex"].get("routing_mode", "current")
            route = tk.StringVar(value=ROUTE_LABELS.get(current_route, ROUTE_LABELS["current"])); self._route_var = route
            ctk.CTkOptionMenu(self.body, variable=route, values=[ROUTE_LABELS[x] for x in ROUTE_CHOICES], dynamic_resizing=False).pack(fill="x", pady=8)
            custom = self.draft["codex"].setdefault("custom", {})
            self._field("Custom profile name", custom.get("profile_name", ""), "custom_profile")
            self._field("Custom base URL", custom.get("base_url", ""), "custom_url")
            self._field("Default model (optional)", custom.get("default_model", ""), "custom_model")
            state = "configured" if self._secret_state.get("custom_api_key") else "not configured"
            self._field(f"Custom API key ({state}; leave blank to keep)", "", "custom_secret", True)
            ctk.CTkCheckBox(self.body, text="Explicitly delete the stored Custom API key", variable=self._clear_values["custom"]).pack(anchor="w", pady=4)
        else:
            ctk.CTkLabel(self.body, text="Review and apply", font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w")
            ctk.CTkLabel(self.body, text="Non-secret settings will be saved to settings.json. Secrets are stored only in the secure credential store; existing values are never displayed.", wraplength=450, justify="left").pack(anchor="w", pady=8)
            ctk.CTkLabel(self.body, text=f"Route: {self.draft['codex'].get('routing_mode')}\nTunnel profile: {self.draft['connection'].get('profile_name')}\nTunnel Runtime Key: {'new value entered' if self._secret_values['tunnel'] else 'configured state unchanged'}\nCustom API key: {'new value entered' if self._secret_values['custom'] else 'configured state unchanged'}", justify="left", anchor="w").pack(fill="x")

    def _capture(self):
        if self._step.get() == 0:
            for key in ("tunnel_id", "base_url", "profile_name"): self.draft["connection"][key] = getattr(self, f"_var_{key}").get()
            value = self._var_tunnel_secret.get(); self._secret_values["tunnel"] = value or None
        elif self._step.get() == 2:
            selected = canonical_route(self._route_var.get())
            self.draft["codex"]["routing_mode"] = selected
            custom = self.draft["codex"]["custom"]; custom.update(profile_name=self._var_custom_profile.get(), base_url=self._var_custom_url.get(), default_model=self._var_custom_model.get(), enabled=selected == "custom")
            value = self._var_custom_secret.get(); self._secret_values["custom"] = value or None

    def _next(self):
        self._capture(); self.error.configure(text="")
        if self._step.get() < 3:
            errors = validate_draft(self.draft)
            if errors: self.error.configure(text=errors[0]); return
            self._step.set(self._step.get() + 1); self._render_step(); return
        try:
            self.controller.apply(self.draft, tunnel_runtime_key=self._secret_values["tunnel"], custom_api_key=self._secret_values["custom"], clear_tunnel_key=self._clear_values["tunnel"].get(), clear_custom_key=self._clear_values["custom"].get())
        except Exception as exc:
            self.error.configure(text=str(exc)); return
        if self.on_applied: self.on_applied()
        self.destroy()


SettingsDialog = SetupWizard

__all__ = ["ROUTE_CHOICES", "ROUTE_LABELS", "FirstRunStatus", "SettingsController", "SetupWizard", "SettingsDialog", "canonical_route", "first_run_status", "is_first_run", "legacy_installation_detected", "needs_setup", "validate_draft"]
