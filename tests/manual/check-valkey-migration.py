#!/usr/bin/env python3
"""Compare Valkey migration renders with a supplied Git baseline.

Run: python3 tests/manual/check-valkey-migration.py BASE_REF OLD_CHART NEW_CHART
Requires git, flux, helm, kustomize, and yq. Uses no cluster credentials.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
BASE, OLD, NEW = sys.argv[1:]
OLD, NEW = str(Path(OLD).resolve()), str(Path(NEW).resolve())
APPS = {"paperless": "administration", "langfuse": "ai", "litellm": "ai",
        "open-webui": "ai", "immich": "home"}


def run(*args, data=None, cwd=ROOT):
    return subprocess.check_output(args, input=data, text=True, cwd=cwd)


def documents(data):
    return [json.loads(line) for line in run(
        "yq", "ea", "-o=json", "-I=0", "select(. != null)", "-", data=data).splitlines()]


def resource(items, kind, name):
    return next(item for item in items if item["kind"] == kind and item["metadata"]["name"] == name)


def build(root, app, namespace):
    path = root / "kubernetes/apps" / namespace / app
    return documents(run("flux", "build", "kustomization", app, "--namespace", namespace,
                         "--dry-run", "--kustomization-file", str(path / "ks.yaml"),
                         "--path", str(path / "app"), cwd=root))


def render(chart, release, namespace, directory):
    name = release["metadata"]["name"]
    spec = release["spec"]
    values = directory / "values.json"
    values.write_text(json.dumps(spec["values"]))
    rendered = run("helm", "template", name, chart, "--namespace", namespace,
                   "--values", str(values))
    # Kubernetes parses octal file modes before emitting JSON, unlike yq's YAML 1.2 reader.
    for renderer in spec.get("postRenderers") or [{"kustomize": {}}]:
        (directory / "rendered.yaml").write_text(rendered)
        (directory / "kustomization.yaml").write_text(json.dumps({
            "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
            "resources": ["rendered.yaml"], **renderer["kustomize"]}))
        rendered = run("kustomize", "build", str(directory), "--load-restrictor=LoadRestrictionsNone")
    return documents(rendered)


with tempfile.TemporaryDirectory(prefix="valkey-migration-") as temporary:
    directory = Path(temporary)
    baseline = directory / "baseline"
    baseline.mkdir()
    archive = directory / "baseline.tar"
    subprocess.run(["git", "archive", "--output", str(archive), BASE, "kubernetes"], cwd=ROOT, check=True)
    subprocess.run(["tar", "-xf", str(archive), "-C", str(baseline)], check=True)
    charts = [documents(run("helm", "show", "chart", chart))[0] for chart in (OLD, NEW)]
    assert [chart["version"] for chart in charts] == ["0.12.1", "1.0.0"]
    assert charts[0]["appVersion"] == charts[1]["appVersion"] == "9.1.3"
    sources = documents(run("kustomize", "build", str(ROOT / "kubernetes/apps/database/valkey/app"),
                            "--load-restrictor=LoadRestrictionsNone"))
    for name, version in (("valkey", "0.12.1"), ("valkey-v1", "1.0.0")):
        assert resource(sources, "OCIRepository", name)["spec"]["ref"]["tag"] == version

    for app, namespace in APPS.items():
        name = app + "-valkey"
        before, after = [build(root, app, namespace) for root in (baseline, ROOT)]
        old_hr, new_hr = [resource(items, "HelmRelease", name) for items in (before, after)]
        assert old_hr["spec"]["chartRef"]["name"] == "valkey"
        assert new_hr["spec"]["chartRef"] == {
            "kind": "OCIRepository", "name": "valkey-v1", "namespace": "database"}
        values = new_hr["spec"]["values"]
        assert not {"dataStorage", "valkeyConfig"} & values.keys()
        assert values["networkPolicy"]["enabled"] is True
        assert values["networkPolicy"]["allowExternal"] is False
        assert values["networkPolicy"]["allowExternalEgress"] is False
        for key in ("auth", "resources", "replica", "deploymentStrategy"):
            assert old_hr["spec"]["values"][key] == values[key], (app, key)
        assert values["extraConfig"] == old_hr["spec"]["values"]["valkeyConfig"]
        # Only the Valkey HelmRelease changes inside each consumer's Flux build.
        assert [x for x in before if x != old_hr] == [x for x in after if x != new_hr], app
        old, new = [render(chart, hr, namespace, directory) for chart, hr in ((OLD, old_hr), (NEW, new_hr))]
        assert {(x["kind"], x["metadata"]["name"]) for x in old} == {
            (x["kind"], x["metadata"]["name"]) for x in new}, app
        for kind in ("Service", "NetworkPolicy", "PersistentVolumeClaim"):
            for item in (x for x in old if x["kind"] == kind):
                updated = resource(new, kind, item["metadata"]["name"])
                assert item["spec"] == updated["spec"], (app, kind)
                if kind == "PersistentVolumeClaim":
                    assert updated["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
        assert resource(old, "ConfigMap", name + "-config")["data"] == resource(new, "ConfigMap", name + "-config")["data"]
        old_deploy, new_deploy = [resource(items, "Deployment", name)["spec"] for items in (old, new)]
        for key in ("selector", "strategy", "replicas"):
            assert old_deploy[key] == new_deploy[key], (app, key)
        old_pod, new_pod = [deployment["template"]["spec"] for deployment in (old_deploy, new_deploy)]
        for key in ("securityContext", "serviceAccountName", "automountServiceAccountToken"):
            assert old_pod[key] == new_pod[key], (app, key)
        old_volumes, new_volumes = [{v["name"]: v for v in pod["volumes"]} for pod in (old_pod, new_pod)]
        for key in ("valkey-data", "valkey-users-secret"):
            assert old_volumes[key] == new_volumes[key], (app, key)
        persistent = app in ("paperless", "langfuse", "immich")
        assert values["persistence"]["enabled"] is persistent
        if persistent:
            assert new_volumes["valkey-data"]["persistentVolumeClaim"]["claimName"] == name
            assert "appendonly yes" in values["extraConfig"]
            assert "maxmemory-policy noeviction" in values["extraConfig"]
        else:
            assert "emptyDir" in new_volumes["valkey-data"]
            assert not any(x["kind"] == "PersistentVolumeClaim" for x in new)
            assert "maxmemory-policy allkeys-lru" in values["extraConfig"]
        for category, container_name in (("containers", "valkey"), ("initContainers", "init")):
            previous, current = old_pod[category][0], new_pod[category][0]
            assert current["name"] == container_name
            for key in ("image", "resources", "securityContext"):
                assert previous.get(key) == current.get(key), (app, category, key)
            assert {"name": "valkey-data", "mountPath": "/data"} in current["volumeMounts"]
        container = new_pod["containers"][0]
        auth_env = next(e for e in container["env"] if e["name"] == "VALKEY_PROBE_PASSWORD")
        assert auth_env["valueFrom"]["secretKeyRef"] == {"name": name, "key": "VALKEY_PASSWORD"}
        # Exercise the rendered probe against server replies, including CLI errors with exit code zero.
        cli = directory / "valkey-cli"
        cli.write_text('#!/bin/sh\nprintf "%s\\n" "$PROBE_RESPONSE"\n')
        cli.chmod(0o755)
        for probe in ("readinessProbe", "startupProbe", "livenessProbe"):
            for reply in ("PONG", "LOADING dataset", "NOAUTH Authentication required", "WRONGPASS invalid password"):
                result = subprocess.run(container[probe]["exec"]["command"], capture_output=True,
                                        env={**os.environ, "PATH": f"{directory}:{os.environ['PATH']}",
                                             "PROBE_RESPONSE": reply})
                expected = reply == "PONG" or (reply.startswith("LOADING") and probe != "readinessProbe")
                assert (result.returncode == 0) is expected, (app, probe, reply)
        print(f"{namespace}/{name}: storage, auth, resources, selectors, services and exact policy preserved")

print("All five Valkey migrations passed. Live rollout and data recovery remain separate gates.")
