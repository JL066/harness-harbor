"""Host diagnostics module for Harness Harbor.

Provides safe, restricted, structured read-only diagnostics for the Windows host:
- port_listeners: query TCP/UDP listening sockets and owning PIDs.
- process_inspect: query process metadata by PID (strictly read-only, with credential redaction).
- http_probe: probe local/LAN HTTP/HTTPS services with DNS pinning and SSRF protection.
- tls_inspect: inspect TLS certificate metadata, SANs, and independent trust chain.
- firewall_query: read-only query for Windows Defender Firewall rules (AND logic, port ranges, literal exec).
- network_interfaces: list network adapters, IPs, gateways, and DNS servers.
- tcp_connect_probe: test raw TCP connectivity without application data (SSRF restricted).
- dns_resolve: resolve hostnames to IPv4 and IPv6 addresses with bounded timeout.

All functions are strictly read-only, parameterized without shell interpolation,
and bounded by deterministic timeouts.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from control_plane import IS_WINDOWS, run_safe_subprocess


# ---------------------------------------------------------------------------
# Validation Helpers
# ---------------------------------------------------------------------------

def validate_port(port: Any) -> int:
    """Validate that port is an integer in the valid TCP/UDP range (1-65535)."""
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValueError(f"Port must be an integer between 1 and 65535, got {type(port).__name__}")
    if port < 1 or port > 65535:
        raise ValueError(f"Port must be between 1 and 65535, got {port}")
    return port


def validate_pid(pid: Any) -> int:
    """Validate that PID is a positive integer."""
    if isinstance(pid, bool) or not isinstance(pid, int):
        raise ValueError(f"PID must be a positive integer, got {type(pid).__name__}")
    if pid <= 0 or pid > 2_147_483_647:
        raise ValueError(f"PID must be a positive 32-bit integer, got {pid}")
    return pid


def validate_protocol(protocol: Any, allowed: tuple[str, ...] = ("tcp", "udp")) -> str:
    """Validate and normalize network protocol."""
    if not isinstance(protocol, str):
        raise ValueError(f"Protocol must be a string in {allowed}, got {type(protocol).__name__}")
    normalized = protocol.strip().lower()
    if normalized not in allowed:
        raise ValueError(f"Invalid protocol '{protocol}'. Allowed: {allowed}")
    return normalized


def validate_timeout(timeout_seconds: Any, default: int = 5, max_timeout: int = 15) -> float:
    """Validate that timeout is a positive number within explicit safety bounds."""
    if timeout_seconds is None:
        return float(default)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ValueError(f"Timeout must be a number between 1 and {max_timeout}")
    if timeout_seconds <= 0 or timeout_seconds > max_timeout:
        raise ValueError(f"Timeout must be between 1 and {max_timeout} seconds, got {timeout_seconds}")
    return float(timeout_seconds)


# ---------------------------------------------------------------------------
# Command Line Secret Redaction Helper
# ---------------------------------------------------------------------------

def redact_command_line(cmd: str | None) -> str | None:
    """Redact common credentials from process command lines while preserving execution paths."""
    if not cmd or not isinstance(cmd, str):
        return cmd

    # 1. Redact CLI flags: --api-key <val>, --token <val>, --password <val>
    # Note: Bare '-p' is intentionally not redacted to avoid false positives with port/project/path flags.
    flag_pattern = re.compile(
        r"(--(?:api[-_]?key|apikey|token|access[-_]?token|access_token|password|passwd|secret))"
        r"(\s*[:=]\s*|\s+)"
        r'([^\s"\'\\]+|"[^"]*"|\'[^\']*\')',
        re.IGNORECASE,
    )
    redacted = flag_pattern.sub(r"\1\2<redacted>", cmd)

    # 2. Redact environment assignments: OPENAI_API_KEY=..., TOKEN=..., etc.
    env_pattern = re.compile(
        r"\b([A-Za-z0-9_]*(?:KEY|TOKEN|PASSWORD|PASSWD|SECRET|AUTH)[A-Za-z0-9_]*)"
        r"(\s*=\s*)"
        r'([^\s"\'\\]+|"[^"]*"|\'[^\']*\')',
        re.IGNORECASE,
    )
    redacted = env_pattern.sub(r"\1\2<redacted>", redacted)

    # 3. Redact Authorization: Bearer <token> and Bearer <token>
    # Matches any characters until whitespace, quote, or command/header boundary
    auth_pattern = re.compile(
        r"((?:Authorization\s*:\s*)?Bearer\s+)([^\s\"\'\r\n;]+|\"[^\"]*\"|\'[^\']*\')",
        re.IGNORECASE,
    )
    redacted = auth_pattern.sub(r"\1<redacted>", redacted)

    return redacted


# ---------------------------------------------------------------------------
# HTTP Response Header Redaction Helper
# ---------------------------------------------------------------------------

_EXPLICIT_SENSITIVE_HEADERS = {
    "set-cookie",
    "cookie",
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "x-auth-token",
    "x-access-token",
    "x-session-token",
}

_PRESERVED_HEADER_EXCEPTIONS = {
    "www-authenticate",
    "proxy-authenticate",
}

_SENSITIVE_HEADER_KEYWORDS = ("token", "secret", "api-key", "apikey")


def redact_response_headers(headers: list[tuple[str, str]] | dict[str, str]) -> dict[str, str]:
    """Redact sensitive credentials from HTTP response headers while preserving

    header names and non-sensitive metadata (e.g. content-type, server, location).
    """
    redacted: dict[str, str] = {}
    items = headers.items() if isinstance(headers, dict) else headers
    for name, value in items:
        name_lower = name.lower()
        if name_lower in _EXPLICIT_SENSITIVE_HEADERS:
            redacted[name] = "<redacted>"
            continue

        if name_lower in _PRESERVED_HEADER_EXCEPTIONS:
            redacted[name] = value
            continue

        if any(kw in name_lower for kw in _SENSITIVE_HEADER_KEYWORDS) or ("auth" in name_lower and not name_lower.startswith("content-")):
            redacted[name] = "<redacted>"
            continue

        redacted[name] = value
    return redacted


# ---------------------------------------------------------------------------
# Explicit Network Allowlist & SSRF Guard
# ---------------------------------------------------------------------------

_ALLOWED_IPV4_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),       # Loopback
    ipaddress.ip_network("10.0.0.0/8"),        # RFC 1918 Private
    ipaddress.ip_network("172.16.0.0/12"),     # RFC 1918 Private
    ipaddress.ip_network("192.168.0.0/16"),    # RFC 1918 Private
    ipaddress.ip_network("169.254.0.0/16"),    # Link-local
    ipaddress.ip_network("100.64.0.0/10"),     # CGNAT / Tailscale
]

_ALLOWED_IPV6_NETWORKS = [
    ipaddress.ip_network("::1/128"),           # Loopback
    ipaddress.ip_network("fc00::/7"),          # Unique Local Address (ULA)
    ipaddress.ip_network("fe80::/10"),         # Link-Local
]

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("198.18.0.0/15"),     # Benchmark / Transparent proxy TUN fake-IP space
]


def is_ip_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if an IP address is within the strictly approved explicit local/LAN allowlist.

    Explicitly blocks 198.18.0.0/15 (TUN fake-IP space), public IPs, and unapproved ranges.
    Normalizes IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1) before checking.
    Does NOT rely on ip.is_private or ip.is_reserved.
    """
    # Normalize IPv4-mapped IPv6
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    # Explicit blocklist check (e.g. TUN fake-IP 198.18.0.0/15)
    for blocked_net in _BLOCKED_NETWORKS:
        if ip in blocked_net:
            return False

    if isinstance(ip, ipaddress.IPv4Address):
        for allowed_net in _ALLOWED_IPV4_NETWORKS:
            if ip in allowed_net:
                return True
    elif isinstance(ip, ipaddress.IPv6Address):
        for allowed_net in _ALLOWED_IPV6_NETWORKS:
            if ip in allowed_net:
                return True

    return False


# ---------------------------------------------------------------------------
# Bounded DNS Resolution & IP Pinning Helper
# ---------------------------------------------------------------------------

def bounded_resolve(
    hostname: str,
    timeout_seconds: float = 5.0,
) -> tuple[bool, list[str], str, str]:
    """Resolve a hostname with an explicit bounded timeout using an isolated subprocess.

    Guarantees worker threads cannot be blocked indefinitely by getaddrinfo().
    Returns (ok, ip_list, error_msg, error_type).
    """
    clean_host = hostname.strip()
    if not clean_host:
        return False, [], "Hostname must be a non-empty string", "ValidationError"

    # Fast path: already an IP address
    try:
        ip_obj = ipaddress.ip_address(clean_host)
        return True, [str(ip_obj)], "", ""
    except ValueError:
        pass

    # Resolve in an isolated safe subprocess with guaranteed termination on timeout
    resolver_script = (
        "import socket, sys, json\n"
        "try:\n"
        "    res = socket.getaddrinfo(sys.argv[1], None, socket.AF_UNSPEC, socket.SOCK_STREAM)\n"
        "    ips = []\n"
        "    for r in res:\n"
        "        ip = r[4][0]\n"
        "        if ip not in ips:\n"
        "            ips.append(ip)\n"
        "    print(json.dumps({'ok': True, 'ips': ips}))\n"
        "except socket.gaierror as exc:\n"
        "    print(json.dumps({'ok': False, 'error': str(exc), 'error_type': 'DNSLookupFailed'}))\n"
        "except Exception as exc:\n"
        "    print(json.dumps({'ok': False, 'error': str(exc), 'error_type': 'DNSLookupFailed'}))\n"
    )

    proc = run_safe_subprocess(
        [sys.executable, "-u", "-c", resolver_script, clean_host],
        timeout=timeout_seconds,
    )

    if proc.returncode == -1:
        return False, [], f"DNS resolution timed out after {timeout_seconds}s", "DNSLookupTimeout"

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return False, [], f"DNS resolution failed: {proc.stderr or 'No output'}", "DNSLookupFailed"

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return False, [], f"DNS resolution returned unparseable output: {stdout[:200]}", "DNSLookupFailed"

    if not data.get("ok"):
        return False, [], data.get("error", "DNS lookup failed"), data.get("error_type", "DNSLookupFailed")

    return True, data.get("ips", []), "", ""


def resolve_and_validate_target(
    host: str,
    port: int,
    timeout_seconds: float = 5.0,
) -> tuple[bool, str, str, str]:
    """Resolve target host, validate all candidate IPs against the explicit allowlist,

    and return the pinned IP address to prevent DNS TOCTOU / rebinding.
    Returns (ok, pinned_ip, error_msg, error_type).
    """
    ok, ips, err_msg, err_type = bounded_resolve(host, timeout_seconds=timeout_seconds)
    if not ok:
        return False, "", err_msg, err_type

    if not ips:
        return False, "", f"Could not determine valid IP address for '{host}'", "DNSLookupFailed"

    # Every resolved candidate IP must satisfy the strict allowlist
    for ip_str in ips:
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, "", f"Invalid resolved IP '{ip_str}'", "DNSLookupFailed"

        if not is_ip_allowed(ip_obj):
            return (
                False,
                "",
                f"Target '{host}' resolves to '{ip_str}' which is outside allowed local/LAN scope (SSRF restriction active)",
                "SSRFBlocked",
            )

    # Pin to the first validated candidate IP
    pinned_ip = ips[0]
    return True, pinned_ip, "", ""


# ---------------------------------------------------------------------------
# Pinned HTTPS Connection (Prevents secondary DNS resolution while preserving SNI)
# ---------------------------------------------------------------------------

class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that connects TCP directly to pinned_ip, while preserving

    server_hostname for TLS SNI and certificate verification.
    """

    def __init__(
        self,
        pinned_ip: str,
        orig_hostname: str,
        port: int,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(pinned_ip, port=port, timeout=timeout, context=context)
        self._orig_hostname = orig_hostname

    def connect(self) -> None:
        raw_sock = socket.create_connection(
            (self.host, self.port),
            timeout=self.timeout,
            source_address=self.source_address,
        )
        self.sock = self._context.wrap_socket(
            raw_sock,
            server_hostname=self._orig_hostname,
        )


# ---------------------------------------------------------------------------
# Safe PowerShell Helper
# ---------------------------------------------------------------------------

def _run_powershell_script(script: str, timeout: float = 10.0) -> tuple[bool, str, str]:
    """Execute a PowerShell script using UTF-16LE Base64 EncodedCommand.

    Guarantees no shell interpretation or quote escaping vulnerability.
    Suppresses progress stream noise and ensures UTF-8 output encoding.
    Returns (success, stdout, stderr).
    """
    powershell_exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    exe_str = str(powershell_exe) if powershell_exe.is_file() else "powershell.exe"

    full_script = (
        "$ProgressPreference = 'SilentlyContinue'\n"
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        "$OutputEncoding = [System.Text.Encoding]::UTF8\n"
        + script
    )

    encoded = base64.b64encode(full_script.encode("utf-16le")).decode("ascii")
    argv = [
        exe_str,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", encoded,
    ]

    proc = run_safe_subprocess(argv, timeout=timeout)
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    return proc.returncode == 0, stdout.strip(), stderr.strip()


# ---------------------------------------------------------------------------
# 1. port_listeners
# ---------------------------------------------------------------------------

def port_listeners(port: int, protocol: Literal["tcp", "udp"] = "tcp") -> dict[str, Any]:
    """Query current listening sockets and owning PIDs for a specific TCP or UDP port.

    Distinguishes bindings such as 127.0.0.1, 0.0.0.0, LAN IPv4, ::1, and ::.
    """
    try:
        valid_port = validate_port(port)
        valid_proto = validate_protocol(protocol, allowed=("tcp", "udp"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "error_type": "ValidationError"}

    if not IS_WINDOWS:
        return {
            "ok": False,
            "error": "port_listeners is currently only supported on Windows",
            "error_type": "UnsupportedPlatform",
        }

    netstat_exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "netstat.exe"
    exe_str = str(netstat_exe) if netstat_exe.is_file() else "netstat.exe"

    proc = run_safe_subprocess([exe_str, "-ano"], timeout=10.0)
    if proc.returncode != 0 and not proc.stdout:
        return {
            "ok": False,
            "error": f"Failed to execute netstat: {proc.stderr or 'return code ' + str(proc.returncode)}",
            "error_type": "SubprocessError",
        }

    listeners: list[dict[str, Any]] = []
    lines = (proc.stdout or "").splitlines()

    for line in lines:
        parts = line.strip().split()
        if len(parts) < 4:
            continue

        proto = parts[0].lower()
        if proto != valid_proto:
            continue

        if valid_proto == "tcp":
            if len(parts) < 5:
                continue
            local_addr_raw = parts[1]
            state = parts[3]
            if state.upper() != "LISTENING":
                continue
            try:
                pid = int(parts[4])
            except ValueError:
                continue
            state_label = "Listen"
        else:
            local_addr_raw = parts[1]
            try:
                pid = int(parts[3])
            except ValueError:
                continue
            state_label = "Bound"

        # Parse address and port
        if local_addr_raw.startswith("["):
            match = re.match(r"^\[(.*?)\]:(\d+)$", local_addr_raw)
            if not match:
                continue
            addr_str = match.group(1)
            parsed_port = int(match.group(2))
        else:
            match = re.match(r"^(.*?):(\d+)$", local_addr_raw)
            if not match:
                continue
            addr_str = match.group(1)
            parsed_port = int(match.group(2))

        if parsed_port == valid_port:
            listeners.append({
                "local_address": addr_str,
                "local_port": parsed_port,
                "pid": pid,
                "state": state_label,
            })

    listeners.sort(key=lambda item: (item["local_address"], item["pid"]))

    return {
        "ok": True,
        "port": valid_port,
        "protocol": valid_proto,
        "listeners": listeners,
    }


# ---------------------------------------------------------------------------
# 2. process_inspect
# ---------------------------------------------------------------------------

def process_inspect(pid: int) -> dict[str, Any]:
    """Inspect metadata of a specific process by PID in a strictly read-only manner.

    Applies secret redaction to command line credentials.
    """
    try:
        valid_pid = validate_pid(pid)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "error_type": "ValidationError"}

    if not IS_WINDOWS:
        return {
            "ok": False,
            "error": "process_inspect is currently only supported on Windows",
            "error_type": "UnsupportedPlatform",
        }

    script = f"""
$p = Get-CimInstance Win32_Process -Filter "ProcessId = {valid_pid}" -ErrorAction SilentlyContinue
if ($p) {{
    $startTime = if ($p.CreationDate) {{ ([datetime]$p.CreationDate).ToString("o") }} else {{ $null }}
    [PSCustomObject]@{{
        found = $true
        pid = [int]$p.ProcessId
        name = $p.Name
        executable = $p.ExecutablePath
        command_line = $p.CommandLine
        parent_pid = if ($p.ParentProcessId) {{ [int]$p.ParentProcessId }} else {{ $null }}
        start_time = $startTime
        working_directory = $null
    }} | ConvertTo-Json -Compress
}} else {{
    [PSCustomObject]@{{
        found = $false
    }} | ConvertTo-Json -Compress
}}
"""
    success, stdout, stderr = _run_powershell_script(script, timeout=10.0)

    if not stdout:
        return {
            "ok": False,
            "error": f"Process inspection query failed or timed out: {stderr}",
            "error_type": "QueryFailed",
        }

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": f"Failed to parse process inspect output: {exc}",
            "error_type": "MalformedOutput",
        }

    if not data.get("found"):
        return {
            "ok": False,
            "pid": valid_pid,
            "error": f"Process with PID {valid_pid} not found",
            "error_type": "ProcessNotFound",
        }

    # Redact credentials from command line before returning
    safe_command_line = redact_command_line(data.get("command_line"))

    return {
        "ok": True,
        "pid": data.get("pid"),
        "name": data.get("name"),
        "executable": data.get("executable"),
        "command_line": safe_command_line,
        "working_directory": data.get("working_directory"),
        "start_time": data.get("start_time"),
        "parent_pid": data.get("parent_pid"),
    }


# ---------------------------------------------------------------------------
# 3. http_probe
# ---------------------------------------------------------------------------

def http_probe(
    url: str,
    method: Literal["HEAD", "GET"] = "HEAD",
    timeout_seconds: int = 5,
) -> dict[str, Any]:
    """Perform a read-only HTTP/HTTPS probe with strict DNS pinning and SSRF protection.

    Restricted to explicit local/LAN allowlist (127/8, 10/8, 172.16/12, 192.168/16,
    169.254/16, 100.64/10, ::1/128, fc00::/7, fe80::/10). Explicitly blocks 198.18/15
    TUN fake-IPs and public addresses.
    HEAD is the default read-only probe method. If GET is explicitly requested, it
    carries read-intent but cannot guarantee that poorly designed remote services have
    no server-side side effects. State-mutating methods (POST/PUT/PATCH/DELETE) and
    request bodies are strictly forbidden. Target IP is pinned to prevent DNS TOCTOU.
    Response headers undergo credential redaction.
    Timeout is bounded between 1 and 15 seconds.
    """
    if not isinstance(url, str) or not url.strip():
        return {"ok": False, "error": "url must be a non-empty string", "error_type": "ValidationError"}

    try:
        method_norm = validate_protocol(method, allowed=("head", "get")).upper()
        timeout_val = validate_timeout(timeout_seconds, default=5, max_timeout=15)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "error_type": "ValidationError"}

    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception as exc:
        return {"ok": False, "error": f"Malformed URL: {exc}", "error_type": "MalformedURL"}

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return {
            "ok": False,
            "error": f"Unsupported scheme '{parsed.scheme}'. Only 'http' and 'https' are permitted",
            "error_type": "InvalidScheme",
        }

    hostname = parsed.hostname
    if not hostname:
        return {"ok": False, "error": "URL hostname is missing", "error_type": "MalformedURL"}

    port = parsed.port or (443 if scheme == "https" else 80)
    if port < 1 or port > 65535:
        return {"ok": False, "error": f"Invalid URL port: {port}", "error_type": "MalformedURL"}

    # Resolve and validate target IP with DNS pinning (prevents TOCTOU & SSRF)
    target_ok, pinned_ip, err_msg, err_type = resolve_and_validate_target(
        hostname, port, timeout_seconds=timeout_val
    )
    if not target_ok:
        return {
            "ok": False,
            "url": url,
            "error": err_msg,
            "error_type": err_type,
        }

    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    is_ipv6_literal = False
    try:
        if isinstance(ipaddress.ip_address(hostname), ipaddress.IPv6Address):
            is_ipv6_literal = True
    except ValueError:
        pass

    formatted_host = f"[{hostname}]" if is_ipv6_literal else hostname

    host_header = formatted_host
    if (scheme == "http" and port != 80) or (scheme == "https" and port != 443):
        host_header = f"{formatted_host}:{port}"

    start_time = time.perf_counter()
    is_tls = scheme == "https"

    try:
        if is_tls:
            context = ssl.create_default_context()
            conn: http.client.HTTPConnection = PinnedHTTPSConnection(
                pinned_ip=pinned_ip,
                orig_hostname=hostname,
                port=port,
                timeout=timeout_val,
                context=context,
            )
        else:
            conn = http.client.HTTPConnection(
                pinned_ip,
                port=port,
                timeout=timeout_val,
            )

        try:
            req_headers = {
                "Host": host_header,
                "User-Agent": "Harness-Harbor-Diagnostics/1.0",
                "Accept": "*/*",
            }
            conn.request(method_norm, path, headers=req_headers)
            resp = conn.getresponse()
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            headers = redact_response_headers(resp.getheaders())
            version_str = "HTTP/1.1" if resp.version == 11 else ("HTTP/1.0" if resp.version == 10 else f"HTTP/{resp.version}")

            return {
                "ok": True,
                "url": url,
                "status_code": resp.status,
                "final_url": url,
                "http_version": version_str,
                "elapsed_ms": elapsed_ms,
                "headers": headers,
                "tls": is_tls,
                "pinned_ip": pinned_ip,
            }
        finally:
            conn.close()

    except socket.gaierror as exc:
        return {"ok": False, "url": url, "error": f"DNS failure: {exc}", "error_type": "DNSLookupFailed"}
    except ConnectionRefusedError as exc:
        return {"ok": False, "url": url, "error": f"Connection refused: {exc}", "error_type": "ConnectionRefused"}
    except (socket.timeout, TimeoutError):
        return {"ok": False, "url": url, "error": f"Connection timed out after {timeout_val}s", "error_type": "Timeout"}
    except ConnectionResetError as exc:
        return {"ok": False, "url": url, "error": f"Connection reset: {exc}", "error_type": "ConnectionReset"}
    except ssl.SSLCertVerificationError as exc:
        err_msg_str = str(exc)
        if "hostname" in err_msg_str.lower() or "match" in err_msg_str.lower():
            err_type = "TLSHostnameMismatch"
        else:
            err_type = "TLSCertificateError"
        return {"ok": False, "url": url, "error": f"TLS certificate error: {err_msg_str}", "error_type": err_type}
    except ssl.CertificateError as exc:
        return {"ok": False, "url": url, "error": f"TLS hostname mismatch: {exc}", "error_type": "TLSHostnameMismatch"}
    except ssl.SSLError as exc:
        return {"ok": False, "url": url, "error": f"TLS error: {exc}", "error_type": "TLSCertificateError"}
    except OSError as exc:
        return {"ok": False, "url": url, "error": f"Network error: {exc}", "error_type": "NetworkError"}
    except Exception as exc:
        return {"ok": False, "url": url, "error": f"Probe error: {exc}", "error_type": "ProbeError"}


# ---------------------------------------------------------------------------
# 4. tls_inspect
# ---------------------------------------------------------------------------

def _match_hostname_or_ip(host: str, dns_names: list[str], ip_addresses: list[str]) -> bool:
    """Check if host matches the certificate's SAN entries."""
    try:
        target_ip = ipaddress.ip_address(host)
        return str(target_ip) in ip_addresses
    except ValueError:
        pass

    target_host = host.lower()
    for name in dns_names:
        name_lower = name.lower()
        if name_lower == target_host:
            return True
        if name_lower.startswith("*."):
            suffix = name_lower[1:]  # e.g. .example.com
            if target_host.endswith(suffix) and target_host.count(".") == name_lower.count("."):
                return True
    return False


def tls_inspect(host: str, port: int = 443, timeout_seconds: int = 5) -> dict[str, Any]:
    """Inspect HTTPS/TLS certificate metadata and verify SANs and trust chain independently.

    Strictly inspects client-side peer certificate. Zero private key access.
    Target IP is validated and pinned against explicit local allowlist.
    Timeout is bounded between 1 and 15 seconds.
    """
    if not isinstance(host, str) or not host.strip():
        return {"ok": False, "error": "host must be a non-empty string", "error_type": "ValidationError"}

    try:
        valid_port = validate_port(port)
        timeout_val = validate_timeout(timeout_seconds, default=5, max_timeout=15)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "error_type": "ValidationError"}

    clean_host = host.strip()

    # SSRF & Target resolution validation with IP pinning
    target_ok, pinned_ip, err_msg, err_type = resolve_and_validate_target(
        clean_host, valid_port, timeout_seconds=timeout_val
    )
    if not target_ok:
        return {
            "ok": False,
            "host": clean_host,
            "port": valid_port,
            "error": err_msg,
            "error_type": err_type,
        }

    # 1. Test chain trust with system default CA store (check_hostname = False to decouple chain trust from hostname match)
    chain_trusted = False
    default_ctx = ssl.create_default_context()
    default_ctx.check_hostname = False
    default_ctx.verify_mode = ssl.CERT_REQUIRED
    try:
        with socket.create_connection((pinned_ip, valid_port), timeout=timeout_val) as raw_sock:
            with default_ctx.wrap_socket(raw_sock, server_hostname=clean_host) as s_sock:
                chain_trusted = True
    except (ssl.SSLCertVerificationError, ssl.CertificateError):
        chain_trusted = False
    except (ConnectionRefusedError, socket.timeout, TimeoutError, socket.gaierror, ConnectionResetError) as exc:
        if isinstance(exc, socket.gaierror):
            return {"ok": False, "host": clean_host, "port": valid_port, "error": f"DNS resolution failed: {exc}", "error_type": "DNSLookupFailed"}
        if isinstance(exc, ConnectionRefusedError):
            return {"ok": False, "host": clean_host, "port": valid_port, "error": f"Connection refused on {clean_host}:{valid_port}", "error_type": "ConnectionRefused"}
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return {"ok": False, "host": clean_host, "port": valid_port, "error": f"Connection timed out after {timeout_val}s", "error_type": "Timeout"}
        if isinstance(exc, ConnectionResetError):
            return {"ok": False, "host": clean_host, "port": valid_port, "error": f"Connection reset: {exc}", "error_type": "ConnectionReset"}
    except Exception:
        pass

    # 2. Retrieve peer certificate in binary DER form using unverified context
    unverified_ctx = ssl._create_unverified_context()
    unverified_ctx.check_hostname = False
    unverified_ctx.verify_mode = ssl.CERT_NONE

    der_cert = b""
    try:
        with socket.create_connection((pinned_ip, valid_port), timeout=timeout_val) as raw_sock:
            with unverified_ctx.wrap_socket(raw_sock, server_hostname=clean_host) as s_sock:
                der_cert = s_sock.getpeercert(binary_form=True)
    except ConnectionRefusedError as exc:
        return {"ok": False, "host": clean_host, "port": valid_port, "error": f"Connection refused: {exc}", "error_type": "ConnectionRefused"}
    except (socket.timeout, TimeoutError):
        return {"ok": False, "host": clean_host, "port": valid_port, "error": f"Connection timed out after {timeout_val}s", "error_type": "Timeout"}
    except socket.gaierror as exc:
        return {"ok": False, "host": clean_host, "port": valid_port, "error": f"DNS resolution failed: {exc}", "error_type": "DNSLookupFailed"}
    except ssl.SSLError as exc:
        return {"ok": False, "host": clean_host, "port": valid_port, "error": f"TLS handshake failed: {exc}", "error_type": "TLSHandshakeFailed"}
    except Exception as exc:
        return {"ok": False, "host": clean_host, "port": valid_port, "error": f"TLS inspection failed: {exc}", "error_type": "InspectFailed"}

    if not der_cert:
        return {"ok": False, "host": clean_host, "port": valid_port, "error": "Server did not provide a certificate", "error_type": "NoCertificate"}

    sha256_fingerprint = hashlib.sha256(der_cert).hexdigest()

    subject_str = ""
    issuer_str = ""
    serial_str = ""
    not_before_str = ""
    not_after_str = ""
    dns_names: list[str] = []
    ip_addresses: list[str] = []
    certificate_parse_ok = False
    parse_error = ""

    try:
        from cryptography import x509
        cert = x509.load_der_x509_certificate(der_cert)
        subject_str = cert.subject.rfc4514_string()
        issuer_str = cert.issuer.rfc4514_string()
        serial_str = format(cert.serial_number, "x")

        try:
            not_before_str = cert.not_valid_before_utc.isoformat()
            not_after_str = cert.not_valid_after_utc.isoformat()
        except AttributeError:
            not_before_str = cert.not_valid_before.isoformat()
            not_after_str = cert.not_valid_after.isoformat()

        try:
            san_ext = cert.extensions.get_extension_for_oid(x509.ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
            for name in san_ext.value:
                if isinstance(name, x509.DNSName):
                    dns_names.append(name.value)
                elif isinstance(name, x509.IPAddress):
                    ip_addresses.append(str(name.value))
        except x509.ExtensionNotFound:
            pass

        certificate_parse_ok = True
    except ImportError:
        parse_error = "cryptography library is not available"
    except Exception as exc:
        parse_error = f"Failed to parse X.509 certificate: {exc}"

    hostname_matches = _match_hostname_or_ip(clean_host, dns_names, ip_addresses)

    result = {
        "ok": True,
        "host": clean_host,
        "port": valid_port,
        "pinned_ip": pinned_ip,
        "subject": subject_str,
        "issuer": issuer_str,
        "serial_number": serial_str,
        "sha256_fingerprint": sha256_fingerprint,
        "not_before": not_before_str,
        "not_after": not_after_str,
        "dns_names": dns_names,
        "ip_addresses": ip_addresses,
        "hostname_matches": hostname_matches,
        "chain_trusted": chain_trusted,
        "certificate_parse_ok": certificate_parse_ok,
    }
    if not certificate_parse_ok:
        result["parse_error"] = parse_error

    return result


# ---------------------------------------------------------------------------
# 5. firewall_query
# ---------------------------------------------------------------------------

def firewall_query(
    port: int | None = None,
    protocol: Literal["tcp", "udp"] | None = None,
    executable: str | None = None,
) -> dict[str, Any]:
    """Read-only query for Windows Defender Firewall rules matching port or executable.

    When multiple criteria (port, protocol, executable) are specified, rules must satisfy
    ALL provided conditions (AND / intersection semantics).
    Supports port ranges (e.g. 5000-6000) and 'Any' port.
    Executable matching is literal (no wildcard injection).
    """
    if port is None and (not executable or not str(executable).strip()):
        return {
            "ok": False,
            "error": "At least one of 'port' or 'executable' must be provided",
            "error_type": "ValidationError",
        }

    target_port_str = ""
    if port is not None:
        try:
            target_port_str = str(validate_port(port))
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "error_type": "ValidationError"}

    target_proto_str = ""
    if protocol is not None:
        try:
            target_proto_str = validate_protocol(protocol, allowed=("tcp", "udp")).upper()
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "error_type": "ValidationError"}

    target_exec_str = ""
    if executable is not None:
        if not isinstance(executable, str):
            return {"ok": False, "error": "executable must be a string", "error_type": "ValidationError"}
        target_exec_str = executable.strip().replace("/", "\\")

    if not IS_WINDOWS:
        return {
            "ok": False,
            "error": "firewall_query is currently only supported on Windows",
            "error_type": "UnsupportedPlatform",
        }

    # Pass target executable safely as base64 to eliminate any PowerShell escaping or script injection
    b64_exec = base64.b64encode(target_exec_str.encode("utf-8")).decode("ascii")

    script = f"""
$TargetPort = '{target_port_str}'
$TargetProtocol = '{target_proto_str}'
$TargetExecutable = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{b64_exec}'))

function Test-PortMatch([object]$rulePorts, [int]$tPort) {{
    if (-not $tPort) {{ return $true }}
    if (-not $rulePorts) {{ return $false }}
    foreach ($entry in @($rulePorts)) {{
        if (-not $entry) {{ continue }}
        $str = $entry.ToString().Trim()
        if ($str -eq "Any" -or $str -eq "*") {{ return $true }}
        foreach ($p in ($str -split ",")) {{
            $p = $p.Trim()
            if ($p -eq "Any" -or $p -eq "*") {{ return $true }}
            if ($p -match "^\\d+$") {{
                if ([int]$p -eq $tPort) {{ return $true }}
            }} elseif ($p -match "^(\\d+)-(\\d+)$") {{
                $low = [int]$matches[1]
                $high = [int]$matches[2]
                if ($tPort -ge $low -and $tPort -le $high) {{ return $true }}
            }}
        }}
    }}
    return $false
}}

function Test-ExecMatch([string]$prog, [string]$tExec) {{
    if (-not $tExec) {{ return $true }}
    if (-not $prog) {{ return $false }}
    if ($prog.IndexOf($tExec, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {{ return $true }}
    try {{
        $pName = [System.IO.Path]::GetFileName($prog)
        $tName = [System.IO.Path]::GetFileName($tExec)
        if ($pName -and $tName -and [string]::Equals($pName, $tName, [System.StringComparison]::OrdinalIgnoreCase)) {{ return $true }}
    }} catch {{}}
    return $false
}}

$intPort = if ($TargetPort) {{ [int]$TargetPort }} else {{ 0 }}

$pfs = Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
$pfMap = @{{}}
foreach ($pf in $pfs) {{ $pfMap[$pf.InstanceID] = $pf }}

$afs = Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue
$afMap = @{{}}
foreach ($af in $afs) {{ $afMap[$af.InstanceID] = $af }}

$pfSet = $null
if ($TargetPort -or $TargetProtocol) {{
    $matchingPfs = @($pfs | Where-Object {{
        $protoOk = if ($TargetProtocol) {{ ($_.Protocol -eq $TargetProtocol) -or ($_.Protocol -eq "Any") }} else {{ $true }}
        $portOk = if ($intPort) {{ Test-PortMatch $_.LocalPort $intPort }} else {{ $true }}
        $protoOk -and $portOk
    }})
    $pfSet = [System.Collections.Generic.HashSet[string]]::new([string[]]@($matchingPfs | ForEach-Object {{ $_.InstanceID }}))
}}

$afSet = $null
if ($TargetExecutable) {{
    $matchingAfs = @($afs | Where-Object {{
        Test-ExecMatch $_.Program $TargetExecutable
    }})
    $afSet = [System.Collections.Generic.HashSet[string]]::new([string[]]@($matchingAfs | ForEach-Object {{ $_.InstanceID }}))
}}

$finalIds = $null
if ($pfSet -and $afSet) {{
    $finalIds = [System.Collections.Generic.List[string]]::new()
    foreach ($id in $pfSet) {{
        if ($afSet.Contains($id)) {{ $finalIds.Add($id) }}
    }}
}} elseif ($pfSet) {{
    $finalIds = @($pfSet)
}} elseif ($afSet) {{
    $finalIds = @($afSet)
}} else {{
    $finalIds = @()
}}

$rules = @()
if ($finalIds.Count -gt 0) {{
    $rules = @(Get-NetFirewallRule -Name $finalIds -ErrorAction SilentlyContinue)
}}

$output = @()
foreach ($r in $rules) {{
    if (-not $r) {{ continue }}
    $pf = $pfMap[$r.InstanceID]
    $af = $afMap[$r.InstanceID]

    $localPorts = @()
    if ($pf -and $pf.LocalPort) {{
        $localPorts = @($pf.LocalPort)
    }}

    $profiles = @()
    if ($null -ne $r.Profile) {{
        $profiles = @(($r.Profile.ToString() -split ',') | ForEach-Object {{ $_.Trim() }})
    }}

    $output += [PSCustomObject]@{{
        name = if ($r.DisplayName) {{ $r.DisplayName }} else {{ $r.Name }}
        enabled = ($r.Enabled.ToString() -eq "True")
        direction = $r.Direction.ToString()
        action = $r.Action.ToString()
        protocol = if ($pf -and $pf.Protocol) {{ $pf.Protocol }} else {{ $null }}
        local_ports = $localPorts
        program = if ($af -and $af.Program) {{ $af.Program }} else {{ $null }}
        profiles = $profiles
    }}
}}

[PSCustomObject]@{{
    ok = $true
    rules = $output
}} | ConvertTo-Json -Depth 5 -Compress
"""
    success, stdout, stderr = _run_powershell_script(script, timeout=15.0)

    if not stdout:
        return {
            "ok": False,
            "error": f"Firewall query failed or timed out: {stderr}",
            "error_type": "QueryFailed",
        }

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": f"Failed to parse firewall query output: {exc}",
            "error_type": "MalformedOutput",
        }

    rules = data.get("rules") or []
    if isinstance(rules, dict):
        rules = [rules]

    return {
        "ok": True,
        "rules": rules,
    }


# ---------------------------------------------------------------------------
# 6. network_interfaces (Phase 2)
# ---------------------------------------------------------------------------

def network_interfaces() -> dict[str, Any]:
    """Inspect local network adapters, IP addresses, default gateways, and DNS servers."""
    if not IS_WINDOWS:
        return {
            "ok": False,
            "error": "network_interfaces is currently only supported on Windows",
            "error_type": "UnsupportedPlatform",
        }

    script = """
$configs = Get-NetIPConfiguration -ErrorAction SilentlyContinue
$adapters = @()
foreach ($c in $configs) {
    if (-not $c) { continue }
    $ipv4 = @()
    if ($c.IPv4Address) {
        foreach ($ip in $c.IPv4Address) {
            $ipv4 += [PSCustomObject]@{
                ip_address = $ip.IPAddress
                prefix_length = $ip.PrefixLength
            }
        }
    }
    $ipv6 = @()
    if ($c.IPv6Address) {
        foreach ($ip in $c.IPv6Address) {
            $ipv6 += [PSCustomObject]@{
                ip_address = $ip.IPAddress
                prefix_length = $ip.PrefixLength
            }
        }
    }
    $gateways = @()
    if ($c.IPv4DefaultGateway) {
        foreach ($gw in $c.IPv4DefaultGateway) {
            if ($gw.NextHop) { $gateways += $gw.NextHop }
        }
    }
    $dns = @()
    if ($c.DNSServer) {
        foreach ($d in $c.DNSServer.ServerAddresses) {
            if ($d) { $dns += $d }
        }
    }
    $status = if ($c.NetAdapter -and $c.NetAdapter.Status) { $c.NetAdapter.Status.ToString() } else { "Unknown" }
    $adapters += [PSCustomObject]@{
        name = $c.InterfaceAlias
        description = $c.InterfaceDescription
        status = $status
        ipv4 = $ipv4
        ipv6 = $ipv6
        gateways = $gateways
        dns_servers = $dns
    }
}
[PSCustomObject]@{
    ok = $true
    interfaces = $adapters
} | ConvertTo-Json -Depth 5 -Compress
"""
    success, stdout, stderr = _run_powershell_script(script, timeout=15.0)

    if not stdout:
        return {
            "ok": False,
            "error": f"Network interfaces query failed or timed out: {stderr}",
            "error_type": "QueryFailed",
        }

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": f"Failed to parse network interfaces output: {exc}",
            "error_type": "MalformedOutput",
        }

    interfaces = data.get("interfaces") or []
    if isinstance(interfaces, dict):
        interfaces = [interfaces]

    return {
        "ok": True,
        "interfaces": interfaces,
    }


# ---------------------------------------------------------------------------
# 7. tcp_connect_probe (Phase 2)
# ---------------------------------------------------------------------------

def tcp_connect_probe(host: str, port: int, timeout_seconds: int = 3) -> dict[str, Any]:
    """Test raw TCP connection to a host and port without sending application data.

    Target host is validated against the explicit local allowlist (SSRF restriction active).
    Timeout is bounded between 1 and 10 seconds.
    """
    if not isinstance(host, str) or not host.strip():
        return {"ok": False, "error": "host must be a non-empty string", "error_type": "ValidationError"}

    try:
        valid_port = validate_port(port)
        timeout_val = validate_timeout(timeout_seconds, default=3, max_timeout=10)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "error_type": "ValidationError"}

    clean_host = host.strip()

    # SSRF & Target resolution validation with IP pinning
    target_ok, pinned_ip, err_msg, err_type = resolve_and_validate_target(
        clean_host, valid_port, timeout_seconds=timeout_val
    )
    if not target_ok:
        return {
            "ok": False,
            "host": clean_host,
            "port": valid_port,
            "connected": False,
            "error": err_msg,
            "error_type": err_type,
        }

    start_time = time.perf_counter()

    try:
        with socket.create_connection((pinned_ip, valid_port), timeout=timeout_val) as sock:
            elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
            peer = sock.getpeername()
            remote_ip = peer[0] if isinstance(peer, tuple) else pinned_ip
            return {
                "ok": True,
                "host": clean_host,
                "port": valid_port,
                "connected": True,
                "remote_ip": remote_ip,
                "pinned_ip": pinned_ip,
                "elapsed_ms": elapsed_ms,
            }
    except ConnectionRefusedError:
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        return {
            "ok": True,
            "host": clean_host,
            "port": valid_port,
            "connected": False,
            "error": "Connection refused",
            "pinned_ip": pinned_ip,
            "elapsed_ms": elapsed_ms,
        }
    except (socket.timeout, TimeoutError):
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        return {
            "ok": True,
            "host": clean_host,
            "port": valid_port,
            "connected": False,
            "error": f"Connection timed out after {timeout_val}s",
            "pinned_ip": pinned_ip,
            "elapsed_ms": elapsed_ms,
        }
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        return {
            "ok": False,
            "host": clean_host,
            "port": valid_port,
            "connected": False,
            "error": f"TCP probe error: {exc}",
            "error_type": "ConnectionError",
            "pinned_ip": pinned_ip,
            "elapsed_ms": elapsed_ms,
        }


# ---------------------------------------------------------------------------
# 8. dns_resolve (Phase 2)
# ---------------------------------------------------------------------------

def dns_resolve(hostname: str) -> dict[str, Any]:
    """Resolve a hostname to IPv4 (A) and IPv6 (AAAA) addresses with bounded timeout."""
    if not isinstance(hostname, str) or not hostname.strip():
        return {"ok": False, "error": "hostname must be a non-empty string", "error_type": "ValidationError"}

    clean_hostname = hostname.strip()

    ok, ips, err_msg, err_type = bounded_resolve(clean_hostname, timeout_seconds=5.0)
    if not ok:
        res_err_type = "DNSLookupTimeout" if err_type == "DNSLookupTimeout" else "ResolutionFailed"
        return {
            "ok": False,
            "hostname": clean_hostname,
            "error": err_msg,
            "error_type": res_err_type,
        }

    ipv4_list: list[str] = []
    ipv6_list: list[str] = []

    for ip_str in ips:
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            if isinstance(ip_obj, ipaddress.IPv4Address):
                if ip_str not in ipv4_list:
                    ipv4_list.append(ip_str)
            elif isinstance(ip_obj, ipaddress.IPv6Address):
                if ip_str not in ipv6_list:
                    ipv6_list.append(ip_str)
        except ValueError:
            continue

    return {
        "ok": True,
        "hostname": clean_hostname,
        "ipv4_addresses": ipv4_list,
        "ipv6_addresses": ipv6_list,
    }
