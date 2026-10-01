"""Serialized, bounded client for the shared runtime JSON-lines protocol."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import uuid

from harbor_runtime import PROTOCOL_VERSION, RUNTIME_VERSION
from harbor_runtime.protocol import MAX_MESSAGE, METHODS, redact, validate
from harbor_platform.process import spawn_owned, terminate_tree
from runtime_bootstrap import runtime_executable


class BridgeError(RuntimeError):
    pass


class BridgeClient:
    def __init__(self, *, executable=None, credential_store=None, timeout=35, popen_factory=None):
        self.executable = Path(executable) if executable is not None else None
        self.credential_store = credential_store
        self.timeout = timeout
        self.popen_factory = popen_factory
        self._closed = False
        self._lock = threading.RLock()
        self._proc = None
        self._responses = None

    def _environment(self):
        from launcher.credential_store import (CredentialStore, CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY,
                                               CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY)
        from launcher.user_settings import load_user_settings
        from harbor_runtime.config import validate_settings, requires_custom
        from harbor_platform.paths import PlatformPaths
        env = dict(os.environ)
        # Parent runtime ownership and old connection credentials are never reused.
        for key in ('TUNNEL_RUNTIME_KEY', 'HARBOR_CODEX_CUSTOM_API_KEY', 'CONTROL_PLANE_API_KEY',
                    'CONTROL_PLANE_TUNNEL_ID', 'TUNNEL_ID', 'HARBOR_PROCESS_REGISTRY',
                    'HARBOR_RUNTIME_INSTANCE_ID', 'PYTHONPATH', 'PYTHONHOME'):
            env.pop(key, None)
        env['HARBOR_RUNTIME_MODE'] = 'packaged'
        env.update(PlatformPaths(environ=env).environment())
        data = validate_settings(load_user_settings().to_dict())
        store = self.credential_store if self.credential_store is not None else CredentialStore()
        secrets = []
        pairs = [('TUNNEL_RUNTIME_KEY', CREDENTIAL_TARGET_TUNNEL_RUNTIME_KEY)]
        if requires_custom(data):
            pairs.append(('HARBOR_CODEX_CUSTOM_API_KEY', CREDENTIAL_TARGET_CODEX_CUSTOM_API_KEY))
        for key, reference in pairs:
            secret = store.read(reference)
            if secret:
                env[key] = secret
                secrets.append(secret)
        return env, secrets

    def _read(self, proc, responses):
        try:
            while True:
                line = proc.stdout.readline(MAX_MESSAGE + 1)
                if not line or len(line) > MAX_MESSAGE or not line.endswith(b'\n'):
                    responses.put_nowait(None)
                    return
                responses.put_nowait(line)
        except (OSError, ValueError, queue.Full):
            try:
                responses.put_nowait(None)
            except queue.Full:
                pass

    def _drain_errors(self, proc, log_path, secrets):
        try:
            dropping = False
            while True:
                line = proc.stderr.readline(8192)
                if not line:
                    return
                if len(line) == 8192 and not line.endswith(b'\n'):
                    dropping = True
                    continue
                text = '[oversized line omitted]\n' if dropping else line.decode('utf-8', errors='replace')
                dropping = False
                for secret in secrets:
                    text = text.replace(secret, '[REDACTED]')
                text = redact(text)
                with log_path.open('w' if log_path.exists() and log_path.stat().st_size > 2_000_000 else 'a', encoding='utf-8') as stream:
                    stream.write(text)
        except (OSError, ValueError):
            pass

    def _connect(self):
        if self._closed:
            raise BridgeError("Runtime client is closed.")
        if self._proc is not None:
            if self._proc.poll() is None:
                return
            self._abort()
        try:
            executable = self.executable or runtime_executable()
            if not executable.is_absolute() or not executable.is_file():
                raise BridgeError('Bundled runtime is missing. Repair the application bundle.')
            env, secrets = self._environment()
            # The sidecar validates the same immutable boundary independently.
            from harbor_platform.paths import PlatformPaths
            paths = PlatformPaths(environ=env)
            bundle = Path(env.get('HARBOR_BUNDLE_ROOT', str(executable.parent.parent))).resolve()
            for value in paths.environment().values():
                if Path(value).resolve().is_relative_to(bundle):
                    raise BridgeError('Runtime data must be outside the application bundle.')
            paths.logs_dir().mkdir(parents=True, exist_ok=True)
            paths.state_dir().mkdir(parents=True, exist_ok=True)
            proc = spawn_owned([str(executable), 'bridge'], popen_factory=self.popen_factory,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               cwd=str(paths.state_dir()), env=env, bufsize=0)
            self._proc = proc
            self._responses = queue.Queue(maxsize=8)
            threading.Thread(target=self._read, args=(proc, self._responses), daemon=True).start()
            threading.Thread(target=self._drain_errors,
                             args=(proc, paths.logs_dir() / 'bridge.log', secrets), daemon=True).start()
            greeting = self._exchange('hello', {})
            if (greeting.get('protocol_version') != PROTOCOL_VERSION or
                greeting.get('runtime_version') != RUNTIME_VERSION or
                not set(METHODS).issubset(set(greeting.get('capabilities', [])))):
                raise BridgeError('Launcher and bundled runtime versions are incompatible.')
        except Exception as exc:
            self._abort()
            if isinstance(exc, BridgeError):
                raise
            raise BridgeError('Bundled runtime could not connect. Check setup and safe diagnostics.') from None

    def _exchange(self, method, params):
        request = {'v': PROTOCOL_VERSION, 'id': str(uuid.uuid4()), 'method': method, 'params': params}
        validate(request)
        data = json.dumps(request, ensure_ascii=True).encode('utf-8') + b'\n'
        if len(data) > MAX_MESSAGE:
            raise BridgeError('Runtime request exceeds the protocol limit.')
        self._proc.stdin.write(data)
        self._proc.stdin.flush()
        try:
            line = self._responses.get(timeout=self.timeout)
        except queue.Empty:
            raise BridgeError('Runtime request timed out.') from None
        if line is None:
            raise BridgeError('Runtime connection closed or returned an invalid response.')
        try:
            response = json.loads(line)
            if (not isinstance(response, dict) or type(response.get('v')) is not int or
                response['v'] != PROTOCOL_VERSION or response.get('id') != request['id'] or
                type(response.get('ok')) is not bool):
                raise ValueError()
            expected = {'v', 'id', 'ok', 'result' if response['ok'] else 'error'}
            if set(response) != expected:
                raise ValueError()
            if not response['ok']:
                raise BridgeError('Runtime rejected the operation. Check setup and safe diagnostics.')
            if not isinstance(response['result'], dict):
                raise ValueError()
            return response['result']
        except (ValueError, TypeError, KeyError):
            raise BridgeError('Runtime returned an invalid protocol response.') from None

    def request(self, method, params=None):
        with self._lock:
            try:
                self._connect()
                return self._exchange(method, {} if params is None else params)
            except Exception as exc:
                self._abort()
                if isinstance(exc, BridgeError):
                    raise
                raise BridgeError('Runtime operation failed. Check safe diagnostics.') from None

    def _abort(self):
        proc = self._proc
        if proc is None:
            return
        if not terminate_tree(proc, grace=2):
            raise BridgeError('Owned runtime cleanup is incomplete; restart is blocked.')
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream:
                stream.close()
        self._proc = None
        self._responses = None

    def close(self):
        with self._lock:
            self._closed = True
            if self._proc is None:
                return
            try:
                if self._proc.poll() is None:
                    self._exchange('shutdown', {})
                    self._proc.wait(timeout=self.timeout)
            finally:
                self._abort()

    def restart(self):
        with self._lock:
            self.close()
            self._closed = False
            return self.request('runtime.start')
