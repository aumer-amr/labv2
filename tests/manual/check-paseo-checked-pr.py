"""Exercise the checked merge with a temporary Git remote and an in-memory GitHub API."""

from contextlib import chdir
from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".paseo/plugins/playbooks/scripts/checked-pr.py"
spec = spec_from_file_location("checked_pr", SCRIPT)
release = module_from_spec(spec)
spec.loader.exec_module(release)


class CheckedPR(unittest.TestCase):
    def setUp(self):
        temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(chdir(temp))
        self.enterContext(patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(temp / "gitconfig"),
            "GIT_ALLOW_PROTOCOL": "file", "GIT_AUTHOR_NAME": "Test", "GIT_COMMITTER_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid", "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }))
        self.git("init", "--bare", "-q", "remote")
        self.git("init", "-q", "-b", "main", "work")
        self.git("config", "--global", f"url.{(temp / 'remote').as_uri()}.insteadOf", release.REMOTE)
        self.enterContext(chdir(temp / "work"))
        self.git("commit", "-q", "--allow-empty", "-m", "Initial")
        self.git("push", "-q", release.REMOTE, "main")
        self.base = self.git("rev-parse", "HEAD")
        self.git("switch", "-qc", "work")
        Path("change").write_text("approved change\n")
        self.git("add", "change")
        self.git("commit", "-qm", "fix(ai): handle a task")
        self.sha = self.git("rev-parse", "HEAD")
        self.branch = f"paseo/labv2-7-{self.sha}"
        self.env = {"PLAYBOOK_STEP_ID": "open_pr", "PLAYBOOK_INPUT_TASK_KEY": "LABV2-7",
                    "PLAYBOOK_INPUT_REVIEWED_SHA": self.sha, "PLAYBOOK_INPUT_CANDIDATE_SHA": self.sha}
        self.pr = None
        self.merges = []
        self.posts = 0
        self.results = [{"name": name, "head_sha": self.sha, "status": "completed",
                         "conclusion": "success", "app": {"id": release.ACTIONS}}
                        for name in sorted(release.REQUIRED)]
        self.enterContext(patch.object(release, "api", side_effect=self.api))

    def git(self, *args):
        return subprocess.check_output(["git", *args], text=True).strip()

    def api(self, path, *args):
        if path.startswith("pulls?state=all&head=aumer-amr:" + self.branch):
            return [self.pr] if self.pr else []
        if path == "pulls":
            self.assertIn("POST", args)
            self.assertIn(f"head={self.branch}", args)
            self.assertIn("base=main", args)
            self.posts += 1
            self.pr = {"base": {"repo": {"full_name": release.REPO}, "ref": "main"},
                       "head": {"repo": {"full_name": release.REPO}, "ref": self.branch, "sha": self.sha},
                       "draft": False, "state": "open", "number": 7, "html_url": "https://example.invalid/7",
                       "mergeable": True, "mergeable_state": "clean"}
            return self.pr
        if path == "pulls/7":
            return self.pr
        if path == "rules/branches/main":
            return [{"type": "required_status_checks", "parameters": {"required_status_checks": [
                {"context": "Extra check", "integration_id": 42}]}}]
        if path == f"commits/{self.sha}/check-runs?filter=latest&per_page=100":
            self.assertEqual(args, ("--paginate", "--slurp"))
            return [{"check_runs": self.results[:1]}, {"check_runs": self.results[1:]}]
        if path == "pulls/7/merge":
            self.assertEqual(args, ("--method", "PUT", "-f", "merge_method=merge", "-f", f"sha={self.sha}"))
            self.merges.append(self.sha)
            self.pr.update(state="closed", merged_at="now", merge_commit_sha="a" * 40)
            return {"merged": True, "sha": "a" * 40}
        self.fail(f"Unexpected API path: {path}")

    def open(self):
        self.assertEqual(release.publish(self.env)["pr_number"], "7")
        self.env.update(PLAYBOOK_STEP_ID="merge_pr", PLAYBOOK_INPUT_PR_NUMBER="7")

    def test_publish_retry_and_checked_merge(self):
        self.open()
        self.assertEqual(self.git("ls-remote", release.REMOTE, "refs/heads/main").split()[0], self.base)
        self.assertEqual(self.git("ls-remote", release.REMOTE, self.branch).split()[0], self.sha)
        release.publish(dict(self.env, PLAYBOOK_STEP_ID="open_pr"))
        self.assertEqual(self.posts, 1)
        with self.assertRaisesRegex(release.Pending, "Extra check"):
            release.publish(self.env)
        self.results.append(dict(self.results[0], name="Extra check", app={"id": 42}))
        self.assertEqual(release.publish(self.env), {"merge_sha": "a" * 40})
        self.assertEqual(release.publish(self.env), {"merge_sha": "a" * 40})
        self.assertEqual(self.merges, [self.sha])

    def test_required_checks(self):
        self.open()
        self.results.append(dict(self.results[0], name="Extra check", app={"id": 42}))
        original = self.results.pop(0)
        for delta, error in [
            (None, release.Pending), ({"head_sha": self.base}, release.Pending),
            ({"app": {"id": 99}}, release.Pending), ({"status": "in_progress"}, release.Pending),
            ({"conclusion": "failure"}, ValueError), ({"conclusion": "skipped"}, ValueError),
            ({"conclusion": "neutral"}, ValueError), ({"conclusion": "cancelled"}, ValueError),
        ]:
            with self.subTest(delta=delta), self.assertRaises(error):
                if delta is None:
                    release.publish(self.env)
                else:
                    with patch.object(self, "results", self.results + [dict(original, **delta)]):
                        release.publish(self.env)
        self.assertFalse(self.merges)

    def test_local_drift_and_branch_collision(self):
        with self.assertRaises(ValueError):
            release.publish(dict(self.env, PLAYBOOK_INPUT_CANDIDATE_SHA=self.base))
        Path("change").write_text("unreviewed\n")
        with self.assertRaises(ValueError):
            release.publish(self.env)
        self.git("restore", "change")
        self.git("push", "-q", release.REMOTE, f"{self.base}:refs/heads/{self.branch}")
        with self.assertRaises(ValueError):
            release.publish(self.env)
        self.assertEqual(self.posts, 0)

    def test_pr_drift(self):
        self.open()
        self.results.append(dict(self.results[0], name="Extra check", app={"id": 42}))
        for field, value in [("draft", True), ("state", "closed"), ("mergeable_state", "dirty"),
                             ("mergeable_state", "behind"), ("mergeable", False),
                             ("base", dict(self.pr["base"], ref="another-branch")),
                             ("head", dict(self.pr["head"], sha=self.base)),
                             ("head", dict(self.pr["head"], repo={"full_name": "fork/labv2"}))]:
            with self.subTest(field=field), patch.dict(self.pr, {field: value}), self.assertRaises(ValueError):
                release.publish(self.env)
        changed = deepcopy(self.pr)
        changed["head"]["sha"] = self.base
        with patch.object(release, "api", side_effect=[self.pr, [], [{"check_runs": self.results}], changed]):
            with self.assertRaises(ValueError):
                release.publish(self.env)
        self.assertFalse(self.merges)


if __name__ == "__main__":
    unittest.main()
