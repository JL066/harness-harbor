# AGY macOS headless permission mapping

Audited against the installed AGY CLI 1.2.2 on 2026-09-12. This applies to the
macOS production source launcher; it does not update an already built DMG.

## Root causes

- Harbor passed the process cwd but did not register it as an AGY workspace or
  select an execution mode. Default review mode cannot approve headless writes.
- AGY can return exit code 0 and JSON `status: SUCCESS` with an empty `response`
  and nonempty `denied_actions`. This is CLI turn completion, not task success.
- Harbor treated the raw metadata JSON as a usable response and marked it completed.

## Mapping

On macOS, `workspace-write` adds these verified native arguments:

```text
--mode accept-edits --sandbox --add-dir <task cwd>
```

The process cwd is still set, and the prompt identifies that directory as the
root for relative task paths. `--add-dir` is necessary: an initial CLI audit
using cwd alone wrote the disposable output into AGY's own scratch directory.
Only the task cwd is added; Harbor does not edit AGY settings or permission lists.
The existing dangerous-permission opt-in remains off by default and was off in
all acceptance runs. Windows command mapping and Codex/MiniMax are unchanged.

`read-only` is still rejected. AGY's `--mode plan` supplies planning instructions;
it is not an enforceable read-only filesystem policy.

Permissions remain enforced by AGY. With the audited configuration, direct writes
inside the declared workspace succeed and a direct write to a sibling fixture is
denied. Existing user/admin permission rules still apply; Harbor does not erase
or override them. Shell commands can still be denied in headless mode: accepting
file edits is not an unrestricted command approval. AGY's terminal sandbox is
not whole-process containment; CLI-owned cache, conversation and artifact data
can be stored outside the workspace. No absolute whole-machine isolation claim
is made, and user-configured broader grants are not narrowed by these flags.

## Result classification

Permission denials fail with `failure_type: agy_permission_denied`, even with
exit code 0 or `status: SUCCESS`. Parsed denied actions and stdout/stderr diagnostics
are retained. Metadata-only or missing results, non-success JSON status and
nonzero self-exit cannot count as successful task completion. Historical job
records are left unchanged; replay validation uses separate output files.

## Reproduce acceptance

```sh
.venv/bin/python -m pytest tests/test_agy_workspace_permissions.py tests/test_agy_probe_consistency.py tests/test_codex_job_worker.py -q
.venv/bin/python macos/agy_smoke.py --live
```

The live check runs MCP `task_start`, the queue daemon and `task_poll` against a
new `build/agy-write-smoke-*` directory. It verifies the exact file content,
completed status, no denied actions, no dangerous bypass, expected workspace
outputs only, and an unchanged global AGY settings digest. Reports and original
job diagnostics are preserved inside the fixture.

This is a `macos` live check. Parser/command regression tests are `shared` and use
fixtures on Mac/Windows. Windows real-machine integration was not run for this
macOS-specific change.

References: [AGY modes](https://www.antigravity.google/docs/cli/modes/),
[permissions](https://www.antigravity.google/docs/cli/permissions/),
[terminal sandbox](https://www.antigravity.google/docs/cli/sandbox/).

## Acceptance record (2026-09-12)

- Installed CLI: 1.2.2; native inside-workspace direct write passed. A sibling
  direct-write probe returned `denied_actions: write_file`; no sibling file appeared.
- Harbor MCP live acceptance job: completed, exact
  `HARNESS_HARBOR_AGY_WRITE_OK` content, no denials, dangerous bypass off, global
  settings digest unchanged. The final collector also successfully replayed this
  captured response after its stricter validation was integrated.
- Original failed-write job replay: failed,
  `agy_permission_denied`, with denied actions preserved. Original state unchanged.
- Final full Python 3.12 regression: 354 passed and 78 subtests passed. Production
  Python 3.14 lacks Tk, so its full collection cannot run; focused worker tests
  pass there (55 tests, 13 subtests). Existing Pydantic warning remains unrelated.
- Two subsequent live reruns were blocked before dispatch by AGY's model-catalogue
  eligibility request returning network EOF. No probe checks were bypassed and no
  global network settings were changed. Live success is one observed run, not a
  claim of reliable upstream network availability.
