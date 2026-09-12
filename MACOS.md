# macOS native development build — 1.1.0

Status: Apple Silicon downloadable preview. Ad-hoc signed, not notarized.

[Download macOS 1.1.0 preview DMG and checksums](https://github.com/JL066/harness-harbor/releases/tag/macos-v1.1.0-preview.1).
Intel and clean-machine Gatekeeper acceptance remain unvalidated.

The native SwiftUI app has a menu-bar entry, dashboard, setup, settings, private credential files,
login item, logs and diagnostics. It owns a bundled `harbor-runtime` sidecar with
protocol v1. Closing a window leaves the menu app running; Quit stops Harbor.

## Build and launch

```sh
.venv/bin/python -m pip install -r macos/requirements-build.txt
sh macos/check.sh
# Full Xcode is required for XCTest (Command Line Tools alone lack XCTest):
swift test --package-path macos --scratch-path build/swift-tests
.venv/bin/python macos/build.py --dmg
# Optional native bridge integration against the bundle, in isolated build/ state:
sh macos/check.sh /absolute/path/to/harbor-runtime
```

Open `dist/macos-<timestamp>/Harness Harbor.app` in Finder. Build output is unique
per run and remains inside this checkout. The bundle includes Python and runtime
dependencies; users do not need a Python installation or source checkout.
Coding CLIs and tunnel-client are operator-installed dependencies and are not
bundled with accounts or credentials. The setup executable pickers/Auto Detect
handle Finder's minimal PATH.

SwiftUI requires macOS 13+. The actual application minimum is computed from the
bundled Mach-O dependencies. The accepted Apple Silicon development artifact uses
Python 3.12 and declares macOS 13.0. An earlier Homebrew Python 3.14 build required
macOS 26; use a compatible Python distribution, rather than merely lowering
Info.plist. Declared minimum OS is not clean-machine compatibility evidence.
CI uses Python 3.12 on macos-14 for its separate artifact.

First run: verify runtime → detect CLI → configure tunnel URL/ID/key → choose Codex
route → run configuration checks → Finish/Start. A connection test submits no
coding job and does not claim remote connectivity. Keys go into private local files, never
settings JSON. Runtime start health and an external MCP round trip are separate
checks. Missing optional AGY/MiniMax remain unavailable without blocking Codex.

## Runtime and isolated acceptance

```sh
.venv/bin/python -m harbor_runtime version
.venv/bin/python -m harbor_runtime doctor
.venv/bin/python -m harbor_runtime bridge
.venv/bin/python -m pytest tests/test_macos_runtime.py -q
.venv/bin/python macos/smoke.py /absolute/path/to/harbor-runtime
# Explicit real Codex job in a newly created build/ worktree:
.venv/bin/python macos/smoke.py /absolute/path/to/harbor-runtime --live-codex
```

`mcp` uses stdio; `daemon` serves the same queue. The internal `worker` subcommand
is only used by the frozen daemon and is not an IPC method. The bridge supports
`hello`, status, start/stop/restart, telemetry, configuration test, bounded logs,
diagnostics and shutdown. Requests require hello and protocol v1. Unsupported
methods/parameters fail closed. Diagnostics and runtime logs are redacted.

State is under Application Support, logs under Library/Logs, and credentials
under `~/Library/Application Support/Harness Harbor/credentials/`. See
[configuration](docs/CONFIGURATION.md) for all path/CLI overrides. App resources
are immutable. Runtime lock/manifest and worker process registrations enable
verified recovery. A PID/queue mismatch or unverifiable orphan group fails closed
and requires operator investigation; no process-name based cleanup is performed.

The smoke script preserves its fixture and `report.json` under `build/` for review.
It starts actual MCP/daemon, uses a minimal PATH, and checks restart and shutdown.
Live mode creates a Git worktree, submits through `task_start`, independently
checks the code, and verifies `task_poll` after restart. It does not read or alter
existing Harbor jobs, tunnel profiles, or global CLI configuration.

## Release operator steps

The default build is ad-hoc signed, with no notarization or automatic publication.
After installing an authorized Developer ID signing identity in the release
environment, build with `macos/build.py --signing-identity 'Developer ID Application: …'`.
PyInstaller signs nested binaries; the outer app enables Hardened Runtime.
`macos/notarize.py /absolute/path/to/Harness\ Harbor.app --identity 'Developer ID Application: …' --notary-profile <existing-profile>`
submits the signed app, checks Accepted, staples, assesses Gatekeeper, creates and
notarizes/staples a DMG. It uses an existing credential profile and does not install
certificates, change keychains, or publish a GitHub Release.

Before stable public distribution: run Windows/macOS CI, real credential-file save/read/remove,
login enable/disable, Secure Tunnel → MCP → Codex end-to-end, Developer ID and
notarization, and a clean-machine drag-to-Applications/Gatekeeper check.
Do not label macOS Supported until all PRD gates pass. See
[acceptance record](docs/MACOS_ACCEPTANCE.md).

## Legacy source checkout

The previous source launcher remains available. The full MCP implementation is `server_legacy.py`.

```sh
./start-harbor.sh mcp
./start-harbor.sh daemon
./start-harbor.sh tunnel
```

Run each command in its own terminal. These are foreground processes; keep them running.
The MCP listener defaults to `http://127.0.0.1:8765/mcp` and binds loopback only.
The launcher loads the private `.env` if present and uses `.venv/bin/python`.
Use absolute CLI executable overrides and include the Codex Node runtime on PATH.
Set `NO_PROXY=127.0.0.1,localhost` for local HTTP clients when a proxy is inherited.

The private tunnel profile is `.control/tunnel/harness_harbor_mac.yaml`.
Keep credentials, tunnel IDs and runtime state under ignored `.control/` or `.env`.
The launcher clears inherited tunnel identity/key overrides so the private profile
is authoritative. Harbor uses its own tunnel identity and the standard `main`
channel, with the full MCP server launched over stdio.
Never point Harbor at Coot's `main` channel on a shared tunnel.
ChatGPT's tunnel selector does not expose a custom channel field; a separate tunnel
identity is needed unless the client explicitly supports selecting Harbor's channel.
Local readiness alone does not prove ChatGPT is connected.

Codex CLI 0.147.0 rejected the current global `gpt-6-astra` model. The read-only
smoke succeeded with a task-level `model="gpt-5.5"` override; global config is unchanged.
AGY supports the existing `workspace-write` mapping only. Its smoke prompt prohibits
all tool use and file changes; dangerous permission bypass stays disabled.
MiniMax remains unavailable and is not installed for this stage.

Validation:

```sh
. .venv/bin/activate
python3 -m unittest discover -s tests -v
git diff --check
```

The system Python lacks `mcp`; use the existing project virtual environment.
Local smoke evidence is in `.control/mac-smoke.json` and `.jobs/`.
