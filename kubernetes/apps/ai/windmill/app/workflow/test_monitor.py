import copy
import importlib.util
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


monitor = load("monitor")
bootstrap = load("bootstrap")
readiness = load("ready")


class MonitoringTests(unittest.TestCase):
    def evidence(self, value=0.9):
        return [{"id": "stable", "check": "memory_pressure", "labels": {"namespace": "ai"},
                 "value": value, "query": "test", "timestamp": 10000}]

    def test_persistence_and_cooldown(self):
        state = {"version": 1}
        self.assertEqual(monitor.observe(state, self.evidence(), 10000), [])
        self.assertEqual(monitor.observe(state, self.evidence(), 10900), [])
        ready = monitor.observe(state, self.evidence(0.95), 11800)
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["first_seen"], 10000)
        ready[0]["last_investigated"] = 11800
        self.assertEqual(monitor.observe(state, self.evidence(), 12700), [])

    def test_gap_and_reappearance_reset_persistence(self):
        state = {"version": 1}
        monitor.observe(state, self.evidence(), 10000)
        self.assertEqual(monitor.observe(state, self.evidence(), 13000), [])
        self.assertEqual(state["observations"]["stable"]["first_seen"], 13000)
        monitor.observe(state, [], 13900)
        self.assertEqual(state["observations"]["stable"]["status"], "resolved")
        monitor.observe(state, self.evidence(), 14800)
        self.assertEqual(state["observations"]["stable"]["consecutive"], 1)

    def test_missing_metrics_do_not_claim_coverage(self):
        with patch.object(monitor, "query", return_value=[]):
            with self.assertRaisesRegex(ValueError, "Missing or stale"):
                monitor.collect(10000)

    def test_prometheus_warnings_rejected(self):
        response = {"status": "success", "warnings": ["partial result"],
                    "data": {"resultType": "vector", "result": []}}
        with patch.object(monitor, "http", return_value=response):
            with self.assertRaises(ValueError):
                monitor.query("up", 10000)

    def test_nonfinite_sample_rejected(self):
        def query(expression, now):
            return [{"metric": {}, "value": [now, str(now) if expression.startswith("max(timestamp(") else "NaN"]}]
        with patch.object(monitor, "query", side_effect=query):
            with self.assertRaisesRegex(ValueError, "Non-finite"):
                monitor.collect(10000)

    def test_all_active_resources_keep_their_persistence(self):
        def query(expression, now):
            self.assertNotIn("topk(", expression)
            if expression.startswith("max(timestamp("):
                return [{"metric": {}, "value": [now, str(now)]}]
            if expression == monitor.QUERIES["memory_pressure"]:
                return [{"metric": {"pod": "pod-" + str(i)}, "value": [now, str(0.9 + i / 1000)]} for i in range(6)]
            return []
        state = {"version": 1}
        with patch.object(monitor, "query", side_effect=query):
            for now in (10000, 10900, 11800):
                candidates = monitor.observe(state, monitor.collect(now), now)
        self.assertEqual(len(candidates), 6)
        self.assertTrue(all(item["consecutive"] == 3 and item["status"] == "observing" for item in candidates))

    def test_excess_signals_fail_coverage_instead_of_truncating(self):
        def query(expression, now):
            if expression.startswith("max(timestamp("):
                return [{"metric": {}, "value": [now, str(now)]}]
            return [{"metric": {"pod": str(i)}, "value": [now, "1"]} for i in range(monitor.MAX_SIGNALS + 1)]
        with patch.object(monitor, "query", side_effect=query):
            with self.assertRaisesRegex(ValueError, "Signal limit exceeded"):
                monitor.collect(10000)

    def test_failed_collection_preserves_observations(self):
        state = {"version": 1}
        monitor.observe(state, self.evidence(), 10000)
        with patch.object(monitor, "wm", return_value=json.dumps(state)), \
             patch.object(monitor, "collect", side_effect=ValueError("missing")), \
             patch.object(monitor, "save") as save:
            with self.assertRaisesRegex(RuntimeError, "coverage incomplete"):
                monitor.main()
            saved = save.call_args.args[0]
            self.assertEqual(saved["observations"]["stable"]["status"], "observing")
            self.assertEqual(saved["last_run"]["coverage"], "incomplete")

    def test_budget_reserved_before_failed_investigation(self):
        state = {"version": 1, "observations": {}}
        for now in (10000, 10900):
            monitor.observe(state, self.evidence(), now)
        persisted = []
        with patch.object(monitor, "wm", return_value=json.dumps(state)), \
             patch.object(monitor.time, "time", return_value=11800), \
             patch.object(monitor, "collect", return_value=self.evidence()), \
             patch.object(monitor, "save", side_effect=lambda s: persisted.append(copy.deepcopy(s))), \
             patch.object(monitor, "investigate", side_effect=TimeoutError), \
             patch.object(monitor, "trace", return_value="trace"):
            with self.assertRaisesRegex(RuntimeError, "Investigation or trace"):
                monitor.main()
        self.assertEqual(persisted[1]["budget"]["investigations"], 1)
        self.assertNotIn("assessment", persisted[-1]["observations"]["stable"])
        self.assertEqual(persisted[-1]["observations"]["stable"]["investigation_error"], "TimeoutError")

    def test_daily_limit_blocks_new_calls(self):
        state = {"version": 1, "budget": {"day": "1970-01-01", "investigations": 8}}
        for now in (10000, 10900):
            monitor.observe(state, self.evidence(), now)
        with patch.object(monitor, "wm", return_value=json.dumps(state)), \
             patch.object(monitor.time, "time", return_value=11800), \
             patch.object(monitor, "collect", return_value=self.evidence()), \
             patch.object(monitor, "save"), patch.object(monitor, "investigate") as investigate:
            result = monitor.main()
        investigate.assert_not_called()
        self.assertEqual(result["issues_created"], 0)

    def test_due_log_review_precedes_metric_backlog(self):
        state = {"version": 1}
        for now in (10000, 10900):
            monitor.observe(state, self.evidence(), now)
        investigated = []
        with patch.object(monitor, "wm", return_value=json.dumps(state)), \
             patch.object(monitor.time, "time", return_value=11800), \
             patch.object(monitor, "collect", return_value=self.evidence()), \
             patch.object(monitor, "save"), patch.object(monitor, "trace", return_value="trace"), \
             patch.object(monitor, "investigate", side_effect=lambda item: investigated.append(item["check"]) or {"verdict": "uncertain"}):
            monitor.main()
        self.assertEqual(investigated, ["scheduled_log_review", "memory_pressure"])

    def test_log_review_covers_interval_since_previous_success(self):
        state = {"version": 1, "log_review": {"id": "scheduled-log-review", "check": "scheduled_log_review",
                 "labels": {}, "value": 0, "query": "logs", "first_seen": 10000,
                 "last_investigated": 28000, "last_review_end": 28000}}
        with patch.object(monitor, "wm", return_value=json.dumps(state)), \
             patch.object(monitor.time, "time", return_value=50000), \
             patch.object(monitor, "collect", return_value=[]), \
             patch.object(monitor, "save"), patch.object(monitor, "trace", return_value="trace"), \
             patch.object(monitor, "investigate", return_value={"verdict": "expected"}) as investigate:
            monitor.main()
        item = investigate.call_args.args[0]
        self.assertEqual(item["review_start"], 28000)
        self.assertEqual(item["review_end"], 50000)
        self.assertEqual(item["coverage_gap_seconds"], 0)

    def test_uncertain_log_review_does_not_advance_coverage(self):
        state = {"version": 1}
        saved = []
        with patch.object(monitor, "wm", side_effect=lambda path: json.dumps(state)), \
             patch.object(monitor.time, "time", return_value=50000), \
             patch.object(monitor, "collect", return_value=[]), \
             patch.object(monitor, "save", side_effect=lambda value: saved.append(copy.deepcopy(value))), \
             patch.object(monitor, "trace", return_value="trace"), \
             patch.object(monitor, "investigate", return_value={"verdict": "uncertain"}):
            monitor.main()
        self.assertEqual(saved[-1]["log_review"]["last_review_end"], 50000 - 6 * 3600)

    def test_untrusted_agent_output_rejected(self):
        response = {"choices": [{"message": {"content": '{"verdict":"delete-cluster"}'}}]}
        with patch.object(monitor, "http", return_value=response), patch.dict(os.environ, {"HERMES_API_KEY": "test"}):
            with self.assertRaisesRegex(ValueError, "verdict"):
                monitor.investigate(self.evidence()[0])

    def test_trace_contains_no_raw_assessment_or_credentials(self):
        with patch.dict(os.environ, {"LANGFUSE_PUBLIC_KEY": "public-test", "LANGFUSE_SECRET_KEY": "secret-test"}), \
             patch.object(monitor, "http", return_value={"errors": []}) as http:
            monitor.trace("job", self.evidence()[0], {"verdict": "actionable", "summary": "private-content"}, 1, 2)
        serialized = json.dumps(http.call_args.args[1])
        self.assertNotIn("private-content", serialized)
        self.assertNotIn("secret-test", serialized)
        self.assertEqual(http.call_args.args[1]["batch"][0]["body"]["sessionId"], "stable")


class BootstrapTests(unittest.TestCase):
    def test_readiness_requires_owner_and_disabled_demo(self):
        owner = {"email": "owner@example.invalid", "disabled": False}
        self.assertFalse(readiness.ready([], owner["email"]))
        self.assertFalse(readiness.ready([owner, {"email": "admin@windmill.dev", "disabled": False}], owner["email"]))
        self.assertTrue(readiness.ready([owner, {"email": "admin@windmill.dev", "disabled": True}], owner["email"]))

    def test_existing_schedule_and_state_are_preserved(self):
        calls = []
        source = Path(__file__).with_name("monitor.py").read_text()

        def api(path, data=None, method=None, missing=False):
            calls.append((path, data, method))
            if path.startswith("/users/exists/"):
                return not path.endswith("admin%40windmill.dev")
            if path == "/workspaces/exists":
                return True
            if path.endswith("/users/list"):
                return [{"email": "owner@example.invalid"}]
            if "/scripts/get/p/" in path:
                return {"content": source, "hash": "abc"}
            if "/flows/get/" in path:
                return {"value": {"modules": [{"id": "monitor", "value": {"type": "script", "path": bootstrap.PATH,
                                                                                 "input_transforms": {}}}]}}
            return {"existing": True}

        with patch.dict(os.environ, {"ADMIN_EMAIL": "owner@example.invalid", "ADMIN_PASSWORD": "x" * 32}), \
             patch.object(bootstrap, "api", side_effect=api):
            bootstrap.main()
        self.assertFalse(any("/create" in path or "/update" in path for path, _, _ in calls))


class DeploymentTests(unittest.TestCase):
    def test_worker_passes_monitor_credentials_to_jobs(self):
        path = Path(__file__).parents[1] / "helmrelease.yaml"
        manifest = json.loads(subprocess.check_output(["yq", "-o=json", ".", str(path)], text=True))
        env = manifest["spec"]["values"]["windmill"]["workerGroups"][0]["extraEnv"]
        whitelist = next(item["value"] for item in env if item["name"] == "WHITELIST_ENVS").split(",")
        self.assertEqual(set(whitelist), {"HERMES_API_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"})
        self.assertTrue(all(any(item["name"] == name and "valueFrom" in item for item in env) for name in whitelist))


if __name__ == "__main__":
    unittest.main()
