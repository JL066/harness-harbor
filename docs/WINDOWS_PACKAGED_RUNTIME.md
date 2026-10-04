# Windows packaged runtime

Harness Harbor v1.1.4 uses a Windows Launcher and a separate runtime sidecar.
The Launcher starts the sidecar through `run_runtime.py` and communicates
through the versioned JSON-lines bridge. The bundle contains both executables;
the standalone Launcher executable is not a complete distribution.

## Build from a Windows source checkout

Install the dependencies in `requirements-windows-build.txt`, then run
`python build_exe.py`. The build writes to a new `dist/windows-<UTC>-<id>`
directory, validates the bundle, and creates a local development ZIP and
checksums. Building does not publish or install the result.

## Runtime data

The bundle is immutable. Settings and credentials belong in the Windows user
profile, and jobs, control state, logs, and caches belong in per-user runtime
directories. Secrets are stored through Windows Credential Manager; the
`ChatGPT-Harbor` target and `chatgpt-harbor` tunnel profile are compatibility
identities. Existing legacy installations can supply their paths with the
`HARBOR_*` settings described in [Configuration](CONFIGURATION.md).

## Validation

Run the focused Windows tests under `tests/`, then test setup, start, stop,
restart, tray exit, tunnel recovery, and active-job exit behavior on an
isolated Windows account before distributing a build. The public CI workflow
runs focused tests and a local build without uploading artifacts.
