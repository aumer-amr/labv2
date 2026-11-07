#!/usr/bin/env python3
"""Test TLS arguments and disabled multiplayer with the configured Helm charts.

Run with python3 tests/manual/check-langfuse-windmill-values.py LANGFUSE_CHART WINDMILL_CHART.
Supply local chart archives that match the repository pins. Requires helm, kustomize, and yq.
"""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]


def run(*args, data=None):
    return subprocess.check_output(args, input=data, text=True, cwd=ROOT)


def documents(data):
    return [json.loads(line) for line in run(
        "yq", "ea", "-o=json", "-I=0", "select(. != null)", "-", data=data).splitlines()]


with tempfile.TemporaryDirectory(prefix="langfuse-windmill-values-") as directory:
    values_file = Path(directory) / "values.json"
    for app, chart_path in zip(("langfuse", "windmill"), sys.argv[1:], strict=True):
        chart = str(Path(chart_path).resolve())
        app_path = ROOT / "kubernetes/apps/ai" / app / "app"
        built = documents(run("kustomize", "build", str(app_path),
                              "--load-restrictor=LoadRestrictionsNone"))
        values = next(item for item in built if item["kind"] == "HelmRelease"
                      and item["metadata"]["name"] == app)["spec"]["values"]
        source = next(item for item in built if item["kind"] == "OCIRepository"
                      and item["metadata"]["name"] == app)
        assert documents(run("helm", "show", "chart", chart))[0]["version"] == source["spec"]["ref"]["tag"]

        def render(configuration):
            values_file.write_text(json.dumps(configuration))
            return documents(run("helm", "template", app, chart, "--namespace", "ai",
                                 "--values", str(values_file)))

        previous = copy.deepcopy(values)
        if app == "langfuse":
            assert values["postgresql"]["args"] == "sslmode=require"
            assert "args" not in values["postgresql"]["auth"]
            previous["postgresql"]["auth"]["args"] = previous["postgresql"].pop("args")
        else:
            assert "multiplayerReplicas" not in values["windmill"]
            assert values["windmill"]["extraReplicas"] == 0
            previous["windmill"]["multiplayerReplicas"] = 0

        before, after = render(previous), render(values)
        if app == "langfuse":
            deployments = [item for item in after if item["kind"] == "Deployment"]
            assert {item["metadata"]["name"] for item in deployments} == {"langfuse-web", "langfuse-worker"}
            for deployment in deployments:
                container = deployment["spec"]["template"]["spec"]["containers"][0]
                env = {item["name"]: item for item in container["env"]}
                assert env["DATABASE_ARGS"] == {"name": "DATABASE_ARGS", "value": "sslmode=require"}
                assert env["DATABASE_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
                    "name": "langfuse-postgres", "key": "password"}
                assert "DATABASE_URL" not in env and "DIRECT_URL" not in env
                assert container["envFrom"] == [{"secretRef": {"name": "langfuse-secret"}}]
                # Removing only the intended new environment variable must restore the old render.
                container["env"].remove(env["DATABASE_ARGS"])
        else:
            assert {item["metadata"]["name"] for item in after if item["kind"] == "Deployment"} == {
                "windmill-app", "windmill-workers-default"}
            assert not any(item["metadata"]["name"] == "windmill-extra" for item in after)
        assert before == after, f"Unexpected {app} render changes"

    ignored = documents((ROOT / ".deadvalues.yaml").read_text())[0]["ignore"]
    assert not any(item.get("app") in {"langfuse", "windmill"} for item in ignored)

print("Langfuse TLS arguments and secret references passed; Windmill render is unchanged")
