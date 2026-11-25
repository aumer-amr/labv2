"""Check interactive Paseo plugin recovery, installation and retries."""

import json
import subprocess
import tempfile
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch


CONFIG = Path(__file__).resolve().parents[2] / "kubernetes/apps/ai/paseo/app/config"
spec = spec_from_file_location("paseo_plugins", CONFIG / "plugins.py")
plugins = module_from_spec(spec)
spec.loader.exec_module(plugins)
REQUIRED = json.loads((CONFIG / "plugins.json").read_text())


class PluginStartup(unittest.TestCase):
    def test_recover_and_preserve_registrations(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            store = home / "plugins"
            records = {}
            paths = {}
            for name, item in REQUIRED.items():
                path = store / name / "first" / item["directory"]
                path.mkdir(parents=True)
                (path / "paseo-plugin.json").write_text(json.dumps({"id": name}))
                paths[name] = path
                records[name] = {"registry": {"url": "https://plugins.paseo.sh/", "id": item["registry"]}}
            (store / "sources.json").write_text(json.dumps(records))
            output = home / "output"
            output.mkdir()
            template = home / "template"
            template.mkdir()
            (template / "codex.toml").symlink_to(CONFIG / "config.toml")
            (template / "paseo.json").symlink_to(CONFIG / "paseo.json")
            plugins.prepare(home, template, output, REQUIRED)
            prepared = json.loads((output / "paseo.json").read_text())
            self.assertTrue(prepared["pluginsEnabled"])
            self.assertEqual(set(REQUIRED), set(prepared["plugins"]))
            for name, path in paths.items():
                self.assertEqual(prepared["plugins"][name], {"source": "directory", "path": str(path), "enabled": True})
            self.assertEqual((CONFIG / "config.toml").read_bytes(), (output / "codex.toml").read_bytes())
            with patch.object(subprocess, "run", side_effect=AssertionError("offline restart accessed the CLI")):
                plugins.install_missing(REQUIRED, prepared)

            # A registered version wins over an orphan. Other user-installed entries survive.
            another = store / "tasks" / "older" / REQUIRED["tasks"]["directory"]
            another.mkdir(parents=True)
            (another / "paseo-plugin.json").write_text('{"id":"tasks"}')
            manual = {"source": "directory", "path": "/custom/example", "enabled": False}
            prepared["plugins"]["manual"] = manual
            (home / "config.json").write_text(json.dumps(prepared))
            plugins.prepare(home, template, output, REQUIRED)
            self.assertEqual(json.loads((output / "paseo.json").read_text()), prepared)

            (home / "config.json").write_text("{}")
            records["genui"]["registry"]["id"] = "someone/else"
            (store / "sources.json").write_text(json.dumps(records))
            (paths["progress"] / "paseo-plugin.json").write_text("{")
            (paths["paseo-playbooks"] / "paseo-plugin.json").unlink()
            plugins.prepare(home, template, output, REQUIRED)
            recovered = json.loads((output / "paseo.json").read_text())["plugins"]
            self.assertEqual(set(recovered), {"host-monitor"})

    def test_fresh_install_waits_for_daemon_and_retries_only_failed_plugins(self):
        installed = []
        attempts = []
        polls = 0

        def cli(args, **kwargs):
            nonlocal polls
            if args[2] == "ls":
                polls += 1
                if polls == 1:
                    raise subprocess.CalledProcessError(1, args)
                return subprocess.CompletedProcess(args, 0, json.dumps(installed).encode())
            self.assertEqual(args[:3], ["paseo", "plugin", "install"])
            source, flag, name = args[3:]
            self.assertEqual(flag, "--id")
            self.assertEqual(source, REQUIRED[name]["registry"])
            attempts.append(name)
            if name == "tasks" and attempts.count(name) == 1:
                raise subprocess.CalledProcessError(1, args)
            installed.append({"id": name})
            return subprocess.CompletedProcess(args, 0)

        with patch.object(subprocess, "run", side_effect=cli), patch.object(plugins.time, "sleep"):
            plugins.install_missing(REQUIRED, {"plugins": {}})
        self.assertCountEqual(attempts, [*REQUIRED, "tasks"])
        self.assertCountEqual([item["id"] for item in installed], REQUIRED)


if __name__ == "__main__":
    unittest.main()
