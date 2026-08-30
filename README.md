# ChatGPT Harbor

ChatGPT Harbor is a local MCP control plane that lets you run and supervise coding-agent CLI jobs on your own machine directly from ChatGPT conversations.

Harbor connects ChatGPT to local Codex, MiniMax, and Antigravity/agy CLIs through MCP. From the chat interface, you can submit jobs, inspect status, poll results, cancel work, and coordinate multiple local agents without switching back and forth between terminals.

Combined with background scheduling and ChatGPT scheduled tasks, Harbor can also support automated workflows such as: wake up on a schedule, inspect the previous job, decide the next step, submit new work, and continue later without manual terminal supervision.

```text
ChatGPT chat
    ↓
OpenAI official tunnel transport
    ↓
ChatGPT Harbor (local MCP control plane)
    ↓
Codex CLI / MiniMax CLI / Antigravity (agy) CLI
```

Harbor is currently Windows-oriented. Runtime job records live under `.jobs/`; routing state and project aliases live under `.control/`. Both are local-only state and should not be committed.

## What Harbor does

- Control local coding-agent CLI jobs from ChatGPT conversations through MCP.
- Submit jobs to Codex, MiniMax, or Antigravity/agy and run them in the background.
- Poll job status and retrieve results without keeping an interactive terminal session open.
- Cancel jobs and coordinate concurrent work across supported harnesses.
- Keep runtime state local to the machine running Harbor.
- Combine with scheduled ChatGPT tasks for recurring or multi-stage automated workflows.

## Requirements

Harbor does **not** bundle Codex, MiniMax, Antigravity, or the OpenAI tunnel client.

Install only the agent CLIs you plan to use:

- **Codex CLI** for Codex jobs.
- **MiniMax CLI** for MiniMax jobs.
- **Antigravity/agy CLI** for Antigravity jobs.

You do not need to install all three. Harbor can use whichever supported CLIs are available on your machine.

Each CLI must be installed and configured independently with its own account, credentials, provider settings, and other required local configuration. Put the executable on `PATH`, or point Harbor to it with the corresponding `HARBOR_*` environment variable shown in `.env.example`.

To connect ChatGPT to Harbor, you also need OpenAI's official `tunnel-client` / official tunnel transport. Harbor does not include a tunnel executable, profile, or credentials.

## Setup

1. Install Python 3.11 or newer and create a virtual environment.
2. Run `python -m pip install -r requirements.txt`.
3. Install and configure at least one supported agent CLI: Codex, MiniMax, or Antigravity/agy.
4. Put the CLIs you use on `PATH`, or set the matching `HARBOR_*` variables shown in `.env.example` in the process that launches Harbor.
5. Run `python server_legacy.py` for the full MCP server. `server.py` is the smaller Codex-only compatibility server.
6. For background job scheduling, run `start-codex-job-daemon.ps1` separately. It uses `python` unless `HARBOR_PYTHON` selects another interpreter.
7. Configure OpenAI's official tunnel transport so ChatGPT can reach the local Harbor MCP server.

### ChatGPT tunnel requirement

Connecting ChatGPT to Harbor's local MCP server requires OpenAI's official `tunnel-client` / official tunnel transport. The tunnel client is an external prerequisite and is not bundled with this repository. Users must obtain and configure their own official tunnel setup, profile, and credentials according to OpenAI's applicable instructions.

The included tunnel supervisor is optional convenience tooling and contains no bundled executable, profile, or credentials. `start-tunnel.ps1` refuses to start until its executable and profile settings are provided through environment variables.

This project is compatible with OpenAI's tunnel transport but is not endorsed by, affiliated with, or partnered with OpenAI.

### Agent CLI integrations

Harbor integrates with external Codex, MiniMax, and Antigravity/agy command-line tools. These CLIs are separate software and must be installed and configured independently.

Harbor invokes the CLI versions of these tools; it does not depend on or control their desktop applications.

These integrations are optional. Install only the CLIs you intend to use and supply their required local configuration and credentials yourself. This project is not endorsed by, affiliated with, certified by, or partnered with those third parties.

## Automation

Harbor's job daemon can keep queued work running independently of an interactive ChatGPT conversation. When Harbor is combined with scheduled ChatGPT tasks, a conversation can periodically reconnect to the control plane, inspect job state and results, and decide what to do next.

A typical multi-stage workflow can look like this:

```text
Scheduled ChatGPT task
    ↓
Check Harbor job status / result
    ↓
Review or decide next action
    ↓
Submit the next local CLI job
    ↓
Harbor daemon runs it in the background
    ↓
Next scheduled check continues the workflow
```

This makes Harbor useful not only for one-off remote control from ChatGPT, but also for longer-running supervised development workflows where work can continue across multiple scheduled check-ins.

## Development

Run the isolated unit suite without starting Harbor, a tunnel, or the daemon:

```powershell
python -m unittest discover -s tests -v
```

The tests use temporary directories and mocked subprocesses for runtime state.

## Security and local data

Do not commit `.env`, `.jobs/`, `.control/`, logs, CLI configuration files, tunnel profiles, credentials, or diagnostic scripts. Harbor can invoke local agent CLIs and perform filesystem/Git operations, so expose its MCP transport only to clients and networks you trust.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
