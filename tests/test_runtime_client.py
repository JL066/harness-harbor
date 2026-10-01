from __future__ import annotations

import json
from collections import deque
from pathlib import Path
import queue

import pytest

from harbor_runtime import PROTOCOL_VERSION, RUNTIME_VERSION
from harbor_runtime.protocol import MAX_MESSAGE, METHODS
from launcher.runtime_client import BridgeClient, BridgeError


pytestmark = pytest.mark.windows_ci


class _FixedUUID:
    def __init__(self, value: str):
        self._value = value

    def __str__(self) -> str:
        return self._value


class _FakeStdin:
    def __init__(self):
        self.writes = []

    def write(self, payload: bytes):
        self.writes.append(payload)

    def flush(self):
        pass

    def close(self):
        pass


class _FakeStream:
    def __init__(self, lines: list[bytes] | None = None):
        self._lines = deque(lines or [])

    def readline(self, _size: int | None = None) -> bytes:
        return self._lines.popleft() if self._lines else b""

    def close(self):
        pass


class _FakeProcess:
    def __init__(self, *, stdout_lines: list[bytes] | None = None, poll_value=1):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream([])
        self._poll_value = poll_value
        self.wait_calls = []

    def poll(self):
        return self._poll_value

    def wait(self, timeout: float | None = None):
        self.wait_calls.append(timeout)
        return 0


def _bundle_env(tmp_path: Path) -> dict[str, str]:
    data_root = tmp_path.parent / "runtime-state-root"
    appdata = data_root / "AppData" / "Roaming"
    localappdata = data_root / "AppData" / "Local"
    return {
        "APPDATA": str(appdata),
        "LOCALAPPDATA": str(localappdata),
        "HARBOR_RUNTIME_MODE": "packaged",
    }


def _fake_compatible_hello(request_id: str, runtime_version: str = RUNTIME_VERSION) -> bytes:
    response = {
        "v": PROTOCOL_VERSION,
        "id": request_id,
        "ok": True,
        "result": {
            "protocol_version": PROTOCOL_VERSION,
            "runtime_version": runtime_version,
            "build_version": "dev",
            "capabilities": list(METHODS),
            "platform": "win32",
        },
    }
    return json.dumps(response, ensure_ascii=True).encode("utf-8") + b"\n"


def _fake_result(method: str, request_id: str, payload: dict) -> bytes:
    response = {"v": PROTOCOL_VERSION, "id": request_id, "ok": True, "result": payload}
    return json.dumps(response, ensure_ascii=True).encode("utf-8") + b"\n"


def _set_isolated_environment(client: BridgeClient, tmp_path: Path):
    env = _bundle_env(tmp_path)
    client._environment = lambda: (env, [])  # type: ignore[method-assign]


def _runtime_path(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime" / "harbor-runtime.exe"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_bytes(b"runtime")
    return runtime


def test_exchange_success_emits_valid_request_and_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime = _runtime_path(tmp_path)
    proc = _FakeProcess(poll_value=None)
    client = BridgeClient(executable=runtime, timeout=0.05)
    _set_isolated_environment(client, tmp_path)

    request_id = "request-ok-1"
    monkeypatch.setattr("launcher.runtime_client.uuid.uuid4", lambda: _FixedUUID(request_id))
    client._proc = proc
    client._responses = queue.Queue()
    client._responses.put(_fake_result("status.snapshot", request_id, {"state": "stopped"}))

    result = client._exchange("status.snapshot", {})

    assert result == {"state": "stopped"}
    assert json.loads(proc.stdin.writes[0].decode("utf-8")) == {
        "v": PROTOCOL_VERSION,
        "id": request_id,
        "method": "status.snapshot",
        "params": {},
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"v": PROTOCOL_VERSION, "id": "unexpected", "ok": True, "result": {"v": 1}},
        {"v": 999, "id": "request-ok-2", "ok": True, "result": {"state": "stopped"}},
    ],
)
def test_exchange_rejects_wrong_id_or_version(payload, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime = _runtime_path(tmp_path)
    proc = _FakeProcess(poll_value=None)
    client = BridgeClient(executable=runtime, timeout=0.05)
    _set_isolated_environment(client, tmp_path)

    request_id = "request-ok-2"
    monkeypatch.setattr("launcher.runtime_client.uuid.uuid4", lambda: _FixedUUID(request_id))
    client._proc = proc
    client._responses = queue.Queue()
    client._responses.put(json.dumps(payload, ensure_ascii=True).encode("utf-8") + b"\n")

    with pytest.raises(BridgeError, match="Runtime returned an invalid protocol response."):
        client._exchange("status.snapshot", {})


def test_exchange_timeout_without_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime = _runtime_path(tmp_path)
    proc = _FakeProcess(poll_value=None)
    client = BridgeClient(executable=runtime, timeout=0.01)
    _set_isolated_environment(client, tmp_path)

    request_id = "request-timeout"
    monkeypatch.setattr("launcher.runtime_client.uuid.uuid4", lambda: _FixedUUID(request_id))
    client._proc = proc
    client._responses = queue.Queue()

    with pytest.raises(BridgeError, match="Runtime request timed out"):
        client._exchange("status.snapshot", {})


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"x" * (MAX_MESSAGE + 1), id="oversize"),
        pytest.param(b"", id="eof"),
    ],
)
def test_connect_rejects_oversize_or_eof_payload(payload, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime = _runtime_path(tmp_path)
    request_id = "request-connect-failed"
    monkeypatch.setattr("launcher.runtime_client.uuid.uuid4", lambda: _FixedUUID(request_id))

    fake_process = _FakeProcess(stdout_lines=[payload])
    fake_spawned = {"count": 0}
    def spawn(*_args, **_kwargs):
        fake_spawned["count"] += 1
        return fake_process

    monkeypatch.setattr("launcher.runtime_client.spawn_owned", spawn)
    client = BridgeClient(executable=runtime, timeout=0.05)
    _set_isolated_environment(client, tmp_path)
    monkeypatch.setattr("launcher.runtime_client.terminate_tree", lambda *_args, **_kwargs: True)

    with pytest.raises(BridgeError, match="Runtime connection closed or returned an invalid response"):
        client.request("status.snapshot")

    assert fake_process.poll() == 1
    assert fake_spawned["count"] == 1


def test_connect_rejects_incompatible_hello_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime = _runtime_path(tmp_path)
    request_id = "request-hello-version"
    monkeypatch.setattr("launcher.runtime_client.uuid.uuid4", lambda: _FixedUUID(request_id))

    fake_process = _FakeProcess(stdout_lines=[_fake_compatible_hello(request_id, runtime_version="0.0.0")])
    monkeypatch.setattr("launcher.runtime_client.spawn_owned", lambda *_args, **_kwargs: fake_process)
    monkeypatch.setattr("launcher.runtime_client.terminate_tree", lambda *_args, **_kwargs: True)

    client = BridgeClient(executable=runtime, timeout=0.05)
    _set_isolated_environment(client, tmp_path)

    with pytest.raises(BridgeError, match="Launcher and bundled runtime versions are incompatible"):
        client._connect()


def test_owned_cleanup_failure_blocks_reconnect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime = _runtime_path(tmp_path)
    client = BridgeClient(executable=runtime, timeout=0.01)
    _set_isolated_environment(client, tmp_path)
    stale_process = _FakeProcess(stdout_lines=[], poll_value=2)
    client._proc = stale_process
    client._responses = queue.Queue()

    spawn_calls = {"count": 0}
    def _spawn_called(*_args, **_kwargs):
        spawn_calls["count"] += 1
        raise RuntimeError("spawn should not run")

    monkeypatch.setattr("launcher.runtime_client.spawn_owned", _spawn_called)
    monkeypatch.setattr("launcher.runtime_client.terminate_tree", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("launcher.runtime_client.uuid.uuid4", lambda: _FixedUUID("request-cleanup-fail"))

    with pytest.raises(BridgeError, match="Owned runtime cleanup is incomplete; restart is blocked"):
        client.request("runtime.start")

    with pytest.raises(BridgeError, match="Owned runtime cleanup is incomplete; restart is blocked"):
        client.request("runtime.start")

    assert client._proc is stale_process
    assert spawn_calls["count"] == 0
