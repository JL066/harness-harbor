"""Own only children launched through the fixed runtime executable contract."""
import json
from collections import deque
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

from harbor_platform.commands import serialize_command
from harbor_platform.process import process_identity, settled_identity, spawn_owned, terminate_tree, descendants, process_image_path, RecoveredProcess, recover_owned
from harbor_platform.host import acquire_lock
from runtime_queue import queue_root_fingerprint
from . import RUNTIME_VERSION
from .config import runtime_command
from .protocol import redact


TUNNEL_WATCHDOG_INTERVAL_SECONDS = 5.0
LOCAL_MCP_FAILURE_THRESHOLD = 3
LOCAL_MCP_FAILURE_WINDOW_SECONDS = 90.0
MCP_CHILD_MISSING_THRESHOLD = 2
MCP_CHILD_START_GRACE_SECONDS = 30.0
TUNNEL_RECOVERY_BASE_COOLDOWN_SECONDS = 30.0
TUNNEL_RECOVERY_MAX_COOLDOWN_SECONDS = 300.0
LOCAL_MCP_METHODS = frozenset({"tools/call", "initialize"})


def tunnel_health_route(url, route):
    """Resolve a local health base URL to one explicit tunnel-client route."""
    parsed = urlsplit(url)
    segments = [part for part in parsed.path.split("/") if part]
    if segments and segments[-1] in {"healthz", "readyz"}:
        segments.pop()
    path = "/" + "/".join((*segments, route))
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("x", encoding="utf-8") as out:
        os.chmod(temp, 0o600)
        json.dump(value, out, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)


def doctor(paths, found):
    from .protocol import hello
    return {**hello(), "executables": found,
            "paths": {"state": str(paths.state_dir()), "jobs": str(paths.jobs_dir()),
                      "control": str(paths.control_dir()), "logs": str(paths.logs_dir())},
            "queue_fingerprint": queue_root_fingerprint(paths.jobs_dir()),
            "checks": {"codex_executable": bool(found.get("codex")),
                       "tunnel_executable": bool(found.get("tunnel"))}}


class Runtime:
    def __init__(self, paths, settings, found):
        self.paths, self.settings, self.found = paths, settings, found
        self.processes = {}
        self.identities = {}
        self.messages = {}
        self.instance_id = uuid.uuid4().hex
        self.manifest = paths.state_dir() / "run/components.json"
        self.stopped = True
        self._telemetry = {"harnesses": [], "agy_models": []}
        self._telemetry_thread = None
        self._lock_file = None
        self._lifecycle_lock = threading.RLock()
        self._health_lock = threading.RLock()
        self._watchdog_stop = None
        self._watchdog_thread = None
        self._watchdog_generation = 0
        self._tunnel_generation = 0
        self._tunnel_mcp_child_state = "unknown"
        self._tunnel_mcp_seen = False
        self._tunnel_ready = False
        self._tunnel_started_at = None
        self._missing_mcp_child_observations = 0
        self._local_mcp_failures = deque()
        self._mcp_route_state = "stopped"
        self._mcp_recovery_requested = False
        self._mcp_recovery_reason = ""
        self._recovery_pending = False
        self._recovery_attempts = 0
        self._next_recovery_at = 0.0

    def acquire(self):
        self._lock_file = acquire_lock(self.manifest.parent / "bridge.lock")
        if self.manifest.exists():
            if self.manifest.stat().st_size > 65536:
                raise ValueError("Invalid component manifest")
            previous = json.loads(self.manifest.read_text())
            if previous.get("queue_fingerprint") != queue_root_fingerprint(self.paths.jobs_dir()):
                raise ValueError("Queue root changed; refusing recovery")
            for name, record in previous.get("components", {}).items():
                if name not in {"mcp", "daemon", "tunnel"}:
                    raise ValueError("Invalid component manifest")
                identity = record["identity"]
                recovered = recover_owned(identity)
                if recovered is not None:
                    self.processes[name] = recovered
                    self.identities[name] = identity
            self.recovery_instance = previous.get("instance_id")
            # Restart verified old children; preserve all queue/job/lease files.
            self.stop()

    def save_manifest(self):
        atomic_json(self.manifest, {"instance_id": self.instance_id,
                    "queue_fingerprint": queue_root_fingerprint(self.paths.jobs_dir()),
                    "components": {name: {"component": name, "identity": identity,
                                   "executable": runtime_command(name)[0] if name != "tunnel" else self.found.get("tunnel")}
                                   for name, identity in self.identities.items()}})

    def log(self, component, text):
        self.paths.logs_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.paths.logs_dir() / f"{component}.log"
        # ponytail: one bounded log generation; add rotation history if needed.
        with path.open("w" if path.exists() and path.stat().st_size > 2_000_000 else "a", encoding="utf-8") as out:
            os.chmod(path, 0o600)
            out.write(redact(text[:8192]))

    def _drain(self, stream, component, tunnel_generation=None):
        try:
            dropping = False
            while True:
                line = stream.readline(8192)
                if not line:
                    break
                if len(line) == 8192 and not line.endswith(b"\n"):
                    dropping = True
                    continue
                if dropping:
                    dropping = False
                    try:
                        self.log(component, "[oversized log line omitted]\n")
                    except (OSError, ValueError):
                        pass
                    continue
                text = line.decode("utf-8", errors="replace")
                try:
                    self.log(component, text)
                except (OSError, ValueError):
                    # A log sink failure must not stop draining the child pipe.
                    pass
                if component == "tunnel":
                    try:
                        self.observe_tunnel_log(text, tunnel_generation=tunnel_generation)
                    except Exception:
                        # A malformed log record must not interrupt the child pipe.
                        pass
        except (OSError, ValueError):
            pass

    def spawn(self, name, argv, *, stdin=subprocess.DEVNULL, env=None, tunnel_generation=None):
        if name == "tunnel":
            with self._health_lock:
                if tunnel_generation is None:
                    self._tunnel_generation += 1
                    tunnel_generation = self._tunnel_generation
                elif tunnel_generation != self._tunnel_generation:
                    raise RuntimeError("Tunnel generation was superseded before launch")
        child_env = dict(os.environ if env is None else env)
        child_env.update(self.paths.environment())
        child_env["HARBOR_RUNTIME_INSTANCE_ID"] = self.instance_id
        child_env["HARBOR_PROCESS_REGISTRY"] = str(self.paths.state_dir() / "run/processes")
        if name != "tunnel":
            child_env.pop("TUNNEL_RUNTIME_KEY", None)
        proc = spawn_owned(argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=child_env, cwd=str(Path(__file__).resolve().parents[1]),
                    bufsize=0)
        self.processes[name] = proc
        # ps start time/argv may settle just after exec; inspect after Popen exec handshake.
        identity = settled_identity(proc.pid)
        if identity is None:
            terminate_tree(proc)
            raise RuntimeError("Component exited during launch")
        self.identities[name] = identity
        self.save_manifest()
        threading.Thread(target=self._drain, args=(proc.stderr, name if name != "mcp" else "runtime", tunnel_generation), daemon=True).start()
        if name != "mcp":
            threading.Thread(target=self._drain, args=(proc.stdout, name, tunnel_generation), daemon=True).start()
        return proc

    def test_connection(self, settings=None, credentials=None):
        from .config import validate_settings
        from harbor_platform.commands import resolve_executable
        present = credentials if credentials is not None else {"tunnel": bool(os.environ.get("TUNNEL_RUNTIME_KEY")), "custom": bool(os.environ.get("HARBOR_CODEX_CUSTOM_API_KEY"))}
        try:
            candidate = validate_settings(settings if settings is not None else self.settings,
                                          credentials=present, require_connection=True)
        except Exception as exc:
            from launcher.user_settings import SettingsError
            if not isinstance(exc, (ValueError, SettingsError)):
                raise
            return {"ok": False, "checks": {"settings": False}, "message": str(exc)}
        namespace = "windows" if sys.platform == "win32" else "macos"
        override = candidate.get(namespace, {}).get("executables", {}).get("tunnel", "")
        tunnel_exe = resolve_executable("tunnel-client", override) if settings is not None else self.found.get("tunnel")
        checks = {"settings": True,
                  "credential_readable": present["tunnel"],
                  "tunnel_executable": bool(tunnel_exe),
                  "runtime": Path(runtime_command("mcp")[0]).is_file()}
        checks["profile_renderable"] = checks["settings"]
        return {"ok": all(checks.values()), "checks": checks,
                "message": "Configuration checks passed; remote connectivity is verified after Start." if all(checks.values()) else "Complete Connection settings and secure credential-store setup."}

    def profile(self):
        conn = self.settings["connection"]
        root = self.paths.tunnel_state_dir()
        value = {"admin_ui": {"open_browser": False}, "config_version": 1,
                 "control_plane": {"api_key": "env:TUNNEL_RUNTIME_KEY", "base_url": conn["base_url"], "tunnel_id": conn["tunnel_id"]},
                 "health": {"listen_addr": "127.0.0.1:0", "url_file": str(root / "health.url")},
                 "log": {"format": "json", "level": "info"},
                 "mcp": {"commands": [{"channel": "main", "command": serialize_command(runtime_command("mcp"))}]}}
        # Omitting log.file keeps raw tunnel logs in the parent's redacting pipe.
        atomic_json(root / (conn["profile_name"] + ".yaml"), value)

    def start(self):
        with self._lifecycle_lock:
            if self.stopped:
                with self._health_lock:
                    # Fence readers left over from the previous runtime before
                    # accepting any state for this start lifecycle.
                    self._tunnel_generation += 1
                    self._local_mcp_failures.clear()
                    self._mcp_route_state = "starting"
                    self._mcp_recovery_requested = False
                    self._recovery_pending = False
                    self._recovery_attempts = 0
                    self._next_recovery_at = 0.0
            self.stopped = False
            self.messages = {}
            try:
                self.paths.jobs_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
                # Older bridge manifests may still contain the duplicate readiness MCP.
                # Retire only that owned child; the tunnel launches the real MCP process.
                legacy_mcp = self.processes.get("mcp")
                if legacy_mcp is not None:
                    if not terminate_tree(legacy_mcp):
                        raise RuntimeError("Previous bridge MCP tree is still active")
                    for stream in (getattr(legacy_mcp, "stdin", None), getattr(legacy_mcp, "stdout", None), getattr(legacy_mcp, "stderr", None)):
                        if stream:
                            stream.close()
                    self.processes.pop("mcp", None)
                    self.identities.pop("mcp", None)
                    self.save_manifest()

                daemon = self.processes.get("daemon")
                if daemon is None or daemon.poll() is not None:
                    if daemon is not None and not terminate_tree(daemon):
                        raise RuntimeError("Previous daemon tree is still active")
                    self.spawn("daemon", runtime_command("daemon"))
                if self.test_connection()["ok"]:
                    tunnel = self.processes.get("tunnel")
                    if tunnel is not None and tunnel.poll() is None:
                        self._ensure_watchdog()
                        self.watchdog_tick(allow_recovery=False)
                        return self.snapshot()
                    if tunnel is not None:
                        if not terminate_tree(tunnel):
                            raise RuntimeError("Previous tunnel tree is still active")
                        self.processes.pop("tunnel", None)
                        self.identities.pop("tunnel", None)
                    self._launch_tunnel()
                    self._ensure_watchdog()
                    self.watchdog_tick(allow_recovery=False)
                else:
                    self.messages["tunnel"] = "Setup required: configure connection and Keychain credential."
            except Exception:
                self.stop()
                self.stopped = False
                self.messages["mcp"] = "Runtime launch failed; inspect safe logs and diagnostics."
            return self.snapshot()

    def stop(self):
        with self._lifecycle_lock:
            return self._stop()

    def _stop(self):
        # Retire the tunnel stream before closing its pipes. A buffered line can
        # still be delivered by its drain thread while process teardown runs.
        with self._health_lock:
            self._tunnel_generation += 1
        stop_event = self._watchdog_stop
        if stop_event is not None:
            stop_event.set()
        self._watchdog_generation += 1
        self._watchdog_stop = None
        self._watchdog_thread = None
        for name in ("tunnel", "daemon", "mcp"):
            proc = self.processes.get(name)
            if proc is None:
                continue
            if isinstance(proc, RecoveredProcess) and proc.poll() is None and process_identity(proc.pid) != proc.identity:
                raise RuntimeError("Component identity changed")
            if not terminate_tree(proc, grace=3 if name == "daemon" else 0.5):
                raise RuntimeError("Component process group has not exited")
            for stream in (getattr(proc, "stdin", None), getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
                if stream:
                    stream.close()
            self.processes.pop(name, None)
            self.identities.pop(name, None)
        self.stop_registered_children()
        self.stopped = True
        self.messages = {}
        with self._health_lock:
            self._tunnel_mcp_child_state = "unknown"
            self._tunnel_mcp_seen = False
            self._tunnel_ready = False
            self._tunnel_started_at = None
            self._missing_mcp_child_observations = 0
            self._local_mcp_failures.clear()
            self._mcp_route_state = "stopped"
            self._mcp_recovery_requested = False
            self._mcp_recovery_reason = ""
            self._recovery_pending = False
            self.save_manifest()
        return self.snapshot()

    def _begin_tunnel_generation(self):
        """Retire old health state before starting a new tunnel log stream."""
        with self._health_lock:
            self._tunnel_generation += 1
            generation = self._tunnel_generation
            self._local_mcp_failures.clear()
            self._mcp_route_state = "starting"
            self._mcp_recovery_requested = False
            self._mcp_recovery_reason = ""
            self._tunnel_mcp_child_state = "unknown"
            self._tunnel_mcp_seen = False
            self._tunnel_ready = False
            self._tunnel_started_at = time.monotonic()
            self._missing_mcp_child_observations = 0
            return generation

    def _launch_tunnel(self):
        self.profile()
        from .config import child_environment
        env = child_environment(self.settings, tunnel=True)
        generation = self._begin_tunnel_generation()
        proc = self.spawn("tunnel", [self.found["tunnel"], "run", "--profile-dir", str(self.paths.tunnel_state_dir()),
                           "--profile", self.settings["connection"]["profile_name"]], env=env,
                           tunnel_generation=generation)
        return proc

    def _ensure_watchdog(self):
        with self._lifecycle_lock:
            thread = self._watchdog_thread
            stop_event = self._watchdog_stop
            if (thread is not None and thread.is_alive()
                    and stop_event is not None and not stop_event.is_set()):
                return
            self._watchdog_generation += 1
            generation = self._watchdog_generation
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._watchdog_loop,
                args=(generation, stop_event),
                name=f"harbor-tunnel-watchdog-{generation}",
                daemon=True,
            )
            self._watchdog_stop = stop_event
            self._watchdog_thread = thread
            thread.start()

    def _watchdog_loop(self, generation, stop_event):
        while not stop_event.wait(TUNNEL_WATCHDOG_INTERVAL_SECONDS):
            # Holding the lifecycle lock across the ownership check and tick
            # prevents a stopped generation from observing a later start.
            with self._lifecycle_lock:
                if (stop_event.is_set() or generation != self._watchdog_generation
                        or self._watchdog_stop is not stop_event or self.stopped):
                    return
                try:
                    self.watchdog_tick()
                except Exception:
                    # A health observation must not terminate bridge service.
                    pass

    @staticmethod
    def _tunnel_log_record(line):
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def observe_tunnel_log(self, line, *, tunnel_generation=None, now=None):
        """Count confirmed local MCP transport failures from dispatcher records."""
        record = self._tunnel_log_record(line)
        if record is None:
            return
        message = record.get("msg") or record.get("message")
        method = record.get("rpc_method")
        now = time.monotonic() if now is None else now
        with self._health_lock:
            if tunnel_generation is not None and tunnel_generation != self._tunnel_generation:
                return
            # tunnel-client v0.0.12's forwarded and notification-ack records do
            # not prove a clean, correlated MCP protocol round-trip. Let failure
            # history age out through its bounded window instead of resetting it
            # on those ambiguous signals.
            if message == "dispatcher received MCP upstream error; posted error response to control plane":
                status = record.get("status_code")
                is_local_mcp_failure = (
                    record.get("component") == "dispatcher"
                    and method in LOCAL_MCP_METHODS
                    and type(status) is int and 500 <= status <= 599
                    and record.get("failure_source") == "client_internal"
                    and record.get("upstream_response_received") is False
                    and record.get("upstream_status") is None
                )
                if is_local_mcp_failure:
                    cutoff = now - LOCAL_MCP_FAILURE_WINDOW_SECONDS
                    while self._local_mcp_failures and self._local_mcp_failures[0] < cutoff:
                        self._local_mcp_failures.popleft()
                    self._local_mcp_failures.append(now)
                    count = len(self._local_mcp_failures)
                    self._mcp_route_state = "failed" if count >= LOCAL_MCP_FAILURE_THRESHOLD else "warning"
                    self.messages["mcp"] = (
                        f"Tunnel-owned MCP reported a local failure ({count}/{LOCAL_MCP_FAILURE_THRESHOLD})."
                    )
                    if count >= LOCAL_MCP_FAILURE_THRESHOLD:
                        self._mcp_recovery_requested = True
                        self._mcp_recovery_reason = "repeated local MCP upstream failures"
                return

    @staticmethod
    def _same_image_path(actual, expected):
        if not actual or not expected:
            return False
        actual = str(actual).replace("\\\\?\\", "")
        expected = str(expected).replace("\\\\?\\", "")
        return os.path.normcase(os.path.normpath(actual)) == os.path.normcase(os.path.normpath(expected))

    def tunnel_mcp_child_status(self, tunnel=None):
        """Check the expected MCP executable inside the retained tunnel-owned tree."""
        tunnel = self.processes.get("tunnel") if tunnel is None else tunnel
        if tunnel is None or tunnel.poll() is not None:
            return "missing"
        identity = self.identities.get("tunnel") or getattr(tunnel, "_harbor_identity", None) or getattr(tunnel, "identity", None)
        members = descendants(identity)
        if members is None or tunnel.pid not in members:
            return "unknown"
        expected = runtime_command("mcp")[0]
        unverified_member = False
        for pid in members:
            if pid == tunnel.pid:
                continue
            image = process_image_path(pid)
            if image is None:
                unverified_member = True
                continue
            if self._same_image_path(image, expected):
                return "alive"
        return "unknown" if unverified_member else "missing"

    def tunnel_ready(self):
        path = self.paths.tunnel_state_dir() / "health.url"
        try:
            if not path.exists() or path.stat().st_size > 2048:
                return False
            url = path.read_text(encoding="utf-8").strip()
            from .health import probe_loopback
            return probe_loopback(tunnel_health_route(url, "readyz"))[0]
        except (OSError, ValueError):
            return False

    def _queue_tunnel_recovery(self, reason):
        with self._health_lock:
            self._mcp_recovery_requested = True
            self._mcp_recovery_reason = reason
            self._mcp_route_state = "failed"

    def _recovery_cooldown(self, attempt):
        delay = TUNNEL_RECOVERY_BASE_COOLDOWN_SECONDS * (2 ** max(0, attempt - 1))
        return min(delay, TUNNEL_RECOVERY_MAX_COOLDOWN_SECONDS)

    def watchdog_tick(self, *, now=None, allow_recovery=True):
        """Observe the owned tunnel path, then perform a tunnel-only repair if due."""
        now = time.monotonic() if now is None else now
        with self._lifecycle_lock:
            if self.stopped:
                return False
            tunnel = self.processes.get("tunnel")
            if tunnel is None:
                with self._health_lock:
                    requested = self._mcp_recovery_requested
                    due = now >= self._next_recovery_at
                    reason = self._mcp_recovery_reason
                if allow_recovery and requested and due:
                    return self._recover_tunnel_only(reason or "tunnel-client is unavailable", now=now)
                return False
            if tunnel.poll() is not None:
                self._queue_tunnel_recovery("tunnel-client process exited")
            else:
                child_state = self.tunnel_mcp_child_status(tunnel)
                ready = self.tunnel_ready()
                with self._health_lock:
                    cutoff = now - LOCAL_MCP_FAILURE_WINDOW_SECONDS
                    while self._local_mcp_failures and self._local_mcp_failures[0] < cutoff:
                        self._local_mcp_failures.popleft()
                    if not self._local_mcp_failures and self._mcp_route_state == "warning":
                        self._mcp_route_state = "unverified" if child_state == "alive" else "starting"
                        if self.messages.get("mcp", "").startswith("Tunnel-owned MCP reported a local failure"):
                            self.messages.pop("mcp", None)
                    self._tunnel_mcp_child_state = child_state
                    self._tunnel_ready = ready
                    if child_state == "alive":
                        self._tunnel_mcp_seen = True
                        self._missing_mcp_child_observations = 0
                        self._tunnel_started_at = None
                        if self.messages.get("mcp") == "Tunnel-owned MCP process is missing.":
                            self.messages.pop("mcp", None)
                        if self._mcp_route_state not in {"warning", "failed"}:
                            self._mcp_route_state = "unverified"
                    elif child_state == "missing":
                        if self._tunnel_mcp_seen:
                            self._missing_mcp_child_observations += 1
                        else:
                            if self._tunnel_started_at is None:
                                self._tunnel_started_at = now
                            startup_grace_elapsed = now - self._tunnel_started_at >= MCP_CHILD_START_GRACE_SECONDS
                            if startup_grace_elapsed:
                                self._missing_mcp_child_observations = MCP_CHILD_MISSING_THRESHOLD
                                self._mcp_recovery_requested = True
                                self._mcp_recovery_reason = "tunnel-owned MCP process did not appear before startup grace"
                                self._mcp_route_state = "failed"
                        if self._missing_mcp_child_observations:
                            self.messages["mcp"] = "Tunnel-owned MCP process is missing."
                        if self._missing_mcp_child_observations >= MCP_CHILD_MISSING_THRESHOLD:
                            self._mcp_recovery_requested = True
                            if self._tunnel_mcp_seen:
                                self._mcp_recovery_reason = "tunnel-owned MCP process disappeared"
                            self._mcp_route_state = "failed"
                    else:
                        self._missing_mcp_child_observations = 0
                        if self._mcp_route_state not in {"warning", "failed"}:
                            self._mcp_route_state = "starting"

            with self._health_lock:
                requested = self._mcp_recovery_requested
                due = now >= self._next_recovery_at
                reason = self._mcp_recovery_reason
            if allow_recovery and requested and due:
                return self._recover_tunnel_only(reason, now=now)
            return False

    def _recover_tunnel_only(self, reason, *, now=None):
        """Recycle only tunnel-client's retained owned tree; never stop daemon/workers."""
        now = time.monotonic() if now is None else now
        with self._lifecycle_lock:
            if self.stopped:
                return False
            with self._health_lock:
                # Old-generation readers are stale as soon as recovery begins,
                # including while the owned tree is being terminated.
                self._tunnel_generation += 1
                self._recovery_pending = True
                self._mcp_route_state = "failed"
            try:
                if not self.test_connection()["ok"]:
                    raise RuntimeError("Tunnel setup is no longer valid")
                old_tunnel = self.processes.get("tunnel")
                if old_tunnel is not None:
                    if not terminate_tree(old_tunnel):
                        raise RuntimeError("Owned tunnel process tree has not exited")
                    for stream in (getattr(old_tunnel, "stdin", None), getattr(old_tunnel, "stdout", None), getattr(old_tunnel, "stderr", None)):
                        if stream:
                            stream.close()
                    self.processes.pop("tunnel", None)
                    self.identities.pop("tunnel", None)
                    self.save_manifest()
                self.messages.pop("tunnel", None)
                self.messages.pop("mcp", None)
                self._launch_tunnel()
                with self._health_lock:
                    self._recovery_pending = False
                    self._recovery_attempts += 1
                    self._next_recovery_at = now + self._recovery_cooldown(self._recovery_attempts)
                self.log("runtime", f"Tunnel-only recovery started: {reason}.\n")
                return True
            except Exception:
                with self._health_lock:
                    self._recovery_pending = False
                    self._mcp_route_state = "failed"
                    self._mcp_recovery_requested = True
                    self._mcp_recovery_reason = self._mcp_recovery_reason or reason
                    self._recovery_attempts += 1
                    self._next_recovery_at = now + self._recovery_cooldown(self._recovery_attempts)
                self.messages["tunnel"] = "Tunnel-only recovery failed; inspect safe diagnostics."
                self.messages["mcp"] = "Tunnel-owned MCP path remains unavailable."
                return False

    def stop_registered_children(self):
        # Workers/CLI sessions can outlive a crashed daemon. Each registration
        # was made at spawn, never inferred from a process name or PID alone.
        root = self.paths.state_dir() / "run/processes"
        records = sorted(root.glob("*.json"))
        if len(records) > 4096:
            raise RuntimeError("Process registry exceeds recovery bound")
        owned_instances = {self.instance_id, getattr(self, "recovery_instance", None)} - {None}
        for path in records:
            if path.stat().st_size > 4096:
                raise RuntimeError("Invalid process registration")
            record = json.loads(path.read_text())
            if record.get("instance_id") not in owned_instances:
                continue
            identity = record["identity"]
            proc = recover_owned(identity)
            if proc is not None:
                proc._harbor_registry = path
                if not terminate_tree(proc, grace=1):
                    raise RuntimeError("Worker group has not exited")
            else:
                path.unlink(missing_ok=True)

    def refresh_telemetry(self):
        if self._telemetry_thread and self._telemetry_thread.is_alive():
            return
        def refresh():
            try:
                from control_plane import harness_telemetry_snapshot
                from launcher.harnesses import list_harnesses, agy_models
                snapshot = harness_telemetry_snapshot()
                self._telemetry = {"harnesses": [{"name": row["name"], "available": row["available"],
                    "status": row["status"], "summary": row["detail"]} for row in list_harnesses(telemetry_snapshot=snapshot)],
                    "agy_models": agy_models(telemetry_snapshot=snapshot)}
            except Exception:
                self._telemetry = {"harnesses": [{"name": n, "available": False, "status": "Unavailable", "summary": "Core probe failed"}
                                  for n in ("codex", "agy", "minimax")], "agy_models": []}
        self._telemetry_thread = threading.Thread(target=refresh, daemon=True)
        self._telemetry_thread.start()

    def tunnel_healthy(self):
        path = self.paths.tunnel_state_dir() / "health.url"
        try:
            if not path.exists() or path.stat().st_size > 2048:
                return False
            url = path.read_text(encoding="utf-8").strip()
            from .health import probe_loopback
            return probe_loopback(tunnel_health_route(url, "healthz"))[0]
        except (OSError, ValueError):
            return False

    def snapshot(self):
        components = {}
        daemon = self.processes.get("daemon")
        daemon_alive = daemon is not None and daemon.poll() is None
        components["daemon"] = {"state": "healthy" if daemon_alive else "failed" if daemon else "stopped",
                                "message": self.messages.get("daemon", "")}

        tunnel = self.processes.get("tunnel")
        tunnel_alive = tunnel is not None and tunnel.poll() is None
        if tunnel_alive:
            tunnel_state = "healthy" if self.tunnel_healthy() else "running"
        else:
            tunnel_state = "failed" if tunnel is not None else "stopped"
        if "tunnel" in self.messages:
            tunnel_state = "warning"
        components["tunnel"] = {"state": tunnel_state, "message": self.messages.get("tunnel", "")}

        with self._health_lock:
            mcp_message = self.messages.get("mcp", "")
            if self.stopped:
                mcp_state = "stopped"
            elif self._recovery_pending:
                mcp_state = "restarting"
                mcp_message = mcp_message or "Repairing the tunnel-owned MCP path."
            elif self._mcp_route_state == "failed" or self._missing_mcp_child_observations >= MCP_CHILD_MISSING_THRESHOLD:
                mcp_state = "failed"
            elif self._mcp_route_state == "warning":
                mcp_state = "warning"
            elif not tunnel_alive:
                mcp_state = "failed" if tunnel is not None else "warning" if "tunnel" in self.messages else "stopped"
            elif self._tunnel_mcp_child_state == "alive" and self._tunnel_ready:
                mcp_state = "running"
                mcp_message = mcp_message or "Tunnel-owned MCP child is present; MCP protocol health remains unverified."
            elif self._tunnel_mcp_child_state == "alive":
                mcp_state = "starting"
                mcp_message = mcp_message or "Tunnel-owned MCP child is present; tunnel readiness and MCP protocol health remain unverified."
            elif self._tunnel_mcp_child_state == "unknown":
                mcp_state = "starting"
                mcp_message = mcp_message or "Tunnel-owned MCP child identity or liveness could not be verified."
            elif self._missing_mcp_child_observations:
                mcp_state = "warning"
                mcp_message = mcp_message or "Tunnel-owned MCP process is missing; confirming before repair."
            else:
                mcp_state = "starting"
                mcp_message = mcp_message or "Waiting for the tunnel-owned MCP child."
        components["mcp"] = {"state": mcp_state, "message": mcp_message}
        states = {c["state"] for c in components.values()}
        if self.stopped:
            overall = "stopped"
        elif "failed" in states:
            overall = "failed"
        elif states == {"healthy"}:
            overall = "healthy"
        elif (
            components["tunnel"]["state"] == "healthy"
            and components["mcp"]["state"] == "running"
            and components["daemon"]["state"] == "healthy"
        ):
            # Child presence and tunnel readiness establish operational status,
            # but do not prove an MCP protocol round trip.
            overall = "running"
        else:
            overall = "partial"
        return {"state": overall, "components": components, "runtime_version": RUNTIME_VERSION,
                "harnesses": self.harness_activity(connected=not self.stopped and components["mcp"]["state"] in {"healthy", "running"}), "paths": doctor(self.paths, self.found)["paths"],
                "queue_fingerprint": queue_root_fingerprint(self.paths.jobs_dir()),
                "setup_required": not self.test_connection()["ok"]}

    def harness_activity(self, *, connected=True):
        # Refresh inexpensive queue data on every dashboard tick, independently
        # of capability/quota probes, which may take seconds or be unavailable.
        from .snapshot import harness_snapshot
        return harness_snapshot(self.paths.jobs_dir(), self._telemetry["harnesses"], connected=connected)

    def logs_tail(self, component, lines=100):
        path = self.paths.logs_dir() / f"{component}.log"
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 32768))
                text = stream.read(32768).decode("utf-8", errors="replace")
        except FileNotFoundError:
            text = ""
        return {"text": redact("\n".join(text.splitlines()[-lines:]))}
