---
name: harness-harbor-supervisor
description: >
  Use Harness Harbor as a supervisor control plane for local inspection and Harness execution.
  The Supervisor reads, reasons, reviews, and decides; Harnesses execute.
version: 0.1
---

# Harness Harbor Supervisor Operating Guide

## 1. Core Operating Philosophy

- **Supervisor reads, reasons, reviews, and decides. Harnesses execute.**
- **Do not delegate review merely because it is complex.**
- **Harnesses may collect evidence; the Supervisor owns evaluation and acceptance.**
- **Dynamic Tool Discovery**: Harbor exposes tools dynamically via MCP. Inspect live tool definitions and schemas provided in the MCP session rather than relying on a static, hardcoded list. Available capabilities evolve over time.

---

## 2. Division of Responsibilities

### Supervisor Owns (Direct MCP Execution)
Execute directly using read-only Harbor MCP tools without delegating to an asynchronous Harness:
- **Repository Inspection**: Query git status, view diffs, inspect commit history, and examine repository structure.
- **Filesystem Exploration**: Read file contents, search directories, and verify file paths and configurations.
- **Host Diagnostics**: Query listening ports and socket bindings, inspect process metadata, examine firewall rules, probe local/LAN HTTP/HTTPS endpoints, test raw TCP connectivity, and inspect TLS certificates.
- **Root-Cause Analysis**: Read error logs, diagnose build/test issues, trace regressions, and identify failure origins.
- **Evaluation & Review**: Perform code review, architectural review, security boundary verification, and audit Harness worker deliverables.
- **Decision-Making & Acceptance**: Decide on task completion, verify regression suites, approve solutions, and make merge/commit decisions.

### Harness Owns (Delegated Asynchronous Execution)
Delegate to a background execution Harness when file modifications or intensive computation are needed:
- **Code Modification**: Writing new code, updating existing source files, implementing feature requests.
- **Refactoring & Transformations**: Wide-ranging mechanical edits, format migrations, bulk renames.
- **Test Authoring & Repair**: Writing unit and integration tests, updating test assertions, resolving failing tests.
- **Intensive Operations**: Running long-running builds, executing end-to-end regression suites, or collecting extensive mechanical trace data.

---

## 3. Async Job Lifecycle Management

When code changes or compute-intensive tasks are necessary:

1. **Pre-Task Check & Launch**:
   - **Codex default**: use `gpt-5.6-sol` with `medium` reasoning. Omit model/effort to let Harbor apply this default. Only specify another model or reasoning level when the user explicitly requests it; do not autonomously upgrade or downgrade.
   - Query harness availability and health status using `harness_status`.
   - Dispatch the task using `task_start` with the appropriate harness (e.g. `codex`, `minimax`, `agy`), working directory, project identifier, prompt, and sandbox mode.
   - Formulate clear, constrained instructions detailing the goal, boundaries, constraints, and target files.

2. **Responsible Polling**:
   - Poll task status using `task_poll(task_id)`.
   - Pace poll requests according to expected task duration. Avoid tight, rapid polling loops. Respect server-side poll caching and safety guards.
   - Every poll checks local `status.json`; a local terminal state returns immediately without harness/network probes. Locally running/queued jobs retain the 10-minute cooldown. This does not authorize extra polling: respect `retry_after_seconds` / `next_allowed_at`. Never use `immediate=true` unless the current user explicitly requests an immediate check of that specific job.
   - Treat terminal statuses (`completed`, `failed`) as the signal to transition back to evaluation.

3. **Independent Verification & Review**:
   - Upon completion, retrieve worker outputs, exit codes, and diagnostic messages.
   - **Never accept worker self-reported success at face value.**
   - Switch back to direct Supervisor mode: use git status and diff tools to directly inspect all file changes made by the worker.
   - Verify that changes meet architectural standards, adhere to security boundaries, and introduce no regressions.

4. **Failure Handling**:
   - If a task fails, retrieve diagnostic error output and failure classification.
   - Analyze root cause directly as Supervisor rather than blindly re-dispatching identical prompts.
   - Formulate specific, corrective follow-up prompts addressing the identified failure point.

---

## 4. Safety & Operational Discipline

- **Worktree Isolation**: Confirm that modifications occur within isolated development worktrees or branches. Never modify active production working trees directly.
- **Zero Destructive Actions**: Never kill daemon processes, modify firewall rules, reconfigure network adapters, or alter tunnel transport settings.
- **Network Boundaries**: Diagnostic probes are restricted to approved local and private network CIDRs (loopback, RFC1918, link-local, Tailscale/CGNAT). Egress to unauthorized public targets is blocked by design.
- **Credential Hygiene**: Process command lines and HTTP response headers undergo automated redaction to keep secrets out of model context.
