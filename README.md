[English](README.md) | [简体中文](README.zh-CN.md)

# Harness Harbor

**Turn local AI coding CLIs into a worker pool for your AI supervisor.**

Harness Harbor is a lightweight local execution broker for AI coding harnesses.

It exposes locally installed tools such as **Codex**, **AGY**, and **MiniMax** through a single MCP control plane, allowing an upstream AI supervisor to dispatch focused coding jobs, track long-running work, manage workspace access, recover execution state, and continue multi-stage development workflows.

One particularly useful setup is:

```text
ChatGPT Chat
     │
     │ reasoning · planning · review
     │ acceptance · scheduling
     │
     │ MCP
     ▼
OpenAI Secure MCP Tunnel
     │
     ▼
Harness Harbor
     │
     ├── Codex
     ├── AGY
     └── MiniMax
          │
          ▼
    Local worktrees
```

In this architecture, **ChatGPT remains the Supervisor**.

Harbor does not replace ChatGPT with another local autonomous agent. Instead, it gives the ChatGPT conversation a reliable execution layer on your development machine.

ChatGPT can break a larger project into bounded stages, send one focused task at a time to a coding harness, inspect the result, and decide what should happen next.

When combined with **ChatGPT Scheduled Tasks**, the Supervisor can also come back later by itself, inspect a completed Codex job, accept or reject the result, and dispatch the next stage.

This makes workflows possible that continue far beyond a single interactive chat turn.

> **Platform status:** Windows 1.1.0 convergence changes have not completed native Windows testing. Earlier validation does not certify this update.
> macOS has an Apple Silicon development implementation: native SwiftUI Lighthouse and a bundled Python runtime. An Apple Silicon DMG preview is available; stable release acceptance and Intel validation remain pending. See [macOS setup and release gates](MACOS.md).

## Downloads and platform versions

- **macOS 1.1.0 preview (Apple Silicon)**: [Download the DMG and SHA256 checksums](https://github.com/JL066/harness-harbor/releases/tag/macos-v1.1.0-preview.1). Ad-hoc signed, not notarized; Gatekeeper may block opening it. This is not a stable release.
- **Windows 1.1.0**: shared logic and mock tests passed on Mac. Native Windows CI, launcher/tray, Credential Manager, Task Scheduler and real process-tree checks remain incomplete. Source only; no Windows binary release yet.
- Platforms release independently: `macos-v<version>` and `windows-v<version>` tags, with `-preview.N` for previews. Releases and assets are platform-specific; shared source remains in this repository.

See the [test matrix](docs/convergence/TEST_MATRIX.md) for the acceptance scope.


---

## Why Harness Harbor?

AI coding tools are increasingly capable, but they usually live behind separate CLIs with their own authentication, model catalogs, quota behavior, process semantics, and execution state.

Without a broker, an upstream Supervisor needs separate glue for every worker:

```text
Supervisor
   ├── Codex integration
   ├── AGY integration
   ├── MiniMax integration
   ├── job tracking
   ├── concurrency handling
   ├── workspace locking
   └── crash recovery
```

Harness Harbor collapses that into one execution surface:

```text
Supervisor
    │
   MCP
    │
    ▼
Harness Harbor
    │
    ├── Codex
    ├── AGY
    └── MiniMax
```

The Supervisor keeps the reasoning loop.

Harbor handles the local execution loop.

---

## ChatGPT can be the Supervisor

Harness Harbor does **not** require another long-lived local AI agent runtime.

A normal ChatGPT conversation can remain the place where you:

- discuss requirements;
- reason about architecture;
- break a project into stages;
- decide which worker should handle each stage;
- review implementation results;
- reject or redirect failed work;
- decide when the project is ready to continue.

Harbor sits underneath that conversation.

```text
┌───────────────────────────────────────┐
│             ChatGPT Chat              │
│                                       │
│ Understand · Reason · Plan            │
│ Review · Accept · Retry · Schedule    │
└───────────────────┬───────────────────┘
                    │
                 MCP tools
                    │
                    ▼
          OpenAI Secure MCP Tunnel
                    │
                    ▼
┌───────────────────────────────────────┐
│           Harness Harbor              │
│                                       │
│ Probe · Route · Queue · Lease         │
│ Run · Track · Recover                 │
└───────────┬──────────┬────────────────┘
            │          │
            ▼          ▼
          Codex       AGY       MiniMax
            │          │          │
            └──────────┴──────────┘
                       │
                       ▼
                Local worktrees
```

The responsibilities stay deliberately separate.

### ChatGPT / upstream Supervisor

The Supervisor:

- understands the user's intent;
- plans the work;
- decomposes large goals into stages;
- chooses an appropriate coding harness;
- provides each worker with a focused task;
- evaluates the returned result;
- decides whether the stage passes;
- chooses whether to retry, correct, or continue;
- schedules future checks when necessary.

### Harness Harbor

Harbor:

- exposes the local worker pool through MCP;
- probes harness availability;
- queues work;
- controls concurrency;
- leases workspaces;
- starts local coding jobs;
- persists job state;
- returns results;
- recovers stale reservations and execution state.

### Coding harnesses

Codex, AGY, MiniMax, or another supported worker performs the actual implementation work:

- reading repositories;
- editing code;
- running commands;
- executing tests;
- inspecting results;
- completing the bounded task assigned by the Supervisor.

The coding harness is a **worker**, not the long-lived project Supervisor.

---

## Long-lived Supervisor, bounded workers

A central design idea behind this workflow is simple:

> **Keep project continuity in the Supervisor, but keep individual coding jobs focused and bounded.**

Instead of asking one coding agent to remain inside a single ever-growing session for an entire project, ChatGPT can decompose the project into smaller stages.

For example:

```text
Project goal
    │
    ▼
Stage 1: inspect architecture
    │
    ▼
Codex job
    │
    ▼
Supervisor review
    │
    ▼
Stage 2: implement backend change
    │
    ▼
Codex job
    │
    ▼
Supervisor review
    │
    ▼
Stage 3: add tests
    │
    ▼
AGY job
    │
    ▼
Supervisor review
    │
    ▼
Stage 4: release audit
```

Each worker is asked to solve **one specific problem with explicit boundaries and acceptance criteria**.

The Supervisor retains the larger project picture.

This separation can help reduce several problems that tend to appear in very long coding-agent sessions:

- **context drift** — earlier requirements become less prominent as the execution history grows;
- **scope creep** — the worker starts changing things outside the task it was originally given;
- **instruction dilution** — important constraints become buried inside a large accumulated context;
- **stale assumptions** — conclusions made much earlier continue influencing later work even after the repository has changed;
- **review fatigue** — implementation and evaluation become mixed together instead of occurring at clear stage boundaries.

Harbor does not claim to make context-window limitations disappear.

Instead, it makes a different workflow practical:

```text
large project context
        │
        ▼
    Supervisor
        │
        ├── focused task A ──► worker
        │                       │
        │◄────── result ────────┘
        │
        ├── review / update project state
        │
        ├── focused task B ──► worker
        │                       │
        │◄────── result ────────┘
        │
        └── review / continue
```

The long-running reasoning loop and the coding execution loop no longer have to be the same session.

That makes it easier to keep each implementation stage narrow, testable, and independently reviewable.

---

## Continuous workflows with ChatGPT Scheduled Tasks

Harbor's persistent jobs become especially useful when combined with ChatGPT's built-in scheduling capability.

A coding job may take several minutes or much longer.

The user should not need to manually ask:

> Is Codex finished yet?

every time.

Instead, the workflow can be:

```text
User request
     │
     ▼
ChatGPT plans Stage 1
     │
     ▼
task_start(...)
     │
     ▼
Harbor returns job_id
     │
     ▼
ChatGPT schedules a future check
     │
     │
     │     Codex continues working locally
     │
     ▼
Scheduled Task wakes ChatGPT
     │
     ▼
task_poll(job_id)
     │
     ▼
ChatGPT inspects the result
     │
     ├── PASS
     │     │
     │     ▼
     │   plan Stage 2
     │     │
     │     ▼
     │   task_start(...)
     │     │
     │     ▼
     │   schedule next review
     │
     ├── NEEDS FIX
     │     │
     │     ▼
     │   dispatch corrective task
     │
     └── COMPLETE
           │
           ▼
        report result
```

The important part is that the scheduled wake-up is not merely a notification.

The Supervisor can use that later run to **continue the supervision loop**:

```text
Plan
  ↓
Dispatch
  ↓
Wait
  ↓
Wake
  ↓
Inspect
  ↓
Accept / Reject
  ↓
Plan next bounded stage
  ↓
Dispatch
  └──────────────↺
```

For example, ChatGPT might:

1. ask Codex to implement a backend change;
2. store the returned Harbor `job_id`;
3. schedule a later check;
4. return later and poll the job;
5. inspect the changed files and test results;
6. reject the implementation if acceptance criteria were not met;
7. issue a small corrective task;
8. review that result;
9. dispatch a separate test-writing task;
10. later run a final audit stage.

The user can leave the workflow while those stages are being supervised.

This turns a normal ChatGPT conversation into a practical long-running development Supervisor without moving the reasoning layer into another local agent runtime.

The exact availability of Scheduled Tasks, MCP integrations, persistent permissions, and unattended actions depends on the user's ChatGPT plan and workspace configuration.

Harbor itself does not implement the scheduler.

It provides the persistent execution state that makes this supervision pattern possible.

---

## Why not just give one agent the entire project?

You can.

Harbor does not prevent that workflow.

But for larger projects, there is another useful pattern:

```text
One huge agent session

Requirement
   ↓
Architecture
   ↓
Implementation
   ↓
More implementation
   ↓
Tests
   ↓
Debugging
   ↓
More debugging
   ↓
Release
```

As that execution history grows, the worker has to carry more accumulated context and more historical decisions.

Harbor makes it practical to use a staged alternative:

```text
              Supervisor
                  │
        ┌─────────┼─────────┐
        ▼         ▼         ▼
     Stage 1   Stage 2   Stage 3
        │         │         │
      worker    worker    worker
        │         │         │
        └──── results ──────┘
                  │
                  ▼
           Supervisor review
```

Each task can have:

- a narrow goal;
- a defined workspace;
- explicit constraints;
- acceptance criteria;
- a clear stopping point.

The Supervisor then decides what the next task should be based on the actual repository state.

This architecture can reduce context drift and scope expansion while making intermediate review much easier.

It also allows different coding harnesses to be used for different stages without changing the higher-level workflow.

---

## What Harbor provides

### One MCP control plane

The Supervisor talks to Harbor instead of implementing a completely separate execution integration for every coding CLI.

### Persistent jobs

Jobs are persisted under `.jobs/`.

The Supervisor can submit work now, retain a stable `job_id`, and inspect the same job later.

That later inspection can happen in another supervision turn rather than immediately after submission.

### Harness probing

Harbor can inspect supported local harness integrations and expose their verified availability and capabilities.

### Workspace leases

Harbor prevents conflicting workers from simultaneously owning the same project workspace.

### Per-harness concurrency

Queued work is leased according to per-harness concurrency limits.

### Supervised subprocess execution

Workers run as controlled local processes with bounded execution behavior and persisted state.

### Recovery

Harbor can recover stale reservations and workspace leases after worker or daemon failure or restart.

### Codex routing

For Codex, the Supervisor can explicitly choose between several execution routes, including controlled fallback between an official provider and a generic custom Responses API provider.

### Authoritative telemetry and quota

Harbor provides a unified, read-only telemetry model (`harness_telemetry`) covering harness status, process activity, and AGY models. Quota is reported only when an authoritative provider source exists; otherwise it is reported unavailable and never guessed.

### Safe Git delivery

Harbor exposes constrained `git_ls_remote`, `git_push_dry_run`, and `git_push_ref` tools for safe branch delivery without exposing generic shell access or accepting credential injection.

---

## Harbor is intentionally not an agent framework

Harness Harbor is not intended to become another autonomous-agent runtime.

The distinction is:

```text
Supervisor / ChatGPT
────────────────────────────────────
Understand
Reason
Plan
Decompose
Choose worker
Review result
Accept / reject
Schedule future work

                │
               MCP
                │
                ▼

Harness Harbor
────────────────────────────────────
Probe
Route
Queue
Lease
Run
Persist
Track
Recover

                │
                ▼

Coding harness
────────────────────────────────────
Inspect repository
Edit code
Run commands
Run tests
Complete assigned task
```

Harbor does not provide:

- general-purpose reasoning;
- project planning;
- result acceptance;
- conversational memory;
- personality;
- a replacement chat interface;
- a long-lived autonomous reasoning loop;
- scheduled wake-ups.

The Supervisor remains the brain.

The coding harness remains the worker.

Harbor connects the two.

---

## Supported harnesses

Current integrations include:

| Harness | Status | Notes |
| --- | --- | --- |
| Codex | Supported | Includes configurable execution routes |
| Antigravity / AGY | Supported | Uses the locally installed and configured CLI |
| MiniMax | Supported | Uses the locally installed and configured CLI |

Each CLI must already be installed and configured locally.

Harbor does not bundle:

- coding CLIs;
- models;
- credentials;
- subscriptions;
- API accounts;
- provider access.

Use the MCP tools `harness_list` and `harness_status` to inspect what is actually available on the current machine.

The MCP server also exposes `harness_telemetry` for unified, authoritative inspection of installation state, process activity, and AGY models. Quota is displayed only when an authoritative provider source exists; otherwise it is reported unavailable and never guessed. Lighthouse consumes this authoritative telemetry directly in the UI.

---

## Quick Start

The Windows entrypoint uses **Windows PowerShell**. Native Windows CI and real-machine acceptance of this 1.1.0 update remain pending. Python 3.11/3.12 are configured CI targets, not evidence that this update passed.

### 1. Clone the repository

```powershell
git clone https://github.com/JL066/harness-harbor.git
cd harness-harbor
```

### 2. Create a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

### 4. Configure local coding harnesses

Install and authenticate the coding CLIs you intend to use.

Harbor can use executables already available on `PATH`, or the corresponding `HARBOR_*` environment variables described in [.env.example](.env.example).

Harbor does not automatically load `.env.example`.

### 5. Start the full MCP server

```powershell
python server_legacy.py
```

`server_legacy.py` is currently the complete MCP server.

Its filename is retained for source compatibility.

`server.py` is the smaller Codex-only compatibility server.

### 6. Optional: start the job daemon

For background dispatch of queued jobs, open another PowerShell window and run:

```powershell
.\start-codex-job-daemon.ps1
```

The launcher uses `python` unless another interpreter is selected through `HARBOR_PYTHON`.

### 7. Optional: launch Lighthouse graphical launcher or package standalone executable

Harness Harbor includes **Lighthouse**, a standalone Windows graphical launcher providing a setup/settings wizard, service lifecycle management, system tray support, log viewer, diagnostics, and background health monitoring.

Start Lighthouse with Python:

```powershell
python run_launcher.py
```

or using the PowerShell launcher script:

```powershell
.\run-launcher.ps1
```

To package Lighthouse as a standalone Windows executable:

```powershell
python build_exe.py
```

Packaging uses `harbor_launcher.spec` and bundles brand icon assets from `launcher/assets/`.

---

## Core + Lighthouse Launcher

This Public RC includes both the Python Core and Lighthouse, a standalone Windows graphical launcher. Lighthouse provides the setup/settings wizard, secure CredentialStore integration, managed tunnel profile/lifecycle, start/stop/restart controls, tray support, log viewing, diagnostics, and a background health monitor. The local development version is `1.1.0`; start it with `run_launcher.py` or `run-launcher.ps1`, and package it with `build_exe.py` or `harbor_launcher.spec`. Harbor/Lighthouse icons and runtime packaging assets are included in `launcher/assets/`.

The wizard persists only non-secret settings. Tunnel runtime keys and custom provider keys are stored in Windows Credential Manager and passed only to the relevant child process environment; plaintext fallback is forbidden. Managed tunnel settings use operator-supplied generic HTTPS control configuration and local paths, with no private endpoint or account binding bundled.

Lighthouse consumes Core's authoritative telemetry for Codex, MiniMax, and Antigravity/AGY. It displays verified installation/capability state, activity, and AGY models. Quota is displayed only when an authoritative provider source exists; otherwise it is reported unavailable and never guessed.

---

## Connecting ChatGPT to Harbor

A Harbor MCP server running on a private development machine cannot normally be reached directly by ChatGPT.

For the ChatGPT Supervisor architecture, the connection is:

```text
ChatGPT Chat
     │
     │ MCP
     ▼
OpenAI Secure MCP Tunnel
     │
     ▼
Harness Harbor
     │
     ▼
Local coding harnesses
```

OpenAI Secure MCP Tunnel provides a way for supported OpenAI environments to connect to an MCP server running on a developer machine or private network without directly exposing that MCP server to the public internet.

Once the Harbor MCP integration is available inside the ChatGPT environment, the conversation can invoke its tools like any other permitted MCP integration.

The tunnel client, OpenAI-side configuration, authentication, and permissions are external to this repository.

Harbor does not bundle tunnel credentials or an OpenAI account configuration.

For environments using managed tunnels, Lighthouse provides managed tunnel profile and lifecycle controls directly from the launcher interface, with tunnel runtime credentials stored securely in Windows Credential Manager without plaintext fallback. All managed tunnel settings use operator-supplied generic HTTPS control configuration and local paths, with no private endpoint or account binding bundled.

The included `start-tunnel.ps1` is optional convenience tooling for headless or command-line environments where the external tunnel client has already been configured.

It refuses to start until its executable and profile settings are supplied through environment variables, and it contains no bundled tunnel executable, profile, or credentials.

A tunnel is unnecessary when the MCP client can already reach Harbor directly or when Harbor is being used locally through an appropriate transport.

---

## A typical Harbor job

An upstream Supervisor can first inspect available workers:

```text
harness_list()
```

or query an integration:

```text
harness_status(...)
```

It can then start one focused stage:

```text
task_start(...)
```

Harbor returns:

```text
job_id
```

The Supervisor stores that identifier.

It does **not** need to block the conversation waiting for completion.

Later, it can inspect the same job:

```text
task_poll(job_id)
```

Then the Supervisor makes the important decision:

```text
result
  │
  ├── accepted
  │      │
  │      ▼
  │   next stage
  │
  ├── rejected
  │      │
  │      ▼
  │   corrective task
  │
  └── project complete
```

That separation between **execution** and **acceptance** is fundamental to Harbor's intended workflow.

The worker does the coding.

The Supervisor decides whether the coding is good enough.

---

## Two-layer routing

Harbor separates two routing decisions.

### Layer 1: choose the harness

The Supervisor selects:

```text
codex
agy
minimax
```

### Layer 2: choose the Codex route

When the selected harness is Codex, the Supervisor can additionally choose:

```text
current
official
custom
official_then_custom
```

MiniMax and AGY currently accept `current` only.

### `current`

Uses the user's normal Codex CLI configuration without applying a Harbor provider override.

### `official`

Adds the process-local Codex override:

```text
-c model_provider="openai"
```

### `custom`

Uses a generic process-local provider named `harbor_custom` with Responses API wire mode.

Configure it with:

```text
HARBOR_CODEX_CUSTOM_BASE_URL
HARBOR_CODEX_CUSTOM_API_KEY
```

and optionally:

```text
HARBOR_CODEX_CUSTOM_MODEL
```

Harbor passes the API key only through a copied child-process environment using the provider's `env_key`.

It does not write the key into the user's Codex configuration and does not place the key in command-line arguments.

The custom route is a configurable generic OpenAI-compatible provider. The Public RC contains no personal route, private provider, account binding, or provider-specific fallback configuration.

### `official_then_custom`

This route first attempts the subscription-backed official provider.

The custom route is attempted only when the first attempt clearly identifies OpenAI/Codex subscription or usage-quota exhaustion.

It does not automatically fall back for:

- authentication failures;
- ordinary rate limits;
- generic HTTP 429 responses;
- unknown failures;
- coding failures;
- task failures;
- prompt failures;
- parser failures;
- test failures.

Harbor records only the requested and used routes, sanitized classification information, and a bounded attempt summary.

The Supervisor remains responsible for deciding whether the result is acceptable.

---

## Safe Git delivery

The complete MCP server exposes constrained `git_ls_remote`, `git_push_dry_run`, and `git_push_ref` tools for safe branch delivery:

- accept configured remote names only;
- require credential-free HTTPS push URLs and exact `refs/heads/*` branch refs;
- reject tags, branch deletions, force pushes, and arbitrary refspecs;
- verify the expected remote HEAD before push;
- perform one non-force fast-forward update and verify the resulting remote ref;
- stream bounded, secret-redacted Git output without exposing a generic shell or credential injection API.

---

## Local execution model

Runtime job records are stored under:

```text
.jobs/
```

Routing state and project aliases are stored under:

```text
.control/
```

Both contain local runtime state and are ignored by Git.

The optional `codex_job_daemon.py` dispatches queued jobs locally.

It is part of the execution layer.

It is not:

- the reasoning service;
- the project Supervisor;
- the scheduler;
- the acceptance loop.

Harbor's local execution path can be summarized as:

```text
Probe
  ↓
Route
  ↓
Queue
  ↓
Lease
  ↓
Run
  ↓
Track
  ↓
Recover
```

---

## Security

Harness Harbor can invoke powerful coding-agent CLIs against real local repositories.

Treat access to the Harbor MCP endpoint accordingly.

Only expose Harbor to clients and networks you trust.

Do not commit:

- `.env` files;
- API keys;
- CLI credentials;
- tunnel credentials;
- tunnel profiles;
- `.jobs/`;
- `.control/`;
- local logs;
- secret-bearing provider configuration;
- diagnostic outputs, dumps, reports, or machine-specific artifacts.

AGY's `--dangerously-skip-permissions` flag is never enabled automatically.

Set:

```text
HARBOR_AGY_DANGEROUSLY_SKIP_PERMISSIONS=1
```

only if you explicitly intend to use that behavior and have reviewed [SECURITY.md](SECURITY.md).

The setup wizard persists only non-secret settings. Tunnel runtime keys and custom provider keys are stored in Windows Credential Manager and passed only to the relevant child process environment; plaintext fallback is forbidden. Managed tunnel settings use operator-supplied generic HTTPS control configuration and local paths, with no private endpoint or account binding bundled.

Read [SECURITY.md](SECURITY.md) before connecting Harbor to important worktrees or exposing its MCP endpoint remotely.

---

## Development and testing

Run the isolated unit suite with:

```powershell
python -m unittest discover -s tests -v
```

The tests use temporary directories and mocked subprocess behavior and do not require starting Harbor, the tunnel, the daemon, or real coding-agent CLIs.

GitHub Actions is configured to run Core tests on Windows and macOS with Python 3.11/3.12, plus macOS Swift and packaging checks. A configured workflow is not evidence of a completed CI run.

---

## Platform support

| Platform | Status |
| --- | --- |
| Windows | 1.1.0 native CI / real-machine acceptance incomplete; no new binary release |
| macOS | Apple Silicon DMG preview; not notarized; Intel / stable acceptance pending |
| Linux | Not currently validated |

The current implementation is Windows-first, including its PowerShell launchers, Windows Credential Manager integration, Lighthouse GUI, and process behavior.

Native macOS setup, build instructions and outstanding release acceptance are documented in [MACOS.md](MACOS.md).

---

## Project scope

Harness Harbor is intentionally a relatively small execution broker.

It does not attempt to own the entire AI development workflow.

An upstream Supervisor might be:

- a normal ChatGPT conversation;
- another MCP-capable AI assistant;
- a custom agent;
- an internal automation service;
- another application implementing its own reasoning and acceptance loop.

Harbor stays underneath that layer.

Its job is simple:

> **Give the Supervisor a persistent, reliable way to operate local coding workers without turning those workers into the Supervisor itself.**

---

## Third-party services and trademarks

Harness Harbor is an independent third-party project.

It is not affiliated with, endorsed by, or sponsored by OpenAI or by the providers of Codex, MiniMax, Antigravity / AGY, or any tunnel service.

References to third-party products describe interoperability only.

---

## License

Licensed under the Apache License, Version 2.0.

See [LICENSE](LICENSE).
