# macOS implementation and acceptance

Harness Harbor v1.1.2 is published for Apple Silicon (arm64) using SwiftUI
and a bundled Python runtime. The published DMG is ad-hoc signed and not
notarized; Intel validation remains pending.

Implemented: native dashboard and menu, setup/settings, private local credential
storage, external state directories, process ownership and recovery, queue
monitoring, terminal-first polling, versioned App/DMG packaging, and app/runtime
version visibility in the Dashboard.

Historical development checks covered isolated queue sessions, terminal polling,
CLI execution, native UI and bundle startup. Those checks describe earlier builds;
they do not establish acceptance of every subsequent change or other platforms.
Host identities, live job identifiers, queue fingerprints and local operation logs
are omitted from this public summary.

For the latest automated results and open gates, see
[convergence acceptance](convergence/EXECUTION.md) and
[test matrix](convergence/TEST_MATRIX.md). Build instructions are in [MACOS.md](../MACOS.md).

---

## v1.1.2 published release

The public v1.1.2 macOS release was published from source commit
`474fd16d7710995b523356cd9ec64f03dd1c1e08`.

Highlights:

- Dashboard version footer shows app version from `CFBundleShortVersionString` and runtime version from the bridge protocol.
- GPT-6 routing defaults use `gpt-6-luna` / `max` as the normal worker path; supervising callers may explicitly select `gpt-6-sol` for unusually difficult work or `gpt-6-astra` for exceptional supervisor-level reasoning. Harbor itself does not auto-escalate.
- Existing private credential-file handling is retained; launch-time Keychain credential reads remain avoided and route-specific secrets are loaded lazily.
- App, runtime, launcher metadata, packaging paths, and native bridge compatibility checks are synchronized on v1.1.2.

Validation recorded for the public source:

- Python 3.12.14
- 389 tests passed, 0 failed; 80 subtests passed
- `macos/check.sh`: PASS
- `git diff --check`: PASS
- Sanitization: PASS

Published assets:

- `Harness-Harbor-v1.1.2-macos-arm64.dmg`
- `SHA256SUMS.txt`
- DMG SHA-256: `318ea0e85286e87cbac4fac1bee2a4bdcd4e82463d3165fef98a42844067163c`
