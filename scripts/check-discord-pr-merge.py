#!/usr/bin/env python3
"""Exercise the Discord/GitHub merge boundary without credentials or network."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "bridge", ROOT / "kubernetes/apps/ai/hermes/app/discord-review/bridge.py"
)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
HEAD, BASE = "a" * 40, "b" * 40


class API:
    def __init__(self):
        self.writes = []
        marker = {"version": 1, "head_sha": HEAD, "base_sha": BASE, "review_result": "clean"}
        self.pr = {
            "title": "Test PR", "state": "open", "draft": False, "mergeable": True,
            "mergeable_state": "clean", "head": {"sha": HEAD, "repo": {"full_name": bridge.REPO}},
            "base": {"sha": BASE, "ref": "main"},
        }
        self.comment = {
            "id": 42, "updated_at": "2026-09-24T15:30:00Z", "user": {"id": 41898282},
            "performed_via_github_app": {"id": 15368},
            "body": f"<!-- ai-pr-reviewer:{json.dumps(marker)} -->\n✅ **Automated recommendation: APPROVE**",
        }
        self.comments = [self.comment]
        self.checks = [{
            "name": name, "status": "completed", "conclusion": "success", "app": {"id": 15368},
            "details_url": f"https://github.com/{bridge.REPO}/actions/runs/9/job/8",
        } for name in ("review", "Talos Compatibility", "Flate - Success")]
        self.statuses = []
        self.reviews = []
        self.run = {"path": bridge.WORKFLOW, "event": "pull_request_target", "head_sha": HEAD,
                    "status": "completed", "conclusion": "success"}

    def request(self, path, method="GET", data=None):
        if method == "PUT":
            self.writes.append((path, data))
            self.pr.update(state="closed", merged=True)
            return {"merged": True, "sha": "c" * 40}
        if path == "pulls/1":
            return deepcopy(self.pr)
        if path == "actions/runs/9":
            return deepcopy(self.run)
        raise AssertionError(path)

    def pages(self, path, key=None):
        if path == "issues/1/comments":
            return deepcopy(self.comments)
        if "/check-runs" in path:
            return deepcopy(self.checks)
        if path.endswith("/statuses"):
            return deepcopy(self.statuses)
        if path == "pulls/1/reviews":
            return deepcopy(self.reviews)
        raise AssertionError(path)


class MergeBoundary(unittest.TestCase):
    def test_only_exact_reviewed_head_is_sent_to_merge_api(self):
        api = API()
        card = bridge.snapshot(api, 1)
        self.assertTrue(card["eligible"])
        self.assertEqual(bridge.merge(api, card), "c" * 40)
        self.assertEqual(api.writes, [("pulls/1/merge", {"sha": HEAD, "merge_method": "merge"})])
        with self.assertRaises(bridge.Blocked):
            bridge.merge(api, card)
        self.assertEqual(len(api.writes), 1)

    def test_remote_changes_fail_closed_without_write(self):
        mutations = [
            lambda a: a.pr["head"].update(sha="d" * 40),
            lambda a: a.pr["base"].update(sha="d" * 40),
            lambda a: a.pr.update(state="closed"),
            lambda a: a.pr.update(draft=True),
            lambda a: a.pr.update(mergeable=None),
            lambda a: a.pr.update(mergeable_state="blocked"),
            lambda a: a.pr["head"].update(repo={"full_name": "fork/labv2"}),
            lambda a: a.comment.update(performed_via_github_app=None),
            lambda a: a.comment["user"].update(id=1),
            lambda a: a.comment.update(body=a.comment["body"].replace('"clean"', '"issues"')),
            lambda a: a.comment.update(body=a.comment["body"].replace("APPROVE", "REQUEST CHANGES")),
            lambda a: a.comment.update(id=43),
            lambda a: a.checks[0].update(status="in_progress", conclusion=None),
            lambda a: a.checks[1].update(conclusion="failure"),
            lambda a: a.checks[0]["app"].update(id=7),
            lambda a: a.checks.pop(),
            lambda a: a.run.update(path=".github/workflows/fake.yaml"),
            lambda a: a.run.update(head_sha="d" * 40),
            lambda a: a.run.update(conclusion="failure"),
            lambda a: a.statuses.append({"context": "security", "state": "pending"}),
            lambda a: a.reviews.append({"user": {"id": 99}, "state": "CHANGES_REQUESTED"}),
        ]
        for i, mutate in enumerate(mutations):
            with self.subTest(case=i):
                api = API()
                card = bridge.snapshot(api, 1)
                mutate(api)
                with self.assertRaises(bridge.Blocked):
                    bridge.merge(api, card)
                self.assertEqual(api.writes, [])

    def test_newer_negative_review_overrides_old_approval(self):
        api = API()
        negative = deepcopy(api.comment)
        negative.update(id=44, updated_at="2026-09-24T16:00:00Z",
                        body=negative["body"].replace('"clean"', '"issues"'))
        api.comments.append(negative)
        self.assertFalse(bridge.snapshot(api, 1)["eligible"])

    def test_discord_identity_and_message_binding(self):
        state = bridge.snapshot(API(), 1)
        cards = {1: {"token": "nonce", "message": 55, "state": state}}
        def interaction():
            return NS(user=NS(id=10), channel_id=20, data={"custom_id": "prmerge:nonce"},
                      message=NS(id=55, author=NS(id=30)))
        self.assertIs(bridge.authorized_card(interaction(), cards, {10}, 20, 30), cards[1])
        for change in [lambda i: setattr(i.user, "id", 11), lambda i: setattr(i, "channel_id", 21),
                       lambda i: setattr(i.message, "id", 56), lambda i: setattr(i.message.author, "id", 31),
                       lambda i: i.data.update(custom_id="prmerge:old"), lambda i: setattr(i, "message", None)]:
            i = interaction()
            change(i)
            with self.assertRaises(bridge.Blocked):
                bridge.authorized_card(i, cards, {10}, 20, 30)
        state["eligible"] = False
        with self.assertRaises(bridge.Blocked):
            bridge.authorized_card(interaction(), cards, {10}, 20, 30)

    def test_allowlist_rejects_wildcards_and_names(self):
        self.assertEqual(bridge.user_ids("123, 456"), {123, 456})
        for value in ("", "*", "username", "123,*"):
            with self.assertRaises(ValueError):
                bridge.user_ids(value)


if __name__ == "__main__":
    unittest.main()
