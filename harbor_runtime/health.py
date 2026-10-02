"""Shared loopback-only, proxy-free health probe policy."""
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler, Request
from urllib.error import HTTPError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def probe_loopback(url, timeout=0.5):
    started = time.monotonic()
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.username or parsed.password or parsed.fragment:
            return False, 0, 0.0
        host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
        if parsed.port is not None:
            host += f":{parsed.port}"
        target = urlunsplit((parsed.scheme, host, parsed.path, parsed.query, ""))
        with build_opener(ProxyHandler({}), NoRedirect()).open(Request(target), timeout=timeout) as response:
            return response.status == 200, response.status, (time.monotonic() - started) * 1000
    except HTTPError as exc:
        return False, exc.code, (time.monotonic() - started) * 1000
    except (OSError, ValueError):
        return False, 0, (time.monotonic() - started) * 1000
