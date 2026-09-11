# Test execution matrix

Development host: macOS. A successful mock test here is not Windows CI or Windows
real-machine acceptance. Classification is enforced by `tests/conftest.py` and
the CI jobs select lanes explicitly. New portable tests default to `shared`;
platform or manual tests must be assigned their appropriate lane during review.

| Lane | What it covers | Where required | Current execution |
| --- | --- | --- | --- |
| `shared` | Core start/poll/cancel, fingerprints, worker finalization, settings contract, snapshots, ownership contract, protocol and release naming | Both macOS and Windows CI, Python 3.11/3.12 | Run on local Mac; Windows CI pending user synchronization |
| `macos` | POSIX runtime recovery/process groups, Mac shell entrypoint, real stdio sessions using fixture CLIs | Local Mac and macOS CI | Run locally |
| `windows_ci` | `tests/unit_launcher`: Windows launcher/config/tray/autostart/credentials/tunnel logic with injected fixtures; no personal credentials | Windows hosted CI | Some portable mocks also run on Mac as supplementary evidence; native CI pending |
| `windows_manual` | Real GUI/tray, Credential Manager, Task Scheduler coexistence, installed tunnel, authenticated Codex and historical queue | User's Windows development machine | Not run: development environment is macOS |

`tests/test_shared_process.py` executes the real platform adapter using only test
children. This is `shared`: POSIX groups on Mac and Job Objects on Windows CI.
It does not require an installed or authenticated harness. Mac-only tests embedded
in mixed files are classified individually; the AGY 30-second timeout contract is
shared even though its historical filename contains `mac`.

## Local Mac commands

```sh
.venv/bin/python -m pytest -q -m shared --junitxml=build/shared-results.xml
.venv/bin/python -m pytest -q -m macos --junitxml=build/macos-results.xml
swift build --package-path macos --scratch-path build/swift-tests
sh macos/check.sh
```

`swift test --package-path macos --scratch-path build/swift-tests` additionally runs
XCTest where available. This development host currently lacks the XCTest module;
equivalent configuration protocol/round-trip assertions also run in `macos/check.sh`.
The macOS CI build retains the XCTest command.

## Windows CI after GitHub synchronization

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-launcher.txt
python -m pytest -q -m shared --junitxml=build/shared-results.xml
python -m pytest -q -m windows_ci --junitxml=build/windows-ci-results.xml
python build_exe.py --release
```

The existing unit workflow runs both platforms. The Windows artifact workflow
builds a versioned onedir ZIP. Workflow configuration is not a completed CI run.

## Windows manual acceptance after user synchronization

Use an isolated checkout and fixture work directory. Preserve the existing `.jobs`
and settings before testing; do not migrate, rename, purge or rewrite historical jobs.

1. Run the two Windows CI test commands above and retain their XML results.
2. Launch `python run_launcher.py`. Verify setup, save/reopen, tray, start/stop/restart,
   and the existing Task Scheduler fallback. Never run two supervisors against the
   same queue. The new bridge is not a replacement for the legacy launcher until
   its native acceptance has passed.
3. With test credentials in Windows Credential Manager, verify required-key checks,
   replacement, failure rollback and no plaintext settings/profile/log leakage.
4. Configure a test tunnel/profile and authenticated Codex CLI. Submit a bounded
   read-only task against the fixture workspace; record job ID, terminal poll,
   queued cancellation, queue fingerprint and runtime restart behavior.
5. Verify running → idle, disconnect → cleared IDs, reconnect and delayed capability
   responses. Confirm an unrelated process stays alive during owned-tree cleanup.
6. Read historical `.jobs` through the existing installation and confirm old task IDs
   remain visible. Do not run recovery or synthetic writes against historical data.
7. Run the extracted Windows distribution and verify it launches and reaches the
   configured runtime. A portable launcher ZIP alone does not prove a standalone
   bundled Core/runtime installation.

There are currently no automated credential-bearing `windows_manual` tests. Future
ones must require both the marker and `--windows-manual`; ordinary CI will skip them.

## Mac real-installation acceptance

The fixture suite does not prove Finder `.app` launch, real Keychain credentials,
installed tunnel health or authenticated Codex execution. These require an isolated
test profile and explicit real-installation execution. Existing opt-in scripts are
`macos/smoke.py` and `macos/queue_smoke.py`; their live flags are never run by ordinary
unit CI. Record packaging, signing/notarization and live checks separately.
