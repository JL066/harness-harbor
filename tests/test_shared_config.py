"""Both shells submit the same contract before accepting settings."""
import os
import unittest
from unittest.mock import patch

from harbor_runtime.config import parse_settings, validate_settings, configure
from launcher.ui.setup_wizard import validate_draft, SettingsController, first_run_status
from launcher.user_settings import UserSettings
from launcher.credential_store import CredentialStore, InMemoryCredentialBackend, CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY
import tempfile
from pathlib import Path


class SharedConfigTests(unittest.TestCase):
    def test_same_invalid_drafts_and_fallback_credentials(self):
        for route in ("custom", "official_then_custom"):
            draft = {"connection": {"tunnel_id": "fixture"}, "codex": {"routing_mode": route}}
            with self.assertRaises(ValueError) as failure:
                validate_settings(draft, require_connection=True)
            self.assertEqual(validate_draft(draft), [str(failure.exception)])
            draft["codex"]["custom"] = {"base_url": "https://provider.example/v1"}
            store = CredentialStore(backend=InMemoryCredentialBackend())
            store.store(CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY, "fixture-only")
            self.assertTrue(first_run_status(UserSettings.from_dict(draft), store).required)
            with tempfile.TemporaryDirectory() as tmp:
                controller = SettingsController(credential_store=store, settings_path=Path(tmp) / "settings.json")
                with self.assertRaisesRegex(ValueError, "Custom API key"):
                    controller.apply(draft)
                self.assertFalse((Path(tmp) / "settings.json").exists())

    def test_environment_precedes_saved_route_and_model(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "HARBOR_USER_SETTINGS_DIR": tmp, "HARBOR_CODEX_DEFAULT_ROUTE": "official",
            "HARBOR_CODEX_CUSTOM_MODEL": "explicit-model", "HARBOR_CODEX_CUSTOM_BASE_URL": "https://env.example/v1",
        }, clear=True), patch("harbor_runtime.config.resolve_executable", return_value=None):
            configure()
            self.assertEqual(os.environ["HARBOR_CODEX_DEFAULT_ROUTE"], "official")
            self.assertEqual(os.environ["HARBOR_CODEX_CUSTOM_MODEL"], "explicit-model")
            self.assertEqual(os.environ["HARBOR_CODEX_CUSTOM_BASE_URL"], "https://env.example/v1")

    def test_invalid_urls_rejected_by_both_consumers(self):
        for url in ("http://external.example", "https://user:pass@example.test", "https://example.test/?key=x"):
            draft = {"connection": {"tunnel_id": "fixture", "base_url": url}}
            with self.assertRaises(ValueError):
                validate_settings(draft)
            self.assertTrue(validate_draft(draft))


if __name__ == "__main__":
    unittest.main()
