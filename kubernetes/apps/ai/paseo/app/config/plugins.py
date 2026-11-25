"""Prepare the image's startup configuration and install missing Paseo plugins."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def read_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def installed_directory(root, plugin_id, spec, record, previous):
    registry = record.get("registry", {})
    if (
        registry.get("id") != spec["registry"]
        or registry.get("url", "").rstrip("/") != "https://plugins.paseo.sh"
    ):
        return None

    def valid(path):
        manifest = path / "paseo-plugin.json"
        if not manifest.resolve().is_relative_to(root):
            return False
        try:
            data = read_json(manifest)
        except (OSError, ValueError):
            return False
        return isinstance(data, dict) and data.get("id") == plugin_id

    if previous.get("source") == "directory":
        path = Path(previous["path"])
        if path.is_absolute() and valid(path):
            return path

    # Recover the registration lost by older images only when the installed copy is unambiguous.
    candidates = [path for path in root.glob("*/" + spec["directory"]) if valid(path)]
    return candidates[0] if len(candidates) == 1 else None


def prepare(home, template, output, required):
    config = json.loads((template / "paseo.json").read_text())
    previous = read_json(home / "config.json").get("plugins", {})
    plugins = {**previous, **config.get("plugins", {})}
    records = read_json(home / "plugins/sources.json")
    for plugin_id, spec in required.items():
        entry = plugins.pop(plugin_id, {})
        path = installed_directory(
            home / "plugins" / plugin_id, plugin_id, spec, records.get(plugin_id, {}), entry
        )
        if path:
            plugins[plugin_id] = {"source": "directory", "path": str(path), "enabled": True}
    config["plugins"] = plugins
    (output / "paseo.json").write_text(json.dumps(config))
    shutil.copyfile(template / "codex.toml", output / "codex.toml")


def install_missing(required, prepared):
    pending = {key: spec["registry"] for key, spec in required.items() if key not in prepared["plugins"]}
    while pending:
        try:
            result = subprocess.run(
                ["paseo", "plugin", "ls", "--json"], capture_output=True, check=True, timeout=15
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            time.sleep(5)
            continue
        configured = {item["id"] for item in json.loads(result.stdout)}
        for plugin_id, source in list(pending.items()):
            if plugin_id not in configured:
                print(f"[paseo] Installing {plugin_id} from {source}", flush=True)
                try:
                    subprocess.run(
                        ["paseo", "plugin", "install", source, "--id", plugin_id],
                        check=True,
                        timeout=300,
                    )
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    print(f"[paseo] Will retry {plugin_id}", file=sys.stderr, flush=True)
                    continue
            del pending[plugin_id]
        if pending:
            time.sleep(60)


if __name__ == "__main__":
    template = Path(__file__).resolve().parent
    output = Path("/etc/paseo")
    required = json.loads((template / "plugins.json").read_text())
    if sys.argv[1] == "prepare":
        prepare(Path(os.environ["PASEO_HOME"]), template, output, required)
    elif sys.argv[1] == "install":
        install_missing(required, read_json(output / "paseo.json"))
    else:
        sys.exit("Expected prepare or install")
