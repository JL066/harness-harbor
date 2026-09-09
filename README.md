[English](README.md) | [简体中文](README.zh-CN.md)

# Harness Harbor

*A lightweight local broker for AI coding harnesses.*

Harness Harbor turns locally installed coding harnesses into a reliable worker
pool for an upstream Supervisor/Agent. MCP is the primary integration surface
in this public release: the upstream system submits work, and Harbor provides
a small, disk-backed execution layer for **Probe → Route → Lease → Run →
Track → Recover**. The Supervisor remains the brain: it owns reasoning,
planning, acceptance, retry policy, and scheduled or event-driven wake-ups.

> **Current status: Windows-first / validated on Windows.** This public release
> is implemented and tested for Windows. **macOS: Not yet supported / planned.**
> macOS has not been implemented or validated for this release.

## Why Harness Harbor?

Coding harnesses often have separate authentication, model catalogs,
quota/availability signals, and process behavior. A Supervisor should not need
bespoke glue for every locally installed CLI. Harbor gives an MCP-capable
upstream system one narrow control surface for discovering harnesses, starting
work, polling results, and handling local execution state.

## How it fits

```text
Supervisor / Agent (reasoning, planning, acceptance, wake-up)
                         |
                        MCP
                         v
Harness Harbor (Probe · Route · Lease · Run · Track · Recover)
                         |
                         v
                 Codex / AGY / MiniMax
```

Harbor is deliberately small and relatively dumb. It does not decide what a
task means or which result is acceptable. Any upstream system that can call
MCP can use the current release. Long-running workflows are especially useful
when that upstream system also has a scheduler, future wake-up mechanism,
cron/event loop, or equivalent. Harbor itself does not provide that wake-up.

## What Harbor does

- **Probe** installed harness integrations and their verified capabilities.
- **Route** caller-selected work to the current local project/workspace and,
  for Codex, to a second generic execution route. Harbor does not perform
  intelligent automatic routing: the Supervisor selects both layers.
- **Lease** queued work with per-harness concurrency limits and an exclusive
  workspace lease.
- **Run** bounded local CLI jobs with supervised subprocess I/O.
- **Track** job status and results in disk-backed records under `.jobs/` for
  polling and diagnostics.
- **Recover** stale reservations and workspace leases after worker or daemon
  failure/restart.

The complete MCP server is `server_legacy.py`; its name is retained for source
compatibility. `server.py` is the smaller Codex-only compatibility server. The
optional `codex_job_daemon.py` dispatches queued jobs locally; it is an
execution component, not an upstream reasoning or wake-up service.

## Two-layer routing

The Supervisor controls two independent choices in `task_start`:

1. **Harness selection** chooses `codex`, `minimax`, or `agy`.
2. **Codex route selection** chooses `current`, `official`, `custom`, or
   `official_then_custom` when the harness is Codex. MiniMax and AGY accept
   `current` only.

`current` passes through the user's normal Codex CLI configuration. `official`
adds the process-local Codex override `-c model_provider="openai"`. `custom`
adds a generic process-local provider named `harbor_custom` with Responses API
wire mode. Set `HARBOR_CODEX_CUSTOM_BASE_URL` and
`HARBOR_CODEX_CUSTOM_API_KEY`; `HARBOR_CODEX_CUSTOM_MODEL` is optional. Harbor
passes the key only through a copied child environment using the provider's
`env_key`; it never writes the user's Codex config or places the key in argv.

For example, a Supervisor can request `route="official_then_custom"`. Harbor
tries the subscription-backed `official` route first, then tries the generic
`custom` route only when the first attempt clearly identifies OpenAI/Codex
subscription or usage quota exhaustion. Auth errors, ordinary rate limits,
429s, unknown errors, and task, code, prompt, parser, or test failures do not
trigger this fallback.
The job record stores only the requested/used route, sanitized classification,
and a bounded attempt summary.

## What Harbor deliberately does not do

- It is not an agent framework, model runtime, or the Supervisor's brain.
- It does not provide reasoning, planning, result acceptance, memory,
  personality, or generic orchestration.
- It does not own scheduled/event-driven wake-ups or the policy for retries.
- It does not claim universal harness coverage; integrations are explicit and
  limited to the verified code in this release.

## Example: ChatGPT as a Supervisor

ChatGPT is one concrete integration example, not a dependency or endorsement.
An MCP-capable ChatGPT workflow could look like this:

1. ChatGPT reasons about the request and plans a stage.
2. It calls Harbor's MCP `task_start` tool with the selected harness and
   project/workspace.
3. It saves the returned `job_id`.
4. ChatGPT's scheduled wake-up capability revisits the job later and calls
   `task_poll` when appropriate. Harbor itself does not schedule this wake-up.
5. ChatGPT inspects the result and decides whether the stage is accepted.
6. It dispatches the next task or follow-up stage through MCP.

The same execution layer can be used by an MCP-capable Supervisor/Agent,
including a custom agent or another compatible local workflow. Mentioning
ChatGPT here does not imply affiliation with or endorsement by OpenAI.

## Supported platform

**Windows-first / validated on Windows.** The current release candidate is
implemented and tested for Windows, including its process and PowerShell
launcher behavior. **macOS: Not yet supported / planned** and has not been
implemented or validated. No macOS setup instructions are provided yet.

## Supported harnesses

The current integrations in code are:

- **Codex**
- **MiniMax**
- **Antigravity / AGY**

Each CLI and its credentials/configuration must be installed and configured
locally. Use the MCP `harness_list` or `harness_status` tools to inspect actual
local availability and verified capabilities. These are current integrations,
not a claim of universal coverage or third-party affiliation. Harbor does not
bundle CLIs, models, credentials, or account access.

## Setup

The release candidate is for Windows PowerShell and is verified with Python
3.11 and 3.12.

1. Create and activate a virtual environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. Install the runtime dependency:

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. Put the CLIs you use on `PATH`, or set the matching `HARBOR_*` variables
   shown in [.env.example](.env.example) in the process that launches Harbor.
   Harbor does not load `.env.example` automatically. AGY's
   `--dangerously-skip-permissions` flag is never added by default; set
   `HARBOR_AGY_DANGEROUSLY_SKIP_PERMISSIONS=1` only after reviewing
   [SECURITY.md](SECURITY.md).

4. Start the complete MCP server:

   ```powershell
   python server_legacy.py
   ```

5. For optional background dispatch of queued jobs, run the daemon in a
   separate PowerShell window:

   ```powershell
   .\start-codex-job-daemon.ps1
   ```

   It uses `python` unless `HARBOR_PYTHON` selects another interpreter.

## Optional tunnel integration

Use a tunnel only if you choose to connect a remote MCP client to Harbor's
local server. Local stdio use and the unit suite do not require one. The
external tunnel client, transport, profile, and credentials are not bundled;
obtain and configure them separately according to the applicable provider's
instructions.

The included `start-tunnel.ps1` is convenience tooling only. It refuses to
start until its executable and profile settings are supplied through
environment variables, and it contains no bundled executable, profile, or
credentials.

## Development and testing

Run the isolated unit suite without starting Harbor, a tunnel, a daemon, or a
real coding-agent CLI:

```powershell
python -m unittest discover -s tests -v
```

The tests use temporary directories and mocked subprocesses for runtime state.
The GitHub Actions workflow runs the same suite on Windows with Python 3.11
and 3.12.

## Local data and security

Runtime records live under `.jobs/`; routing state and project aliases live
under `.control/`. Both are local-only state and are ignored by Git. Do not
commit `.env`, runtime directories, logs, CLI configuration files, tunnel
profiles, credentials, or diagnostic scripts. Harbor can invoke local agent
CLIs and perform filesystem/Git operations on behalf of its MCP client, so
expose it only to clients and networks you trust. Read
[SECURITY.md](SECURITY.md) before connecting Harbor to a real worktree.

Harbor is an independent third-party project. It is not affiliated with,
endorsed by, or sponsored by OpenAI, the Codex/MiniMax/Antigravity providers,
or any tunnel provider.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
