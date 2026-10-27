"""Approval boundaries, evidence gates, recovery, and shared-state serialization."""

import copy
import importlib.util
import json
import os
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("publication_monitor", Path(__file__).with_name("monitor.py"))
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.now = 50000
        self.item = {"id": "observation", "check": "memory_pressure", "labels": {
            "namespace": "home", "pod": "minecraft", "container": "app"},
            "query": monitor.QUERIES["memory_pressure"], "status": "observing", "consecutive": 5,
            "first_seen": 46000, "last_seen": self.now, "last_investigated": 49900,
            "investigation_job_id": "collection-job", "trace_id": "trace", "assessment": {
                "verdict": "actionable", "summary": "Persistent memory pressure", "evidence": ["Six-hour history"],
                "next_step": "Inspect workload", "hermes_session_id": "session", "tool_activity": [{
                    "query_success": True, "query_arguments": {"query": "", "start_time": "28300",
                                                              "end_time": "49900", "step": "30m"}}]}}
        self.item["assessment"]["tool_activity"][0]["query_arguments"]["query"] = monitor.metric_history(self.item)
        self.state = {"version": 1, "last_run": {"coverage": "metrics_complete", "at": self.now},
                      "observations": {self.item["id"]: self.item}}
        self.settings = {"mode": "approval_required", "paused": False, "reviewer": "owner"}
        self.job = {"created_by": "owner", "script_path": "f/cluster/monitor", "job_kind": "script"}
        self.saved = []
        self.draft = monitor.draft_for(self.state, self.item, self.now)

    def wm(self, path, *args, **kwargs):
        if path.startswith("/jobs_u/get/"):
            return self.job
        if path == "/resources/get_value/" + monitor.STATE_PATH:
            return copy.deepcopy(self.state)
        if path == "/resources/get_value/" + monitor.PUBLICATION_PATH:
            return copy.deepcopy(self.settings)
        self.fail("Unexpected Windmill call: " + path)

    def save(self, state):
        self.state = copy.deepcopy(state)
        self.saved.append(copy.deepcopy(state))

    def review(self, action="approve", draft_hash=None, approve=True):
        with patch.object(monitor.time, "time", return_value=self.now), \
             patch.object(monitor, "wm", side_effect=self.wm), \
             patch.object(monitor, "save", side_effect=self.save), \
             patch.dict(os.environ, {"WM_JOB_ID": "approval-job"}):
            return monitor.review_draft(self.settings, self.state, action, self.item["id"],
                                        self.draft["hash"] if draft_hash is None else draft_hash, approve)

    def test_draft_contains_evidence_workload_and_correlation_without_publication(self):
        with patch.object(monitor, "publish_draft") as publish:
            result = self.review(action="drafts")
        publish.assert_not_called()
        text = result["drafts"][0]["payload"]["description"]
        for required in ("minecraft", "28300", "49900", "trace", "collection-job", "session", "may contain errors"):
            self.assertIn(required, text)
        self.assertEqual(self.draft["expires_at"], 71500)

    def test_missing_stale_incomplete_and_failed_evidence_cannot_create_drafts(self):
        cases = [{"status": "resolved"}, {"consecutive": 2}, {"first_seen": 49900},
                 {"last_seen": 47000}, {"last_investigated": 20000}, {"investigation_error": "TimeoutError"},
                 {"trace_error": "HTTPError"}, {"trace_id": ""}, {"investigation_job_id": ""}]
        for change in cases:
            with self.subTest(change=change):
                item = {**copy.deepcopy(self.item), **change}
                self.assertIsNone(monitor.draft_for(self.state, item, self.now))
        for verdict in ("expected", "uncertain"):
            item = copy.deepcopy(self.item)
            item["assessment"]["verdict"] = verdict
            self.assertIsNone(monitor.draft_for(self.state, item, self.now))
        for last_run in ({"coverage": "incomplete", "at": 50000}, {"coverage": "metrics_complete", "at": 40000}):
            self.assertIsNone(monitor.draft_for({**self.state, "last_run": last_run}, self.item, self.now))

    def test_successful_irrelevant_or_wrong_window_query_cannot_validate_publication(self):
        for change in ({"query": "sum(up)"}, {"end_time": "45000"}, {"start_time": "49000"}, {"step": "1s"}):
            item = copy.deepcopy(self.item)
            item["assessment"]["tool_activity"][0]["query_arguments"].update(change)
            self.assertIsNone(monitor.draft_for(self.state, item, self.now))

    def test_approval_is_persisted_before_publication_and_audits_exact_draft(self):
        def publish(draft):
            record = self.saved[-1]["publication_audit"][draft["hash"]]
            self.assertEqual(record["status"], "approved")
            self.assertEqual(record["reviewer"], "owner")
            self.assertEqual(record["job_id"], "approval-job")
            return 101
        with patch.object(monitor, "publish_draft", side_effect=publish):
            record = self.review()
        self.assertEqual(record["task_id"], 101)
        self.assertEqual(record["status"], "published")

    def test_rejection_and_replayed_decisions_never_publish(self):
        with patch.object(monitor, "publish_draft") as publish:
            self.assertEqual(self.review(action="reject")["status"], "rejected")
            self.assertEqual(self.review()["status"], "rejected")
        publish.assert_not_called()

    def test_identity_schedule_child_jobs_and_preview_cannot_approve(self):
        for change in ({"created_by": "attacker"}, {"schedule_path": "schedule"}, {"parent_job": "parent"},
                       {"trigger_kind": "schedule"}, {"script_path": "other"}, {"job_kind": "preview"}):
            with self.subTest(change=change), patch.object(monitor, "publish_draft") as publish:
                self.job = {"created_by": "owner", "script_path": "f/cluster/monitor", "job_kind": "script", **change}
                with self.assertRaisesRegex(ValueError, "administrator"):
                    self.review()
                publish.assert_not_called()

    def test_checkbox_hash_pause_and_expiry_gates(self):
        with patch.object(monitor, "publish_draft") as publish:
            with self.assertRaisesRegex(ValueError, "checkbox"):
                self.review(approve=False)
            with self.assertRaisesRegex(ValueError, "changed"):
                self.review(draft_hash="other")
            self.settings["paused"] = True
            with self.assertRaisesRegex(ValueError, "paused"):
                self.review()
            self.settings["paused"] = False
            self.now = self.draft["expires_at"]
            with self.assertRaisesRegex(ValueError, "changed"):
                self.review()
            publish.assert_not_called()

    def test_persistence_failure_prevents_write(self):
        with patch.object(monitor.time, "time", return_value=self.now), \
             patch.object(monitor, "wm", side_effect=self.wm), \
             patch.object(monitor, "save", side_effect=OSError), \
             patch.object(monitor, "publish_draft") as publish, \
             patch.dict(os.environ, {"WM_JOB_ID": "approval-job"}), self.assertRaises(OSError):
            monitor.review_draft(self.settings, self.state, "approve", self.item["id"], self.draft["hash"], True)
        publish.assert_not_called()

    def test_settings_change_after_approval_blocks_publication(self):
        original_save = self.save
        def save_and_pause(state):
            original_save(state)
            self.settings["paused"] = True
        with patch.object(monitor.time, "time", return_value=self.now), \
             patch.object(monitor, "wm", side_effect=self.wm), \
             patch.object(monitor, "save", side_effect=save_and_pause), \
             patch.object(monitor, "publish_draft") as publish, \
             patch.dict(os.environ, {"WM_JOB_ID": "approval-job"}), self.assertRaisesRegex(ValueError, "settings changed"):
            monitor.review_draft(self.settings, self.state, "approve", self.item["id"], self.draft["hash"], True)
        publish.assert_not_called()

    def test_paused_publication_preserves_collection_and_shadow_blocks_review(self):
        for mode in ("shadow", "approval_required"):
            with patch.object(monitor, "monitor_lock", return_value=nullcontext()), \
                 patch.object(monitor, "wm", return_value={"mode": mode, "paused": True}), \
                 patch.object(monitor, "collect_main", return_value={"mode": mode}) as collect:
                self.assertEqual(monitor.main()["mode"], mode)
                collect.assert_called_once_with(mode)
        for mode in ("shadow", "automatic"):
            with patch.object(monitor, "monitor_lock", return_value=nullcontext()), \
                 patch.object(monitor, "wm", return_value={"mode": mode}), \
                 patch.object(monitor, "review_draft") as review, self.assertRaises(ValueError):
                monitor.main(action="approve", approve=True)
            review.assert_not_called()

    def test_query_history_matches_successful_call_id_and_discards_other_arguments(self):
        arguments = {"tool_id": "query-tool", "arguments": {"query": "sum(up)", "end_time": "50000",
                                                              "start_time": "28400", "private": "excluded"}}
        messages = [{"role": "assistant", "tool_calls": [{"id": "call-1", "function": {
            "arguments": json.dumps(arguments)}}]}, {"role": "tool", "tool_call_id": "call-1",
            "tool_name": "mcp__litellm__call_tool", "content": '{"result":"samples"}'}]
        with patch.object(monitor, "http", return_value={"data": messages}), \
             patch.dict(os.environ, {"HERMES_API_KEY": "test"}):
            activity = monitor.investigation_tools("session")
        self.assertTrue(activity[0]["query_success"])
        self.assertEqual(activity[0]["query_arguments"], {"query": "sum(up)", "end_time": "50000", "start_time": "28400"})

    def test_all_metric_histories_scope_the_observed_resource(self):
        cases = {"restarts": {"namespace": "home", "pod": "pod", "container": "app"},
                 "storage_growth": {"namespace": "home", "persistentvolumeclaim": "minecraft"},
                 "flux_not_ready": {"exported_namespace": "ai", "name": "windmill", "customresource_kind": "Kustomization"},
                 "unavailable_replicas": {"namespace": "ai", "deployment": "windmill-app"},
                 "failed_scrapes": {"job": "prometheus", "instance": "host:9090"}}
        for check, labels in cases.items():
            with self.subTest(check=check):
                expression = monitor.metric_history({"check": check, "query": monitor.QUERIES[check], "labels": labels})
                for key, value in labels.items():
                    self.assertIn(key + "=" + json.dumps(value), expression)
                self.assertNotIn("{}", expression)

    def test_lost_write_response_retains_approval_for_identity_recovery(self):
        with patch.object(monitor, "publish_draft", side_effect=TimeoutError), self.assertRaises(TimeoutError):
            self.review()
        self.assertEqual(self.saved[-1]["publication_audit"][self.draft["hash"]]["status"], "approved")

    def test_existing_open_or_done_task_is_reused_without_changes(self):
        for done in (False, True):
            with self.subTest(done=done), patch.object(monitor, "publication_mcp", side_effect=[
                {"items": [{"id": 2, "title": "LabV2"}]},
                {"items": [{"id": 101, "project_id": 2, "done": done, "title": "Existing [monitor:observation]"}]},
            ]) as mcp:
                self.assertEqual(monitor.publish_draft(self.draft), 101)
                self.assertEqual([c.args[0] for c in mcp.call_args_list], ["projects_list", "tasks_list"])

    def test_new_task_uses_resolved_project_and_default_board_bucket(self):
        with patch.object(monitor, "publication_mcp", side_effect=[
            {"items": [{"id": 2, "title": "LabV2", "views": [{"view_kind": "kanban", "default_bucket_id": 4}]}]},
            {"items": [], "page": 1, "per_page": 50, "total": 0, "total_pages": 0},
            {"id": 101, "project_id": 2},
        ]) as mcp:
            self.assertEqual(monitor.publish_draft(self.draft), 101)
        payload = mcp.call_args.args[1]
        self.assertEqual(payload["project"], 2)
        self.assertEqual(payload["bucket_id"], 4)
        self.assertFalse(payload["done"])
        self.assertEqual(payload["description"], self.draft["payload"]["description"])

    def test_ambiguous_lookup_never_creates(self):
        for response in ({"items": [], "total_pages": 2}, {"items": [
            {"id": 1, "project_id": 2, "title": "[monitor:observation]"},
            {"id": 2, "project_id": 2, "title": "[monitor:observation]"}]}):
            with patch.object(monitor, "publication_mcp", side_effect=[
                {"items": [{"id": 2, "title": "LabV2"}]}, response,
            ]) as mcp, self.assertRaisesRegex(ValueError, "Ambiguous"):
                monitor.publish_draft(self.draft)
            self.assertEqual(mcp.call_count, 2)


class SerializationTests(unittest.TestCase):
    def test_listing_or_collection_with_approval_inputs_cannot_silently_succeed(self):
        for action in ("collect", "drafts"):
            for inputs in ({"draft_id": "observation"}, {"draft_hash": "hash"}, {"approve": True}):
                with self.subTest(action=action, inputs=inputs), patch.object(monitor, "monitor_lock") as lock:
                    with self.assertRaisesRegex(ValueError, "Nothing was approved.*Monitor Review app"):
                        monitor.main(action=action, **inputs)
                    lock.assert_not_called()

    def test_lock_covers_body_and_releases_on_failure(self):
        with patch.dict(os.environ, {"WM_JOB_ID": "owner-job"}), patch.object(monitor, "wm") as wm:
            with self.assertRaises(ValueError), monitor.monitor_lock():
                self.assertTrue(wm.call_args.args[0].endswith("/resources/create"))
                raise ValueError("body failed")
        self.assertEqual(wm.call_args.args[0], "/resources/delete/" + monitor.LOCK_PATH)
        self.assertEqual(wm.call_args.kwargs["method"], "DELETE")

    def test_contender_never_runs_or_deletes_owner_lock(self):
        with patch.dict(os.environ, {"WM_JOB_ID": "contender"}), \
             patch.object(monitor, "wm", side_effect=[RuntimeError("conflict"), {"job_id": "owner"}]) as wm:
            with self.assertRaisesRegex(RuntimeError, "locked"), monitor.monitor_lock():
                self.fail("Contender entered critical section")
        self.assertEqual(wm.call_count, 2)

    def test_lost_lock_creation_response_recovers_only_own_admission(self):
        with patch.dict(os.environ, {"WM_JOB_ID": "owner"}), \
             patch.object(monitor, "wm", side_effect=[TimeoutError(), {"job_id": "owner"}, None]) as wm:
            with monitor.monitor_lock():
                pass
        self.assertEqual(wm.call_count, 3)


if __name__ == "__main__":
    unittest.main()
