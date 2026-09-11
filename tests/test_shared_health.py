import unittest
from unittest.mock import Mock, patch
from harbor_runtime.health import probe_loopback, NoRedirect


class HealthPolicyTests(unittest.TestCase):
    def test_only_loopback_without_proxy_or_redirect(self):
        with patch("harbor_runtime.health.build_opener") as opener:
            for url in ("https://external.example/health", "http://user:pass@127.0.0.1/", "http://127.0.0.1:bad/"):
                self.assertFalse(probe_loopback(url)[0])
            opener.assert_not_called()
            response = Mock(status=200)
            opener.return_value.open.return_value.__enter__.return_value = response
            self.assertTrue(probe_loopback("http://localhost:1234/health")[0])
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:1234/health")
            self.assertIsNone(NoRedirect().redirect_request(None, None, None, None, None, None))


if __name__ == "__main__":
    unittest.main()
