# Changelog

## [1.1.3] - 2026-10-01

### Added

- GPT-6 primary and worker defaults, with the accepted routing policy.
- An exit guard for active jobs and fail-closed recovery of stale running jobs.

### Fixed

- Replaced the duplicate bridge-owned MCP readiness process with tunnel-owned MCP process and readiness monitoring.
- Added bounded tunnel-only recovery for repeated local MCP transport failures, without stopping the daemon or worker trees.
- Guarded tunnel watchdog and recovery callbacks by generation to prevent stale events from changing a newer run.
- Kept overall Harbor status green when the runtime is running while MCP protocol health remains unverified.

All notable changes to the ChatGPT Harbor Launcher are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
for the launcher package (`launcher/__init__.py:__version__`).

---

## [Unreleased] — Public-Release Configuration Foundation

### Added — Batch 1: Configuration Resolution Layer

The launcher's old machine-specific paths are now routed
through a single resolution layer. Operators can point the launcher at
any install location without editing source.

- **`launcher/settings.py`** — new module implementing a 2-tier
  resolution chain: process environment (`HARBOR_*` variables) over
  built-in defaults. Exposes `get`, `all_settings`, `source_for`,
  `describe`, `config_path`, and `reload`. Forward-compatible with the
  planned external YAML/JSON config file tier.
- **`docs/CONFIGURATION.md`** — public guide listing every supported
  override, the resolution precedence, and usage examples.
- **`tests/unit_launcher/test_settings.py`** — 18 unit tests pinning
  down the public contract: defaults, env-var overrides, type coercion
  (Path / float), legacy alias, malformed-overwrite safety, and
  backward compatibility with `launcher.config`.
- **`CHANGELOG.md`** — this file.

### Changed

- **`launcher/config.py`** — every legacy constant is now derived from
  `launcher.settings` at import time. The public import surface
  (`from launcher.config import X`) is unchanged; every existing
  consumer continues to work without modification.
- **`README.md`** — added a Launcher section that links to
  `docs/CONFIGURATION.md` and surfaces the new `launcher/settings.py`
  module.
- **`run-launcher.ps1`** — header comment now documents the
  `HARBOR_*` env-var override mechanism and points to
  `docs/CONFIGURATION.md`.

### Stability

- The set of names exported by `launcher.config` is part of the
  launcher's public import surface. New names may be added; existing
  names must not be removed without a deprecation cycle.
- The set of `HARBOR_*` environment variable names and the public
  keys in `settings.DEFAULTS` are part of the public contract.
