#!/usr/bin/env python3
"""Exercise summary coverage, revision updates, and the secret/publication boundary."""

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("summary", ROOT / "scripts/pr-rendered-summary.py")
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)
if "--flate" not in sys.argv:
    workflow = json.loads(subprocess.check_output([
        "yq", "-o=json", ".", str(ROOT / ".github/workflows/rendered-summary.yaml"),
    ]))
    assert workflow["permissions"] == {}
    assert set(workflow["on"]) == {"pull_request_target"}
    for job in workflow["jobs"].values():
        assert job["runs-on"] == "ubuntu-latest"
        checkout = job["steps"][0]["with"]
        assert checkout["persist-credentials"] is False
        assert checkout["ref"] == "${{ github.event.pull_request.base.sha }}"
        assert not any("upload-artifact" in str(step) for step in job["steps"])
    assert workflow["jobs"]["render"]["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["publish"]["permissions"] == {"contents": "read", "pull-requests": "write"}
    assert workflow["jobs"]["publish"]["needs"] == "render"
SENTINEL = "DO_NOT_PUBLISH_SECRET_VALUE"
event = {"number": 7, "repository": {"full_name": "owner/repo"}, "pull_request": {
    "base": {"sha": "a" * 40}, "head": {"sha": "b" * 40, "repo": {"full_name": "owner/repo"}},
}}


def resource(kind, name, **fields):
    group = "apps/v1" if kind == "Deployment" else "rbac.authorization.k8s.io/v1" if kind in summary.RBAC else "v1"
    return {"apiVersion": group, "kind": kind, "metadata": {"name": name, "namespace": "demo"}, **fields}


old = [resource("Deployment", "app", spec={"replicas": 2, "selector": {"matchLabels": {"app": "demo"}},
       "template": {"metadata": {"labels": {"app": "demo"}}, "spec": {"containers": [
           {"name": "app", "image": "ghcr.io/example/app:1", "env": [{"name": "TOKEN", "value": SENTINEL}]}]}}}),
       resource("ConfigMap", "removed", data={"password": SENTINEL}),
       resource("Role", "reader", rules=[{"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]}]),
       resource("Secret", "credentials", stringData={"password": SENTINEL})]
new = deepcopy([old[0], old[2], old[3]])
new[0]["spec"]["replicas"] = 3
new[0]["spec"]["template"]["spec"]["containers"][0]["image"] = "ghcr.io/example/app:2"
new[0]["spec"]["template"]["spec"]["initContainers"] = [{"name": "init", "image": "busybox:1"}]
new[1]["rules"][0]["verbs"].append("list")
new[2]["stringData"]["password"] = SENTINEL + "_NEW"


def evidence(before=old, after=new):
    return {"base": "a" * 40, "head": "b" * 40, "status": "rendered",
            **summary.compare(summary.inventory(before), summary.inventory(after))}


data = evidence()
body = summary.body(event, data)
for expected in ("Namespaces: `demo`", "Deployment demo/app", "2 → 3", "ghcr.io/example/app:1",
                 "ghcr.io/example/app:2", "busybox:1", "ConfigMap demo/removed", "Role demo/reader",
                 "a" * 40, "b" * 40, "live-generated values", "suspended producers"):
    assert expected in body, expected
assert SENTINEL not in body and SENTINEL not in json.dumps(data)
assert "credentials" not in body
assert "unavailable" in summary.body(event, {})
assert "unavailable" in summary.body(event, {**data, "head": "c" * 40})
assert "None observed" in summary.body(event, evidence(old, old))
custom_old = [resource("InferenceService", "model", spec={"image": "example/model:1", "token": SENTINEL})]
custom_new = deepcopy(custom_old)
custom_new[0]["spec"]["image"] = "example/model:2"
custom_body = summary.body(event, evidence(custom_old, custom_new))
assert "InferenceService demo/model`: changed" in custom_body
assert SENTINEL not in custom_body
many = [resource("ConfigMap", f"item-{i}") for i in range(summary.LIMIT + 1)]
assert "1 additional changed resources omitted" in summary.body(event, evidence([], many))
defaulted = deepcopy(new)
del defaulted[0]["spec"]["replicas"]
assert "2 → 1" in summary.body(event, evidence(old, defaulted))
assert "ClusterRole demo/system:metrics-reader" in summary.body(event, evidence([], [resource("ClusterRole", "system:metrics-reader")]))
conflicting = [resource("ConfigMap", "duplicate", data={"value": "one"}),
               resource("ConfigMap", "duplicate", data={"value": "two"})]
partial = evidence(conflicting, [])
assert partial["rows"] == [] and partial["ambiguous"] == 1
assert "conflicting rendered definitions" in summary.body(event, partial)
partial = evidence([resource("Certificate", "example")], [resource("Certificate", "..PLACEHOLDER_DOMAIN..")])
assert partial["rows"] == [] and partial["ambiguous"] == 2
assert "unresolved secret-derived names" in summary.body(event, partial)
for image in ("https://user:password@registry.test/image", "user:password@registry.test/image", "image\nINJECTED", "[link](https://example.test)"):
    invalid = deepcopy(new)
    invalid[0]["spec"]["template"]["spec"]["containers"][0]["image"] = image
    try:
        summary.inventory(invalid)
        raise AssertionError("unsafe image accepted")
    except ValueError:
        pass

# Same bot comment is patched on the next revision; user-spoofed markers are ignored.
comments = [{"id": 11, "user": {"login": "someone"}, "body": summary.MARKER},
            {"id": 22, "user": {"login": "github-actions[bot]"}, "body": summary.MARKER}]
for head in ("b" * 40, "c" * 40):
    updated = deepcopy(event)
    updated["pull_request"]["head"]["sha"] = head
    current = {**updated["pull_request"], "state": "open"}
    with patch.object(summary, "gh", side_effect=[current, comments, current, {}]) as gh:
        summary.publish(updated, {**data, "head": head})
        assert gh.call_args.args[0] == "repos/owner/repo/issues/comments/22"
        assert f"Head: `{head}`" in gh.call_args.args[1]["body"]
with patch.object(summary, "gh", return_value={**event["pull_request"], "state": "closed"}) as gh:
    summary.publish(event, data)
    assert gh.call_count == 1
current = {**event["pull_request"], "state": "open"}
stale = deepcopy(current)
stale["head"]["sha"] = "d" * 40
with patch.object(summary, "gh", side_effect=[current, comments, stale]) as gh:
    summary.publish(event, data)
    assert gh.call_count == 3
with patch.object(summary, "gh", side_effect=[current, [], current, {}]) as gh:
    summary.publish(event, {})
    assert gh.call_args.args[0] == "repos/owner/repo/issues/7/comments"
    assert "unavailable" in gh.call_args.args[1]["body"]
for invalid in ({**data, "rows": [[SENTINEL]]}, {**data, "omitted": "not a number"}):
    with patch.object(summary, "gh", side_effect=[current, comments, current, {}]) as gh:
        summary.publish(event, invalid)
        assert "unavailable" in gh.call_args.args[1]["body"]
        assert SENTINEL not in gh.call_args.args[1]["body"]

# Failed renders and fork inputs cannot masquerade as wholesale deletions.
fork = deepcopy(event)
fork["pull_request"]["head"]["repo"]["full_name"] = "fork/repo"
with patch.object(summary, "run") as run:
    assert summary.collect(fork)["status"] == "unavailable"
    run.assert_not_called()
with patch.object(summary, "run", side_effect=subprocess.CalledProcessError(1, "flate", stderr=SENTINEL)):
    failed = summary.collect(event)
    assert failed["status"] == "unavailable" and SENTINEL not in json.dumps(failed)
with patch.object(summary, "run", side_effect=[b"", b"d" * 40]):
    assert summary.collect(event)["status"] == "unavailable"
with patch.object(summary, "run", side_effect=[b"", b"b" * 40, b"", json.dumps(old).encode(), b"",
                                               b"", json.dumps(new).encode(), b""]) as run:
    assert summary.collect(event) == data
    builds = [c.args[0] for c in run.call_args_list if c.args[0][0] == "flate"]
    assert len(builds) == 2 and all("--restrict-egress" in b and "--skip-secrets" in b for b in builds)
    checkouts = [c.args[0][-1] for c in run.call_args_list if c.args[0][:3] == ["git", "worktree", "add"]]
    assert checkouts == ["a" * 40, "b" * 40]
with patch.object(summary, "run", side_effect=[b"", b"b" * 40, b"", json.dumps(old).encode(), b"",
                                               b"", subprocess.CalledProcessError(1, "flate"), b""]):
    assert summary.collect(event) == {"base": "a" * 40, "head": "b" * 40, "status": "unavailable"}

# Optional real Flate smoke check: no network, credentials, or repository writes.
if "--flate" in sys.argv:
    with tempfile.TemporaryDirectory() as directory:
        inventories = []
        for side, docs in (("base", old), ("head", new)):
            root = Path(directory) / side
            app = root / "app"
            app.mkdir(parents=True)
            summary.run(["git", "init", "--quiet", str(root)])
            (root / "source.yaml").write_text(json.dumps({
                "apiVersion": "source.toolkit.fluxcd.io/v1", "kind": "GitRepository",
                "metadata": {"name": "flux-system", "namespace": "flux-system"},
                "spec": {"url": "https://example.test/cluster.git", "ref": {"branch": "main"}},
            }))
            (root / "ks.yaml").write_text(json.dumps({
                "apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization",
                "metadata": {"name": "demo", "namespace": "flux-system"},
                "spec": {"interval": "1h", "path": "./app", "prune": True,
                         "sourceRef": {"kind": "GitRepository", "name": "flux-system"}},
            }))
            (app / "kustomization.yaml").write_text(json.dumps({
                "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
                "resources": [f"{i}.yaml" for i in range(len(docs))],
            }))
            for i, doc in enumerate(docs):
                (app / f"{i}.yaml").write_text(json.dumps(doc))
            rendered = summary.run(["flate", "build", "all", "--path", str(root), "--output", "json",
                                    "--skip-secrets", "--skip-crds=false", "--restrict-egress",
                                    "--cache-dir", str(Path(directory) / "cache")],
                                   env={**os.environ, "FLATE_BASE": ""})
            inventories.append(summary.inventory(json.loads(rendered)))
        actual = {**data, **summary.compare(*inventories)}
        assert actual == data
        assert SENTINEL not in json.dumps(actual)

print("Rendered summary checks passed: categories, revision updates, unavailable evidence, secret exclusion")
