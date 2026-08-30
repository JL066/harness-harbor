# ChatGPT Harbor

ChatGPT Harbor is a local MCP control plane compatible with Codex, MiniMax,
and Antigravity CLIs for running and supervising jobs. Runtime records live
under `.jobs/`; routing
state and project aliases live under `.control/`. Both are local-only state.

This release candidate is Windows-oriented. It preserves the tested
control plane, worker, daemon, and tests while removing machine-specific paths
and runtime artifacts.

## Setup

1. Install Python 3.11 or newer and create a virtual environment.
2. Run `python -m pip install -r requirements.txt`.
3. Put the CLIs you use on `PATH`, or set the matching `HARBOR_*` variables
   shown in `.env.example` in the process that launches Harbor.
4. Run `python server_legacy.py` for the full MCP server. `server.py` is the
   smaller Codex-only compatibility server.
5. For background scheduling, run `start-codex-job-daemon.ps1` separately. It
   uses `python` unless `HARBOR_PYTHON` selects another interpreter.

### ChatGPT tunnel requirement

Connecting ChatGPT to Harbor's local MCP server requires OpenAI's official
`tunnel-client` / official tunnel transport. The tunnel client is an external
prerequisite and is not bundled with this repository. Users must obtain and
configure their own official tunnel setup, profile, and credentials according
to OpenAI's applicable instructions.

The included tunnel supervisor is optional convenience tooling and contains no
bundled executable, profile, or credentials. `start-tunnel.ps1` refuses to
start until its executable and profile settings are provided through
environment variables. This project is compatible with OpenAI's tunnel
transport but is not endorsed by, affiliated with, or partnered with OpenAI.

### Third-party integrations

Harbor supports or integrates with external Codex, MiniMax, and
Antigravity/agy tooling. These integrations are optional and require users to
install the relevant tools and supply their own configuration and credentials.
This project is not endorsed by, affiliated with, certified by, or partnered
with those third parties.

## Development

Run the isolated unit suite without starting Harbor, a tunnel, or the daemon:

```powershell
python -m unittest discover -s tests -v
```

The tests use temporary directories and mocked subprocesses for runtime state.

## Security and local data

Do not commit `.env`, `.jobs/`, `.control/`, logs, CLI configuration files,
tunnel profiles, credentials, or diagnostic scripts. Harbor can invoke local
agent CLIs and perform filesystem/Git operations, so expose its MCP transport
only to clients and networks you trust.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
