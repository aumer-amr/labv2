"""Regression checks for scoped security updates and durable change notifications."""

import copy
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONFIG = {'RULE': 'test-rule',
 'STATE': 'test-state',
 'TOKEN': 'test-token',
 'TICKET_TOKEN': 'test-ticket_token',
 'MCP': 'http://gateway.example.test/mcp-rest/tools/call',
 'VIKUNJA_SERVER': 'test-vikunja_server',
 'ALERTMANAGER': 'http://alerts.example.test/api/v2/alerts',
 'PREFIX': '(http.host eq "hooks.example.test" and http.request.method eq "POST" and '
           'http.request.uri.path eq "/hooks" and ip.src in {',
 'RULESET_URL': 'https://cloudflare.example.test/rulesets/test-ruleset',
 'PROJECT_NAME': 'LabV2',
 'TRACKING_URL': 'https://tasks.example.test/tasks/1',
 'VIKUNJA_URL': 'https://tasks.example.test',
 'JOB_URL': 'https://jobs.example.test/sync',
 'GITHUB_META_URL': 'https://api.github.com/meta'}

sync = load("github_hooks")
load_config = sync.config
sync.config = lambda: CONFIG
bootstrap = load("bootstrap")
OLD = ["140.82.112.0/20", "2a0a:a440::/29"]
NEW = ["143.55.64.0/20", "2a0a:a440::/29"]


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.state = {"version": 1}
        self.desired = OLD[:]
        self.rules = [
            {"id": CONFIG["RULE"], "action": "skip", "action_parameters": {"ruleset": "current"},
             "expression": CONFIG["PREFIX"] + " ".join(OLD) + sync.SUFFIX,
             "enabled": True, "logging": {"enabled": True}},
            {"id": "country", "action": "block", "enabled": True,
             "expression": 'ip.src.country eq "US"'},
        ]
        self.calls = []
        self.snapshots = []
        self.fail_notification = False
        self.fail_ticket = False

    def ticket(self, event):
        if self.fail_ticket:
            raise OSError("Vikunja unavailable")
        event.setdefault("ticket_id", 75)

    def http(self, url, body=None, token=None, method=None):
        self.calls.append((url, body, token, method))
        if url == "https://api.github.com/meta":
            return {"hooks": self.desired}
        if url == CONFIG["ALERTMANAGER"]:
            if self.fail_notification:
                raise OSError("unavailable")
            return None
        if url == CONFIG["RULESET_URL"]:
            return {"success": True, "result": {"phase": "http_request_firewall_custom", "kind": "zone",
                                                 "rules": copy.deepcopy(self.rules)}}
        self.assertEqual(url, CONFIG["RULESET_URL"] + "/rules/" + CONFIG["RULE"])
        self.assertEqual(method, "PATCH")
        self.assertEqual(set(body), {"expression"})
        self.assertEqual(token, "test-token")
        self.rules[0]["expression"] = body["expression"]
        return {"success": True}

    def run_sync(self, now=100000):
        with patch.object(sync, "http", side_effect=self.http), \
             patch.object(sync, "wm", return_value="test-token"), \
             patch.object(sync, "ticket", side_effect=self.ticket), \
             patch.object(sync, "save", side_effect=lambda s: self.snapshots.append(copy.deepcopy(s))):
            return sync.synchronize(self.state, now)

    def updates(self):
        return [call for call in self.calls if call[3] == "PATCH"]

    def notifications(self):
        return [call[1][0] for call in self.calls if call[0] == CONFIG["ALERTMANAGER"]]

    def test_first_run_and_reordering_do_not_write_or_alert(self):
        self.desired = list(reversed(OLD)) + [OLD[0]]
        self.assertTrue(self.run_sync()["synchronized"])
        self.assertFalse(self.updates())
        self.assertFalse(self.notifications())
        self.assertEqual(self.state["hooks"], sorted(OLD))

    def test_change_patches_only_target_expression_and_notifies(self):
        self.desired = NEW
        original_country = copy.deepcopy(self.rules[1])
        self.run_sync()
        self.assertEqual(len(self.updates()), 1)
        self.assertEqual(self.rules[1], original_country)
        self.assertEqual(sync.rule_ranges(self.rules[0]), sorted(NEW))
        self.assertEqual(self.snapshots[0]["pending"]["new"], sorted(NEW))
        notice = self.notifications()[0]
        self.assertEqual(notice["labels"]["alertname"], "GitHubWebhookRangesChanged")
        self.assertIn("143.55.64.0/20", notice["annotations"]["description"])
        self.assertIn("140.82.112.0/20", notice["annotations"]["description"])
        self.assertNotIn("pending", self.state)

    def test_notification_failure_is_retried_without_second_rule_update(self):
        self.desired = NEW
        self.fail_notification = True
        with self.assertRaises(OSError):
            self.run_sync()
        self.assertIn("pending", self.state)
        event_id = self.state["pending"]["id"]
        self.fail_notification = False
        self.calls = []
        self.run_sync()
        self.assertFalse(self.updates())
        self.assertEqual(self.notifications()[0]["labels"]["change"], event_id)
        self.assertNotIn("pending", self.state)

    def test_ticket_failure_keeps_intent_and_does_not_block_alertmanager(self):
        self.desired = NEW
        self.fail_ticket = True
        with self.assertRaisesRegex(RuntimeError, "ticket creation remains pending"):
            self.run_sync()
        self.assertIn("pending", self.state)
        self.assertIn("Ticket creation pending", self.notifications()[0]["annotations"]["description"])
        self.fail_ticket = False
        self.calls = []
        self.run_sync()
        self.assertFalse(self.updates())
        self.assertIn("https://tasks.example.test/tasks/75", self.notifications()[0]["annotations"]["description"])
        self.assertNotIn("pending", self.state)

    def test_upstream_change_is_reported_if_rule_was_already_updated(self):
        self.state["hooks"] = OLD
        self.desired = NEW
        self.rules[0]["expression"] = CONFIG["PREFIX"] + " ".join(NEW) + sync.SUFFIX
        self.run_sync()
        self.assertFalse(self.updates())
        self.assertEqual(len(self.notifications()), 1)

    def test_delayed_notification_survives_long_alertmanager_outage(self):
        self.desired = NEW
        self.fail_notification = True
        with self.assertRaises(OSError):
            self.run_sync()
        self.fail_notification = False
        self.calls = []
        self.run_sync(now=120000)
        self.assertFalse(self.updates())
        self.assertEqual(self.notifications()[0]["endsAt"], sync.stamp(127200))
        self.assertNotIn("pending", self.state)

    def test_active_change_is_resent_with_same_identity_and_fixed_expiry(self):
        self.desired = NEW
        self.run_sync()
        first = self.notifications()[0]
        self.calls = []
        self.run_sync(now=103600)
        notice = self.notifications()[0]
        self.assertEqual(notice["labels"], first["labels"])
        self.assertEqual(notice["startsAt"], first["startsAt"])
        self.assertEqual(notice["endsAt"], first["endsAt"])
        self.assertFalse(self.updates())
        self.calls = []
        self.run_sync(now=110000)
        self.assertFalse(self.notifications())
        self.assertNotIn("last_change", self.state)

    def test_state_failure_prevents_cloudflare_write(self):
        self.desired = NEW
        with patch.object(sync, "http", side_effect=self.http), \
             patch.object(sync, "wm", return_value="test-token"), \
             patch.object(sync, "save", side_effect=OSError):
            with self.assertRaises(OSError):
                sync.synchronize(self.state, 100000)
        self.assertFalse(self.updates())

    def test_rule_order_change_is_detected_after_update(self):
        self.desired = NEW
        before = copy.deepcopy(self.rules)
        after = copy.deepcopy(self.rules)
        after[0]["expression"] = CONFIG["PREFIX"] + " ".join(NEW) + sync.SUFFIX
        after.reverse()
        with patch.object(sync, "http", side_effect=self.http), \
             patch.object(sync, "wm", return_value="test-token"), \
             patch.object(sync, "save"), \
             patch.object(sync, "ruleset", side_effect=[before, after]):
            with self.assertRaisesRegex(ValueError, "order"):
                sync.synchronize(self.state, 100000)
        self.assertFalse(self.notifications())

    def test_invalid_upstream_ranges_cannot_reach_cloudflare(self):
        for values in ([], ["0.0.0.0/0", OLD[1]], ["127.0.0.0/8", OLD[1]],
                       ["140.82.112.1/20", OLD[1]], ["invalid"], [True], [OLD[0]], OLD * 40):
            with self.subTest(values=values):
                self.calls = []
                self.desired = values
                with self.assertRaises((ValueError, TypeError)):
                    self.run_sync()
                self.assertEqual(len(self.calls), 1)

    def test_scope_or_skip_setting_drift_prevents_writes(self):
        original = copy.deepcopy(self.rules[0])
        for change in ({"action": "block"}, {"enabled": False}, {"logging": {"enabled": False}},
                       {"action_parameters": {"ruleset": "current", "phases": ["http_request_firewall_managed"]}},
                       {"expression": original["expression"].replace('"/hooks"', '"/"')},
                       {"expression": original["expression"] + ' or http.host eq "other.example.test"'}):
            with self.subTest(change=change):
                self.rules[0] = {**original, **change}
                self.desired = NEW
                with self.assertRaises(ValueError):
                    self.run_sync()
                self.assertFalse(self.updates())

    def test_missing_rule_is_never_recreated(self):
        self.rules = self.rules[1:]
        with self.assertRaises(ValueError):
            self.run_sync()
        self.assertFalse(self.updates())

    def test_readback_failure_keeps_notification_intent(self):
        self.desired = NEW
        with patch.object(sync, "http", side_effect=self.http), \
             patch.object(sync, "wm", return_value="test-token"), \
             patch.object(sync, "save"), \
             patch.object(sync, "ruleset", side_effect=[copy.deepcopy(self.rules), copy.deepcopy(self.rules)]):
            with self.assertRaisesRegex(ValueError, "readback"):
                sync.synchronize(self.state, 100000)
        self.assertIn("pending", self.state)
        self.assertFalse(self.notifications())

    def test_recovery_resolves_failure_alert(self):
        self.state["failed_at"] = 90000
        self.run_sync()
        notice = self.notifications()[0]
        self.assertEqual(notice["labels"]["alertname"], "GitHubWebhookSyncFailed")
        self.assertEqual(notice["endsAt"], sync.stamp(100000))
        self.assertNotIn("failed_at", self.state)

    def test_main_redacts_failure_and_still_alerts_without_state(self):
        with patch.object(sync, "wm", side_effect=RuntimeError("credential-value")), \
             patch.object(sync, "alert") as alert:
            with self.assertRaises(RuntimeError) as caught:
                sync.main()
        self.assertNotIn("credential-value", str(caught.exception))
        self.assertEqual(alert.call_args.args[0], "GitHubWebhookSyncFailed")
        self.assertNotIn("credential-value", alert.call_args.args[1])

    def test_http_errors_never_echo_response_body(self):
        failure = HTTPError("https://api.cloudflare.com", 403, "Forbidden", {}, BytesIO(b"credential-value"))
        with patch.object(sync.HTTP, "open", side_effect=failure):
            with self.assertRaisesRegex(RuntimeError, "Upstream HTTP 403") as caught:
                sync.http("https://api.cloudflare.com", token="credential-value")
        self.assertNotIn("credential-value", str(caught.exception))


class BootstrapTests(unittest.TestCase):
    def test_missing_token_does_not_activate_schedule(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(bootstrap, "api", return_value=None) as api:
            bootstrap.github_hooks("/w/cluster")
        self.assertTrue(any(c.args[0].endswith("/scripts/create") for c in api.call_args_list))
        self.assertFalse(any("/schedules/" in c.args[0] or "/variables/" in c.args[0] for c in api.call_args_list))

    def test_token_is_encrypted_not_passed_as_job_argument(self):
        with patch.dict(os.environ, {"CLOUDFLARE_WAF_API_TOKEN": "test-token", "GITHUB_HOOKS_LITELLM_KEY": "test-token"}), \
             patch.object(bootstrap, "api", return_value=None) as api:
            bootstrap.github_hooks("/w/cluster")
        variable = next(c.args[1] for c in api.call_args_list if c.args[0].endswith("/variables/create"))
        self.assertTrue(variable["is_secret"])
        schedule = next(c.args[1] for c in api.call_args_list if c.args[0].endswith("/schedules/create"))
        self.assertEqual(schedule["args"], {})
        self.assertEqual(schedule["schedule"], "0 17 * * * *")
        self.assertNotIn("test-token", json.dumps(schedule))

    def test_existing_paused_schedule_is_preserved(self):
        def api(path, *args, **kwargs):
            if path.endswith("/scripts/get/p/f/cluster/github-hooks-sync"):
                return {"content": Path(__file__).with_name("github_hooks.py").read_text()}
            if "/variables/get_value/" in path:
                return "test-token"
            return {"existing": True, "enabled": False}
        with patch.dict(os.environ, {"CLOUDFLARE_WAF_API_TOKEN": "test-token", "GITHUB_HOOKS_LITELLM_KEY": "test-token"}), \
             patch.object(bootstrap, "api", side_effect=api) as mocked:
            bootstrap.github_hooks("/w/cluster")
        self.assertFalse(any("/create" in c.args[0] or "/update" in c.args[0] for c in mocked.call_args_list))


class TicketTests(unittest.TestCase):
    def setUp(self):
        self.event = {"id": "abcdef0123456789", "at": 100000, "old": OLD, "new": NEW}
        self.project = {"id": 2, "title": "LabV2", "views": [{"view_kind": "kanban", "default_bucket_id": 4}]}
        self.tasks = []
        self.creates = []
        self.lose_response = False

    def vikunja(self, name, args):
        if name == "projects_list":
            return {"items": [self.project]}
        if name == "tasks_list":
            self.assertEqual(args["filter"], "project = 2")
            self.assertEqual(args["q"], self.event["id"])
            return {"items": self.tasks}
        self.assertEqual(name, "tasks_create")
        self.assertEqual(args["project"], 2)
        self.assertEqual(args["bucket_id"], 4)
        self.assertFalse(args["done"])
        self.creates.append(args)
        result = {"id": 75, "project_id": 2, "title": args["title"]}
        self.tasks.append(result)
        if self.lose_response:
            raise TimeoutError("Response lost after creation")
        return result

    def test_create_in_labv2_includes_change_and_verified_outcome(self):
        with patch.object(sync, "vikunja", side_effect=self.vikunja):
            sync.ticket(self.event)
        self.assertEqual(self.event["ticket_id"], 75)
        self.assertIn(self.event["id"], self.creates[0]["title"])
        self.assertIn("143.55.64.0/20", self.creates[0]["description"])
        self.assertIn("140.82.112.0/20", self.creates[0]["description"])
        self.assertIn("read back successfully", self.creates[0]["description"])

    def test_lost_create_response_is_recovered_without_duplicate(self):
        self.lose_response = True
        with patch.object(sync, "vikunja", side_effect=self.vikunja):
            with self.assertRaises(TimeoutError):
                sync.ticket(self.event)
            self.lose_response = False
            sync.ticket(self.event)
            sync.ticket(self.event)
        self.assertEqual(len(self.creates), 1)
        self.assertEqual(self.event["ticket_id"], 75)

    def test_missing_or_ambiguous_project_never_falls_back_to_inbox(self):
        for projects in ([], [self.project, self.project], [{**self.project, "title": "Inbox"}]):
            with self.subTest(projects=projects), patch.object(sync, "vikunja", return_value={"items": projects}) as v:
                with self.assertRaises(ValueError):
                    sync.ticket(self.event)
                self.assertEqual(v.call_count, 1)

    def test_ambiguous_task_lookup_does_not_create(self):
        title = "GitHub webhook ranges changed [" + self.event["id"] + "]"
        self.tasks = [{"id": 75, "project_id": 2, "title": title}] * 2
        with patch.object(sync, "vikunja", side_effect=self.vikunja):
            with self.assertRaisesRegex(ValueError, "Ambiguous"):
                sync.ticket(self.event)
        self.assertFalse(self.creates)

    def test_mcp_gateway_unwraps_structured_and_text_results(self):
        for response in ({"structuredContent": {"items": []}}, {"content": [{"type": "text", "text": '{"items": []}'}]}):
            with self.subTest(response=response), patch.object(sync, "wm", return_value="gateway-key"), \
                 patch.object(sync, "http", return_value=response) as http:
                self.assertEqual(sync.vikunja("projects_list", {"q": "LabV2"}), {"items": []})
                self.assertEqual(http.call_args.args[0], CONFIG["MCP"])
                self.assertEqual(http.call_args.args[1]["server_id"], CONFIG["VIKUNJA_SERVER"])
                self.assertNotIn("gateway-key", json.dumps(http.call_args.args[1]))

    def test_mcp_tool_error_cannot_be_mistaken_for_an_empty_lookup(self):
        with patch.object(sync, "wm", return_value="gateway-key"), \
             patch.object(sync, "http", return_value={"isError": True, "content": []}):
            with self.assertRaisesRegex(ValueError, "MCP operation failed"):
                sync.vikunja("projects_list", {})


class ConfigTests(unittest.TestCase):
    def test_mounted_configuration_loads_and_missing_fields_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for value in (CONFIG, {}, {**CONFIG, "RULE": ""}, []):
                with self.subTest(valid=value == CONFIG):
                    path.write_text(json.dumps(value))
                    load_config.cache_clear()
                    with patch.object(sync, "CONFIG_PATH", str(path)):
                        if value == CONFIG:
                            self.assertEqual(load_config(), CONFIG)
                        else:
                            with self.assertRaises(ValueError):
                                load_config()
            load_config.cache_clear()

    def test_worker_mounts_secret_read_only_without_exporting_configuration(self):
        manifest = json.loads(subprocess.check_output([
            "yq", "-o=json", ".", str(Path(__file__).parents[1] / "helmrelease.yaml")], text=True))
        worker = manifest["spec"]["values"]["windmill"]["workerGroups"][0]
        volume = next(v for v in worker["volumes"] if v["name"] == "github-hooks-config")
        mount = next(v for v in worker["volumeMounts"] if v["name"] == "github-hooks-config")
        self.assertEqual(volume["secret"]["secretName"], "windmill-github-hooks-config")
        self.assertTrue(mount["readOnly"])
        self.assertEqual(mount["mountPath"], str(Path(sync.CONFIG_PATH).parent))
        self.assertFalse(any(item["name"] in CONFIG for item in worker["extraEnv"]))


if __name__ == "__main__":
    unittest.main()
