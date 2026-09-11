from launcher.harnesses import agy_models, list_harnesses


def test_supported_names_and_installed_states():
    values = {"codex": True, "minimax": False, "agy": True}
    rows = list_harnesses(lambda name: {"available": values[name], "version": "x"})
    assert [row["display_name"] for row in rows] == ["Codex CLI", "MiniMax CLI", "Antigravity/AGY"]
    assert [row["status"] for row in rows] == ["Installed", "Not installed", "Installed"]


def test_agy_models_are_dynamic_and_not_hard_coded():
    current = [["model-a"]]

    def provider(name):
        return {"available": True, "models": current[0]} if name == "agy" else {"available": False}

    assert agy_models(provider) == ["model-a"]
    current[0] = ["model-b", "model-c"]
    assert agy_models(provider) == ["model-b", "model-c"]


def test_probe_failure_is_fail_soft():
    rows = list_harnesses(lambda name: (_ for _ in ()).throw(OSError("probe failed")))
    assert all(row["status"] == "Not installed" and not row["available"] for row in rows)


def test_json_model_records_are_supported():
    assert agy_models(lambda name: {"available": True, "models": [{"id": "dynamic-id"}]}) == ["dynamic-id"]


def test_installed_but_blocked_harness_is_not_mislabeled_not_installed():
    """Transient probe failures must surface as Blocked, not Not installed."""

    def provider(name):
        if name == "agy":
            return {
                "available": False,
                "executable_exists": True,
                "blocker": "Antigravity CLI capability probes did not pass: missing verified exec capabilities: print, model",
                "models": [],
            }
        if name == "minimax":
            return {
                "available": False,
                "executable_exists": False,
                "blocker": "MiniMax CLI not found at C:/missing/mcode.cmd",
            }
        return {"available": True, "executable": "codex", "version": "0.0.0"}

    rows = list_harnesses(provider)
    by_name = {row["name"]: row for row in rows}

    # Installed-but-blocked: probe failed, binary present, blocker set.
    assert by_name["agy"]["status"] == "Blocked"
    assert by_name["agy"]["available"] is False
    assert "probes did not pass" in by_name["agy"]["detail"]
    assert by_name["agy"]["raw"]["blocker"].startswith("Antigravity")

    # Missing executable: must still be "Not installed" (not "Blocked").
    assert by_name["minimax"]["status"] == "Not installed"
    assert by_name["minimax"]["available"] is False

    # Healthy harness: unaffected.
    assert by_name["codex"]["status"] == "Installed"
    assert by_name["codex"]["available"] is True


def test_blocked_agy_models_returns_empty_when_probe_failed():
    """When AGY is installed-but-blocked, the model dropdown must be empty,
    not populated with the wrong signal that the harness is unusable."""

    def provider(name):
        if name == "agy":
            return {
                "available": False,
                "executable_exists": True,
                "blocker": "Antigravity CLI capability probes did not pass",
                "models": [],
            }
        return {"available": False, "executable_exists": False}

    assert agy_models(provider) == []


def test_authoritative_available_quota_is_rendered_in_harness_detail():
    snapshot = {
        "harnesses": [
            {
                "name": "codex", "available": True, "installed": True,
                "version": "1.0", "activity": {"state": "idle"},
                "quota": {"state": "available", "stale": True, "windows": [
                    {"label": "5-hour", "used_percent": 42, "resets_at": "2030-01-01T00:00:00Z"},
                ]},
            },
            {"name": "minimax", "available": False, "installed": False,
             "activity": {"state": "idle"}, "quota": {"state": "unavailable", "windows": []}},
            {"name": "agy", "available": False, "installed": False,
             "activity": {"state": "idle"}, "quota": {"state": "unavailable", "windows": []}},
        ]
    }
    rows = list_harnesses(telemetry_snapshot=snapshot)
    codex = next(row for row in rows if row["name"] == "codex")
    assert "Quota 5-hour: 42% used" in codex["detail"]
    assert "telemetry stale" in codex["detail"]


def test_unavailable_or_invalid_quota_is_not_fabricated():
    snapshot = {
        "harnesses": [
            {"name": "codex", "available": True, "installed": True,
             "activity": {"state": "idle"},
             "quota": {"state": "unavailable", "windows": [], "error": "no source"}},
            {"name": "minimax", "available": False, "installed": False,
             "activity": {"state": "idle"}, "quota": {"state": "available", "windows": [{"label": "bad"}]}},
            {"name": "agy", "available": False, "installed": False,
             "activity": {"state": "idle"}, "quota": {"state": "available", "windows": []}},
        ]
    }
    rows = list_harnesses(telemetry_snapshot=snapshot)
    assert all("Quota " not in row["detail"] for row in rows)
