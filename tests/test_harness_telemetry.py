from __future__ import annotations

import unittest

from harness_process_adapter import UnavailableProcessActivityAdapter
from harness_telemetry import HarnessTelemetryProvider


class HarnessTelemetryTests(unittest.TestCase):
    def test_snapshot_is_safe_and_normalized(self) -> None:
        provider = HarnessTelemetryProvider(
            status_provider=lambda name: {
                "available": name == "agy",
                "executable_exists": True,
                "version": "1.2.3",
                "blocker": "credential token should not escape",
                "models": ["gemini-test"],
            },
            codex_quota_provider=lambda: {"state": "unavailable", "error": "no source"},
            job_activity_provider=lambda: {"agy": {"running": 1, "source": "queue"}},
            process_adapter=UnavailableProcessActivityAdapter(),
        )
        snapshot = provider.snapshot(force_refresh=True)
        agy = next(item for item in snapshot["harnesses"] if item["name"] == "agy")
        self.assertEqual("running", agy["activity"]["state"])
        self.assertEqual("Harness capability status is blocked", agy["blocker"])
        self.assertNotIn("credential token", str(snapshot))


if __name__ == "__main__":
    unittest.main()
