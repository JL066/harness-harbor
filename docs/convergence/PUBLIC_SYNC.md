# Public source synchronization audit

## Scope

The public checkout receives a tracked-source snapshot of version 1.1.0, preserving
its existing Git history. Development commit history and machine author metadata
are not imported. Publication/push is a separate operation and was not performed.

## Sanitization

- Replaced three internal acceptance records with public technical summaries:
  `docs/MACOS_ACCEPTANCE.md`, `docs/MACOS_QUEUE_FIX.md` and
  `docs/convergence/EXECUTION.md`.
- Omitted live tunnel/job IDs, queue fingerprints, host process details, local
  operation records and internal development commit references.
- Included only tracked release source, tests and resources. No credentials, local
  configuration, queues, logs, virtual environments or built artifacts were copied.
- Reviewed key-pattern matches: remaining mock secrets belong to credential-store
  and redaction tests. Public repository links, reserved example paths/addresses,
  and public image provenance remain intact.
- Scanned tracked text and binary bytes for known private identities, live ID patterns
  and common credential signatures. No such residual matches were found. This is a
  bounded pattern/manual audit, not proof against every possible secret format.

## Integrity and validation

Source, tests and resources match the development snapshot except a trailing blank
line removed from the Tkinter packaging hook. Other changes are the three public
summaries and a README hard-line-break correction that preserves rendering.
Both whitespace corrections address diff-check failures without changing behavior.

Public-checkout regression: `python -m pytest -q tests` passed all 374 tests and
78 subtests on macOS. One existing Pydantic forward-reference warning remains.
Windows-native execution was not performed on this macOS host.
Platform acceptance limitations remain documented in TEST_MATRIX.md.
