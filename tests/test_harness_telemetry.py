from datetime import datetime, timedelta, timezone
import json
import subprocess
import tempfile
import textwrap
from pathlib import Path
from unittest import mock

import control_plane
from harness_process_adapter import WindowsProcessActivityAdapter, default_process_activity_adapter
from harness_telemetry import HarnessTelemetryProvider
from launcher.harnesses import agy_models, list_harnesses


class FakeProcessAdapter:
    def __init__(self, records):
        self.records = records
        self.calls = 0

    def observe(self, harnesses):
        self.calls += 1
        return self.records


def _statuses(name):
    return {
        "codex": {"available": True, "executable_exists": True, "version": "1.2.3"},
        "minimax": {"available": True, "executable_exists": True, "version": "0.2.0"},
        "agy": {"available": True, "executable_exists": True, "version": "1.1.0", "models": ["dynamic-model"]},
    }[name]


def _quota(reset=1_800_000_000):
    return {
        "state": "available",
        "source": "Codex app-server account/rateLimits/read",
        "windows": [{"label": "5h", "bucket": "codex", "used_percent": 25, "window_minutes": 300, "resets_at": reset}],
        "quota_scope": "codex_account",
    }


def test_snapshot_carries_authoritative_source_freshness_and_stale_fallback():
    current = [datetime(2026, 9, 4, tzinfo=timezone.utc)]
    replies = [_quota(), {"state": "unavailable", "source": "Codex app-server account/rateLimits/read", "error": "ignored diagnostic"}]
    provider = HarnessTelemetryProvider(
        status_provider=_statuses,
        codex_quota_provider=lambda: replies.pop(0),
        job_activity_provider=lambda: {},
        process_adapter=FakeProcessAdapter({}),
        refresh_seconds=15,
        clock=lambda: current[0],
    )
    first = provider.snapshot()
    codex = first["harnesses"][0]
    assert codex["source"] == "Harbor Core harness registry"
    assert codex["quota"]["freshness"] == "fresh"
    assert codex["quota"]["stale"] is False
    assert codex["quota"]["windows"][0]["resets_at"].endswith("Z")
    assert codex["quota"]["windows"][0]["resets_in_seconds"] is not None

    current[0] += timedelta(seconds=16)
    stale = provider.snapshot()["harnesses"][0]["quota"]
    assert stale["freshness"] == "stale"
    assert stale["stale"] is True
    assert stale["windows"][0]["used_percent"] == 25
    assert stale["error"] == "authoritative quota is unavailable"


def test_unavailable_quotas_are_truthful_and_minimax_is_not_attributed():
    provider = HarnessTelemetryProvider(
        status_provider=_statuses,
        codex_quota_provider=lambda: {"state": "unavailable", "source": "Codex app-server", "error": "diagnostic"},
        job_activity_provider=lambda: {},
        process_adapter=FakeProcessAdapter({}),
    )
    by_name = {item["name"]: item for item in provider.snapshot()["harnesses"]}
    assert by_name["codex"]["quota"]["state"] == "unavailable"
    assert by_name["codex"]["quota"]["windows"] == []
    minimax = by_name["minimax"]["quota"]
    assert minimax["state"] == "unavailable"
    assert minimax["account_binding"] == "unavailable"
    assert minimax["quota_scope"] is None
    assert minimax["error"] == "authoritative quota is unavailable"
    agy = by_name["agy"]["quota"]
    assert agy["state"] == "unavailable"
    assert agy["windows"] == []
    assert agy["error"] == "authoritative quota is unavailable"


def test_snapshot_does_not_export_provider_diagnostics_or_secrets():
    secret = "Bearer secret-value-must-not-escape"

    def blocked_status(name):
        record = _statuses(name).copy()
        record["blocker"] = secret
        return record

    provider = HarnessTelemetryProvider(
        status_provider=blocked_status,
        codex_quota_provider=lambda: {"state": "unavailable", "source": "Codex app-server", "error": secret},
        job_activity_provider=lambda: {},
        process_adapter=FakeProcessAdapter({}),
    )
    assert secret not in repr(provider.snapshot())


def test_activity_uses_process_adapter_boundary_not_platform_logic():
    adapter = FakeProcessAdapter({
        "codex": {"process_count": 1, "source": "test process adapter", "error": None},
        "minimax": {"process_count": 0, "source": "test process adapter", "error": None},
        "agy": {"process_count": 0, "source": "test process adapter", "error": None},
    })
    provider = HarnessTelemetryProvider(
        status_provider=_statuses,
        codex_quota_provider=_quota,
        job_activity_provider=lambda: {"codex": {"running": 0, "queued": 0, "source": "test job queue", "error": None}},
        process_adapter=adapter,
    )
    activity = provider.snapshot()["harnesses"][0]["activity"]
    assert adapter.calls == 1
    assert activity == {
        "state": "active", "running_jobs": 0, "queued_jobs": 0,
        "process_count": 1, "source": "test job queue + test process adapter", "error": None,
    }


def test_codex_normalizer_emits_only_backend_percentages():
    normal = control_plane._normalise_codex_rate_limits({
        "rateLimits": {"limitId": "codex", "primary": {"usedPercent": 33.4, "windowDurationMins": 300, "resetsAt": 1_800_000_000}, "secondary": {"windowDurationMins": 10080}},
    })
    assert normal["state"] == "available"
    assert normal["windows"] == [{"label": "5h", "bucket": "codex", "used_percent": 33.4, "window_minutes": 300, "resets_at": 1_800_000_000}]
    unavailable = control_plane._normalise_codex_rate_limits({"rateLimits": {"primary": {"remainingPercent": 90}}})
    assert unavailable["state"] == "unavailable"
    by_id = control_plane._normalise_codex_rate_limits({
        "rateLimitsByLimitId": {"codex-pro": {"primary": {"usedPercent": 10, "windowDurationMins": 60}}}
    })
    assert len(by_id["windows"]) == 1
    assert by_id["windows"][0]["bucket"] == "codex-pro"


def test_codex_rate_limits_snapshot_uses_safe_runner_with_json_rpc_input(tmp_path):
    fake_codex = tmp_path / "codex.exe"
    fake_codex.write_bytes(b"")
    response = json.dumps({
        "jsonrpc": "2.0", "id": 2,
        "result": {"rateLimits": {"primary": {"usedPercent": 12, "windowDurationMins": 300}}},
    })
    completed = subprocess.CompletedProcess([], 0, stdout=f"noise\n{response}\n", stderr="private")
    with mock.patch.object(control_plane, "CODEX_EXE", fake_codex), mock.patch.object(
        control_plane, "run_safe_subprocess", return_value=completed
    ) as runner:
        snapshot = control_plane._codex_rate_limits_snapshot()

    assert snapshot["state"] == "available"
    assert snapshot["windows"][0]["used_percent"] == 12
    call = runner.call_args
    assert call.args[0] == [str(fake_codex), "app-server", "--listen", "stdio://"]
    assert call.kwargs["timeout"] == 6
    messages = [json.loads(line) for line in call.kwargs["input"].decode("utf-8").splitlines()]
    assert messages[-1]["id"] == 2
    assert messages[-1]["method"] == "account/rateLimits/read"


def test_windows_process_adapter_uses_injected_safe_runner_and_keeps_commands_private():
    secret = "Bearer command-line-secret"
    rows = json.dumps([
        {"Name": "codex.exe", "CommandLine": "codex exec task"},
        {"Name": "node.exe", "CommandLine": f"node @minimax-ai\\code\\cli.js {secret}"},
    ])
    runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout=rows, stderr=secret))
    adapter = WindowsProcessActivityAdapter(runner)
    with mock.patch("harness_process_adapter.sys.platform", "win32"):
        result = adapter.observe(("codex", "minimax", "agy"))

    runner.assert_called_once()
    assert runner.call_args.kwargs == {"timeout": 5}
    assert result["codex"]["process_count"] == 1
    assert result["minimax"]["process_count"] == 1
    assert secret not in repr(result)


def test_windows_process_adapter_runner_failure_is_fixed_and_secret_free():
    secret = "credential-must-not-escape"
    adapter = WindowsProcessActivityAdapter(mock.Mock(side_effect=OSError(secret)))
    with mock.patch("harness_process_adapter.sys.platform", "win32"):
        result = adapter.observe(("codex",))
    assert result["codex"]["error"] == "Windows process discovery failed"
    assert secret not in repr(result)


def test_default_windows_adapter_receives_the_injected_runner():
    runner = mock.Mock()
    with mock.patch("harness_process_adapter.sys.platform", "win32"):
        adapter = default_process_activity_adapter(runner)
    assert isinstance(adapter, WindowsProcessActivityAdapter)
    assert adapter._runner is runner


def test_launcher_uses_unified_snapshot_and_legacy_provider_is_compatible():
    snapshot = HarnessTelemetryProvider(
        status_provider=_statuses,
        codex_quota_provider=_quota,
        job_activity_provider=lambda: {"agy": {"running": 1, "queued": 0, "source": "queue", "error": None}},
        process_adapter=FakeProcessAdapter({}),
    ).snapshot()
    rows = list_harnesses(telemetry_snapshot=snapshot)
    assert [row["status"] for row in rows] == ["Installed", "Installed", "Installed"]
    assert rows[2]["detail"].endswith("Running")
    assert agy_models(telemetry_snapshot=snapshot) == ["dynamic-model"]
    legacy = list_harnesses(lambda _name: {"available": False, "executable_exists": False})
    assert [row["status"] for row in legacy] == ["Not installed", "Not installed", "Not installed"]


# ---------------------------------------------------------------------------
# Narrow AGY telemetry cache-invalidation regression.  The previous
# generalised ``_safe_status`` rewrite (which inferred availability from
# models/missing fields) is rejected; ``_safe_status`` is restored to
# baseline semantics.  These end-to-end tests reproduce the *real*
# stale-cache sequence:
#   1. canonical ``harness_status("agy")`` fails and populates
#      ``_AGY_PROBE_CACHE``;
#   2. the underlying AGY probe becomes healthy without env/executable/cwd
#      changing (the cache key therefore still matches);
#   3. ``harness_telemetry_snapshot`` MUST invalidate the AGY cache and
#      obtain the fresh canonical healthy result, so telemetry and a
#      back-to-back canonical call agree at the same instant;
#   4. a genuine persistent failure must still be reported as blocked,
#      not silently flipped to available.
# ---------------------------------------------------------------------------


_AGY_HELP_TEXT = textwrap.dedent(
    """\
    Usage: agy [options]
      --print
      --dangerously-skip-permissions
      --output-format text|json|stream-json
      --print-timeout <dur>
      --model <id>
      --effort (low|medium|high)
    """
)


def _completed(argv: list[str], returncode: int, stdout: str, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


def test_telemetry_never_serves_stale_agy_failure_after_canonical_has_recovered():
    """End-to-end regression: with the real ``_agy_cli_status`` cache in
    place, a canonical ``harness_status("agy")`` call that records a
    failing probe must not leave a *stale* failing result in the next
    telemetry snapshot once the underlying probe has actually started
    succeeding.  The snapshot MUST mirror what a fresh canonical call
    would observe at that same instant.
    """
    # Ensure a clean cache and a fresh provider so the wiring under test
    # is the one shipped by ``harness_telemetry_snapshot``.
    control_plane._AGY_PROBE_CACHE = None
    control_plane._HARNESS_TELEMETRY_PROVIDER = None

    probe_state = {"mode": "failing"}

    def probe(argv, *, cwd, env):
        if probe_state["mode"] == "failing":
            if argv[1:] == ["--version"]:
                return _completed(argv, 0, "1.1.26\n")
            if argv[1:] == ["--help"]:
                return _completed(argv, 0, _AGY_HELP_TEXT)
            if argv[1:] == ["models"]:
                return _completed(
                    argv,
                    1,
                    "",
                    "Fetching available models...\nError: Please sign in to view available models.\n",
                )
        else:
            if argv[1:] == ["--version"]:
                return _completed(argv, 0, "1.1.26\n")
            if argv[1:] == ["--help"]:
                return _completed(argv, 0, _AGY_HELP_TEXT)
            if argv[1:] == ["models"]:
                return _completed(
                    argv,
                    0,
                    "gemini-3.7-flash-high\n",
                    "Fetching available models...\n",
                )
        raise AssertionError(f"unexpected probe args: {argv}")

    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        fake_agy = root / "agy.exe"
        fake_agy.write_text("placeholder", encoding="utf-8")
        with mock.patch.object(control_plane, "AGY_EXE", fake_agy), mock.patch.object(
            control_plane, "_run_agy_probe", side_effect=probe
        ), mock.patch.object(
            control_plane, "AGY_PROBE_CACHE_TTL_SECONDS", 60.0
        ), mock.patch.object(
            control_plane, "default_process_activity_adapter",
            return_value=FakeProcessAdapter({}),
        ):
            # Step 1: a real canonical call while the AGY probe is failing
            # populates the shared ``_AGY_PROBE_CACHE`` with a fail-closed
            # record.  This mirrors the production sequence where the
            # launcher probes the CLI before the user has finished signing
            # in.
            failing_canonical = control_plane.harness_status("agy")
            assert failing_canonical["available"] is False, failing_canonical
            assert failing_canonical["blocker"], failing_canonical
            assert control_plane._AGY_PROBE_CACHE is not None, "canonical probe must populate the shared cache"
            assert control_plane._AGY_PROBE_CACHE[2]["available"] is False, control_plane._AGY_PROBE_CACHE

            # Step 2: the underlying AGY probe now succeeds (e.g. the user
            # signed in, the smoke run completed).  Without the fix, the
            # next ``harness_status``/``harness_telemetry_snapshot`` call
            # would still return the cached failing result because the
            # cache key (executable / env / cwd) is unchanged.
            probe_state["mode"] = "successful"

            # Step 3: the telemetry snapshot MUST reflect the same current
            # canonical probe a fresh ``harness_status("agy")`` would
            # observe at this instant - never a stale failing record left
            # over from before the recovery.
            telemetry_snapshot = control_plane.harness_telemetry_snapshot()
            agy_telemetry = next(
                entry for entry in telemetry_snapshot["harnesses"] if entry["name"] == "agy"
            )
            assert agy_telemetry["available"] is True, agy_telemetry
            assert agy_telemetry["blocker"] is None, agy_telemetry
            assert agy_telemetry["installed"] is True, agy_telemetry
            assert agy_telemetry["models"] == ["gemini-3.7-flash-high"], agy_telemetry
            assert agy_telemetry["version"] == "1.1.26", agy_telemetry

            # Step 4: a canonical call at the same instant MUST agree with
            # the telemetry snapshot.  The fix makes the two paths observe
            # the same current probe - the contradiction reported in
            # production is therefore impossible.
            canonical_after = control_plane.harness_status("agy")
            assert canonical_after["available"] is True, canonical_after
            assert canonical_after["blocker"] is None, canonical_after
            assert canonical_after["models"] == ["gemini-3.7-flash-high"], canonical_after

            # Codex and MiniMax semantics are unchanged by the AGY fix.
            by_name = {entry["name"]: entry for entry in telemetry_snapshot["harnesses"]}
            assert by_name["codex"]["name"] == "codex"
            assert by_name["minimax"]["name"] == "minimax"


def test_telemetry_genuine_agy_failure_still_preserves_blocker_end_to_end():
    """The fix must NOT override a real ``available=False`` + blocker from
    the canonical probe.  When the AGY CLI genuinely cannot complete its
    capability verification (e.g. ``agy models`` still exits non-zero
    after a credential refresh), both telemetry and canonical MUST report
    the fail-closed state - never silently flip to ``available=True``
    because models happened to exist in some other context.
    """
    control_plane._AGY_PROBE_CACHE = None
    control_plane._HARNESS_TELEMETRY_PROVIDER = None

    def probe(argv, *, cwd, env):
        if argv[1:] == ["--version"]:
            return _completed(argv, 0, "1.1.26\n")
        if argv[1:] == ["--help"]:
            return _completed(argv, 0, _AGY_HELP_TEXT)
        if argv[1:] == ["models"]:
            return _completed(
                argv,
                1,
                "",
                "Fetching available models...\nError: Please sign in to view available models.\n",
            )
        raise AssertionError(argv)

    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        fake_agy = root / "agy.exe"
        fake_agy.write_text("placeholder", encoding="utf-8")
        with mock.patch.object(control_plane, "AGY_EXE", fake_agy), mock.patch.object(
            control_plane, "_run_agy_probe", side_effect=probe
        ), mock.patch.object(
            control_plane, "AGY_PROBE_CACHE_TTL_SECONDS", 60.0
        ), mock.patch.object(
            control_plane, "default_process_activity_adapter",
            return_value=FakeProcessAdapter({}),
        ):
            canonical = control_plane.harness_status("agy")
            assert canonical["available"] is False, canonical
            assert canonical["blocker"], canonical
            assert canonical["models"] == [], canonical

            telemetry_snapshot = control_plane.harness_telemetry_snapshot()
            agy_telemetry = next(
                entry for entry in telemetry_snapshot["harnesses"] if entry["name"] == "agy"
            )
            assert agy_telemetry["available"] is False, agy_telemetry
            assert agy_telemetry["installed"] is True, agy_telemetry  # exe still present
            assert agy_telemetry["blocker"] == "Harness capability status is blocked", agy_telemetry
            assert agy_telemetry["models"] == [], agy_telemetry


def test_telemetry_force_refresh_observes_current_agy_canonical_state():
    """``force_refresh=True`` on the telemetry snapshot MUST also surface
    the current canonical AGY probe.  The previous fix only re-read the
    same short-lived cache; the cache invalidator wiring now propagates a
    fresh probe to ``force_refresh=True`` as well, so callers asking for
    an explicit refresh never see a stale failing result.
    """
    control_plane._AGY_PROBE_CACHE = None
    control_plane._HARNESS_TELEMETRY_PROVIDER = None

    probe_state = {"mode": "failing"}

    def probe(argv, *, cwd, env):
        if probe_state["mode"] == "failing":
            if argv[1:] == ["--version"]:
                return _completed(argv, 0, "1.1.26\n")
            if argv[1:] == ["--help"]:
                return _completed(argv, 0, _AGY_HELP_TEXT)
            if argv[1:] == ["models"]:
                return _completed(argv, 1, "", "Error: Please sign in\n")
        else:
            if argv[1:] == ["--version"]:
                return _completed(argv, 0, "1.1.26\n")
            if argv[1:] == ["--help"]:
                return _completed(argv, 0, _AGY_HELP_TEXT)
            if argv[1:] == ["models"]:
                return _completed(argv, 0, "gemini-3.7-flash-high\n")
        raise AssertionError(argv)

    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        fake_agy = root / "agy.exe"
        fake_agy.write_text("placeholder", encoding="utf-8")
        with mock.patch.object(control_plane, "AGY_EXE", fake_agy), mock.patch.object(
            control_plane, "_run_agy_probe", side_effect=probe
        ), mock.patch.object(
            control_plane, "AGY_PROBE_CACHE_TTL_SECONDS", 60.0
        ), mock.patch.object(
            control_plane, "default_process_activity_adapter",
            return_value=FakeProcessAdapter({}),
        ):
            # Prime the cache with a failing result.
            control_plane.harness_status("agy")
            probe_state["mode"] = "successful"

            refreshed = control_plane.harness_telemetry_snapshot(force_refresh=True)
            agy_telemetry = next(
                entry for entry in refreshed["harnesses"] if entry["name"] == "agy"
            )
            assert agy_telemetry["available"] is True, agy_telemetry
            assert agy_telemetry["models"] == ["gemini-3.7-flash-high"], agy_telemetry
