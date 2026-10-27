import copy
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


spec = importlib.util.spec_from_file_location("report", Path(__file__).with_name("report.py"))
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def fixture():
    allocations = {"requests": {"cpu": 0.1, "memory": 104857600},
                   "limits": {"cpu": None, "memory": 268435456}}
    return {"scans": [{
        "object": {"namespace": "home", "name": name, "kind": "Deployment", "container": "app",
                   "allocations": copy.deepcopy(allocations), "pods": [{"name": name + "-abc"}],
                   "labels": {"secret": "do-not-publish"}, "annotations": {"secret": "do-not-publish"}},
        "recommended": {section: {key: {"value": value} for key, value in values.items()}
                        for section, values in allocations.items()},
    } for name in sorted(report.WORKLOADS)], "config": {"token": "do-not-publish"}, "errors": []}


OBSERVED = {"coverage": 1, "oom_events": 0, "peak_cpu": 0.09, "peak_memory": 90000000}


class Reports(unittest.TestCase):
    def test_sanitized_report_excludes_configuration_and_annotations(self):
        with patch.object(report, "evidence", return_value=OBSERVED):
            scans = report.sanitize(fixture())
        text = json.dumps(scans) + report.report(scans, "test-time")
        self.assertNotIn("do-not-publish", text)
        self.assertNotIn("annotations", text)
        self.assertIn("100m / unset", text)
        self.assertIn("100Mi / 256Mi", text)
        self.assertIn("AI/model workloads are excluded", text)

    def test_unknown_unset_and_insufficient_history_remain_distinct(self):
        data = fixture()
        data["scans"][0]["recommended"]["requests"]["cpu"]["value"] = "?"
        with patch.object(report, "evidence", return_value={**OBSERVED, "coverage": 0.4, "oom_events": None}):
            scans = report.sanitize(data)
        self.assertEqual(scans[0]["recommended"]["requests"]["cpu"], "?")
        self.assertIsNone(scans[0]["recommended"]["limits"]["cpu"])
        self.assertEqual(len(scans[0]["gaps"]), 3)
        self.assertIn("unknown / unset", report.report(scans, "time"))

    def test_hpa_and_oom_require_review(self):
        data = fixture()
        data["scans"][0]["object"]["hpa"] = {"target_cpu_utilization_percentage": 70}
        with patch.object(report, "evidence", return_value={**OBSERVED, "oom_events": 2}):
            scans = report.sanitize(data)
        self.assertTrue(any("HPA" in gap for gap in scans[0]["gaps"]))
        self.assertTrue(any("OOM events" in gap for gap in scans[0]["gaps"]))

    def test_optional_oom_warning_preserves_numeric_recommendations(self):
        data = fixture()
        data["scans"][0]["object"]["warnings"] = ["NoPrometheusMemoryMetrics"]
        with patch.object(report, "evidence", return_value=OBSERVED):
            scans = report.sanitize(data)
        self.assertEqual(scans[0]["recommended"]["requests"]["memory"], 104857600)
        self.assertIn("optional OOM-limit history", scans[0]["gaps"][0])
        self.assertNotIn("insufficient data or HPA", scans[0]["gaps"][0])

    def test_errors_missing_targets_and_wrong_namespace_fail_closed(self):
        bad = []
        data = fixture(); data["errors"] = [{"error": "private"}]; bad.append(data)
        data = fixture(); data["scans"].pop(); bad.append(data)
        data = fixture(); data["scans"][0]["object"]["namespace"] = "ai"; bad.append(data)
        for data in bad:
            with self.subTest(data=data), patch.object(report, "evidence", return_value=OBSERVED):
                with self.assertRaises(ValueError):
                    report.sanitize(data)

    def test_unselected_workloads_are_not_published(self):
        data = fixture()
        extra = copy.deepcopy(data["scans"][0]); extra["object"]["name"] = "home-assistant"
        data["scans"].append(extra)
        with patch.object(report, "evidence", return_value=OBSERVED):
            self.assertEqual(len(report.sanitize(data)), 3)

    def test_invalid_resource_values_fail(self):
        for value in [float("nan"), float("inf"), -1, True, "private"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                report.number(value)

    def test_coverage_uses_minimum_of_cpu_and_memory_without_replica_summing(self):
        values = [report.EXPECTED_POINTS, report.EXPECTED_POINTS / 2, 0.2, 104857600, 0]
        with patch.object(report, "query", side_effect=[[{"value": [0, str(v)]}] for v in values]) as query:
            evidence = report.evidence(fixture()["scans"][0]["object"])
        self.assertEqual(evidence["coverage"], 0.5)
        self.assertEqual(evidence["peak_cpu"], 0.2)
        self.assertIn("max(rate(", query.call_args_list[0].args[0])

    def test_empty_history_is_a_gap(self):
        with patch.object(report, "query", return_value=[]):
            evidence = report.evidence(fixture()["scans"][0]["object"])
        self.assertEqual(evidence["coverage"], 0)
        self.assertIsNone(evidence["oom_events"])

    def test_pod_name_dots_are_literal_in_promql_regex(self):
        obj = fixture()["scans"][0]["object"]
        obj["pods"] = [{"name": "mealie.test-abc"}]
        with patch.object(report, "query", return_value=[]) as query:
            report.evidence(obj)
        self.assertIn('pod=~' + json.dumps(r"mealie\.test\-abc"), query.call_args.args[0])

    def test_upload_requires_success_and_exact_readback(self):
        payload = b'{"safe": true}'
        with patch.dict(os.environ, {"VIKUNJA_API_TOKEN": "private"}):
            with patch.object(report, "request", side_effect=[{"success": [{"id": 12}]}, payload]) as request:
                report.upload(44, payload)
                self.assertIn(b'name="files"', request.call_args_list[0].args[1])
                self.assertTrue(request.call_args_list[1].kwargs["raw"])
            for responses in [[{"success": [], "errors": [{"message": "private"}]}],
                              [{"success": [{"id": 12}]}, b"wrong"]]:
                with patch.object(report, "request", side_effect=responses), self.assertRaises(ValueError):
                    report.upload(44, payload)

    def test_no_ready_notification_before_verified_attachment(self):
        projects = {"items": [{"id": 2, "title": "LabV2"}]}
        task = {"id": 55, "project_id": 2}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "krr.json"; path.write_text(json.dumps(fixture()))
            with patch.object(report, "vikunja", side_effect=[projects, task]), \
                 patch.object(report.subprocess, "run") as command, \
                 patch.object(report, "Path", return_value=path), \
                 patch.object(report, "evidence", return_value=OBSERVED), \
                 patch.object(report, "upload", side_effect=ValueError("private")), \
                 patch.object(report, "notify") as notify, \
                 patch.dict(os.environ, {"VIKUNJA_API_TOKEN": "private", "LITELLM_API_KEY": "private"}):
                with self.assertRaisesRegex(RuntimeError, "KRR reporting failed"):
                    report.main()
                self.assertEqual(notify.call_count, 1)
                self.assertIn("failed", notify.call_args.args[0])
                self.assertNotIn("--selector", command.call_args.args[0])
                self.assertNotIn("--allow_hpa", command.call_args.args[0])
                self.assertNotIn("LITELLM_API_KEY", command.call_args.kwargs["env"])
                self.assertNotIn("VIKUNJA_API_TOKEN", command.call_args.kwargs["env"])
                self.assertEqual(command.call_args.kwargs["timeout"], 1400)

    def test_mcp_error_is_not_mistaken_for_success(self):
        with patch.dict(os.environ, {"LITELLM_API_KEY": "private"}), \
             patch.object(report, "request", return_value={"isError": True}):
            with self.assertRaisesRegex(RuntimeError, "Vikunja MCP failed"):
                report.vikunja("tasks_create", {})

    def test_success_delivers_sanitized_json_before_notification(self):
        calls = []
        def upload(task_id, payload):
            self.assertEqual(task_id, 55)
            decoded = json.loads(payload)
            self.assertEqual(decoded["history_hours"], 336)
            self.assertEqual(len(decoded["scans"]), 3)
            self.assertNotIn("do-not-publish", payload.decode())
            calls.append("uploaded")
        def notify(message, url):
            self.assertEqual(calls, ["uploaded"])
            self.assertEqual(url, "https://vikunja.aumer.dev/tasks/55")
            calls.append("notified")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "krr.json"; path.write_text(json.dumps(fixture()))
            with patch.object(report, "vikunja", side_effect=[{"items": [{"id": 2, "title": "LabV2"}]},
                                                              {"id": 55, "project_id": 2}]), \
                 patch.object(report.subprocess, "run"), patch.object(report, "Path", return_value=path), \
                 patch.object(report, "evidence", return_value=OBSERVED), \
                 patch.object(report, "upload", side_effect=upload), \
                 patch.object(report, "notify", side_effect=notify), patch("builtins.print"):
                report.main()
        self.assertEqual(calls, ["uploaded", "notified"])

    def test_http_errors_do_not_publish_response_bodies(self):
        from io import BytesIO
        error = HTTPError("http://example", 403, "private", {}, BytesIO(b"private"))
        with patch.object(report.HTTP, "open", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "^Upstream HTTP 403$"):
                report.request("http://example")

    def test_redirects_are_rejected(self):
        self.assertIsNone(report.NoRedirect().redirect_request(None, None, 302, "", {}, "http://untrusted"))


if __name__ == "__main__":
    unittest.main()
