# Cross-platform convergence acceptance

Version: 1.1.0. Development environment: Apple Silicon macOS, Python 3.12.14.
This public summary excludes private host identifiers, operation logs and internal
commit history. It records local evidence, not a completed cross-platform release.

## Implemented scope

- Shared queue defaults, terminal-first polling and fingerprint validation.
- Authoritative Python settings acceptance consumed by both shells, preserving
  unknown platform extensions without storing credentials in settings.
- Shared monitoring snapshots with independent fast activity and slow capabilities.
- Platform process ownership, durable worker finalization and conservative recovery.
- Shared lifecycle partial-start repair and loopback-only health probes.
- Versioned artifacts/checksums and explicit platform test lanes.

## Local results

| Check | Result |
| --- | --- |
| `pytest -q -m shared` | 242 passed, 78 subtests |
| `pytest -q -m macos` | 6 passed |
| `pytest -q -m windows_ci` on Mac | 126 passed; portable mocks only |
| Swift build and `sh macos/check.sh` | Passed |
| Swift XCTest | Unavailable on this host; retained in macOS CI |
| Mac App/DMG build | Passed; ad-hoc signed only |
| Bundled runtime isolated smoke | Hello, MCP/daemon start, restart and shutdown passed |

All 374 collected Python tests passed locally. An existing Pydantic dependency
warning concerned an unresolved `lifespan` forward reference. Tests used isolated
fixtures. No current live-credential acceptance or native Windows results are claimed.

## Open gates

- Windows CI: shared contracts on Python 3.11/3.12, actual Job Objects and descendant
  cleanup, launcher tests and frozen ZIP build. Not executed on this macOS host.
- Windows manual: GUI/tray, Credential Manager, Task Scheduler coexistence, installed
  tunnel/authenticated harness and historical queue compatibility.
- Mac installation: Finder launch of this build, real Keychain/tunnel/harness,
  clean-machine compatibility, signing and notarization remain unverified.
- Legacy Windows scheduled supervisors remain available. Detached launches receive
  saved child settings; registered scheduled tasks retain their established environment.
  This is not an automatic migration to shared bridge ownership.
- Old Windows leases without verifiable ownership remain conservatively retained and
  may need manual inspection. Do not start competing supervisors on the same queue.
- The Windows ZIP packages the existing launcher. Standalone shared Core installation
  and native parity must be verified before calling it release-ready.

Inherited model/routing/timeout policy remains separate from convergence changes.
See [test matrix](TEST_MATRIX.md) and [artifact contract](RELEASE.md).
