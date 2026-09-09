# Harness Harbor Supervisor Skill Sidecar

This document explains the role, architecture, and optional installation of the **Harness Harbor Supervisor Skill** sidecar (`skills/harbor-supervisor/SKILL.md`).

---

## 1. What is the Supervisor Skill?

The Supervisor Skill is an AI operational behavior policy that teaches the model how to act as an effective engineering supervisor when connected to Harness Harbor.

Harbor provides two distinct categories of capabilities:
1. **Direct Read-Only MCP Tools**: Fast, immediate tools for inspecting git repositories, reading files, and performing host network and system diagnostics.
2. **Asynchronous Execution Harnesses**: Background worker engines (e.g. Codex, MiniMax, Antigravity / AGY) for writing code, implementing features, running long builds, and repairing tests.

The Supervisor Skill establishes a disciplined division of responsibility:
> **The Supervisor reads, reasons, reviews, and decides. Harnesses execute.**

The Skill guides the model to perform all inspections, investigations, diff evaluations, and acceptance reviews directly, reserving Harness delegation strictly for active code mutations and heavy operations.

---

## 2. Decoupling from Tunnel & MCP Transport

The Supervisor Skill is completely decoupled from the transport and server runtime layers:

```text
ChatGPT Client Layer
  +-- [Optional] Harbor Supervisor Skill (AI behavioral policy)
  |
  v (MCP JSON-RPC over secure tunnel)
Tunnel Transport Layer (tunnel-client / public tunnel endpoint)
  |
  v (localhost loopback)
Harbor Server Runtime (FastMCP server + Job Daemon)
  |
  v
Local Git, Filesystem, Host Diagnostics, and CLI Harnesses
```

- **Tunnel Isolation**: The Skill does not participate in or configure network transport. It has zero knowledge of tunnel endpoints, profiles, ports, or credentials.
- **Runtime Independence**: Harbor MCP server (`server_legacy.py`) and the background Job Daemon (`codex_job_daemon.py`) execute independently of the Skill file. They do not load, parse, or require `SKILL.md`.

---

## 3. Optional Installation Model

The Supervisor Skill is an **optional sidecar capability**:

- **Skill Installed**: The AI client loads `SKILL.md` as context or instructions, guiding it to follow Harbor supervisor best practices during interactions.
- **Skill Skipped / Unsupported**: If the client surface does not support installing custom skills, or if the user chooses not to install it, Harbor MCP and all associated harnesses continue to function normally.

A missing or uninstalled Skill is **never** an installation failure for Harness Harbor.

---

## 4. Installation & Usage Guidance

When using a ChatGPT client or development surface that supports custom Skill installation:

1. **Locate the Skill File**:
   The skill definition resides at:
   ```text
   skills/harbor-supervisor/SKILL.md
   ```
2. **Install via Client Surface**:
   - In environments supporting skill sidecars or system prompts (e.g., custom instructions, project instructions, or skill directories), import or link `skills/harbor-supervisor/SKILL.md`.
   - The file provides standard frontmatter metadata (`name`, `description`, `version`) and operational rules.
3. **Dynamic Discovery**:
   The Skill instructs the model to dynamically inspect available tools and schemas upon connecting to the Harbor MCP session, ensuring compatibility as new diagnostic or management tools are introduced.

If custom skill installation is not supported by your current surface, no action is required -- connect to Harbor via standard MCP configuration and use the tools directly.
