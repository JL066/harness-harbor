# Security

Harness Harbor is a local harness broker, not a security boundary. It can
launch installed coding-agent CLIs, read and write authorized worktrees, and
run filesystem and Git operations on behalf of its MCP client.

## Trust boundary

Treat the Harbor process, its MCP client, and every configured third-party CLI
as trusted with access to the worktrees and local data that they can reach.
Run Harbor on loopback or through a deliberately configured local tunnel only;
do not expose the MCP server directly to a LAN or the public internet.

Job records under `.jobs/` can contain prompts, outputs, paths, subprocess
metadata, and error text. `.control/` can contain project aliases, routing
state, and configuration backups. Keep both directories private, preserve the
provided ignore rules, and review their contents before sharing diagnostics.

## Credentials and dangerous AGY mode

Do not commit `.env`, CLI configuration, tunnel profiles, tokens, or API keys.
Prefer each third-party CLI's normal local credential store and redact secrets
from prompts, logs, job records, and bug reports.

Harbor does not add AGY's `--dangerously-skip-permissions` flag by default.
Enable it only by explicitly setting
`HARBOR_AGY_DANGEROUSLY_SKIP_PERMISSIONS=1`; this can let AGY bypass its normal
permission checks and act on the selected worktree, so use it only in a
dedicated, trusted environment after reviewing the prompt and target path.

## Reporting

Report suspected Harbor vulnerabilities privately to the repository maintainers
through the repository's private security channel when available. Include the
commit/version, a minimal reproduction, affected scope, and redacted logs; do
not publish credentials or sensitive worktree data. Vulnerabilities in the
installed Codex, MiniMax, AGY, tunnel, or other third-party CLIs should also be
reported to their respective maintainers. Harbor cannot guarantee the security
of those tools or their credential stores.
