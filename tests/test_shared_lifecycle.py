import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from harbor_platform.paths import PlatformPaths
from harbor_runtime.config import parse_settings
from harbor_runtime.lifecycle import Runtime


class LifecycleTests(unittest.TestCase):
    @staticmethod
    def _process(pid):
        process = Mock()
        process.pid = pid
        process.poll.return_value = None
        process.stdin = process.stdout = process.stderr = None
        return process

    @staticmethod
    def _local_mcp_failure(method="tools/call"):
        return json.dumps({
            "level": "WARN",
            "msg": "dispatcher received MCP upstream error; posted error response to control plane",
            "component": "dispatcher",
            "rpc_method": method,
            "status_code": 502,
            "failure_source": "client_internal",
            "upstream_response_received": False,
        })

    def test_stopped_runtime_clears_queue_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp})
            job = paths.jobs_dir() / "fixture" / "status.json"
            job.parent.mkdir(parents=True)
            job.write_text('{"status":"running","harness":"codex"}')
            runtime = Runtime(paths, parse_settings({}), {})
            snapshot = runtime.snapshot()
            self.assertEqual(snapshot["harnesses"][0]["running_job_ids"], [])
            self.assertEqual(snapshot["harnesses"][0]["activity_state"], "disconnected")

    def test_partial_start_repairs_only_missing_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp}), parse_settings({}), {})
            spawned = []
            def spawn(name, argv, **kwargs):
                spawned.append(name)
                proc = Mock()
                proc.poll.return_value = None
                runtime.processes[name] = proc
                return proc

            with patch.object(runtime, "spawn", spawn), patch.object(runtime, "test_connection", return_value={"ok": False}):
                first = runtime.start()
                runtime.start()
            self.assertEqual(spawned, ["daemon"])
            self.assertNotIn("mcp", spawned)
            self.assertNotEqual(first["components"]["mcp"]["state"], "healthy")
            self.assertEqual(first["state"], "partial")

    def test_live_tunnel_missing_its_mcp_child_is_detected_unhealthy(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp}), parse_settings({}), {})
            tunnel = self._process(200)
            runtime.processes["tunnel"] = tunnel
            runtime.identities["tunnel"] = {"pid": 200, "started_at": "1", "job_name": "owned"}
            runtime.stopped = False
            runtime._tunnel_mcp_seen = True
            runtime._tunnel_mcp_child_state = "alive"
            runtime._tunnel_ready = True
            runtime._mcp_route_state = "healthy"

            with patch("harbor_runtime.lifecycle.descendants", return_value=[200]), \
                 patch.object(runtime, "tunnel_ready", return_value=True):
                self.assertFalse(runtime.watchdog_tick(now=10, allow_recovery=False))
                self.assertEqual(runtime.snapshot()["components"]["mcp"]["state"], "warning")
                self.assertFalse(runtime.watchdog_tick(now=15, allow_recovery=False))

            self.assertEqual(runtime.snapshot()["components"]["mcp"]["state"], "failed")
            self.assertTrue(runtime._mcp_recovery_requested)

    def test_local_mcp_failure_threshold_triggers_tunnel_only_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = PlatformPaths(root, {"HARBOR_STATE_DIR": tmp})
            runtime = Runtime(paths, parse_settings({}), {"tunnel": "tunnel-client.exe"})
            old_tunnel = self._process(210)
            daemon = self._process(211)
            runtime.processes.update(tunnel=old_tunnel, daemon=daemon)
            runtime.identities.update(tunnel={"pid": 210}, daemon={"pid": 211})
            runtime.stopped = False
            replacement = self._process(212)

            def launch_tunnel():
                runtime.processes["tunnel"] = replacement
                runtime.identities["tunnel"] = {"pid": replacement.pid}
                return replacement

            with patch.object(runtime, "test_connection", return_value={"ok": True}), \
                 patch.object(runtime, "_launch_tunnel", side_effect=launch_tunnel), \
                 patch.object(runtime, "tunnel_mcp_child_status", return_value="unknown"), \
                 patch.object(runtime, "tunnel_ready", return_value=False), \
                 patch("harbor_runtime.lifecycle.terminate_tree", return_value=True) as terminate:
                for index, method in enumerate(("tools/call", "initialize", "tools/call")):
                    runtime.observe_tunnel_log(self._local_mcp_failure(method), now=1 + index)
                self.assertEqual(len(runtime._local_mcp_failures), 3)
                self.assertTrue(runtime.watchdog_tick(now=3))

            self.assertIs(runtime.processes["tunnel"], replacement)
            self.assertIs(runtime.processes["daemon"], daemon)
            terminate.assert_called_once_with(old_tunnel)

    def test_one_local_mcp_failure_does_not_restart_tunnel(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp}), parse_settings({}), {})
            tunnel = self._process(220)
            runtime.processes["tunnel"] = tunnel
            runtime.identities["tunnel"] = {"pid": 220}
            runtime.stopped = False
            runtime.observe_tunnel_log(self._local_mcp_failure(), now=1)

            with patch.object(runtime, "tunnel_mcp_child_status", return_value="unknown"), \
                 patch.object(runtime, "tunnel_ready", return_value=False), \
                 patch.object(runtime, "_recover_tunnel_only") as recover:
                self.assertFalse(runtime.watchdog_tick(now=2))
                recover.assert_not_called()

            self.assertEqual(len(runtime._local_mcp_failures), 1)

    def test_ambiguous_forward_and_notification_ack_do_not_clear_failure_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp}), parse_settings({}), {})
            runtime.observe_tunnel_log(self._local_mcp_failure(), now=1)
            runtime.observe_tunnel_log(self._local_mcp_failure("initialize"), now=2)

            runtime.observe_tunnel_log(json.dumps({
                "component": "dispatcher",
                "msg": "dispatcher forwarded command to MCP server",
                "rpc_method": "tools/call",
            }), now=3)
            runtime.observe_tunnel_log(json.dumps({
                "component": "dispatcher",
                "msg": "dispatcher acknowledged notification with control plane",
                "rpc_method": "notifications/initialized",
            }), now=4)

            self.assertEqual(list(runtime._local_mcp_failures), [1, 2])
            self.assertFalse(runtime._mcp_recovery_requested)

            runtime.observe_tunnel_log(self._local_mcp_failure(), now=5)

            self.assertEqual(list(runtime._local_mcp_failures), [1, 2, 5])
            self.assertTrue(runtime._mcp_recovery_requested)

    def test_control_plane_proxy_failure_does_not_count_as_local_mcp_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp}), parse_settings({}), {})
            poll_failures = (
                {"msg": "control-plane poll failed", "component": "control-plane"},
                {"msg": "dispatcher received MCP upstream error; posted error response to control plane",
                 "component": "proxy"},
            )
            for index, fields in enumerate(poll_failures, start=1):
                runtime.observe_tunnel_log(json.dumps({
                    "level": "WARN", **fields,
                    "rpc_method": "tools/call", "status_code": 502,
                    "failure_source": "client_internal", "upstream_response_received": False,
                }), now=index)

            self.assertEqual(len(runtime._local_mcp_failures), 0)
            self.assertFalse(runtime._mcp_recovery_requested)

    def test_missing_mcp_child_at_startup_recovers_after_bounded_grace(self):
        from harbor_runtime.lifecycle import MCP_CHILD_START_GRACE_SECONDS
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp}), parse_settings({}), {})
            runtime.processes["tunnel"] = self._process(225)
            runtime.identities["tunnel"] = {"pid": 225}
            runtime.stopped = False
            runtime._tunnel_started_at = 100

            with patch.object(runtime, "tunnel_mcp_child_status", return_value="missing"), \
                 patch.object(runtime, "tunnel_ready", return_value=False), \
                 patch.object(runtime, "_recover_tunnel_only", return_value=True) as recover:
                self.assertFalse(runtime.watchdog_tick(now=99 + MCP_CHILD_START_GRACE_SECONDS))
                recover.assert_not_called()
                self.assertTrue(runtime.watchdog_tick(now=100 + MCP_CHILD_START_GRACE_SECONDS))

            recover.assert_called_once_with(
                "tunnel-owned MCP process did not appear before startup grace",
                now=100 + MCP_CHILD_START_GRACE_SECONDS,
            )

    def test_unknown_mcp_child_identity_does_not_trigger_missing_child_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp}), parse_settings({}), {})
            runtime.processes["tunnel"] = self._process(227)
            runtime.identities["tunnel"] = {"pid": 227}
            runtime.stopped = False
            runtime._tunnel_started_at = 100

            with patch.object(runtime, "tunnel_mcp_child_status", return_value="unknown"), \
                 patch.object(runtime, "tunnel_ready", return_value=False), \
                 patch.object(runtime, "_recover_tunnel_only") as recover:
                self.assertFalse(runtime.watchdog_tick(now=1000))

            recover.assert_not_called()
            self.assertFalse(runtime._mcp_recovery_requested)
            self.assertEqual(runtime._tunnel_mcp_child_state, "unknown")

    def test_readyz_and_child_presence_do_not_claim_mcp_protocol_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp}), parse_settings({}), {})
            runtime.processes["tunnel"] = self._process(226)
            runtime.identities["tunnel"] = {"pid": 226}
            runtime.stopped = False

            with patch.object(runtime, "tunnel_mcp_child_status", return_value="alive"), \
                 patch.object(runtime, "tunnel_ready", return_value=True):
                runtime.watchdog_tick(now=10, allow_recovery=False)
                mcp = runtime.snapshot()["components"]["mcp"]

            self.assertEqual(mcp["state"], "running")
            self.assertIn("protocol health remains unverified", mcp["message"])

    def test_operational_unverified_mcp_keeps_overall_runtime_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp}), parse_settings({}), {})
            runtime.processes.update(tunnel=self._process(228), daemon=self._process(229))
            runtime.identities.update(tunnel={"pid": 228}, daemon={"pid": 229})
            runtime.stopped = False
            runtime._tunnel_mcp_child_state = "alive"
            runtime._tunnel_ready = True

            with patch.object(runtime, "tunnel_healthy", return_value=True), \
                 patch.object(runtime, "test_connection", return_value={"ok": False}):
                snapshot = runtime.snapshot()

            self.assertEqual(snapshot["state"], "running")
            self.assertEqual(snapshot["components"]["mcp"]["state"], "running")
            self.assertIn("protocol health remains unverified", snapshot["components"]["mcp"]["message"])

    def test_mcp_warning_recovery_and_failure_keep_overall_non_normal(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp}), parse_settings({}), {})
            runtime.processes.update(tunnel=self._process(230), daemon=self._process(231))
            runtime.identities.update(tunnel={"pid": 230}, daemon={"pid": 231})
            runtime.stopped = False
            runtime._tunnel_mcp_child_state = "alive"
            runtime._tunnel_ready = True
            runtime._mcp_route_state = "warning"

            with patch.object(runtime, "tunnel_healthy", return_value=True), \
                 patch.object(runtime, "test_connection", return_value={"ok": False}):
                warning = runtime.snapshot()
                runtime._recovery_pending = True
                recovering = runtime.snapshot()
                runtime._recovery_pending = False
                runtime._mcp_route_state = "failed"
                failed = runtime.snapshot()

            self.assertEqual(warning["components"]["mcp"]["state"], "warning")
            self.assertEqual(warning["state"], "partial")
            self.assertEqual(recovering["components"]["mcp"]["state"], "restarting")
            self.assertEqual(recovering["state"], "partial")
            self.assertEqual(failed["components"]["mcp"]["state"], "failed")
            self.assertEqual(failed["state"], "failed")

    def test_tunnel_recovery_preserves_daemon_and_worker_process_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp})
            runtime = Runtime(paths, parse_settings({}), {"tunnel": "tunnel-client.exe"})
            tunnel = self._process(230)
            daemon = self._process(231)
            runtime.processes.update(tunnel=tunnel, daemon=daemon)
            runtime.identities.update(tunnel={"pid": 230}, daemon={"pid": 231})
            runtime.stopped = False
            registry = paths.state_dir() / "run" / "processes"
            registry.mkdir(parents=True)
            worker_record = registry / "worker.json"
            worker_record.write_text(json.dumps({"identity": {"pid": 232}, "instance_id": runtime.instance_id}), encoding="utf-8")
            original_record = worker_record.read_bytes()
            replacement = self._process(233)

            def launch_tunnel():
                runtime.processes["tunnel"] = replacement
                runtime.identities["tunnel"] = {"pid": replacement.pid}
                return replacement

            with patch.object(runtime, "test_connection", return_value={"ok": True}), \
                 patch.object(runtime, "_launch_tunnel", side_effect=launch_tunnel), \
                 patch("harbor_runtime.lifecycle.terminate_tree", return_value=True) as terminate, \
                 patch.object(runtime, "stop_registered_children") as stop_workers:
                self.assertTrue(runtime._recover_tunnel_only("test recovery", now=10))

            self.assertIs(runtime.processes["daemon"], daemon)
            self.assertIsNone(daemon.poll())
            self.assertEqual(worker_record.read_bytes(), original_record)
            terminate.assert_called_once_with(tunnel)
            stop_workers.assert_not_called()

    def test_tunnel_recovery_fails_closed_when_owned_tree_cannot_be_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp})
            runtime = Runtime(paths, parse_settings({}), {"tunnel": "tunnel-client.exe"})
            tunnel = self._process(234)
            daemon = self._process(235)
            runtime.processes.update(tunnel=tunnel, daemon=daemon)
            runtime.identities.update(tunnel={"pid": 234}, daemon={"pid": 235})
            runtime.stopped = False
            replacement = self._process(236)

            with patch.object(runtime, "test_connection", return_value={"ok": True}), \
                 patch.object(runtime, "_launch_tunnel") as launch, \
                 patch("harbor_runtime.lifecycle.terminate_tree", return_value=False) as terminate, \
                 patch.object(runtime, "stop_registered_children") as stop_workers:
                self.assertFalse(runtime._recover_tunnel_only("ownership unavailable", now=10))

            self.assertIs(runtime.processes["tunnel"], tunnel)
            self.assertIs(runtime.processes["daemon"], daemon)
            self.assertIsNone(daemon.poll())
            self.assertNotIn(replacement.pid, [proc.pid for proc in runtime.processes.values()])
            terminate.assert_called_once_with(tunnel)
            launch.assert_not_called()
            stop_workers.assert_not_called()

    def test_tunnel_child_identity_and_image_checks_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp}), parse_settings({}), {})
            tunnel = self._process(237)
            runtime.processes["tunnel"] = tunnel
            runtime.identities["tunnel"] = {"pid": 237, "started_at": "1", "job_name": "owned"}

            with patch("harbor_runtime.lifecycle.descendants", return_value=None):
                self.assertEqual(runtime.tunnel_mcp_child_status(tunnel), "unknown")

            with patch("harbor_runtime.lifecycle.descendants", return_value=[237, 238]), \
                 patch("harbor_runtime.lifecycle.process_image_path", return_value=None):
                self.assertEqual(runtime.tunnel_mcp_child_status(tunnel), "unknown")

            with patch("harbor_runtime.lifecycle.descendants", return_value=[237, 238]), \
                 patch("harbor_runtime.lifecycle.process_image_path", return_value="C:\\Other\\unrelated.exe"):
                self.assertEqual(runtime.tunnel_mcp_child_status(tunnel), "missing")

    def test_stop_start_creates_live_watchdog_for_new_generation(self):
        class FakeThread:
            def __init__(self, *, target, args, name, daemon):
                self.target, self.args, self.name, self.daemon = target, args, name, daemon
                self.alive = False

            def start(self):
                self.alive = True

            def is_alive(self):
                return self.alive

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = PlatformPaths(root, {
                "HARBOR_STATE_DIR": tmp,
                "HARBOR_TUNNEL_PROFILE_DIR": str(root / "tunnel"),
            })
            runtime = Runtime(paths, parse_settings({"connection": {"tunnel_id": "fixture-tunnel"}}),
                              {"tunnel": "C:\\Tools\\tunnel-client.exe"})
            next_pid = 300

            def spawn(name, argv, **kwargs):
                nonlocal next_pid
                next_pid += 1
                process = self._process(next_pid)
                runtime.processes[name] = process
                runtime.identities[name] = {"pid": process.pid}
                return process

            with patch("harbor_runtime.lifecycle.threading.Thread", FakeThread), \
                 patch.object(runtime, "spawn", side_effect=spawn), \
                 patch.object(runtime, "test_connection", return_value={"ok": True}), \
                 patch.object(runtime, "watchdog_tick", return_value=False), \
                 patch.object(runtime, "stop_registered_children"), \
                 patch("harbor_runtime.lifecycle.terminate_tree", return_value=True):
                runtime.start()
                old_thread = runtime._watchdog_thread
                old_event = runtime._watchdog_stop
                old_generation = runtime._watchdog_generation

                runtime.stop()
                runtime.start()

                new_thread = runtime._watchdog_thread
                new_event = runtime._watchdog_stop
                new_generation = runtime._watchdog_generation

            self.assertIsNot(new_thread, old_thread)
            self.assertIsNot(new_event, old_event)
            self.assertTrue(old_event.is_set())
            self.assertTrue(new_thread.is_alive())
            self.assertGreater(new_generation, old_generation)

    def test_buffered_old_tunnel_log_after_stop_start_is_ignored(self):
        class FakeThread:
            def __init__(self, *, target, args, name, daemon):
                self.target, self.args, self.name, self.daemon = target, args, name, daemon
                self.alive = False

            def start(self):
                self.alive = True

            def is_alive(self):
                return self.alive

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = PlatformPaths(root, {
                "HARBOR_STATE_DIR": tmp,
                "HARBOR_TUNNEL_PROFILE_DIR": str(root / "tunnel"),
            })
            runtime = Runtime(paths, parse_settings({"connection": {"tunnel_id": "fixture-tunnel"}}),
                              {"tunnel": "C:\\Tools\\tunnel-client.exe"})
            next_pid = 310

            def spawn(name, argv, **kwargs):
                nonlocal next_pid
                next_pid += 1
                process = self._process(next_pid)
                runtime.processes[name] = process
                runtime.identities[name] = {"pid": process.pid}
                return process

            with patch("harbor_runtime.lifecycle.threading.Thread", FakeThread), \
                 patch.object(runtime, "spawn", side_effect=spawn), \
                 patch.object(runtime, "test_connection", return_value={"ok": True}), \
                 patch.object(runtime, "watchdog_tick", return_value=False), \
                 patch.object(runtime, "stop_registered_children"), \
                 patch("harbor_runtime.lifecycle.terminate_tree", return_value=True):
                runtime.start()
                old_generation = runtime._tunnel_generation
                runtime.stop()
                runtime.start()
                new_generation = runtime._tunnel_generation

                # The old reader had already buffered this line before stop;
                # deliver it only after the replacement runtime is running.
                runtime._drain(io.BytesIO((self._local_mcp_failure() + "\n").encode()),
                               "tunnel", old_generation)

            self.assertGreater(new_generation, old_generation)
            self.assertEqual(list(runtime._local_mcp_failures), [])
            self.assertFalse(runtime._mcp_recovery_requested)

    def test_immediate_replacement_failure_survives_recovery_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = PlatformPaths(root, {
                "HARBOR_STATE_DIR": tmp,
                "HARBOR_TUNNEL_PROFILE_DIR": str(root / "tunnel"),
            })
            settings = parse_settings({"connection": {"tunnel_id": "fixture-tunnel"}})
            runtime = Runtime(paths, settings, {"tunnel": "tunnel-client.exe"})
            old_tunnel = self._process(320)
            daemon = self._process(321)
            replacement = self._process(322)
            runtime.processes.update(tunnel=old_tunnel, daemon=daemon)
            runtime.identities.update(tunnel={"pid": 320}, daemon={"pid": 321})
            runtime.stopped = False
            old_generation = runtime._tunnel_generation
            for timestamp in (1, 2, 3):
                runtime.observe_tunnel_log(self._local_mcp_failure(),
                                           tunnel_generation=old_generation, now=timestamp)

            def spawn(name, argv, *, tunnel_generation=None, **kwargs):
                self.assertEqual(name, "tunnel")
                runtime.processes[name] = replacement
                runtime.identities[name] = {"pid": replacement.pid}
                # Model a failure emitted as soon as the replacement starts,
                # before _launch_tunnel returns to its recovery caller.
                runtime.observe_tunnel_log(self._local_mcp_failure(),
                                           tunnel_generation=tunnel_generation, now=20)
                return replacement

            with patch.object(runtime, "test_connection", return_value={"ok": True}), \
                 patch.object(runtime, "spawn", side_effect=spawn), \
                 patch("harbor_runtime.lifecycle.terminate_tree", return_value=True):
                self.assertTrue(runtime._recover_tunnel_only("repeated local failures", now=10))

            self.assertEqual(list(runtime._local_mcp_failures), [20])
            self.assertFalse(runtime._mcp_recovery_requested)
            self.assertEqual(runtime._mcp_route_state, "warning")
            self.assertIn("(1/3)", runtime.messages["mcp"])
            self.assertIs(runtime.processes["daemon"], daemon)

    def test_old_tunnel_generation_cannot_reset_new_failure_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp}), parse_settings({}), {})
            old_generation = runtime._tunnel_generation
            new_generation = runtime._begin_tunnel_generation()
            for timestamp in (1, 2, 3):
                runtime.observe_tunnel_log(self._local_mcp_failure(),
                                           tunnel_generation=new_generation, now=timestamp)

            new_state = (
                list(runtime._local_mcp_failures), runtime._mcp_route_state,
                runtime._mcp_recovery_requested, runtime._mcp_recovery_reason,
                runtime.messages.get("mcp"),
            )
            runtime.observe_tunnel_log(json.dumps({
                "component": "dispatcher",
                "msg": "dispatcher forwarded command to MCP server",
                "rpc_method": "tools/call",
            }), tunnel_generation=old_generation, now=4)
            runtime.observe_tunnel_log(self._local_mcp_failure(),
                                       tunnel_generation=old_generation, now=5)

            self.assertEqual(
                (
                    list(runtime._local_mcp_failures), runtime._mcp_route_state,
                    runtime._mcp_recovery_requested, runtime._mcp_recovery_reason,
                    runtime.messages.get("mcp"),
                ),
                new_state,
            )

    def test_old_watchdog_generation_cannot_tick_new_runtime_generation(self):
        import threading
        from harbor_runtime import lifecycle
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp}), parse_settings({}), {})
            old_event = threading.Event()
            new_event = threading.Event()
            runtime._watchdog_generation = 2
            runtime._watchdog_stop = new_event
            runtime.stopped = False

            with patch.object(lifecycle, "TUNNEL_WATCHDOG_INTERVAL_SECONDS", 0), \
                 patch.object(runtime, "watchdog_tick") as tick:
                runtime._watchdog_loop(1, old_event)

            tick.assert_not_called()

    def test_recovery_snapshot_ignores_bridge_owned_mcp_readiness_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp}), parse_settings({}), {"tunnel": "tunnel-client.exe"})
            tunnel = self._process(240)
            daemon = self._process(241)
            bridge_mcp = self._process(242)
            runtime.processes.update(tunnel=tunnel, daemon=daemon, mcp=bridge_mcp)
            runtime.identities.update(tunnel={"pid": 240}, daemon={"pid": 241}, mcp={"pid": 242})
            runtime.stopped = False
            runtime.mcp_ready = True  # Legacy field/process must not prove tunnel MCP health.
            replacement = self._process(243)

            def launch_tunnel():
                runtime.processes["tunnel"] = replacement
                runtime.identities["tunnel"] = {"pid": replacement.pid}
                return replacement

            with patch.object(runtime, "test_connection", return_value={"ok": True}), \
                 patch.object(runtime, "_launch_tunnel", side_effect=launch_tunnel), \
                 patch("harbor_runtime.lifecycle.terminate_tree", return_value=True):
                self.assertTrue(runtime._recover_tunnel_only("test recovery", now=10))
                with patch.object(runtime, "tunnel_healthy", return_value=True):
                    snapshot = runtime.snapshot()

            self.assertNotEqual(snapshot["components"]["mcp"]["state"], "healthy")
            self.assertIs(runtime.processes["mcp"], bridge_mcp)

    def test_recovery_cooldown_and_backoff_bound_repeated_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Runtime(PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp}), parse_settings({}), {"tunnel": "tunnel-client.exe"})
            first_tunnel = self._process(250)
            runtime.processes["tunnel"] = first_tunnel
            runtime.identities["tunnel"] = {"pid": 250}
            runtime.stopped = False
            launched = []

            def launch_tunnel():
                process = self._process(251 + len(launched))
                launched.append(process)
                runtime.processes["tunnel"] = process
                runtime.identities["tunnel"] = {"pid": process.pid}
                return process

            with patch.object(runtime, "test_connection", return_value={"ok": True}), \
                 patch.object(runtime, "_launch_tunnel", side_effect=launch_tunnel), \
                 patch.object(runtime, "tunnel_mcp_child_status", return_value="unknown"), \
                 patch.object(runtime, "tunnel_ready", return_value=False), \
                 patch("harbor_runtime.lifecycle.terminate_tree", return_value=True) as terminate:
                self.assertTrue(runtime._recover_tunnel_only("first", now=10))
                runtime._queue_tunnel_recovery("still failing")
                self.assertFalse(runtime.watchdog_tick(now=20))
                self.assertEqual(len(launched), 1)
                self.assertTrue(runtime.watchdog_tick(now=40))
                self.assertEqual(len(launched), 2)

            self.assertEqual(runtime._recovery_attempts, 2)
            self.assertEqual(runtime._next_recovery_at, 100)
            self.assertEqual(terminate.call_count, 2)

    def test_failed_tunnel_launch_retries_after_cooldown_without_other_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp})
            runtime = Runtime(paths, parse_settings({}), {"tunnel": "tunnel-client.exe"})
            tunnel = self._process(255)
            daemon = self._process(256)
            runtime.processes.update(tunnel=tunnel, daemon=daemon)
            runtime.identities.update(tunnel={"pid": 255}, daemon={"pid": 256})
            runtime.stopped = False
            replacement = self._process(257)
            launch_count = 0

            def launch_tunnel():
                nonlocal launch_count
                launch_count += 1
                if launch_count == 1:
                    raise RuntimeError("transient tunnel launch failure")
                runtime.processes["tunnel"] = replacement
                runtime.identities["tunnel"] = {"pid": replacement.pid}
                return replacement

            with patch.object(runtime, "test_connection", return_value={"ok": True}), \
                 patch.object(runtime, "_launch_tunnel", side_effect=launch_tunnel), \
                 patch("harbor_runtime.lifecycle.terminate_tree", return_value=True):
                runtime._queue_tunnel_recovery("first repair")
                self.assertFalse(runtime._recover_tunnel_only("first repair", now=10))
                self.assertNotIn("tunnel", runtime.processes)
                self.assertIs(runtime.processes["daemon"], daemon)
                self.assertFalse(runtime.watchdog_tick(now=20))
                self.assertTrue(runtime.watchdog_tick(now=40))

            self.assertIs(runtime.processes["tunnel"], replacement)
            self.assertIs(runtime.processes["daemon"], daemon)
            self.assertEqual(launch_count, 2)

    def test_start_does_not_spawn_a_duplicate_mcp_readiness_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = PlatformPaths(root, {"HARBOR_STATE_DIR": tmp, "HARBOR_TUNNEL_PROFILE_DIR": str(root / "tunnel")})
            settings = parse_settings({"connection": {"tunnel_id": "fixture-tunnel"}})
            runtime = Runtime(paths, settings, {"tunnel": "C:\\Tools\\tunnel-client.exe"})
            spawned = []

            def spawn(name, argv, **kwargs):
                spawned.append((name, argv))
                process = self._process(260 + len(spawned))
                runtime.processes[name] = process
                return process

            with patch.object(runtime, "spawn", side_effect=spawn), \
                 patch.object(runtime, "test_connection", return_value={"ok": True}), \
                 patch.object(runtime, "_ensure_watchdog"), \
                 patch.object(runtime, "watchdog_tick", return_value=False):
                runtime.start()

            self.assertEqual([name for name, _ in spawned], ["daemon", "tunnel"])
            self.assertNotIn("mcp", runtime.processes)
            profile_path = paths.tunnel_state_dir() / "chatgpt-harbor.yaml"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertEqual(profile["control_plane"]["api_key"], "env:TUNNEL_RUNTIME_KEY")
            self.assertEqual(len(profile["mcp"]["commands"]), 1)

    def test_tunnel_health_routes_are_explicit(self):
        from harbor_runtime.lifecycle import tunnel_health_route
        self.assertEqual(tunnel_health_route("http://127.0.0.1:51234", "healthz"), "http://127.0.0.1:51234/healthz")
        self.assertEqual(tunnel_health_route("http://127.0.0.1:51234/healthz", "readyz"), "http://127.0.0.1:51234/readyz")
    def test_acquire_recovers_and_discards_confirmed_missing_components(self):
        from runtime_queue import queue_root_fingerprint
        with tempfile.TemporaryDirectory() as tmp:
            paths = PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp})
            manifest_path = paths.state_dir() / "run" / "components.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            fp = queue_root_fingerprint(paths.jobs_dir())
            stale_ident = {
                "pid": 999998,
                "started_at": "123456789",
                "job_name": "Local\\HarnessHarbor-999998-123456789",
            }
            manifest_data = {
                "instance_id": "old-instance-id",
                "queue_fingerprint": fp,
                "components": {
                    "mcp": {
                        "component": "mcp",
                        "identity": stale_ident,
                        "executable": "C:\\fake\\harbor-runtime.exe",
                    }
                },
            }
            manifest_path.write_text(json.dumps(manifest_data))

            runtime = Runtime(paths, parse_settings({}), {})
            try:
                # When PID is dead and named job object is confirmed missing:
                with patch("harbor_platform.process.is_alive", return_value=False):
                    runtime.acquire()

                self.assertEqual(runtime.processes, {})
                # Manifest must be updated to clean state
                saved = json.loads(manifest_path.read_text())
                self.assertEqual(saved.get("components"), {})
                self.assertEqual(saved.get("queue_fingerprint"), fp)
                self.assertNotEqual(saved.get("instance_id"), "old-instance-id")
            finally:
                if runtime._lock_file:
                    runtime._lock_file.close()

    def test_acquire_fails_closed_when_ownership_unverifiable(self):
        from runtime_queue import queue_root_fingerprint
        with tempfile.TemporaryDirectory() as tmp:
            paths = PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp})
            manifest_path = paths.state_dir() / "run" / "components.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            fp = queue_root_fingerprint(paths.jobs_dir())
            stale_ident = {
                "pid": 999997,
                "started_at": "123456789",
                "job_name": "Local\\HarnessHarbor-999997-123456789",
            }
            manifest_data = {
                "instance_id": "old-instance-id",
                "queue_fingerprint": fp,
                "components": {
                    "mcp": {
                        "component": "mcp",
                        "identity": stale_ident,
                        "executable": "C:\\fake\\harbor-runtime.exe",
                    }
                },
            }
            manifest_path.write_text(json.dumps(manifest_data))

            runtime = Runtime(paths, parse_settings({}), {})
            try:
                with patch("harbor_platform.process.is_alive", return_value=False), \
                     patch("harbor_runtime.lifecycle.recover_owned", side_effect=RuntimeError("Component ownership could not be verified")):
                    with self.assertRaisesRegex(RuntimeError, "Component ownership could not be verified"):
                        runtime.acquire()

                # Manifest must NOT have been overwritten
                saved = json.loads(manifest_path.read_text())
                self.assertEqual(saved.get("instance_id"), "old-instance-id")
                self.assertIn("mcp", saved.get("components", {}))
            finally:
                if runtime._lock_file:
                    runtime._lock_file.close()

    def test_acquire_repeated_cycles_remain_consistent(self):
        from runtime_queue import queue_root_fingerprint
        with tempfile.TemporaryDirectory() as tmp:
            paths = PlatformPaths(Path(tmp), {"HARBOR_STATE_DIR": tmp})
            manifest_path = paths.state_dir() / "run" / "components.json"
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            fp = queue_root_fingerprint(paths.jobs_dir())
            stale_ident = {
                "pid": 999996,
                "started_at": "123456789",
                "job_name": "Local\\HarnessHarbor-999996-123456789",
            }
            manifest_data = {
                "instance_id": "old-instance-id",
                "queue_fingerprint": fp,
                "components": {
                    "daemon": {
                        "component": "daemon",
                        "identity": stale_ident,
                        "executable": "C:\\fake\\harbor-runtime.exe",
                    }
                },
            }
            manifest_path.write_text(json.dumps(manifest_data))

            # Cycle 1: Discards dead component, rewrites manifest
            runtime1 = Runtime(paths, parse_settings({}), {})
            try:
                with patch("harbor_platform.process.is_alive", return_value=False):
                    runtime1.acquire()
                self.assertEqual(runtime1.processes, {})
                saved1 = json.loads(manifest_path.read_text())
                self.assertEqual(saved1.get("components"), {})
            finally:
                if runtime1._lock_file:
                    runtime1._lock_file.close()

            # Cycle 2: Subsequent startup encounters clean manifest
            runtime2 = Runtime(paths, parse_settings({}), {})
            try:
                with patch("harbor_platform.process.is_alive", return_value=False):
                    runtime2.acquire()
                self.assertEqual(runtime2.processes, {})
                saved2 = json.loads(manifest_path.read_text())
                self.assertEqual(saved2.get("components"), {})
                self.assertEqual(runtime2.snapshot()["state"], "stopped")
            finally:
                if runtime2._lock_file:
                    runtime2._lock_file.close()

    def test_lifecycle_has_no_os_process_mechanisms(self):
        source = (Path(__file__).resolve().parents[1] / "harbor_runtime/lifecycle.py").read_text()
        for forbidden in ("fcntl", "select.select", "start_new_session", "os.kill", "taskkill", "CimInstance", 'identity["pgid"]'):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
