#!/usr/bin/env python3
"""Post-merge revision/readiness checks; synthetic data, no credentials or writes."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rollout", ROOT / "kubernetes/apps/ai/hermes/app/discord-review/rollout.py")
rollout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollout)
SHA, OLDER, NEWER = "a" * 40, "b" * 40, "c" * 40
PR = {"number": 42, "merge_commit_sha": SHA, "merged": True, "base": {"ref": "main"},
      "merged_at": datetime.now(timezone.utc).isoformat()}


def obj(kind, name="echo", **extra):
    return {"kind": kind, "metadata": {"name": name, "namespace": "network", "generation": 2,
            "resourceVersion": "10", "uid": kind + name}, "spec": {}, "status": {}, **extra}


def condition(name="Ready", state="True", generation=2):
    return {"type": name, "status": state, "observedGeneration": generation}


KS = obj("Kustomization", spec={"path": "./kubernetes/apps/network/echo/app", "wait": False,
         "sourceRef": {"kind": "GitRepository", "name": "flux-system", "namespace": "flux-system"}},
         status={"conditions": [condition()], "lastAppliedRevision": "main@sha1:" + SHA,
                 "lastAttemptedRevision": "main@sha1:" + SHA,
                 "inventory": {"entries": [{"id": "network_echo_helm.toolkit.fluxcd.io_HelmRelease", "v": "v2"}]}})
HR = obj("HelmRelease", status={"conditions": [condition()]})
DEP = obj("Deployment", spec={"replicas": 2}, status={"observedGeneration": 2,
          "replicas": 2, "updatedReplicas": 2, "readyReplicas": 2})
DEP["metadata"]["labels"] = {"helm.toolkit.fluxcd.io/name": "echo", "helm.toolkit.fluxcd.io/namespace": "network"}
RS = obj("ReplicaSet", spec={"replicas": 2}, status={"observedGeneration": 2, "replicas": 2, "readyReplicas": 2})
RS["metadata"]["ownerReferences"] = [{"uid": "Deploymentecho"}]
POD = obj("Pod", status={"phase": "Running", "conditions": [condition()]})
POD["metadata"]["ownerReferences"] = [{"uid": "ReplicaSetecho"}]


class API:
    def __init__(self):
        self.writes = []
        self.paths = [{"filename": "kubernetes/apps/network/echo/app/helmrelease.yaml"}]

    def pages(self, path):
        assert path == "pulls/42/files"
        return self.paths

    def request(self, path, method="GET", data=None):
        if method == "POST":
            assert path == f"statuses/{SHA}"
            self.writes.append(data)
            return {}
        assert path.startswith(f"compare/{SHA}...")
        return {"status": "ahead" if path.endswith(NEWER) else "behind"}


class Kube:
    def __init__(self):
        self.data = {kind: [] for kind in rollout.KINDS}
        for item in (KS, HR, DEP, RS, POD):
            self.data[item["kind"]].append(deepcopy(item))
        self.reads = []

    def get(self, path):
        self.reads.append(path)
        kind = next(k for k in rollout.KINDS if rollout.api_path(k) == path)
        return {"items": deepcopy(self.data[kind])}


api, kube = API(), Kube()
result = rollout.observe(api, kube, PR)
assert result["reconciliation"] == result["readiness"] == "ready"
assert result["resources"] == 4 and not api.writes
assert all(path.startswith(("/api/", "/apis/")) for path in kube.reads)

# wait:false reconciliation success is not deployment readiness.
kube.data["Deployment"][0]["status"]["readyReplicas"] = 1
result = rollout.observe(api, kube, PR)
assert result["reconciliation"] == "ready" and result["readiness"] == "pending"
assert any("Deployment network/echo: pending" == d for d in result["details"])
state = rollout.feedback(api, kube, PR)
assert [w["state"] for w in api.writes] == ["success", "pending"]
assert all(w["context"].startswith("Flux / ") for w in api.writes)
assert not state["rollout_done"] and "not assessed" in state["reason"]
rollout.feedback(api, kube, PR, state["rollout"])
assert len(api.writes) == 2  # No duplicate statuses every minute.

for field in ("observedGeneration", "updatedReplicas", "readyReplicas"):
    pending = deepcopy(DEP)
    pending["status"][field] = 1
    assert rollout.ready(pending) == "pending"
failed = deepcopy(DEP)
failed["status"]["conditions"] = [{"type": "Progressing", "reason": "ProgressDeadlineExceeded", "status": "False"}]
assert rollout.ready(failed) == "failed"
assert rollout.ready(obj("Job", status={"conditions": [condition("Failed")]})) == "failed"
assert rollout.ready(obj("Job", status={"conditions": [condition("Complete")]})) == "ready"
assert rollout.ready(obj("PersistentVolumeClaim", status={"phase": "Lost"})) == "failed"
assert rollout.ready(obj("PersistentVolumeClaim", status={"phase": "Bound"})) == "ready"
assert rollout.ready(obj("Pod", status={"phase": "Pending"})) == "pending"
assert rollout.ready(obj("Pod", status={"phase": "Failed"})) == "failed"
assert rollout.ready(obj("Pod", status={"phase": "Succeeded"})) == "ready"
assert rollout.ready(obj("HelmRelease", spec={"suspend": True})) == "unavailable"
assert rollout.ready(obj("HelmRelease", status={"conditions": [condition(generation=1)]})) == "pending"

route = obj("HTTPRoute", spec={"parentRefs": [{"name": "gateway"}]}, status={"parents": [{
    "parentRef": {"name": "gateway"}, "conditions": [condition("Accepted"), condition("ResolvedRefs")]}]})
assert rollout.ready(route) == "ready"
route["status"]["parents"][0]["parentRef"]["name"] = "wrong-parent"
assert rollout.ready(route) == "pending"
route["status"]["parents"][0]["parentRef"]["name"] = "gateway"
route["status"]["parents"][0]["conditions"][0]["status"] = "False"
assert rollout.ready(route) == "failed"

# Failure on an older/newer revision must never be attributed to this merge.
for revision, expected in ((OLDER, "pending"), (NEWER, "superseded"), (SHA, "failed")):
    kube = Kube()
    ks = kube.data["Kustomization"][0]
    ks["status"].update(lastAppliedRevision="main@sha1:" + OLDER, lastAttemptedRevision="main@sha1:" + revision)
    ks["status"]["conditions"] = [condition(state="False")]
    result = rollout.observe(API(), kube, PR)
    assert result["reconciliation"] == expected
    assert result["readiness"] != "ready"
kube = Kube()
kube.data["Kustomization"][0]["status"]["lastAttemptedRevision"] = "main@sha1:" + NEWER
assert rollout.observe(API(), kube, PR)["reconciliation"] == "superseded"

for removal, expected in (("HelmRelease", "pending"), ("inventory", "unavailable")):
    kube = Kube()
    if removal == "inventory":
        del kube.data["Kustomization"][0]["status"]["inventory"]
    else:
        kube.data[removal] = []
    assert rollout.observe(API(), kube, PR)["readiness"] == expected

# A new Flux revision arriving during observation invalidates the snapshot.
kube = Kube()
read = kube.get
def changing(path):
    result = read(path)
    if path == rollout.api_path("Kustomization") and kube.reads.count(path) == 2:
        result["items"][0]["metadata"]["resourceVersion"] = "11"
    return result
with patch.object(kube, "get", side_effect=changing):
    assert rollout.observe(API(), kube, PR)["readiness"] == "unavailable"

# Unavailable API data and exhausted observation windows are not health failures.
api, kube = API(), Kube()
with patch.object(kube, "get", side_effect=RuntimeError("PRIVATE_DIAGNOSTIC")):
    report = rollout.feedback(api, kube, PR)
    assert [w["state"] for w in api.writes] == ["error", "error"]
    assert "PRIVATE_DIAGNOSTIC" not in str(report) + str(api.writes)
expired = {**PR, "merged_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}
kube.data["Deployment"][0]["status"]["readyReplicas"] = 0
api = API()
assert rollout.feedback(api, kube, expired)["rollout_done"]
assert [w["state"] for w in api.writes] == ["success", "error"]

# App edits select the deepest owner; shared changes conservatively select all.
parent = deepcopy(KS)
parent["metadata"]["name"] = "cluster-apps"
parent["spec"]["path"] = "./kubernetes/apps"
other = deepcopy(KS)
other["metadata"]["name"] = "other"
other["spec"]["path"] = "./kubernetes/apps/ai/other/app"
owners = [parent, KS, other]
assert rollout.affected(["kubernetes/apps/network/echo/app/helmrelease.yaml"], owners) == [KS]
assert rollout.affected(["kubernetes/apps/network/echo/ks.yaml"], owners) == [KS]
assert rollout.affected(["kubernetes/apps/network/kustomization.yaml"], owners) == [KS]
assert rollout.affected(["kubernetes/components/common/kustomization.yaml"], owners) == owners
assert rollout.affected(["scripts/tool.py"], owners) == []
api = API()
api.paths = [{"filename": "scripts/tool.py"}]
assert rollout.observe(api, Kube(), PR)["readiness"] == "not applicable"
assert rollout.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.test") is None

# GitHub publication failure must still update Discord and retry the statuses.
api, kube = API(), Kube()
with patch.object(api, "request", side_effect=RuntimeError("PRIVATE_DIAGNOSTIC")):
    state = rollout.feedback(api, kube, PR)
assert not state["rollout_done"] and "publication unavailable" in state["reason"]
assert "PRIVATE_DIAGNOSTIC" not in str(state)
assert rollout.feedback(api, kube, PR, state["rollout"])["rollout_done"]
assert len(api.writes) == 2

print("Flux rollout checks passed: exact revision, pending/failure/superseded states, separate readiness, GET-only observation")
