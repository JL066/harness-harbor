"""Small, closed protocol. No subprocess or filesystem parameters from clients."""
import json
import os
import re
import sys
from . import RUNTIME_VERSION, PROTOCOL_VERSION, BUILD_VERSION

MAX_MESSAGE = 262144
METHODS = ("hello", "status.snapshot", "runtime.start", "runtime.stop", "runtime.restart",
           "harness.telemetry", "tunnel.test", "settings.validate", "logs.tail", "diagnostics.run", "shutdown")


def hello():
    return dict(protocol_version=PROTOCOL_VERSION, runtime_version=RUNTIME_VERSION,
                build_version=BUILD_VERSION, capabilities=list(METHODS), platform=sys.platform)


def redact(text):
    for key, value in os.environ.items():
        if value and any(part in key.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)[^\s\"',;]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|runtime[_-]?key|token|password|secret)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]+", r"\1[REDACTED]", text)
    return re.sub(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)


def validate(request):
    if not isinstance(request, dict) or set(request) != {"v", "id", "method", "params"}:
        raise ValueError("invalid_request")
    if type(request["v"]) is not int or request["v"] != PROTOCOL_VERSION:
        raise ValueError("incompatible_protocol")
    if not isinstance(request["id"], str) or not 1 <= len(request["id"]) <= 128:
        raise ValueError("invalid_request")
    method, params = request["method"], request["params"]
    if not isinstance(method, str) or method not in METHODS:
        raise ValueError("unknown_method")
    if not isinstance(params, dict):
        raise ValueError("invalid_params")
    if method == "logs.tail":
        if set(params) - {"component", "lines"} or params.get("component") not in {"runtime", "daemon", "tunnel"}:
            raise ValueError("invalid_params")
        if type(params.get("lines", 100)) is not int or not 1 <= params.get("lines", 100) <= 200:
            raise ValueError("invalid_params")
    elif method == "settings.validate" or method == "tunnel.test" and params:
        expected = {"settings", "credentials", "require_connection"} if method == "settings.validate" else {"settings", "credentials"}
        if set(params) != expected or not isinstance(params["settings"], dict):
            raise ValueError("invalid_params")
        if method == "settings.validate" and type(params["require_connection"]) is not bool:
            raise ValueError("invalid_params")
        credentials = params["credentials"]
        if not isinstance(credentials, dict) or set(credentials) != {"tunnel", "custom"} or any(type(v) is not bool for v in credentials.values()):
            raise ValueError("invalid_params")
    elif params:
        raise ValueError("invalid_params")
    return method, params


def encode(response):
    # Redact values recursively before serialization to keep quoting intact.
    def clean(value):
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value
    data = json.dumps(clean(response), ensure_ascii=True).encode()
    if len(data) >= MAX_MESSAGE:
        data = json.dumps({"v": 1, "id": response.get("id"), "ok": False,
                           "error": {"code": "output_limit", "message": "Response exceeds protocol limit."}}).encode()
    return data + b"\n"
