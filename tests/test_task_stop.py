"""Focused job-scoped stop contract tests; no real process signals."""
import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import control_plane
import codex_job_worker
import codex_job_daemon
import server_legacy
import task_stop_control as stop


class StopTests(unittest.TestCase):
    @contextmanager
    def windows_job_observation(self, worker, native, identities):
        # Keep _tree/snapshot/stop real, including the retained dead identity.
        with patch.object(stop.sys, "platform", "win32"), \
             patch("harbor_platform.windows_process.inspect_job", side_effect=lambda identity: (
                 "active", [member["pid"] for member in (worker if identity["pid"] == 101 else native)])), \
             patch.object(stop.process, "process_identity", side_effect=lambda pid: identities[pid]), \
             patch.object(stop.process, "is_alive", side_effect=lambda pid: any(
                 member["pid"] == pid for member in worker + native)):
            yield

    def test_write_state_creates_missing_status_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            state = {"status": "completed", "job_id": "fixture"}
            codex_job_worker.write_state(path, state)
            self.assertEqual(control_plane.read_json_object(path)["status"], "completed")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.jobs = Path(self.temp.name)
        self.job_id = "a" * 32
        self.job_dir = self.jobs / self.job_id
        self.job_dir.mkdir()
        self.state = {"job_id": self.job_id, "harness": "codex", "status": "queued",
                      "cwd": str(self.jobs), "stdout_tail": "output", "stderr": "diagnostic",
                      "attempts": [{"route": "official"}]}
        self.save()

    def save(self):
        control_plane.write_json(self.job_dir / "status.json", self.state)

    def reload(self):
        self.state = control_plane.read_json_object(self.job_dir / "status.json")

    def own(self, native=True):
        self.state["status"] = "running"
        self.state["stop_ownership"] = {"job_id": self.job_id, "harness": "codex",
            "queue_root": str(self.jobs.resolve()), "generation": "generation-a",
            "worker": {"pid": 101, "pgid": 101, "started_at": "one"},
            "native": {"pid": 201, "pgid": 201, "started_at": "two"} if native else None}
        self.save()

    def test_queued_compatibility_and_graceful(self):
        self.assertTrue(control_plane.cancel_task(self.job_id, self.jobs)["ok"])
        self.reload()
        self.assertEqual("cancelled", self.state["status"])
        self.state["status"] = "queued"
        self.save()
        result = stop.stop(self.job_id, self.jobs)
        self.assertTrue(result["ok"])
        self.reload()
        self.assertEqual("cancelled", self.state["status"])

    def test_graceful_timeout_fingerprint_and_foreign_exclusion(self):
        self.own()
        with patch.object(stop, "_tree", side_effect=[(True, [{"pid": 101}]), (True, [{"pid": 201}])] * 8):
            view = stop.preview(self.job_id, self.jobs)
            self.assertTrue(view["ownership_verified"])
            self.assertEqual({101, 201}, {m["pid"] for t in view["owned_tree"] for m in t["members"]})
            self.assertFalse(view["force_eligible"])
            result = stop.stop(self.job_id, self.jobs)
            self.assertEqual("cancelling", result["status"])
            self.assertFalse(result["process_signal_attempted"])
            self.assertEqual("ownership_unverified", stop.stop(self.job_id, self.jobs, "force")["error_kind"])
            self.assertEqual("ownership_changed", stop.stop(self.job_id, self.jobs, "force", "wrong")["error_kind"])
            self.assertEqual("force_not_yet_allowed", stop.stop(self.job_id, self.jobs, "force", view["ownership_fingerprint"])["error_kind"])
        self.reload()
        self.assertEqual(10, round((datetime.fromisoformat(self.state["stop"]["grace_deadline"]) - datetime.fromisoformat(self.state["stop"]["requested_at"])).total_seconds()))
        self.assertEqual("output", self.state["stdout_tail"])
        self.assertEqual([{"route": "official"}], self.state["attempts"])

    def test_legacy_and_changed_identity_fail_closed(self):
        self.state["status"] = "running"
        self.save()
        stop.stop(self.job_id, self.jobs)
        self.assertFalse(stop.preview(self.job_id, self.jobs)["ownership_verified"])
        self.assertEqual("ownership_unverified", stop.stop(self.job_id, self.jobs, "force", "fingerprint")["error_kind"])
        self.own(native=False)
        self.state["status"] = "cancelling"
        self.state["stop"] = {"grace_deadline": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
        self.save()
        with patch.object(stop, "_tree", return_value=(False, None)):
            self.assertFalse(stop.preview(self.job_id, self.jobs)["ownership_verified"])
            self.assertEqual("ownership_unverified", stop.stop(self.job_id, self.jobs, "force", "fingerprint")["error_kind"])

    def test_force_death_verification_releases_only_after_exit(self):
        self.own(native=False)
        self.state["status"] = "cancelling"
        self.state["stop"] = {"grace_deadline": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
        self.save()
        members = [{"pid": 101}]
        def tree(_):
            return True, list(members)
        def terminate(_proc, grace):
            members.clear()
            return True
        with patch.object(stop, "_tree", side_effect=tree), patch.object(stop.process, "recover_owned", return_value=object()), patch.object(stop.process, "terminate_tree", side_effect=terminate), patch.object(stop, "_release") as release:
            fingerprint = stop.preview(self.job_id, self.jobs)["ownership_fingerprint"]
            result = stop.stop(self.job_id, self.jobs, "force", fingerprint)
            self.assertTrue(result["tree_exit_verified"])
            release.assert_called_once()
        self.reload()
        self.assertEqual("cancelled", self.state["status"])
        self.assertFalse(self.state["stop"]["output_complete"])
        self.assertEqual("diagnostic", self.state["stderr"])
        self.assertEqual([{"route": "official"}], self.state["attempts"])
        self.assertEqual("job_already_terminal", stop.stop(self.job_id, self.jobs, "force", fingerprint)["error_kind"])

    def test_windows_nested_native_job_is_terminated_before_worker(self):
        self.own()
        self.state["status"] = "cancelling"
        self.state["stop"] = {"grace_deadline": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
        self.save()
        worker = [self.state["stop_ownership"]["worker"], self.state["stop_ownership"]["native"],
                  {"pid": 202, "started_at": "child"}]
        native = worker[1:].copy()
        identities = {member["pid"]: member for member in worker}
        calls = []

        def terminate(proc, grace):
            pid = proc.identity["pid"]
            calls.append(pid)
            if pid == 201:
                native.clear()
                worker[:] = worker[:1]
            else:
                worker.clear()
            return True

        with self.windows_job_observation(worker, native, identities), \
             patch.object(stop.process, "recover_owned", side_effect=lambda identity: type("Recovered", (), {"identity": identity})()), \
             patch.object(stop.process, "terminate_tree", side_effect=terminate):
            fingerprint = stop.preview(self.job_id, self.jobs)["ownership_fingerprint"]
            result = stop.stop(self.job_id, self.jobs, "force", fingerprint)
        self.assertEqual([201, 101], calls)
        self.assertTrue(result["tree_exit_verified"])
        self.reload()
        self.assertTrue(self.state["stop_ownership"]["native_exit_verified"])

    def test_windows_nested_job_unrelated_growth_fails_before_worker_signal(self):
        self.own()
        self.state.update(status="cancelling", stop={"grace_deadline": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()})
        self.save()
        worker = [self.state["stop_ownership"]["worker"], self.state["stop_ownership"]["native"]]
        native = worker[1:].copy()
        identities = {member["pid"]: member for member in worker}
        identities[301] = {"pid": 301, "started_at": "unknown"}
        calls = []

        def terminate(proc, grace):
            calls.append(proc.identity["pid"])
            native.clear()
            worker[:] = [identities[101], identities[301]]
            return True

        with self.windows_job_observation(worker, native, identities), \
             patch.object(stop.process, "recover_owned", side_effect=lambda identity: type("Recovered", (), {"identity": identity})()), \
             patch.object(stop.process, "terminate_tree", side_effect=terminate):
            fingerprint = stop.preview(self.job_id, self.jobs)["ownership_fingerprint"]
            result = stop.stop(self.job_id, self.jobs, "force", fingerprint)
        self.assertEqual("process_tree_changed", result["error_kind"])
        self.assertEqual([201], calls)
        self.reload()
        self.assertEqual("cancelling", self.state["status"])

    def test_native_descendant_survival_retains_cancelling_and_lease(self):
        self.own()
        self.state.update(status="cancelling", stop={"grace_deadline": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()})
        self.save()
        worker = [{"pid": 101}, {"pid": 201}, {"pid": 202}]
        native = [{"pid": 201}, {"pid": 202}]
        with patch.object(stop, "_tree", side_effect=lambda identity: (True, list(worker if identity["pid"] == 101 else native))), \
             patch.object(stop.process, "recover_owned", return_value=object()), \
             patch.object(stop.process, "terminate_tree", return_value=False), \
             patch.object(stop, "_release") as release:
            fingerprint = stop.preview(self.job_id, self.jobs)["ownership_fingerprint"]
            result = stop.stop(self.job_id, self.jobs, "force", fingerprint)
            self.assertEqual("force_stop_failed", result["error_kind"])
            release.assert_not_called()
        self.reload()
        self.assertEqual("cancelling", self.state["status"])
        self.assertFalse(self.state["stop_ownership"].get("native_exit_verified", False))

    def test_windows_missing_child_job_requires_verified_exit_marker(self):
        self.own()
        self.state["status"] = "cancelling"
        self.state["stop"] = {}
        self.save()
        def tree(identity):
            return (True, []) if identity["pid"] == 101 else (False, None)
        with patch.object(stop.sys, "platform", "win32"), patch.object(stop, "_tree", side_effect=tree), \
             patch.object(stop.process, "is_alive", return_value=False), \
             patch("harbor_platform.windows_process.inspect_job", return_value=("confirmed_missing", [])):
            self.assertFalse(stop.reconcile(self.job_id, self.jobs))
            self.state["stop_ownership"]["native_exit_verified"] = True
            self.save()
            self.assertTrue(stop.reconcile(self.job_id, self.jobs))

    def test_windows_empty_native_job_handle_closes_with_worker(self):
        self.own()
        self.state.update(status="cancelling", stop={"grace_deadline": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()})
        self.save()
        worker = [{"pid": 101}]

        def tree(identity):
            if identity["pid"] == 101:
                return True, list(worker)
            return (True, []) if worker else (False, None)

        def terminate(proc, grace):
            worker.clear()
            return True

        with patch.object(stop.sys, "platform", "win32"), patch.object(stop, "_tree", side_effect=tree), \
             patch.object(stop.process, "is_alive", return_value=False), \
             patch("harbor_platform.windows_process.inspect_job", return_value=("confirmed_missing", [])), \
             patch.object(stop.process, "recover_owned", side_effect=lambda identity: type("Recovered", (), {"identity": identity})()), \
             patch.object(stop.process, "terminate_tree", side_effect=terminate):
            fingerprint = stop.preview(self.job_id, self.jobs)["ownership_fingerprint"]
            result = stop.stop(self.job_id, self.jobs, "force", fingerprint)
        self.assertTrue(result["tree_exit_verified"])
        self.reload()
        self.assertTrue(self.state["stop_ownership"]["native_exit_verified"])

    def test_worker_result_after_cancelled_cannot_resurrect_job(self):
        self.own(native=False)
        self.state.update(status="cancelled", stop={"tree_exit_verified_at": "verified"}, cancelled_at="now")
        self.save()
        late = {**self.state, "status": "failed", "stdout_tail": "late output", "attempts": [{"route": "late"}]}
        codex_job_worker.write_state(self.job_dir / "status.json", late)
        self.reload()
        self.assertEqual("cancelled", self.state["status"])
        self.assertEqual("late output", self.state["stdout_tail"])
        self.assertEqual([{"route": "late"}], self.state["attempts"])

    def test_reconcile_retains_lease_while_tree_lives(self):
        self.own(native=False)
        self.state["status"] = "cancelling"
        self.state["stop"] = {}
        self.save()
        with patch.object(stop, "_tree", return_value=(True, [{"pid": 101}])), patch.object(stop, "_release") as release:
            self.assertFalse(stop.reconcile(self.job_id, self.jobs))
            release.assert_not_called()
        self.reload()
        self.assertEqual("cancelling", self.state["status"])

    def test_recovery_retains_cancelling_reservation_on_uncertain_observation(self):
        self.own(native=False)
        self.state.update(status="cancelling", stop={"requested_at": "first"})
        self.save()
        with patch.object(stop, "_tree", return_value=(False, None)), patch.object(stop, "_release") as release:
            self.assertEqual([], codex_job_daemon.recover_abandoned_jobs(self.jobs, "codex"))
            self.assertIn(self.job_id, codex_job_daemon.get_disk_active_job_ids(self.jobs)["codex"])
            release.assert_not_called()
        self.reload()
        self.assertEqual("cancelling", self.state["status"])

    def test_recovery_finalizes_only_when_worker_and_native_descendants_die(self):
        self.own()
        self.state.update(status="cancelling", stop={"requested_at": "first"})
        self.save()
        members = {101: [{"pid": 102}], 201: [{"pid": 202}]}
        with patch.object(stop, "_tree", side_effect=lambda identity: (True, members[identity["pid"]])), \
             patch.object(stop, "_release") as release:
            self.assertEqual([], codex_job_daemon.recover_abandoned_jobs(self.jobs, "codex"))
            members[101].clear()
            self.assertEqual([], codex_job_daemon.recover_abandoned_jobs(self.jobs, "codex"))
            release.assert_not_called()
            members[201].clear()
            self.assertEqual([self.job_id], codex_job_daemon.recover_abandoned_jobs(self.jobs, "codex"))
            release.assert_called_once()

    def test_duplicate_graceful_keeps_original_deadline_and_result_order(self):
        self.own(native=False)
        first = control_plane.task_stop(self.job_id, jobs_dir=self.jobs)
        second = control_plane.task_stop(self.job_id, reason="late", jobs_dir=self.jobs)
        self.assertTrue(first["job_state_changed"])
        self.assertFalse(second["job_state_changed"])
        self.assertEqual(first["stop"], second["stop"])
        self.reload()
        candidate = {**self.state, "status": "completed", "stdout_tail": "finished", "attempts": [{"route": "codex"}]}
        codex_job_worker.write_state(self.job_dir / "status.json", candidate)
        self.reload()
        self.assertEqual("cancelling", self.state["status"])
        self.assertEqual("finished", self.state["stdout_tail"])
        self.assertEqual([{"route": "codex"}], self.state["attempts"])
        self.assertIn("worker_result_observed_at", self.state["stop"])

    def test_terminal_task_stop_preserves_result_and_attempts(self):
        result_path = self.job_dir / "result.txt"
        result_path.write_text("answer", encoding="utf-8")
        for status in ("completed", "failed", "cancelled"):
            with self.subTest(status=status):
                self.state.update(status=status, attempts=[{"route": status}], stdout_tail="kept")
                self.save()
                response = control_plane.task_stop(self.job_id, jobs_dir=self.jobs)
                self.assertEqual("job_already_terminal", response["error_kind"])
                self.reload()
                self.assertEqual(status, self.state["status"])
                self.assertEqual([{"route": status}], self.state["attempts"])
                self.assertEqual("answer", result_path.read_text(encoding="utf-8"))

    def test_force_requires_cancelling_and_terminal_preview_is_ineligible(self):
        self.own(native=False)
        with patch.object(stop, "_tree", return_value=(True, [{"pid": 101}])):
            fingerprint = stop.preview(self.job_id, self.jobs)["ownership_fingerprint"]
            self.assertEqual("force_not_yet_allowed", control_plane.task_stop(self.job_id, "force", fingerprint, jobs_dir=self.jobs)["error_kind"])
        self.state["status"] = "queued"
        self.save()
        self.assertEqual("force_not_yet_allowed", control_plane.task_stop(self.job_id, "force", fingerprint, jobs_dir=self.jobs)["error_kind"])
        self.state["status"] = "completed"
        self.save()
        self.assertFalse(control_plane.task_stop_preview(self.job_id, self.jobs)["force_eligible"])
        self.assertEqual("job_already_terminal", control_plane.task_stop(self.job_id, "force", fingerprint, jobs_dir=self.jobs)["error_kind"])

    def test_native_spawn_and_registration_share_job_lock(self):
        self.own(native=False)
        holding = [False]

        @contextmanager
        def locked(_job_dir):
            holding[0] = True
            try:
                yield
            finally:
                holding[0] = False

        proc = object()
        def spawned(*_args, **_kwargs):
            self.assertTrue(holding[0])
            return proc
        def registered(value):
            self.assertIs(proc, value)
            self.assertTrue(holding[0])
        with patch.object(codex_job_worker, "_ACTIVE_STATE_PATH", self.job_dir / "status.json"), \
             patch.object(control_plane, "_job_poll_lock", side_effect=locked), \
             patch.object(codex_job_worker, "spawn_runtime_child", side_effect=spawned), \
             patch.object(codex_job_worker, "_register_native", side_effect=registered):
            self.assertIs(proc, codex_job_worker._spawn_native(["mock-native"]))
        self.assertFalse(holding[0])

    def test_worker_result_normalization_all_harnesses(self):
        for harness in ("codex", "minimax", "agy"):
            with self.subTest(harness=harness):
                self.state.update(harness=harness, status="cancelling", stop={"requested_at": "first"})
                self.save()
                candidate = {**self.state, "status": "failed", "stderr": "native exit",
                             "stdout_tail": "kept output", "attempts": [{"route": harness}]}
                codex_job_worker.write_state(self.job_dir / "status.json", candidate)
                self.reload()
                self.assertEqual("cancelling", self.state["status"])
                self.assertEqual("kept output", self.state["stdout_tail"])
                self.assertEqual([{"route": harness}], self.state["attempts"])

    def test_changed_tree_and_pid_reuse_rejected(self):
        self.own(native=False)
        self.state["status"] = "cancelling"
        self.state["stop"] = {"grace_deadline": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
        self.save()
        with patch.object(stop, "_tree", return_value=(True, [{"pid": 101, "started_at": "one"}])):
            fingerprint = stop.preview(self.job_id, self.jobs)["ownership_fingerprint"]
        with patch.object(stop, "_tree", return_value=(True, [{"pid": 101, "started_at": "reused"}])):
            self.assertEqual("ownership_changed", stop.stop(self.job_id, self.jobs, "force", fingerprint)["error_kind"])
        with patch.object(stop, "_tree", return_value=(True, [{"pid": 101, "started_at": "one"}, {"pid": 301, "started_at": "new child"}])):
            self.assertEqual("ownership_changed", stop.stop(self.job_id, self.jobs, "force", fingerprint)["error_kind"])

    def test_worker_exits_between_preview_and_force(self):
        self.own(native=False)
        self.state["status"] = "cancelling"
        self.state["stop"] = {"grace_deadline": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
        self.save()
        with patch.object(stop, "_tree", return_value=(True, [{"pid": 101}])):
            fingerprint = stop.preview(self.job_id, self.jobs)["ownership_fingerprint"]
        with patch.object(stop, "_tree", return_value=(True, [])), patch.object(stop.process, "terminate_tree") as terminate:
            self.assertEqual("ownership_changed", stop.stop(self.job_id, self.jobs, "force", fingerprint)["error_kind"])
            terminate.assert_not_called()

    def test_process_identity_mock_posix_and_windows(self):
        identity = {"pid": 101, "started_at": "original", "pgid": 101}
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform):
                with patch.object(stop.sys, "platform", platform), patch.object(stop.process, "descendants", return_value=[101]), patch.object(stop.process, "process_identity", return_value=identity):
                    self.assertEqual((True, [identity]), stop._tree(identity))
                with patch.object(stop.sys, "platform", platform), patch.object(stop.process, "descendants", return_value=[101]), patch.object(stop.process, "process_identity", return_value={**identity, "started_at": "reused"}):
                    self.assertEqual((False, None), stop._tree(identity))

    def test_job_a_fingerprint_cannot_force_job_b(self):
        self.own(native=False)
        self.state["status"] = "cancelling"
        self.state["stop"] = {"grace_deadline": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
        self.save()
        second_id = "b" * 32
        second_dir = self.jobs / second_id
        second_dir.mkdir()
        other = json.loads(json.dumps(self.state))
        other["job_id"] = second_id
        other["stop_ownership"]["job_id"] = second_id
        control_plane.write_json(second_dir / "status.json", other)
        with patch.object(stop, "_tree", return_value=(True, [{"pid": 101}])):
            fingerprint = stop.preview(self.job_id, self.jobs)["ownership_fingerprint"]
            self.assertEqual("ownership_changed", stop.stop(second_id, self.jobs, "force", fingerprint)["error_kind"])

    def test_no_git_cleanup_in_stop_path(self):
        self.own(native=False)
        with patch("subprocess.run", side_effect=AssertionError("Git or process probe invoked")):
            result = stop.stop(self.job_id, self.jobs)
        self.assertEqual("cancelling", result["status"])

    def test_exported_mcp_contract(self):
        tools = server_legacy.mcp._tool_manager._tools
        preview = tools["task_stop_preview"]
        mutation = tools["task_stop"]
        self.assertEqual(["job_id"], list(preview.parameters["properties"]))
        self.assertTrue(preview.annotations.readOnlyHint)
        self.assertFalse(mutation.annotations.readOnlyHint)
        self.assertTrue(mutation.annotations.destructiveHint)
        self.assertFalse(mutation.annotations.idempotentHint)
        self.assertNotIn("pid", mutation.parameters["properties"])
        self.assertIn("never terminates", preview.description)


class WindowsTreeTests(unittest.TestCase):
    def setUp(self):
        self.identity = {"pid": 101, "started_at": "1234", "job_name": "Local\\HarnessHarbor-101-1234"}

    def observe(self, status="active", members=None, alive=False, leader=None):
        with patch.object(stop.sys, "platform", "win32"), \
             patch("harbor_platform.windows_process.inspect_job", return_value=(status, members)), \
             patch.object(stop.process, "process_identity", return_value=leader), \
             patch.object(stop.process, "is_alive", return_value=alive):
            return stop._tree(self.identity)

    def test_active_empty_job_with_dead_leader_is_verified(self):
        for leader in (None, self.identity):
            with self.subTest(retained_identity=leader is not None):
                self.assertEqual((True, []), self.observe(members=[], leader=leader))

    def test_active_empty_job_with_live_leader_fails_closed(self):
        self.assertEqual((False, None), self.observe(members=[], alive=True, leader=self.identity))

    def test_unverifiable_job_fails_closed(self):
        self.assertEqual((False, None), self.observe(status="unverifiable"))

    def test_empty_job_with_reused_pid_fails_closed(self):
        reused = {**self.identity, "started_at": "5678", "job_name": "Local\\HarnessHarbor-101-5678"}
        for alive in (True, False):
            with self.subTest(alive=alive):
                self.assertEqual((False, None), self.observe(members=[], alive=alive, leader=reused))

    def test_missing_job_is_not_verified_empty(self):
        self.assertEqual((False, None), self.observe(status="confirmed_missing", members=[]))

    def test_job_query_error_fails_closed(self):
        for error in (PermissionError("access denied"), OSError("query failed")):
            with self.subTest(error=type(error).__name__), \
                 patch.object(stop.sys, "platform", "win32"), \
                 patch("harbor_platform.windows_process.inspect_job", side_effect=error):
                self.assertEqual((False, None), stop._tree(self.identity))

    def test_malformed_identity_fails_closed(self):
        for identity in (None, {}, {"pid": "101"}, {"pid": 101},
                         {**self.identity, "job_name": "foreign-job"}):
            with self.subTest(identity=identity), \
                 patch.object(stop.sys, "platform", "win32"), \
                 patch("harbor_platform.windows_process.api", side_effect=AssertionError("No valid job name")):
                self.assertEqual((False, None), stop._tree(identity))


if __name__ == "__main__":
    unittest.main()
