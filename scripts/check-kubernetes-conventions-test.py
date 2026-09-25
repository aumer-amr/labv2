#!/usr/bin/env python3
"""Small regression fixtures for Flux namespace and network-policy conventions."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile

spec = importlib.util.spec_from_file_location(
    "conventions", Path(__file__).with_name("check-kubernetes-conventions.py"),
)
conventions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conventions)


def child(namespace, name, deps=()):
    return {
        "path": f"./kubernetes/apps/{namespace}/{name}/app", "targetNamespace": namespace,
        "sourceRef": {"kind": "GitRepository", "name": "flux-system", "namespace": "flux-system"},
        "prune": True, "wait": True, "dependsOn": list(deps),
    }


baseline = {
    ("home", "app"): child("home", "app", [{"name": "onepassword-connect", "namespace": "external-secrets"}]),
    ("home", "network-policies"): child("home", "network-policies", [{"name": "app"}]),
    ("home", "consumer"): child("home", "consumer", [{"name": "network-policies"}]),
    ("external-secrets", "onepassword-connect"): child("external-secrets", "onepassword-connect"),
    ("external-secrets", "network-policies"): child("external-secrets", "network-policies"),
}
assert conventions.validate(baseline, baseline) == []
for key, (path, target) in conventions.TARGET_EXCEPTIONS.items():
    after = deepcopy(baseline)
    after[key] = {**child(*key), "path": f"./{path}", "targetNamespace": target}
    assert conventions.validate(baseline, after) == []
    after[key]["path"] += "-other"
    assert conventions.validate(baseline, after)

for key, field, value, message in [
    (("home", "app"), "targetNamespace", "ai", "targetNamespace"),
    (("home", "app"), "path", "./kubernetes/apps/ai/app", "spec.path"),
    (("home", "app"), "path", "./kubernetes/apps/home/../ai/app", "spec.path"),
    (("home", "app"), "dependsOn", [{"name": "onepassword-connect"}], "requires namespace"),
    (("home", "app"), "dependsOn", [{"name": "onepassword-connect", "namespace": "ai"}], "requires namespace"),
    (("home", "app"), "dependsOn", [{"name": "missing", "namespace": "external-secrets"}], "unresolved"),
    (("home", "network-policies"), "dependsOn", [], "removed network-policy gate"),
    (("home", "consumer"), "dependsOn", [], "removed network-policy gate"),
    (("home", "network-policies"), "dependsOn", [{"name": "app", "readyExpr": "true"}], "readyExpr"),
    (("home", "consumer"), "dependsOn", [{"name": "network-policies", "readyExpr": "true"}], "readyExpr"),
    (("home", "network-policies"), "suspend", True, "must be active"),
    (("home", "network-policies"), "wait", False, "wait and prune"),
    (("home", "network-policies"), "prune", False, "wait and prune"),
    (("home", "network-policies"), "path", "./kubernetes/apps/home/app/app", "dedicated"),
    (("home", "network-policies"), "sourceRef", {"kind": "GitRepository", "name": "other"}, "sourceRef"),
]:
    after = deepcopy(baseline)
    after[key][field] = value
    assert any(message in error for error in conventions.validate(baseline, after)), (key, field)

after = deepcopy(baseline)
del after["home", "network-policies"]
assert any("child removed" in error for error in conventions.validate(baseline, after))
# Explicit same-namespace references preserve the same dependency identity.
after = deepcopy(baseline)
after["home", "network-policies"]["dependsOn"][0]["namespace"] = "home"
assert conventions.validate(baseline, after) == []

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    parent_file = root / "kubernetes/flux/cluster/ks.yaml"
    parent_file.parent.mkdir(parents=True)
    parent = {"apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization",
              "metadata": {"name": "cluster-apps", "namespace": "flux-system"},
              "spec": {"path": "./kubernetes/apps", "interval": "1h", "prune": True,
                       "sourceRef": {"kind": "GitRepository", "name": "flux-system"}}}
    parent_file.write_text(json.dumps(parent))
    for namespace in ("home", "external-secrets"):
        folder = root / "kubernetes/apps" / namespace
        folder.mkdir(parents=True)
        resources = []
        for (ns, name), value in baseline.items():
            if ns != namespace:
                continue
            resources.append(f"{name}.yaml")
            document = {"apiVersion": "kustomize.toolkit.fluxcd.io/v1", "kind": "Kustomization",
                        "metadata": {"name": name}, "spec": value}
            (folder / f"{name}.yaml").write_text(json.dumps(document))
        (folder / "kustomization.yaml").write_text(json.dumps({
            "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
            "namespace": namespace, "resources": resources,
        }))
    assert conventions.render(root) == baseline
    root_file = root / "kubernetes/apps/home/kustomization.yaml"
    original = json.loads(root_file.read_text())
    patched = deepcopy(original)
    patched["patches"] = [{"target": {"kind": "Kustomization", "name": "network-policies"},
                           "patch": '[{"op":"remove","path":"/spec/dependsOn"}]'}]
    root_file.write_text(json.dumps(patched))
    assert any("removed network-policy gate" in error for error in conventions.validate(baseline, conventions.render(root)))
    root_file.write_text(json.dumps(original))
    parent["spec"]["patches"] = patched["patches"]
    parent_file.write_text(json.dumps(parent))
    assert any("removed network-policy gate" in error for error in conventions.validate(baseline, conventions.render(root)))
    del parent["spec"]["patches"]
    parent_file.write_text(json.dumps(parent))
    original["resources"].remove("network-policies.yaml")
    root_file.write_text(json.dumps(original))
    assert any("child removed" in error for error in conventions.validate(baseline, conventions.render(root)))

print("Kubernetes convention fixtures passed")
