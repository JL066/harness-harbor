"""Comprehensive tests for Host Diagnostics read-only tools."""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import subprocess
import unittest
from unittest import mock

import host_diagnostics
import server_legacy


class TestHostDiagnosticsParameterValidation(unittest.TestCase):
    """Verify strict type, range, and boundary validations for all diagnostic tools."""

    def test_port_validation(self) -> None:
        for bad_port in [0, 65536, -1, -500, 70000, "5173", True, False, None, 80.5]:
            with self.subTest(port=bad_port):
                with self.assertRaises(ValueError):
                    host_diagnostics.validate_port(bad_port)

        self.assertEqual(host_diagnostics.validate_port(1), 1)
        self.assertEqual(host_diagnostics.validate_port(80), 80)
        self.assertEqual(host_diagnostics.validate_port(5173), 5173)
        self.assertEqual(host_diagnostics.validate_port(65535), 65535)

    def test_pid_validation(self) -> None:
        for bad_pid in [0, -1, -999, "123", True, False, None, 2**31 + 10]:
            with self.subTest(pid=bad_pid):
                with self.assertRaises(ValueError):
                    host_diagnostics.validate_pid(bad_pid)

        self.assertEqual(host_diagnostics.validate_pid(1), 1)
        self.assertEqual(host_diagnostics.validate_pid(12345), 12345)

    def test_protocol_validation(self) -> None:
        for bad_proto in ["ftp", "ssh", "icmp", "http", "", None, 123, True]:
            with self.subTest(proto=bad_proto):
                with self.assertRaises(ValueError):
                    host_diagnostics.validate_protocol(bad_proto)

        self.assertEqual(host_diagnostics.validate_protocol("tcp"), "tcp")
        self.assertEqual(host_diagnostics.validate_protocol("TCP"), "tcp")
        self.assertEqual(host_diagnostics.validate_protocol("udp"), "udp")
        self.assertEqual(host_diagnostics.validate_protocol("UDP"), "udp")

    def test_timeout_validation(self) -> None:
        for bad_to in [0, -1, -5.5, 16, 100, "10", True, False]:
            with self.subTest(timeout=bad_to):
                with self.assertRaises(ValueError):
                    host_diagnostics.validate_timeout(bad_to, default=5, max_timeout=15)

        self.assertEqual(host_diagnostics.validate_timeout(None, default=5, max_timeout=15), 5.0)
        self.assertEqual(host_diagnostics.validate_timeout(1, default=5, max_timeout=15), 1.0)
        self.assertEqual(host_diagnostics.validate_timeout(5, default=5, max_timeout=15), 5.0)
        self.assertEqual(host_diagnostics.validate_timeout(15, default=5, max_timeout=15), 15.0)

    def test_port_listeners_invalid_input(self) -> None:
        res1 = host_diagnostics.port_listeners(0)
        self.assertFalse(res1["ok"])
        self.assertEqual(res1["error_type"], "ValidationError")

        res2 = host_diagnostics.port_listeners(65536)
        self.assertFalse(res2["ok"])
        self.assertEqual(res2["error_type"], "ValidationError")

        res3 = host_diagnostics.port_listeners(5173, protocol="ftp")  # type: ignore
        self.assertFalse(res3["ok"])
        self.assertEqual(res3["error_type"], "ValidationError")

    def test_process_inspect_invalid_input(self) -> None:
        res1 = host_diagnostics.process_inspect(-1)
        self.assertFalse(res1["ok"])
        self.assertEqual(res1["error_type"], "ValidationError")

        res2 = host_diagnostics.process_inspect(0)
        self.assertFalse(res2["ok"])
        self.assertEqual(res2["error_type"], "ValidationError")

        res3 = host_diagnostics.process_inspect("1234")  # type: ignore
        self.assertFalse(res3["ok"])
        self.assertEqual(res3["error_type"], "ValidationError")

    def test_http_probe_invalid_input(self) -> None:
        self.assertFalse(host_diagnostics.http_probe("")["ok"])
        self.assertFalse(host_diagnostics.http_probe("   ")["ok"])

        res_scheme = host_diagnostics.http_probe("ftp://127.0.0.1:21")
        self.assertFalse(res_scheme["ok"])
        self.assertEqual(res_scheme["error_type"], "InvalidScheme")

        res_file = host_diagnostics.http_probe("file:///c:/windows/system32")
        self.assertFalse(res_file["ok"])
        self.assertEqual(res_file["error_type"], "InvalidScheme")

        res_post = host_diagnostics.http_probe("http://127.0.0.1:5173", method="POST")  # type: ignore
        self.assertFalse(res_post["ok"])
        self.assertEqual(res_post["error_type"], "ValidationError")

        res_delete = host_diagnostics.http_probe("http://127.0.0.1:5173", method="DELETE")  # type: ignore
        self.assertFalse(res_delete["ok"])
        self.assertEqual(res_delete["error_type"], "ValidationError")

    def test_firewall_query_invalid_input(self) -> None:
        res = host_diagnostics.firewall_query()
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_type"], "ValidationError")

        res_bad_port = host_diagnostics.firewall_query(port=70000)
        self.assertFalse(res_bad_port["ok"])
        self.assertEqual(res_bad_port["error_type"], "ValidationError")

        res_bad_proto = host_diagnostics.firewall_query(port=80, protocol="icmp")  # type: ignore
        self.assertFalse(res_bad_proto["ok"])
        self.assertEqual(res_bad_proto["error_type"], "ValidationError")

    def test_tls_inspect_invalid_input(self) -> None:
        self.assertFalse(host_diagnostics.tls_inspect("")["ok"])
        self.assertFalse(host_diagnostics.tls_inspect("   ")["ok"])
        self.assertFalse(host_diagnostics.tls_inspect("127.0.0.1", port=0)["ok"])
        self.assertFalse(host_diagnostics.tls_inspect("127.0.0.1", port=70000)["ok"])

    def test_tcp_connect_probe_invalid_input(self) -> None:
        self.assertFalse(host_diagnostics.tcp_connect_probe("", 80)["ok"])
        self.assertFalse(host_diagnostics.tcp_connect_probe("127.0.0.1", 0)["ok"])
        self.assertFalse(host_diagnostics.tcp_connect_probe("127.0.0.1", 70000)["ok"])

    def test_dns_resolve_invalid_input(self) -> None:
        self.assertFalse(host_diagnostics.dns_resolve("")["ok"])
        self.assertFalse(host_diagnostics.dns_resolve("   ")["ok"])


class TestCommandInjectionResistance(unittest.TestCase):
    """Verify that injection attack strings are rejected and never reach subprocess argv."""

    def test_port_injection_strings(self) -> None:
        payloads = [
            "5173 & whoami",
            "5173; calc.exe",
            "5173 | taskkill",
            "5173`whoami`",
            "$(whoami)",
        ]
        for p in payloads:
            with self.subTest(payload=p):
                res = host_diagnostics.port_listeners(p)  # type: ignore
                self.assertFalse(res["ok"])
                self.assertEqual(res["error_type"], "ValidationError")

    def test_pid_injection_strings(self) -> None:
        payloads = [
            "123; whoami",
            "123 & calc.exe",
            "123 | Stop-Process",
            "$(cat /etc/passwd)",
        ]
        for p in payloads:
            with self.subTest(payload=p):
                res = host_diagnostics.process_inspect(p)  # type: ignore
                self.assertFalse(res["ok"])
                self.assertEqual(res["error_type"], "ValidationError")

    def test_firewall_query_injection_strings(self) -> None:
        res1 = host_diagnostics.firewall_query(port="80 & whoami")  # type: ignore
        self.assertFalse(res1["ok"])
        self.assertEqual(res1["error_type"], "ValidationError")

        res2 = host_diagnostics.firewall_query(port=80, protocol="tcp; whoami")  # type: ignore
        self.assertFalse(res2["ok"])
        self.assertEqual(res2["error_type"], "ValidationError")


class TestSSRFAndNetworkAllowlist(unittest.TestCase):
    """Verify explicit CIDR allowlist, TUN 198.18/15 blocking, and IPv4-mapped IPv6 normalization."""

    def test_allowlist_positive_cases(self) -> None:
        # Loopback
        self.assertTrue(host_diagnostics.is_ip_allowed(ipaddress.ip_address("127.0.0.1")))
        self.assertTrue(host_diagnostics.is_ip_allowed(ipaddress.ip_address("127.0.1.1")))
        self.assertTrue(host_diagnostics.is_ip_allowed(ipaddress.ip_address("::1")))

        # RFC1918 Private
        self.assertTrue(host_diagnostics.is_ip_allowed(ipaddress.ip_address("10.0.0.1")))
        self.assertTrue(host_diagnostics.is_ip_allowed(ipaddress.ip_address("172.16.0.1")))
        self.assertTrue(host_diagnostics.is_ip_allowed(ipaddress.ip_address("172.31.255.255")))
        self.assertTrue(host_diagnostics.is_ip_allowed(ipaddress.ip_address("192.168.1.1")))
        self.assertTrue(host_diagnostics.is_ip_allowed(ipaddress.ip_address("192.168.0.1")))

        # Link-local & CGNAT / Tailscale
        self.assertTrue(host_diagnostics.is_ip_allowed(ipaddress.ip_address("169.254.1.1")))
        self.assertTrue(host_diagnostics.is_ip_allowed(ipaddress.ip_address("100.64.0.1")))
        self.assertTrue(host_diagnostics.is_ip_allowed(ipaddress.ip_address("100.64.0.2")))

        # IPv6 ULA & Link-local
        self.assertTrue(host_diagnostics.is_ip_allowed(ipaddress.ip_address("fc00::1")))
        self.assertTrue(host_diagnostics.is_ip_allowed(ipaddress.ip_address("fd00::abcd")))
        self.assertTrue(host_diagnostics.is_ip_allowed(ipaddress.ip_address("fe80::1")))

    def test_allowlist_negative_cases(self) -> None:
        # Public IPv4
        self.assertFalse(host_diagnostics.is_ip_allowed(ipaddress.ip_address("8.8.8.8")))
        self.assertFalse(host_diagnostics.is_ip_allowed(ipaddress.ip_address("1.1.1.1")))
        self.assertFalse(host_diagnostics.is_ip_allowed(ipaddress.ip_address("93.184.216.34")))

        # Public IPv6
        self.assertFalse(host_diagnostics.is_ip_allowed(ipaddress.ip_address("2606:4700:4700::1111")))
        self.assertFalse(host_diagnostics.is_ip_allowed(ipaddress.ip_address("2001:4860:4860::8888")))

        # Explicitly blocked: 198.18.0.0/15 (TUN fake-IP / benchmark space)
        self.assertFalse(host_diagnostics.is_ip_allowed(ipaddress.ip_address("198.18.0.1")))
        self.assertFalse(host_diagnostics.is_ip_allowed(ipaddress.ip_address("198.18.0.220")))
        self.assertFalse(host_diagnostics.is_ip_allowed(ipaddress.ip_address("198.19.255.254")))

    def test_ipv4_mapped_ipv6_normalization(self) -> None:
        # Loopback IPv4-mapped IPv6 allowed
        self.assertTrue(host_diagnostics.is_ip_allowed(ipaddress.ip_address("::ffff:127.0.0.1")))
        self.assertTrue(host_diagnostics.is_ip_allowed(ipaddress.ip_address("::ffff:192.168.1.1")))

        # Public IPv4-mapped IPv6 MUST be blocked
        self.assertFalse(host_diagnostics.is_ip_allowed(ipaddress.ip_address("::ffff:8.8.8.8")))
        self.assertFalse(host_diagnostics.is_ip_allowed(ipaddress.ip_address("::ffff:1.1.1.1")))

        # TUN fake-IP IPv4-mapped IPv6 MUST be blocked
        self.assertFalse(host_diagnostics.is_ip_allowed(ipaddress.ip_address("::ffff:198.18.0.1")))

    def test_http_probe_blocks_public_and_tun_ips(self) -> None:
        res1 = host_diagnostics.http_probe("http://8.8.8.8:80")
        self.assertFalse(res1["ok"])
        self.assertEqual(res1["error_type"], "SSRFBlocked")

        res2 = host_diagnostics.http_probe("http://198.18.0.1:80")
        self.assertFalse(res2["ok"])
        self.assertEqual(res2["error_type"], "SSRFBlocked")


class TestDNSAndPinning(unittest.TestCase):
    """Verify DNS resolution with bounded timeout and IP pinning preventing TOCTOU."""

    @mock.patch("host_diagnostics.run_safe_subprocess")
    def test_dns_timeout_returns_structured_error(self, mock_subp: mock.MagicMock) -> None:
        # Simulate subprocess timeout (returncode=-1)
        mock_subp.return_value = subprocess.CompletedProcess(
            args=["python", "-u", "-c", "...", "slow.domain"],
            returncode=-1,
            stdout="",
            stderr="",
        )
        ok, ips, err_msg, err_type = host_diagnostics.bounded_resolve("slow.domain", timeout_seconds=2.0)
        self.assertFalse(ok)
        self.assertEqual(err_type, "DNSLookupTimeout")
        self.assertIn("timed out", err_msg.lower())

    @mock.patch("host_diagnostics.bounded_resolve")
    @mock.patch("http.client.HTTPConnection")
    def test_http_probe_ip_pinning_prevents_secondary_dns(
        self,
        mock_http_conn: mock.MagicMock,
        mock_resolve: mock.MagicMock,
    ) -> None:
        # Mock DNS resolver returning a validated local IP
        mock_resolve.return_value = (True, ["127.0.0.1"], "", "")

        # Setup mock connection and response
        mock_instance = mock.MagicMock()
        mock_http_conn.return_value = mock_instance
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.version = 11
        mock_resp.getheaders.return_value = [("Content-Type", "text/plain")]
        mock_instance.getresponse.return_value = mock_resp

        res = host_diagnostics.http_probe("http://test.local:8080/path")
        self.assertTrue(res["ok"])
        self.assertEqual(res["pinned_ip"], "127.0.0.1")

        # Verify that HTTPConnection was initialized with the PINNED IP, not the hostname
        mock_http_conn.assert_called_once_with("127.0.0.1", port=8080, timeout=5.0)

        # Verify that Host header was set with the original hostname
        mock_instance.request.assert_called_once()
        call_args = mock_instance.request.call_args
        headers_sent = call_args[1].get("headers", {})
        self.assertEqual(headers_sent.get("Host"), "test.local:8080")

    @mock.patch("host_diagnostics.bounded_resolve")
    @mock.patch("http.client.HTTPConnection")
    def test_dns_rebinding_simulation(
        self,
        mock_http_conn: mock.MagicMock,
        mock_resolve: mock.MagicMock,
    ) -> None:
        # Simulate DNS returns 127.0.0.1 on initial resolve
        mock_resolve.return_value = (True, ["127.0.0.1"], "", "")

        mock_instance = mock.MagicMock()
        mock_http_conn.return_value = mock_instance
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.version = 11
        mock_resp.getheaders.return_value = []
        mock_instance.getresponse.return_value = mock_resp

        res = host_diagnostics.http_probe("http://rebind-attack.local:8080/")
        self.assertTrue(res["ok"])
        self.assertEqual(res["pinned_ip"], "127.0.0.1")

        # Connection was pinned to 127.0.0.1, never called with 8.8.8.8
        mock_http_conn.assert_called_once_with("127.0.0.1", port=8080, timeout=5.0)

    @mock.patch("host_diagnostics.resolve_and_validate_target")
    @mock.patch("http.client.HTTPConnection")
    def test_http_probe_ipv6_host_header(
        self,
        mock_http_conn: mock.MagicMock,
        mock_resolve: mock.MagicMock,
    ) -> None:
        mock_resolve.return_value = (True, "::1", "", "")
        mock_instance = mock.MagicMock()
        mock_http_conn.return_value = mock_instance
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.version = 11
        mock_resp.getheaders.return_value = [("Content-Type", "text/html")]
        mock_instance.getresponse.return_value = mock_resp

        # 1. IPv6 with non-default port
        res1 = host_diagnostics.http_probe("http://[::1]:5173/")
        self.assertTrue(res1["ok"])
        mock_http_conn.assert_called_with("::1", port=5173, timeout=5.0)
        call_args1 = mock_instance.request.call_args_list[-1]
        headers1 = call_args1[1].get("headers", {})
        self.assertEqual(headers1.get("Host"), "[::1]:5173")

        # 2. IPv6 with default port (80)
        res2 = host_diagnostics.http_probe("http://[::1]/")
        self.assertTrue(res2["ok"])
        mock_http_conn.assert_called_with("::1", port=80, timeout=5.0)
        call_args2 = mock_instance.request.call_args_list[-1]
        headers2 = call_args2[1].get("headers", {})
        self.assertEqual(headers2.get("Host"), "[::1]")


class TestTCPAndTLSSSRF(unittest.TestCase):
    """Verify that tcp_connect_probe and tls_inspect block public targets before socket creation."""

    @mock.patch("socket.create_connection")
    def test_tcp_connect_probe_blocks_public_target(self, mock_sock: mock.MagicMock) -> None:
        res1 = host_diagnostics.tcp_connect_probe("8.8.8.8", 443)
        self.assertFalse(res1["ok"])
        self.assertEqual(res1["error_type"], "SSRFBlocked")
        mock_sock.assert_not_called()

        res2 = host_diagnostics.tcp_connect_probe("198.18.0.1", 443)
        self.assertFalse(res2["ok"])
        self.assertEqual(res2["error_type"], "SSRFBlocked")
        mock_sock.assert_not_called()

    @mock.patch("socket.create_connection")
    def test_tls_inspect_blocks_public_target(self, mock_sock: mock.MagicMock) -> None:
        res1 = host_diagnostics.tls_inspect("8.8.8.8", 443)
        self.assertFalse(res1["ok"])
        self.assertEqual(res1["error_type"], "SSRFBlocked")
        mock_sock.assert_not_called()

        res2 = host_diagnostics.tls_inspect("198.18.0.1", 443)
        self.assertFalse(res2["ok"])
        self.assertEqual(res2["error_type"], "SSRFBlocked")
        mock_sock.assert_not_called()


class TestTLSSemanticsAndParse(unittest.TestCase):
    """Verify that chain_trusted and hostname_matches are independent, and parse errors are surfaced."""

    def test_hostname_matching_logic(self) -> None:
        # DNS exact match
        self.assertTrue(host_diagnostics._match_hostname_or_ip("localhost", ["localhost"], []))
        self.assertTrue(host_diagnostics._match_hostname_or_ip("myhost.local", ["myhost.local"], []))

        # Wildcard match
        self.assertTrue(host_diagnostics._match_hostname_or_ip("sub.example.local", ["*.example.local"], []))
        self.assertFalse(host_diagnostics._match_hostname_or_ip("nested.sub.example.local", ["*.example.local"], []))

        # IP SAN match
        self.assertTrue(host_diagnostics._match_hostname_or_ip("192.168.0.1", [], ["192.168.0.1"]))
        self.assertFalse(host_diagnostics._match_hostname_or_ip("192.168.0.2", [], ["192.168.0.1"]))

        # Mismatch
        self.assertFalse(host_diagnostics._match_hostname_or_ip("wrong.local", ["correct.local"], []))

    @mock.patch("host_diagnostics.resolve_and_validate_target")
    @mock.patch("socket.create_connection")
    @mock.patch("ssl.SSLContext.wrap_socket")
    def test_chain_trusted_independent_of_hostname_match(
        self,
        mock_wrap: mock.MagicMock,
        mock_conn: mock.MagicMock,
        mock_resolve: mock.MagicMock,
    ) -> None:
        mock_resolve.return_value = (True, "127.0.0.1", "", "")

        # Simulate: CA chain trusted by Windows store, but SAN only has 'another.host'
        mock_sock = mock.MagicMock()
        mock_conn.return_value.__enter__.return_value = mock_sock

        # Mock dummy DER cert that won't match 'mytest.local'
        fake_der = b"\x30\x82\x01\x00"  # dummy bytes
        mock_sock.getpeercert.return_value = fake_der
        mock_wrap.return_value.__enter__.return_value = mock_sock

        with mock.patch("cryptography.x509.load_der_x509_certificate") as mock_load:
            mock_cert = mock.MagicMock()
            mock_cert.subject.rfc4514_string.return_value = "CN=another.host"
            mock_cert.issuer.rfc4514_string.return_value = "CN=Trusted CA"
            mock_cert.serial_number = 12345
            mock_cert.not_valid_before_utc.isoformat.return_value = "2026-01-01T00:00:00Z"
            mock_cert.not_valid_after_utc.isoformat.return_value = "2027-01-01T00:00:00Z"

            dns_ext = mock.MagicMock()
            dns_ext.value = [mock.MagicMock(value="another.host")]
            from cryptography import x509
            dns_ext.value[0].__class__ = x509.DNSName
            mock_cert.extensions.get_extension_for_oid.return_value = dns_ext
            mock_load.return_value = mock_cert

            res = host_diagnostics.tls_inspect("mytest.local", 443)
            self.assertTrue(res["ok"])
            # Chain is trusted because default context with check_hostname=False succeeded
            self.assertTrue(res["chain_trusted"])
            # Hostname does not match because requested 'mytest.local' != cert 'another.host'
            self.assertFalse(res["hostname_matches"])
            self.assertTrue(res["certificate_parse_ok"])

    @mock.patch("host_diagnostics.resolve_and_validate_target")
    @mock.patch("socket.create_connection")
    @mock.patch("ssl.SSLContext.wrap_socket")
    def test_chain_trusted_and_hostname_matches(
        self,
        mock_wrap: mock.MagicMock,
        mock_conn: mock.MagicMock,
        mock_resolve: mock.MagicMock,
    ) -> None:
        mock_resolve.return_value = (True, "127.0.0.1", "", "")
        mock_sock = mock.MagicMock()
        mock_conn.return_value.__enter__.return_value = mock_sock
        mock_sock.getpeercert.return_value = b"\x30\x82\x01\x00"
        mock_wrap.return_value.__enter__.return_value = mock_sock

        with mock.patch("cryptography.x509.load_der_x509_certificate") as mock_load:
            mock_cert = mock.MagicMock()
            mock_cert.subject.rfc4514_string.return_value = "CN=localhost"
            mock_cert.issuer.rfc4514_string.return_value = "CN=Trusted CA"
            mock_cert.serial_number = 12345
            mock_cert.not_valid_before_utc.isoformat.return_value = "2026-01-01T00:00:00Z"
            mock_cert.not_valid_after_utc.isoformat.return_value = "2027-01-01T00:00:00Z"

            from cryptography import x509
            dns_ext = mock.MagicMock()
            dns_entry = mock.MagicMock(value="localhost")
            dns_entry.__class__ = x509.DNSName
            dns_ext.value = [dns_entry]
            mock_cert.extensions.get_extension_for_oid.return_value = dns_ext
            mock_load.return_value = mock_cert

            res = host_diagnostics.tls_inspect("localhost", 443)
            self.assertTrue(res["ok"])
            self.assertTrue(res["chain_trusted"])
            self.assertTrue(res["hostname_matches"])
            self.assertTrue(res["certificate_parse_ok"])

    @mock.patch("host_diagnostics.resolve_and_validate_target")
    @mock.patch("socket.create_connection")
    @mock.patch("ssl.SSLContext.wrap_socket")
    def test_untrusted_chain_and_hostname_matches(
        self,
        mock_wrap: mock.MagicMock,
        mock_conn: mock.MagicMock,
        mock_resolve: mock.MagicMock,
    ) -> None:
        mock_resolve.return_value = (True, "127.0.0.1", "", "")
        mock_sock = mock.MagicMock()
        mock_conn.return_value.__enter__.return_value = mock_sock
        mock_sock.getpeercert.return_value = b"\x30\x82\x01\x00"

        # wrap_socket raises SSLCertVerificationError on the first call (default_ctx) but succeeds on second (unverified_ctx)
        mock_wrap.side_effect = [
            ssl.SSLCertVerificationError("self signed certificate"),
            mock.MagicMock(__enter__=mock.MagicMock(return_value=mock_sock)),
        ]

        with mock.patch("cryptography.x509.load_der_x509_certificate") as mock_load:
            mock_cert = mock.MagicMock()
            mock_cert.subject.rfc4514_string.return_value = "CN=localhost"
            mock_cert.issuer.rfc4514_string.return_value = "CN=localhost"
            mock_cert.serial_number = 9999
            mock_cert.not_valid_before_utc.isoformat.return_value = "2026-01-01T00:00:00Z"
            mock_cert.not_valid_after_utc.isoformat.return_value = "2027-01-01T00:00:00Z"

            from cryptography import x509
            dns_ext = mock.MagicMock()
            dns_entry = mock.MagicMock(value="localhost")
            dns_entry.__class__ = x509.DNSName
            dns_ext.value = [dns_entry]
            mock_cert.extensions.get_extension_for_oid.return_value = dns_ext
            mock_load.return_value = mock_cert

            res = host_diagnostics.tls_inspect("localhost", 443)
            self.assertTrue(res["ok"])
            self.assertFalse(res["chain_trusted"])  # Untrusted self-signed chain
            self.assertTrue(res["hostname_matches"])  # But hostname matches
            self.assertTrue(res["certificate_parse_ok"])

    @mock.patch("host_diagnostics.resolve_and_validate_target")
    @mock.patch("socket.create_connection")
    @mock.patch("ssl.SSLContext.wrap_socket")
    def test_tls_x509_parse_failure(
        self,
        mock_wrap: mock.MagicMock,
        mock_conn: mock.MagicMock,
        mock_resolve: mock.MagicMock,
    ) -> None:
        mock_resolve.return_value = (True, "127.0.0.1", "", "")
        mock_sock = mock.MagicMock()
        mock_conn.return_value.__enter__.return_value = mock_sock
        mock_sock.getpeercert.return_value = b"\x30\x82\x01\x00"
        mock_wrap.return_value.__enter__.return_value = mock_sock

        with mock.patch("cryptography.x509.load_der_x509_certificate", side_effect=ValueError("Corrupt ASN.1 structure")):
            res = host_diagnostics.tls_inspect("localhost", 443)
            self.assertTrue(res["ok"])
            self.assertFalse(res["certificate_parse_ok"])
            self.assertIn("Corrupt ASN.1 structure", res["parse_error"])


class TestFirewallQueryLogic(unittest.TestCase):
    """Verify firewall query AND/intersection semantics, port ranges/Any, and literal executable matching."""

    @mock.patch("host_diagnostics._run_powershell_script")
    def test_firewall_query_parsing_and_intersection(self, mock_ps: mock.MagicMock) -> None:
        mock_output = {
            "ok": True,
            "rules": [
                {
                    "name": "Example Web (5173)",
                    "enabled": True,
                    "direction": "Inbound",
                    "action": "Allow",
                    "protocol": "TCP",
                    "local_ports": ["5173"],
                    "program": "C:\\Program Files\\nodejs\\node.exe",
                    "profiles": ["Any"],
                }
            ],
        }
        mock_ps.return_value = (True, json.dumps(mock_output), "")

        # Query with both port and executable (AND semantics)
        res = host_diagnostics.firewall_query(port=5173, protocol="tcp", executable="node.exe")
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["rules"]), 1)
        rule = res["rules"][0]
        self.assertEqual(rule["name"], "Example Web (5173)")
        self.assertEqual(rule["program"], "C:\\Program Files\\nodejs\\node.exe")

    @mock.patch("host_diagnostics._run_powershell_script")
    def test_firewall_query_ranges_and_any(self, mock_ps: mock.MagicMock) -> None:
        mock_output = {
            "ok": True,
            "rules": [
                {
                    "name": "Range Rule (5000-6000)",
                    "enabled": True,
                    "direction": "Inbound",
                    "action": "Allow",
                    "protocol": "TCP",
                    "local_ports": ["5000-6000"],
                    "program": None,
                    "profiles": ["Any"],
                },
                {
                    "name": "Any Port Rule",
                    "enabled": True,
                    "direction": "Inbound",
                    "action": "Allow",
                    "protocol": "TCP",
                    "local_ports": ["Any"],
                    "program": None,
                    "profiles": ["Any"],
                },
            ],
        }
        mock_ps.return_value = (True, json.dumps(mock_output), "")

        res = host_diagnostics.firewall_query(port=5173, protocol="tcp")
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["rules"]), 2)
        rule_names = [r["name"] for r in res["rules"]]
        self.assertIn("Range Rule (5000-6000)", rule_names)
        self.assertIn("Any Port Rule", rule_names)

    @mock.patch("host_diagnostics._run_powershell_script")
    def test_firewall_query_literal_wildcard_executable(self, mock_ps: mock.MagicMock) -> None:
        mock_ps.return_value = (True, json.dumps({"ok": True, "rules": []}), "")
        res = host_diagnostics.firewall_query(port=5173, executable="node*")
        self.assertTrue(res["ok"])
        script_arg = mock_ps.call_args[0][0]
        self.assertIn("$TargetExecutable = [System.Text.Encoding]::UTF8.GetString", script_arg)


class TestProcessSecretsRedaction(unittest.TestCase):
    """Verify that credentials in command lines are redacted while preserving execution paths."""

    def test_redact_command_line_flags(self) -> None:
        # API key flag
        self.assertEqual(
            host_diagnostics.redact_command_line("python app.py --api-key sk-abc123456789 --port 80"),
            "python app.py --api-key <redacted> --port 80",
        )
        self.assertEqual(
            host_diagnostics.redact_command_line("python app.py --api_key=sk-secret"),
            "python app.py --api_key=<redacted>",
        )

        # Token and password flags
        self.assertEqual(
            host_diagnostics.redact_command_line("app.exe --token ghp_1234567890"),
            "app.exe --token <redacted>",
        )
        self.assertEqual(
            host_diagnostics.redact_command_line('app.exe --password "mySuperSecretPassword"'),
            "app.exe --password <redacted>",
        )
        self.assertEqual(
            host_diagnostics.redact_command_line("app.exe --password superSecret"),
            "app.exe --password <redacted>",
        )

        # Bare -p flag MUST NOT be redacted (e.g. port, project, path)
        self.assertEqual(
            host_diagnostics.redact_command_line("node server.js -p 5173"),
            "node server.js -p 5173",
        )
        self.assertEqual(
            host_diagnostics.redact_command_line("app.exe -p myproject"),
            "app.exe -p myproject",
        )

    def test_redact_command_line_env_and_bearer(self) -> None:
        # Environment variables
        self.assertEqual(
            host_diagnostics.redact_command_line("cmd.exe /c set OPENAI_API_KEY=sk-proj-xyz && node index.js"),
            "cmd.exe /c set OPENAI_API_KEY=<redacted> && node index.js",
        )

        # Standard Bearer tokens
        self.assertEqual(
            host_diagnostics.redact_command_line('curl -H "Authorization: Bearer secret-bearer-token" http://localhost'),
            'curl -H "Authorization: Bearer <redacted>" http://localhost',
        )

        # Base64 / complex Bearer tokens containing +, /, =
        bearer_complex = "Authorization: Bearer abcDEF+/xyz=="
        redacted_bearer = host_diagnostics.redact_command_line(bearer_complex)
        self.assertEqual(redacted_bearer, "Authorization: Bearer <redacted>")
        self.assertNotIn("abcDEF", redacted_bearer)
        self.assertNotIn("xyz", redacted_bearer)
        self.assertNotIn("==", redacted_bearer)

    def test_preserves_benign_commands(self) -> None:
        normal = 'node "C:\\Users\\Example\\ExampleProject\\node_modules\\vite\\bin\\vite.js" --host 0.0.0.0'
        self.assertEqual(host_diagnostics.redact_command_line(normal), normal)


class TestResponseHeaderRedaction(unittest.TestCase):
    """Verify HTTP response header redaction."""

    def test_redact_response_headers_unit(self) -> None:
        headers = [
            ("Content-Type", "application/json"),
            ("Set-Cookie", "session=super-secret-cookie"),
            ("X-API-Key", "secret-key"),
            ("X-Auth-Token", "secret-token"),
            ("Server", "nginx"),
            ("Location", "/login"),
            ("WWW-Authenticate", 'Basic realm="Access to the staging site"'),
        ]
        redacted = host_diagnostics.redact_response_headers(headers)
        self.assertEqual(redacted["Content-Type"], "application/json")
        self.assertEqual(redacted["Set-Cookie"], "<redacted>")
        self.assertEqual(redacted["X-API-Key"], "<redacted>")
        self.assertEqual(redacted["X-Auth-Token"], "<redacted>")
        self.assertEqual(redacted["Server"], "nginx")
        self.assertEqual(redacted["Location"], "/login")
        self.assertEqual(redacted["WWW-Authenticate"], 'Basic realm="Access to the staging site"')

        result_str = json.dumps(redacted)
        self.assertNotIn("session=super-secret-cookie", result_str)
        self.assertNotIn("secret-key", result_str)
        self.assertNotIn("secret-token", result_str)

    @mock.patch("host_diagnostics.resolve_and_validate_target")
    @mock.patch("http.client.HTTPConnection")
    def test_http_probe_redacts_response_headers(
        self,
        mock_http_conn: mock.MagicMock,
        mock_resolve: mock.MagicMock,
    ) -> None:
        mock_resolve.return_value = (True, "127.0.0.1", "", "")
        mock_instance = mock.MagicMock()
        mock_http_conn.return_value = mock_instance
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.version = 11
        mock_resp.getheaders.return_value = [
            ("Content-Type", "application/json"),
            ("Set-Cookie", "session=super-secret-cookie"),
            ("X-API-Key", "secret-key"),
            ("X-Auth-Token", "secret-token"),
            ("Server", "nginx"),
            ("Location", "/login"),
        ]
        mock_instance.getresponse.return_value = mock_resp

        res = host_diagnostics.http_probe("http://127.0.0.1:8080/api")
        self.assertTrue(res["ok"])
        headers = res["headers"]
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Set-Cookie"], "<redacted>")
        self.assertEqual(headers["X-API-Key"], "<redacted>")
        self.assertEqual(headers["X-Auth-Token"], "<redacted>")
        self.assertEqual(headers["Server"], "nginx")
        self.assertEqual(headers["Location"], "/login")

        res_str = json.dumps(res)
        self.assertNotIn("session=super-secret-cookie", res_str)
        self.assertNotIn("secret-key", res_str)
        self.assertNotIn("secret-token", res_str)


class TestErrorAndTimeoutHandling(unittest.TestCase):
    """Verify timeout and error resilience across diagnostic operations."""

    @mock.patch("host_diagnostics.run_safe_subprocess")
    def test_netstat_subprocess_timeout(self, mock_subp: mock.MagicMock) -> None:
        mock_subp.return_value = subprocess.CompletedProcess(
            args=["netstat.exe", "-ano"],
            returncode=-1,
            stdout="",
            stderr="timed out",
        )
        res = host_diagnostics.port_listeners(5173)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_type"], "SubprocessError")

    @mock.patch("host_diagnostics._run_powershell_script")
    def test_process_inspect_timeout(self, mock_ps: mock.MagicMock) -> None:
        mock_ps.return_value = (False, "", "Timed out after 10.0s")
        res = host_diagnostics.process_inspect(12345)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_type"], "QueryFailed")

    @mock.patch("host_diagnostics._run_powershell_script")
    def test_process_inspect_malformed_json(self, mock_ps: mock.MagicMock) -> None:
        mock_ps.return_value = (True, "{ corrupt json ...", "")
        res = host_diagnostics.process_inspect(12345)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_type"], "MalformedOutput")

    @mock.patch("host_diagnostics._run_powershell_script")
    def test_firewall_query_timeout(self, mock_ps: mock.MagicMock) -> None:
        mock_ps.return_value = (False, "", "Execution timed out")
        res = host_diagnostics.firewall_query(port=5173)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_type"], "QueryFailed")

    @mock.patch("host_diagnostics._run_powershell_script")
    def test_firewall_query_malformed_output(self, mock_ps: mock.MagicMock) -> None:
        mock_ps.return_value = (True, "not json at all", "")
        res = host_diagnostics.firewall_query(port=5173)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_type"], "MalformedOutput")

    @mock.patch("host_diagnostics.resolve_and_validate_target")
    @mock.patch("socket.create_connection")
    def test_tcp_connect_probe_timeout(
        self,
        mock_conn: mock.MagicMock,
        mock_resolve: mock.MagicMock,
    ) -> None:
        mock_resolve.return_value = (True, "127.0.0.1", "", "")
        mock_conn.side_effect = socket.timeout("timed out")
        res = host_diagnostics.tcp_connect_probe("127.0.0.1", 80, timeout_seconds=1)
        self.assertTrue(res["ok"])
        self.assertFalse(res["connected"])
        self.assertIn("timed out", res.get("error", "").lower())

    @mock.patch("host_diagnostics.bounded_resolve")
    def test_dns_resolve_failure(self, mock_resolve: mock.MagicMock) -> None:
        mock_resolve.return_value = (False, [], "Name or service not known", "DNSLookupFailed")
        res = host_diagnostics.dns_resolve("nonexistent-domain-that-does-not-exist.example")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_type"], "ResolutionFailed")


class TestMCPToolRegistration(unittest.TestCase):
    """Verify that all diagnostic tools are registered in FastMCP server_legacy."""

    def test_all_tools_registered(self) -> None:
        tool_names = [t.name for t in server_legacy.mcp._tool_manager.list_tools()]
        expected_tools = [
            "port_listeners",
            "process_inspect",
            "http_probe",
            "tls_inspect",
            "firewall_query",
            "network_interfaces",
            "tcp_connect_probe",
            "dns_resolve",
        ]
        for name in expected_tools:
            self.assertIn(name, tool_names, f"Tool {name} should be registered in FastMCP")


class TestDiagnosticsParsing(unittest.TestCase):
    """Test detailed parsing logic for netstat, process, and firewall outputs."""

    @mock.patch("host_diagnostics.run_safe_subprocess")
    def test_netstat_parsing_comprehensive(self, mock_subp: mock.MagicMock) -> None:
        sample_netstat = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1234
  TCP    0.0.0.0:5173           0.0.0.0:0              LISTENING       5555
  TCP    127.0.0.1:5173         0.0.0.0:0              LISTENING       5555
  TCP    [::]:5173              [::]:0                 LISTENING       5555
  TCP    [::1]:5173             [::]:0                 LISTENING       5555
  TCP    192.168.0.1:5173       192.168.0.2:12345       ESTABLISHED     5555
  TCP    0.0.0.0:8790           0.0.0.0:0              LISTENING       6666
  UDP    0.0.0.0:5353           *:*                                    7777
  UDP    [::]:5353              *:*                                    7777
  UDP    0.0.0.0:5173           *:*                                    8888
"""
        mock_subp.return_value = subprocess.CompletedProcess(
            args=["netstat.exe", "-ano"],
            returncode=0,
            stdout=sample_netstat,
            stderr="",
        )

        res_tcp = host_diagnostics.port_listeners(5173, protocol="tcp")
        self.assertTrue(res_tcp["ok"])
        self.assertEqual(res_tcp["port"], 5173)
        self.assertEqual(res_tcp["protocol"], "tcp")
        listeners = res_tcp["listeners"]
        self.assertEqual(len(listeners), 4)

        addrs = [l["local_address"] for l in listeners]
        self.assertIn("0.0.0.0", addrs)
        self.assertIn("127.0.0.1", addrs)
        self.assertIn("::", addrs)
        self.assertIn("::1", addrs)
        for l in listeners:
            self.assertEqual(l["pid"], 5555)
            self.assertEqual(l["state"], "Listen")
            self.assertEqual(l["local_port"], 5173)

        res_udp = host_diagnostics.port_listeners(5173, protocol="udp")
        self.assertTrue(res_udp["ok"])
        self.assertEqual(len(res_udp["listeners"]), 1)
        self.assertEqual(res_udp["listeners"][0]["local_address"], "0.0.0.0")
        self.assertEqual(res_udp["listeners"][0]["pid"], 8888)
        self.assertEqual(res_udp["listeners"][0]["state"], "Bound")

        res_none = host_diagnostics.port_listeners(9999, protocol="tcp")
        self.assertTrue(res_none["ok"])
        self.assertEqual(len(res_none["listeners"]), 0)

    @mock.patch("host_diagnostics._run_powershell_script")
    def test_process_inspect_parsing(self, mock_ps: mock.MagicMock) -> None:
        mock_data = {
            "found": True,
            "pid": 12345,
            "name": "node.exe",
            "executable": "C:\\Program Files\\nodejs\\node.exe",
            "command_line": "node server.js --token ghp_xyz123",
            "parent_pid": 100,
            "start_time": "2026-09-02T10:00:00Z",
            "working_directory": None,
        }
        mock_ps.return_value = (True, json.dumps(mock_data), "")

        res = host_diagnostics.process_inspect(12345)
        self.assertTrue(res["ok"])
        self.assertEqual(res["pid"], 12345)
        self.assertEqual(res["name"], "node.exe")
        self.assertEqual(res["executable"], "C:\\Program Files\\nodejs\\node.exe")
        # Ensure secret was redacted
        self.assertEqual(res["command_line"], "node server.js --token <redacted>")
        self.assertEqual(res["parent_pid"], 100)
        self.assertEqual(res["start_time"], "2026-09-02T10:00:00Z")
        self.assertIsNone(res["working_directory"])


class TestAsyncServerLegacyTools(unittest.IsolatedAsyncioTestCase):
    """Verify that FastMCP tool functions run cleanly asynchronously via asyncio.to_thread."""

    async def test_async_port_listeners(self) -> None:
        res = await server_legacy.port_listeners(5173)
        self.assertTrue(res["ok"])
        self.assertEqual(res["port"], 5173)

    async def test_async_dns_resolve(self) -> None:
        res = await server_legacy.dns_resolve("localhost")
        self.assertTrue(res["ok"])
        self.assertIn("127.0.0.1", res["ipv4_addresses"])


if __name__ == "__main__":
    unittest.main()
