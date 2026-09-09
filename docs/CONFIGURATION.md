# Harness Harbor Launcher — Configuration Guide

This document explains how to install and operate the Harness Harbor Launcher
with deployment-specific paths supplied by the operator.
The launcher's **Batch 1** configuration foundation resolves every
deployment-specific path, name, and timing constant through a single
resolution layer (`launcher/settings.py`) so operators can point it at
a different install without editing source.

> Looking for the runtime control plane (tunnel / MCP / daemon) docs? See
> [`README.md`](../README.md) and [`docs/SUPERVISOR_SKILL.md`](SUPERVISOR_SKILL.md).

---

## 1. Resolution Precedence

Every configurable value is resolved using the following tier order
(highest priority wins):

| Tier | Source | Notes |
|------|--------|-------|
| 1 | **Environment variable** (`HARBOR_*`) | Set before launching. Read once at startup. |
| 2 | Built-in default | Checkout-relative public defaults. |

A future batch may add tier 3 (an external YAML/JSON config file). The
candidate path is already exposed via `settings.config_path()` and is
`%APPDATA%\harbor-launcher\config.yaml` on Windows by default.

---

## 2. Supported Environment Variables

All environment variable names use the `HARBOR_` prefix. The list below
covers the full set accepted by the launcher. Paths are coerced to
`pathlib.Path`; numeric timings are coerced to `float`.

### Production runtime install

| Variable | Default | Purpose |
|----------|---------|---------|
| `HARBOR_HOME` | Launcher checkout | Root of the Harbor install. Contains `server_legacy.py`, `codex_job_daemon.py`, the PowerShell supervisors, and the configured Python runtime. |
| `HARBOR_PRODUCTION_PATH` | _(alias of `HARBOR_HOME`)_ | Legacy alias; preferred name is `HARBOR_HOME`. |
| `HARBOR_JUNCTION_PATH` | `<HARBOR_HOME>/.harbor-junction` | Optional compatibility path used by process discovery. |

### Tunnel-client

| Variable | Default | Purpose |
|----------|---------|---------|
| `HARBOR_TUNNEL_EXE` | `tunnel-client.exe` | Path to the `tunnel-client.exe` binary. |
| `HARBOR_TUNNEL_PROFILE_DIR` | `%APPDATA%\tunnel-client` | Per-profile state directory. |
| `HARBOR_TUNNEL_PROFILE_NAME` | `harness-harbor` | Active tunnel-client profile name. |
| `HARBOR_TUNNEL_HEALTH_URL_FILE` | `%USERPROFILE%\.local\state\tunnel-client\health\harness-harbor.url` | Health-endpoint URL file written by tunnel-client. |

### Legacy Python venv (drives the MCP server and job daemon)

| Variable | Default | Purpose |
|----------|---------|---------|
| `HARBOR_VENV_PYTHON` | `<HARBOR_HOME>\.venv-legacy\Scripts\python.exe` | Console Python interpreter. |
| `HARBOR_VENV_PYTHONW` | `<HARBOR_HOME>\.venv-legacy\Scripts\pythonw.exe` | Windowless Python interpreter. |

### Runtime job queue

| Variable | Default | Purpose |
|----------|---------|---------|
| `HARBOR_JOBS_DIR` | `<runtime code root>\.jobs` | Absolute path to the shared runtime queue. MCP/control-plane producers, poll/cancel readers, and the daemon must receive the same value. Relative paths are rejected so changing the process working directory cannot redirect the queue. |

Set `HARBOR_JOBS_DIR` explicitly when the MCP server and daemon are installed
in different directories. Leaving it unset uses the runtime code root.

### Script & scheduled-task names

| Variable | Default | Purpose |
|----------|---------|---------|
| `HARBOR_MCP_SCRIPT` | `server_legacy.py` | The **real** Harbor MCP entry script. The launcher explicitly refuses to match the simplified `server.py` placeholder. |
| `HARBOR_DAEMON_SCRIPT` | `codex_job_daemon.py` | Codex Job Daemon entry script. |
| `HARBOR_SCHEDULED_TASK_TUNNEL` | `Harness Harbor Tunnel` | Windows Scheduled Task name for the tunnel supervisor. |
| `HARBOR_SCHEDULED_TASK_DAEMON` | `Harness Harbor Job Daemon` | Windows Scheduled Task name for the daemon supervisor. |
| `HARBOR_AUTOSTART_APP_NAME` | `HarnessHarborLauncher` | HKCU Run registry value name for Windows autostart. |

### Timing

| Variable | Default | Purpose |
|----------|---------|---------|
| `HARBOR_POLL_INTERVAL_SECONDS` | `3.0` | Background health-poll interval. |
| `HARBOR_STOP_TIMEOUT` | `8.0` | Maximum time to wait for processes to terminate on Stop. |
| `HARBOR_START_HEALTH_TIMEOUT` | `15.0` | Maximum time to wait for the runtime to become healthy on Start. |
| `HARBOR_HTTP_PROBE_TIMEOUT` | `2.0` | HTTP probe timeout for the tunnel health endpoint. |

---

## 3. Usage Examples

### 3.1 Point the launcher at a different production install

```powershell
$env:HARBOR_HOME = "E:\harbor\prod"
& "C:\Users\Example\HarnessHarbor\run-launcher.ps1"
```

The launcher derives `TUNNEL_LOG`, `DAEMON_LOG`, `START_TUNNEL_SCRIPT`,
`START_DAEMON_SCRIPT`, and `VENV_PYTHON` / `VENV_PYTHONW` from
`HARBOR_HOME` automatically — no need to override each one separately.

### 3.2 Override individual fields

```powershell
$env:HARBOR_TUNNEL_PROFILE_NAME = "lab-profile"
$env:HARBOR_TUNNEL_EXE = "E:\bin\tunnel-client.exe"
$env:HARBOR_POLL_INTERVAL_SECONDS = "5.0"
python run_launcher.py
```

### 3.3 Inspect resolved configuration

The Diagnostics dialog (🔍 in the launcher UI) shows every resolved value
and which tier produced it (`env:HARBOR_HOME` vs. `default`).

Programmatically:

```python
from launcher import settings
for key, info in settings.describe().items():
    print(f"{key:32s} = {info['value']:60s}  [{info['source']}]")
```

---

## 4. Behavior Guarantees

* **Read-once at startup.** Environment variables are sampled when the
  launcher process starts. Changing an env var in a running launcher
  session requires a restart. This matches the production deployment
  model (env vars are set by the user's shell or by a wrapper script).
* **Malformed overrides fall back to the default** with the Diagnostics
  dialog still reporting `default` for that field. The launcher never
  crashes on a bad env var.
* **Brand assets are launcher-local.** Logo, header mark, tray icon,
  and the Windows `.ico` live next to the launcher binary and are not
  affected by any of the deployment-path overrides above.
* **Backward compatible.** Every name previously exported from
  `launcher.config` continues to exist and is computed from the
  resolver. Existing code that does
  `from launcher.config import PRODUCTION_PATH` keeps working without
  changes.

---

## 5. Public API (for developers)

The settings layer exposes a small, stable surface in
[`launcher/settings.py`](../launcher/settings.py):

| Function | Purpose |
|----------|---------|
| `get(key, default=None)` | Resolve a single key; returns `default` for unknown keys. |
| `all_settings()` | Snapshot of every resolved value. |
| `source_for(key)` | Returns `"env:<NAME>"` or `"default"` for the given key. |
| `describe()` | `{key: {"value": str, "source": str}}` for the Diagnostics dialog. |
| `config_path()` | Candidate path for the (future) YAML config file. |
| `reload()` | Discard the in-process cache. Used by tests and live-reload tools. |

The `launcher.config` module continues to export every legacy constant
as a module-level name. New code is encouraged to import from
`launcher.settings` directly so that hot-reload via `settings.reload()`
is available, but **downstream code may continue to import from
`launcher.config` without modification**.

---

## 6. Roadmap

This is **Batch 1** of the public-release configuration foundation.
Planned follow-ups (in approximate order):

* **Batch 2** — Optional external YAML/JSON config file at
  `config_path()`, with the same schema as the env-var tier.
* **Batch 3** — Refactored PyInstaller spec, install layout, and a
  Windows-friendly first-run setup wizard.
* **Batch 4** — Public release checklist (signing, code-signing cert,
  installer smoke test, etc.).
