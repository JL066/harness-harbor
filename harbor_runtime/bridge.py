import contextlib
import json
import signal
import sys
from .lifecycle import Runtime, doctor
from .protocol import MAX_MESSAGE, encode, hello, validate
from .config import discover_executables


def serve(paths, settings, found, source=None, output=None):
    source = source or sys.stdin.buffer
    output = output or sys.stdout.buffer
    # Also isolates prints from background telemetry threads.
    original_stdout = sys.stdout
    sys.stdout = sys.stderr
    runtime = Runtime(paths, settings, found)
    greeted = False
    runtime.acquire()
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            line = source.readline(MAX_MESSAGE + 1)
            if not line:
                break
            response = {"v": 1, "id": None, "ok": False}
            method = None
            try:
                if len(line) > MAX_MESSAGE:
                    raise ValueError("input_limit")
                request = json.loads(line)
                if isinstance(request, dict) and isinstance(request.get("id"), str) and len(request["id"]) <= 128:
                    response["id"] = request["id"]
                method, params = validate(request)
                if not greeted and method != "hello":
                    raise ValueError("handshake_required")
                with contextlib.redirect_stdout(sys.stderr):
                    if method == "hello":
                        result = hello()
                        greeted = True
                    elif method == "runtime.start":
                        result = runtime.start()
                    elif method in {"runtime.stop", "shutdown"}:
                        result = runtime.stop()
                    elif method == "runtime.restart":
                        runtime.stop()
                        result = runtime.start()
                    elif method == "status.snapshot":
                        result = runtime.snapshot()
                    elif method == "harness.telemetry":
                        runtime.refresh_telemetry()
                        result = {**runtime._telemetry, "harnesses": runtime.harness_activity(connected=not runtime.stopped)}
                    elif method == "tunnel.test":
                        result = runtime.test_connection(**params)
                    elif method == "settings.validate":
                        from .config import validate_settings
                        try:
                            normalized = validate_settings(params["settings"], credentials=params["credentials"],
                                                           require_connection=params["require_connection"])
                            result = {"ok": True, "settings": normalized, "errors": []}
                        except Exception as exc:
                            from launcher.user_settings import SettingsError
                            if not isinstance(exc, (ValueError, SettingsError)):
                                raise
                            result = {"ok": False, "errors": [str(exc)]}
                    elif method == "logs.tail":
                        result = runtime.logs_tail(**params)
                    else:
                        result = {**doctor(paths, found), "detected_executables": discover_executables(), "status": runtime.snapshot()}
                response.update(ok=True, result=result)
            except ValueError as exc:
                code = str(exc) if str(exc) in {"invalid_request", "incompatible_protocol", "unknown_method", "invalid_params", "input_limit", "handshake_required"} else "invalid_json"
                response["error"] = {"code": code, "message": "Request rejected: " + code}
                if code == "incompatible_protocol":
                    greeted = False
            except Exception:
                response["error"] = {"code": "runtime_error", "message": "Operation failed; check safe diagnostics and settings."}
            output.write(encode(response))
            output.flush()
            if method == "shutdown" and response["ok"] or len(line) > MAX_MESSAGE:
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            runtime.stop()
        finally:
            sys.stdout = original_stdout
            if runtime._lock_file:
                runtime._lock_file.close()
