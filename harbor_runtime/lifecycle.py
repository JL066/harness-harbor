"""Own only children launched through the fixed runtime executable contract."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from harbor_platform.commands import serialize_command
from harbor_platform.process import process_identity, settled_identity, spawn_owned, terminate_tree, is_alive, owner_identity_valid, descendants, RecoveredProcess, recover_owned
from harbor_platform.host import acquire_lock, read_line
from runtime_queue import queue_root_fingerprint
from . import RUNTIME_VERSION
from .config import runtime_command, valid_url
from .protocol import redact


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
        self.mcp_ready = False
        self.stopped = True
        self._telemetry = {"harnesses": [], "agy_models": []}
        self._telemetry_thread = None
        self._lock_file = None

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
        with path.open("w" if path.exists() and path.stat().st_size > 2_000_000 else "a") as out:
            os.chmod(path, 0o600)
            out.write(redact(text[:8192]))

    def _drain(self, stream, component):
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
                    self.log(component, "[oversized log line omitted]\n")
                    continue
                self.log(component, line.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            pass

    def spawn(self, name, argv, *, stdin=subprocess.DEVNULL, env=None):
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
        threading.Thread(target=self._drain, args=(proc.stderr, name if name != "mcp" else "runtime"), daemon=True).start()
        if name != "mcp":
            threading.Thread(target=self._drain, args=(proc.stdout, name), daemon=True).start()
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
        override = candidate["macos"].get("executables", {}).get("tunnel", "")
        tunnel_exe = resolve_executable("tunnel-client", override) if settings is not None else self.found.get("tunnel")
        checks = {"settings": True,
                  "credential_readable": present["tunnel"],
                  "tunnel_executable": bool(tunnel_exe),
                  "runtime": Path(runtime_command("mcp")[0]).is_file()}
        checks["profile_renderable"] = checks["settings"]
        return {"ok": all(checks.values()), "checks": checks,
                "message": "Configuration checks passed; remote connectivity is verified after Start." if all(checks.values()) else "Complete Connection settings and install tunnel-client."}

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
        self.stopped = False
        self.messages = {}
        try:
            self.paths.jobs_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
            mcp = self.processes.get("mcp")
            if mcp is None or mcp.poll() is not None or not self.mcp_ready:
                if mcp is not None and not terminate_tree(mcp):
                    raise RuntimeError("Previous MCP tree is still active")
                proc = self.spawn("mcp", runtime_command("mcp"), stdin=subprocess.PIPE)
                request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                    "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "harbor-lighthouse", "version": RUNTIME_VERSION}}}
                proc.stdin.write(json.dumps(request).encode() + b"\n")
                proc.stdin.flush()
                parsed = json.loads(read_line(proc.stdout))
                if parsed.get("id") != 1 or "result" not in parsed:
                    raise RuntimeError("MCP handshake failed")
                proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                proc.stdin.flush()
                self.mcp_ready = True
                threading.Thread(target=self._drain, args=(proc.stdout, "runtime"), daemon=True).start()
            daemon = self.processes.get("daemon")
            if daemon is None or daemon.poll() is not None:
                if daemon is not None and not terminate_tree(daemon):
                    raise RuntimeError("Previous daemon tree is still active")
                self.spawn("daemon", runtime_command("daemon"))
            if self.test_connection()["ok"]:
                tunnel = self.processes.get("tunnel")
                if tunnel is not None and tunnel.poll() is None:
                    return self.snapshot()
                if tunnel is not None and not terminate_tree(tunnel):
                    raise RuntimeError("Previous tunnel tree is still active")
                self.profile()
                from .config import child_environment
                env = child_environment(self.settings, tunnel=True)
                self.spawn("tunnel", [self.found["tunnel"], "run", "--profile-dir", str(self.paths.tunnel_state_dir()),
                           "--profile", self.settings["connection"]["profile_name"]], env=env)
            else:
                self.messages["tunnel"] = "Setup required: configure connection and Keychain credential."
        except Exception:
            self.stop()
            self.stopped = False
            self.messages["mcp"] = "Runtime launch failed; inspect safe logs and diagnostics."
        return self.snapshot()

    def stop(self):
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
        self.mcp_ready = False
        self.stopped = True
        self.messages = {}
        self.save_manifest()
        return self.snapshot()

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
            url = path.read_text().strip()
            from .health import probe_loopback
            return probe_loopback(url)[0]
        except Exception:
            return False

    def snapshot(self):
        components = {}
        for name in ("mcp", "daemon", "tunnel"):
            proc = self.processes.get(name)
            alive = proc is not None and proc.poll() is None
            state = "running" if alive else "stopped"
            if alive and (name == "daemon" or name == "mcp" and self.mcp_ready or name == "tunnel" and self.tunnel_healthy()):
                state = "healthy"
            if proc and not alive:
                state = "failed"
            if name in self.messages:
                state = "warning" if name == "tunnel" else "failed"
            components[name] = {"state": state, "message": self.messages.get(name, "")}
        states = {c["state"] for c in components.values()}
        overall = "stopped" if self.stopped else "healthy" if states == {"healthy"} else "failed" if "failed" in states else "partial"
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
