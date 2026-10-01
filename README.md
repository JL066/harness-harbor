# ChatGPT Harbor

Current Windows release candidate: **v1.1.3**.

ChatGPT Harbor provides a secure, structured local control plane connecting ChatGPT to local development environments via the Model Context Protocol (MCP) and secure tunneling.

---

## Launcher

The `launcher/` package ships a standalone Windows GUI that supervises
the Harbor runtime: start / stop / restart, live health polling,
log viewer, and diagnostics. Configuration is resolved at startup from
environment variables with built-in defaults — see
[`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) for the full override
guide.

When the runtime is running, the overall status reads **Harbor is running**.
The MCP row can still read **Running / protocol health unverified** until a
protocol check confirms its health.

```text
launcher/
  config.py            # Public import surface (derived from settings.py)
  settings.py          # Configuration resolution foundation
  autostart.py         # Windows HKCU autostart
  health_checker.py    # Tunnel / MCP / Daemon health probes
  lifecycle.py         # Start / Stop / Restart
  process_manager.py   # Process tree discovery & safe termination
  diagnostics.py       # Secret-redacted runtime report
  log_reader.py        # Mixed-encoding log tail reader
  ui/                  # customtkinter UI + system tray
  tools/               # Brand asset build pipeline
```

---

## Architecture Overview

```text
ChatGPT Client Layer
  +-- [Optional] Harbor Supervisor Skill (AI behavioral policy)
  |
  v (MCP JSON-RPC over secure tunnel)
Tunnel Transport Layer (tunnel-client)
  |
  v (localhost loopback)
ChatGPT Harbor MCP Server (server_legacy.py / codex_job_daemon.py)
  +-- Direct Read-Only Tools (Git status/diff/log, File operations)
  +-- Host Diagnostics (Port listeners, Process inspection, Firewall query, HTTP/TLS/TCP probes)
  \-- Asynchronous Execution Harnesses (Codex, MiniMax, Antigravity / AGY)
```

---

## Supervisor Skill

Harbor includes an optional **Supervisor Skill** sidecar:
- **Location**: `skills/harbor-supervisor/SKILL.md`
- **Documentation**: [`docs/SUPERVISOR_SKILL.md`](docs/SUPERVISOR_SKILL.md)

### Purpose & Operating Policy
The Supervisor Skill guides the AI model to adopt a disciplined supervisory role:
> **The Supervisor reads, reasons, reviews, and decides. Harnesses execute.**

- **Supervisor Direct Actions**: Uses Harbor's fast, direct read-only tools to inspect repositories, read files, run host network diagnostics, evaluate code diffs, trace errors, and make final acceptance decisions.
- **Harness Delegation**: Dispatches heavy code mutations, multi-file refactors, and test executions to asynchronous execution harnesses.

### Transport & Runtime Decoupling
The Supervisor Skill is an optional, client-side behavioral instruction set. It does **not** alter Tunnel configuration, MCP transport, network bindings, or server startup scripts. Harbor MCP remains 100% operational whether the Skill is installed or skipped.
