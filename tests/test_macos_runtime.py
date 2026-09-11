import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from harbor_platform.commands import serialize_command, resolve_executable
from harbor_platform.paths import PlatformPaths
from harbor_platform.process import track, terminate_tree, process_identity, settled_identity, is_alive
from harbor_runtime.protocol import validate, encode, MAX_MESSAGE
from runtime_queue import resolve_queue_root

ROOT = Path(__file__).resolve().parents[1]


class ProtocolTests(unittest.TestCase):
    def test_dashboard_job_ids_refresh_without_capability_probe(self):
        from harbor_runtime.lifecycle import Runtime
        from harbor_runtime.config import parse_settings
        with tempfile.TemporaryDirectory() as tmp:
            paths = PlatformPaths(ROOT, {"HARBOR_STATE_DIR": tmp})
            runtime = Runtime(paths, parse_settings({}), {})
            runtime._telemetry = {"harnesses": [{"name": name, "summary": "telemetry stale"}
                                                for name in ("codex", "minimax", "agy")]}
            for job_id, status in (("a" * 32, "running"), ("b" * 32, "running"), ("c" * 32, "queued")):
                p = paths.jobs_dir() / job_id / "status.json"
                p.parent.mkdir(parents=True)
                p.write_text(json.dumps({"harness": "codex", "status": status}))
            rows = runtime.harness_activity()
            self.assertEqual(rows[0]["running_job_ids"], ["a" * 32, "b" * 32])
            self.assertEqual(rows[0]["running_jobs"], 2)
            self.assertTrue(rows[0]["activity_fresh"])
            self.assertEqual(rows[1]["running_job_ids"], [])
            for p in paths.jobs_dir().glob("*/status.json"):
                p.write_text('{"harness":"codex","status":"completed"}')
            self.assertEqual(runtime.harness_activity()[0]["running_job_ids"], [])
            p.write_text('broken')
            self.assertFalse(runtime.harness_activity()[0]["activity_fresh"])

    def test_official_tunnel_default_preserves_custom_url(self):
        from harbor_runtime.config import parse_settings
        for connection in ({}, {"base_url": ""}, {"base_url": "  "}):
            self.assertEqual(parse_settings({"connection": connection})["connection"]["base_url"], "https://api.openai.com")
        self.assertEqual(parse_settings({"connection": {"base_url": "https://tunnel.example.test"}})["connection"]["base_url"], "https://tunnel.example.test")

    def test_minimax_is_mcode_and_discovery_is_fresh(self):
        from harbor_runtime.config import EXE_KEYS, EXE_NAMES, parse_settings, discover_executables, configure
        old = {"macos": {"executables": {"minimax": "/Applications/MiniMax.app/Contents/MacOS/MiniMax", "mcode": "/opt/bin/mcode"}}}
        settings = parse_settings(old)
        self.assertEqual(settings["macos"]["executables"], {"minimax": "/opt/bin/mcode"})
        self.assertIn("mcode", old["macos"]["executables"])  # read never changes the input
        self.assertEqual(EXE_KEYS["minimax"], "HARBOR_MINIMAX_CLI_EXE")
        self.assertEqual(EXE_NAMES["minimax"], "mcode")
        with patch("harbor_runtime.config.resolve_executable", side_effect=lambda name: "/opt/bin/" + name):
            found = discover_executables()
        self.assertEqual(set(found), {"codex", "agy", "minimax", "tunnel"})
        self.assertEqual(found["minimax"], "/opt/bin/mcode")
        self.assertEqual(found["tunnel"], "/opt/bin/tunnel-client")
        with patch.dict(os.environ, {"HARBOR_MINIMAX_CLI_EXE": "/Applications/MiniMax.app/Contents/MacOS/MiniMax"}), patch("harbor_runtime.config.load_settings", return_value=parse_settings({})):
            with self.assertRaisesRegex(ValueError, "mcode CLI"):
                configure()

    def test_allowlist_schema_and_bounds(self):
        for method, params in [("shell.exec", {}), ("runtime.start", {"argv": ["bad"]}),
                               ("logs.tail", {"component": "../settings"}),
                               ("logs.tail", {"component": "runtime", "lines": True}),
                               ("tunnel.test", {"settings": {}, "credentials": {"tunnel": "secret"}}),
                               ("tunnel.test", {"settings": {}, "credentials": {"custom": True, "key": "secret"}})]:
            with self.assertRaises(ValueError):
                validate({"v": 1, "id": "test", "method": method, "params": params})
        with self.assertRaisesRegex(ValueError, "incompatible_protocol"):
            validate({"v": 2, "id": "test", "method": "hello", "params": {}})
        with patch.dict(os.environ, {"TUNNEL_RUNTIME_KEY": 'secret-"-12345'}):
            data = json.loads(encode({"id": "x", "result": 'secret-"-12345'}))
            self.assertEqual(data["result"], "[REDACTED]")
        self.assertLess(len(encode({"result": "x" * MAX_MESSAGE})), MAX_MESSAGE)
        validate({"v": 1, "id": "draft", "method": "tunnel.test",
                  "params": {"settings": {}, "credentials": {"tunnel": True, "custom": False}}})

    def test_paths_and_posix_quoting(self):
        paths = PlatformPaths(ROOT, {"HARBOR_STATE_DIR": str(ROOT / "build/state")}, "darwin")
        self.assertEqual(paths.jobs_dir(), ROOT / "build/state/jobs")
        self.assertEqual(resolve_queue_root(ROOT, paths.environment()).path, paths.jobs_dir())
        self.assertNotIn("Resources", str(paths.application_support_dir()))
        with self.assertRaises(ValueError):
            PlatformPaths(ROOT, {"HARBOR_STATE_DIR": "relative"}).state_dir()
        if sys.platform != "win32":
            argv = ["/Applications/Harness Harbor.app/runtime", "mcp", "a'b;$x"]
            self.assertEqual(shlex.split(serialize_command(argv)), argv)
        self.assertIsNone(resolve_executable("codex", str(ROOT / "missing-cli")))


@unittest.skipIf(sys.platform == "win32", "POSIX runtime host")
class PosixTests(unittest.TestCase):
    def test_recovery_checks_identity_and_reclaims_registered_worker(self):
        from harbor_runtime.lifecycle import Runtime, atomic_json
        from harbor_runtime.config import load_settings
        (ROOT / "build").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as temp:
            paths = PlatformPaths(ROOT, {"HARBOR_STATE_DIR": temp + "/state", "HARBOR_USER_SETTINGS_DIR": temp})
            runtime = Runtime(paths, load_settings(paths), {})
            proc = track(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True))
            try:
                identity = settled_identity(proc.pid)
                record_path = paths.state_dir() / "run/processes" / f"{proc.pid}.json"
                atomic_json(record_path, {"instance_id": runtime.instance_id,
                    "identity": {**identity, "started_at": "wrong start time"}})
                with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                    runtime.stop_registered_children()
                self.assertIsNone(proc.poll())
                atomic_json(record_path, {"instance_id": runtime.instance_id, "identity": identity})
                runtime.stop_registered_children()
                proc.wait(timeout=2)
                self.assertFalse(record_path.exists())
            finally:
                terminate_tree(proc)

    def test_resistant_three_generation_group_and_unrelated_survive(self):
        code = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(60)"
        parent = ("import subprocess,sys,time,signal; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                  "p=subprocess.Popen([sys.executable,'-c'," + repr(
                  "import subprocess,sys,time,signal; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                  "p=subprocess.Popen([sys.executable,'-c'," + repr(code) + "]); print(p.pid,flush=True); time.sleep(60)") +
                  "]); print(p.pid,flush=True); time.sleep(60)")
        proc = track(subprocess.Popen([sys.executable, "-c", parent], start_new_session=True, stdout=subprocess.PIPE, text=True))
        other = track(subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True))
        try:
            rows = [proc.stdout.readline().strip() for _ in range(3)]
            children = [int(v) for v in rows if v.isdigit()]
            self.assertEqual(len(children), 2)
            identity = process_identity(proc.pid)
            self.assertEqual(identity["pid"], identity["pgid"])
            self.assertTrue(terminate_tree(proc, grace=0.5))
            self.assertTrue(all(not is_alive(pid) for pid in children))
            self.assertIsNone(other.poll())
        finally:
            terminate_tree(proc)
            terminate_tree(other)
            proc.stdout.close()

    def test_real_bridge_mcp_restart_and_persistence(self):
        (ROOT / "build").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "build") as temp:
            paths = PlatformPaths(ROOT, {"HARBOR_STATE_DIR": temp + "/state", "HARBOR_USER_SETTINGS_DIR": temp,
                                        "HARBOR_LOG_DIR": temp + "/logs", "HARBOR_TUNNEL_PROFILE_DIR": temp + "/tunnel"})
            env = {k: v for k, v in os.environ.items() if not k.startswith("HARBOR_") and k != "TUNNEL_RUNTIME_KEY"}
            env.update(paths.environment())
            for key in ("HARBOR_CODEX_EXE", "HARBOR_AGY_EXE", "HARBOR_MINIMAX_CLI_EXE", "HARBOR_TUNNEL_EXE"):
                env[key] = temp + ("/uninstalled/mcode" if key == "HARBOR_MINIMAX_CLI_EXE" else "/uninstalled")
            proc = subprocess.Popen([sys.executable, "-m", "harbor_runtime", "bridge"], cwd=ROOT,
                                    env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            def call(method, params=None, version=1):
                proc.stdin.write(json.dumps({"v": version, "id": "test", "method": method, "params": params or {}}).encode() + b"\n")
                proc.stdin.flush()
                import select
                self.assertTrue(select.select([proc.stdout], [], [], 25)[0], "bridge timed out")
                raw = proc.stdout.readline()
                self.assertTrue(raw, "bridge exited unexpectedly")
                return json.loads(raw)
            try:
                self.assertFalse(call("runtime.start")["ok"])
                self.assertEqual(call("hello")["result"]["protocol_version"], 1)
                started = call("runtime.start")
                self.assertTrue(started["ok"], started)
                self.assertEqual(started["result"]["components"]["mcp"]["state"], "healthy", started)
                # A terminal fixture survives runtime restart; never dispatch real work here.
                job = paths.jobs_dir() / "completed-fixture"
                job.mkdir()
                state = '{"status":"completed","harness":"codex"}'
                (job / "status.json").write_text(state)
                self.assertTrue(call("runtime.restart")["ok"])
                self.assertEqual((job / "status.json").read_text(), state)
                manifest = json.loads((paths.state_dir() / "run/components.json").read_text())
                pids = [r["identity"]["pid"] for r in manifest["components"].values()]
                self.assertFalse(call("hello", version=99)["ok"])
                self.assertFalse(call("runtime.start")["ok"])
                self.assertTrue(call("hello")["ok"])
                self.assertTrue(call("shutdown")["ok"])
                proc.wait(timeout=10)
                self.assertTrue(all(not is_alive(pid) for pid in pids))
            finally:
                proc.stdin.close()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    proc.wait(timeout=10)
                proc.stdout.close()
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
