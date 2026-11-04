#!/usr/bin/env python3
"""Exercise private context loading without credentials, inference, or network access."""
import base64
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


context = load("private_context_test", ROOT / "scripts/langfuse-private.py")


def bundle(version=1):
    files = {
        "SKILL.md": "---\nname: labv2-private\ndescription: Test context\n---\n",
        "AGENTS.md": "Private rules marker\n",
        "skills/example/SKILL.md": "---\nname: example\ndescription: Test\n---\n",
        "skills/example/scripts/assessment.py": "print('fixture')\n",
        "hermes/prompts/example.txt": "Private task marker\n",
        "hermes/runners/example.sh": "#!/bin/sh\nexit 0\n",
        "hermes/broker.py": "print('broker fixture')\n",
    }
    return {"name": context.SKILL, "version": version, "files": [
        {"path": path, "content": content,
         "sha256Hash": base64.b64encode(hashlib.sha256(content.encode()).digest()).decode()}
        for path, content in files.items()
    ]}


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "context"

    def fetch(self, data=None):
        with patch.object(context, "request", return_value=data or bundle()):
            return context.fetch(self.directory)

    def test_complete_download_and_permissions(self):
        self.fetch()
        self.assertEqual(context.cached_bundle(self.directory), bundle())
        root = (self.directory / "current").resolve()
        self.assertEqual(root.parent, self.directory)
        self.assertEqual((root / "AGENTS.md").stat().st_mode & 0o777, 0o600)
        self.assertEqual((root / "skills/example/scripts/assessment.py").stat().st_mode & 0o777, 0o700)
        self.fetch()
        self.assertEqual(len(list(self.directory.glob("version-*"))), 1)

    def test_temporary_outage_uses_only_intact_cache(self):
        self.fetch()
        with patch.object(context, "request", side_effect=context.Unavailable("offline")), redirect_stderr(io.StringIO()):
            self.assertEqual(context.fetch(self.directory)["version"], 1)
            (self.directory / "current/AGENTS.md").write_text("tampered")
            with self.assertRaises(RuntimeError):
                context.fetch(self.directory)

    def test_cold_outage_and_authentication_failure_stop(self):
        with patch.object(context, "request", side_effect=context.Unavailable("offline")):
            with self.assertRaises(FileNotFoundError):
                context.fetch(self.directory)
        self.fetch()
        with patch.object(context, "request", side_effect=RuntimeError("HTTP 401")):
            with self.assertRaisesRegex(RuntimeError, "401"):
                context.fetch(self.directory)

    def test_invalid_release_never_replaces_last_good_version(self):
        self.fetch()
        for path in ("../outside", "/absolute", "skills/../outside", "a\\b", "a//b", "./file", "context.json"):
            invalid = bundle(2)
            invalid["files"][0]["path"] = path
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                self.fetch(invalid)
            self.assertEqual((self.directory / "current").resolve().name, "version-1")
        for mutation in ("hash", "duplicate", "empty", "version", "missing", "malformed"):
            invalid = bundle(2)
            if mutation == "hash":
                invalid["files"][0]["content"] += "tampered"
            elif mutation == "duplicate":
                invalid["files"].append(invalid["files"][0])
            elif mutation == "empty":
                invalid["files"] = []
            elif mutation == "version":
                invalid["version"] = True
            elif mutation == "missing":
                invalid["files"] = [f for f in invalid["files"] if f["path"] != "AGENTS.md"]
            else:
                invalid["files"][0] = None
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                self.fetch(invalid)

    def test_cache_symlinks_cannot_escape_directory(self):
        self.fetch()
        target = self.directory / "current/AGENTS.md"
        target.unlink()
        outside = self.directory.parent / "outside"
        outside.write_text("Private rules marker\n")
        target.symlink_to(outside)
        with self.assertRaises(RuntimeError):
            context.cached_bundle(self.directory)

    def test_direct_credentials_never_follow_redirects_or_allow_public_http(self):
        env = {"LANGFUSE_PUBLIC_KEY": "fake-public", "LANGFUSE_SECRET_KEY": "fake-secret"}
        for error, kind in [(HTTPError("", 401, "secret detail", {}, None), RuntimeError),
                            (HTTPError("", 302, "secret detail", {}, None), RuntimeError),
                            (HTTPError("", 503, "secret detail", {}, None), context.Unavailable),
                            (URLError("secret detail"), context.Unavailable)]:
            with patch.dict(os.environ, env, clear=True), patch.object(context, "build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(kind) as caught:
                    context.request("/api/public/skills")
                self.assertNotIn("secret detail", str(caught.exception))
        with patch.dict(os.environ, {**env, "LANGFUSE_BASE_URL": "http://example.test"}, clear=True):
            with self.assertRaises(RuntimeError):
                context.request("/api/public/skills")
        self.assertIsNone(context.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test"))

    def test_cluster_transport_uses_explicit_namespace_and_keeps_keys_inside_pod(self):
        for status in (200, 201, 401, 500):
            response = subprocess.CompletedProcess([], 0, json.dumps({"status": status, "body": {"ok": True}}), "")
            with patch.dict(os.environ, {}, clear=True), patch.object(context.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 1, "", ""), response,
            ]) as run:
                if status in (200, 201):
                    self.assertEqual(context.request("/api/public/skills"), {"ok": True})
                else:
                    with self.assertRaises(RuntimeError):
                        context.request("/api/public/skills")
                command = run.call_args.args[0]
                self.assertIn("--namespace", command)
                self.assertEqual(command[command.index("--namespace") + 1], "ai")
                self.assertNotIn("fake-secret", " ".join(command))

    def test_state_validation_and_readback(self):
        state = {"last_checked": "a" * 40, "last_incorporated": "b" * 40}
        with patch.object(context, "request", return_value={"prompt": json.dumps(state)}) as request:
            self.assertEqual(context.read_state(), state)
            self.assertIn("resolve=false", request.call_args.args[0])
        with patch.object(context, "request", return_value={}), patch.object(context, "read_state", return_value=state):
            context.write_state(state)
        with patch.object(context, "request", return_value={}), patch.object(context, "read_state", return_value={**state, "last_checked": "c" * 40}):
            with self.assertRaises(RuntimeError):
                context.write_state(state)
        for invalid in ({}, {**state, "extra": "x"}, {**state, "last_checked": "not-a-commit"}):
            with self.assertRaises(RuntimeError):
                context.validate_state(invalid)

    def test_install_skills_preserves_local_files_and_updates_version_links(self):
        self.fetch()
        root = self.directory.parent / "repository"
        existing = root / ".agents/skills/example"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("Existing local skill")
        with patch.object(context, "ROOT", root), patch.object(context.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            context.install_skills(self.directory)
            self.assertEqual((self.directory / "previous-skills/example/SKILL.md").read_text(), "Existing local skill")
            self.assertEqual(existing.resolve(), self.directory / "version-1/skills/example")
            self.fetch(bundle(2))
            context.install_skills(self.directory)
            self.assertEqual(existing.resolve(), self.directory / "version-2/skills/example")
            self.assertEqual(run.call_count, 1)
        self.assertFalse((root / "AGENTS.override.md").exists())

    def test_install_skills_preserves_tracked_files(self):
        self.fetch()
        root = self.directory.parent / "repository"
        existing = root / ".agents/skills/example"
        existing.mkdir(parents=True)
        (existing / "SKILL.md").write_text("Tracked skill")
        tracked = subprocess.CompletedProcess([], 0, ".agents/skills/example/SKILL.md\n", "")
        with patch.object(context, "ROOT", root), patch.object(context.subprocess, "run", return_value=tracked):
            with self.assertRaisesRegex(RuntimeError, "tracked"):
                context.install_skills(self.directory)
        self.assertEqual((existing / "SKILL.md").read_text(), "Tracked skill")
        self.assertFalse((self.directory / "previous-skills").exists())

    def test_cli_fetch_installs_skills_and_returns_pinned_rules_directory(self):
        root = self.directory.parent / "repository"
        output = io.StringIO()
        with patch.object(context, "ROOT", root), patch.object(context, "request", return_value=bundle()), patch.object(sys, "argv", ["langfuse-private.py", "fetch", "--install-skills"]), redirect_stdout(output):
            context.main()
        resolved = root / ".private/agent-context/version-1"
        self.assertIn(str(resolved), output.getvalue())
        self.assertNotIn("Private rules marker", output.getvalue())
        self.assertEqual((root / ".agents/skills/example").resolve(), resolved / "skills/example")
        self.assertFalse((root / "AGENTS.override.md").exists())

    def test_hermes_uses_same_snapshot_without_exposing_broker_to_agent(self):
        self.fetch()
        module_path = ROOT / "kubernetes/apps/ai/hermes/app/bootstrap/bootstrap.py"
        with patch.dict(sys.modules, {"yaml": types.ModuleType("yaml")}):
            bootstrap = load("hermes_context_test", module_path)
        data = self.directory.parent / "hermes"
        original_path = Path
        def paths(value):
            if str(value) == "/run/private-context/current":
                return self.directory / "current"
            return original_path(value)
        with patch.object(bootstrap, "DATA", data), patch.object(bootstrap, "Path", side_effect=paths), patch.object(bootstrap, "run") as run:
            bootstrap.install_private_skills()
            self.assertIn("fetch", run.call_args.args)
            self.assertNotIn("--install-skills", run.call_args.args)
            self.assertEqual((data / "private-prompts/example.txt").read_text(), "Private task marker\n")
            self.assertEqual((data / "skills/private/example/scripts/assessment.py").stat().st_mode & 0o777, 0o700)
            self.assertFalse((data / "skills/private/example/broker.py").exists())


if __name__ == "__main__":
    unittest.main()
