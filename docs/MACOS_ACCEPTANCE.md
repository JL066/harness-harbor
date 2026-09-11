# macOS implementation and acceptance

Version 1.1.0 provides an Apple Silicon development implementation using SwiftUI
and a bundled Python runtime. It is not yet a notarized public release.

Implemented: native dashboard and menu, setup/settings, platform credential storage,
external state directories, process ownership and recovery, queue monitoring,
terminal-first polling, and versioned App/DMG packaging.

Historical development checks covered isolated queue sessions, terminal polling,
CLI execution, native UI and bundle startup. Those checks describe earlier builds;
they do not establish acceptance of every subsequent change or other platforms.
Host identities, live job identifiers, queue fingerprints and local operation logs
are omitted from this public summary.

For the latest automated results and open gates, see
[convergence acceptance](convergence/EXECUTION.md) and
[test matrix](convergence/TEST_MATRIX.md). Build instructions are in [MACOS.md](../MACOS.md).
