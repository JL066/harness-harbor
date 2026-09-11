# macOS async queue repair

## Cause

Two backends connected through the same tunnel used different queue stores. Tasks
could start on one backend and later be unknown to the other. Exited workers could
also leave nonterminal records counted indefinitely after daemon restart.

## Repair

- macOS direct entrypoints and App runtime share the Application Support queue default.
  Explicit queue overrides remain supported; Windows retains the project default.
- Start/poll/cancel carry queue identity for diagnosis and validate fingerprints.
- Recovery requires verifiable ownership and no surviving owned descendants. Unknown
  or live owners are preserved; interrupted tasks fail without replaying prompts.
  Original state is retained in `status.before-recovery.json`.
- Dashboard activity reads current running IDs independently of slow capability probes;
  disconnected snapshots clear obsolete activity.
- Dashboard startup and reopen use a reusable native window independently of startup
  preferences. Closing its window preserves background services.

## Verification and boundaries

Regression fixtures cover cross-session start/poll/cancel, defaults, worker recovery,
terminal state and monitoring. Earlier development also exercised live harnesses;
private tunnel/job identifiers and machine operation records are not distributed.
See [current acceptance](convergence/EXECUTION.md) for current results and open gates.

Historical queues are not automatically merged or migrated. Preserve their contents
and inspect them through the installation that owns them. Do not run competing
supervisors against the same queue or infer safe recovery from PID absence alone.
