#!/usr/bin/env python3
"""Check rendered Flux children and preserve network-policy gates against a PR base.

Run with mise exec -- python3 scripts/check-kubernetes-conventions.py BASE [HEAD].
Without HEAD, validate the working tree. No secrets are decrypted or printed.
"""

import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
# Approved colocated MCP services: exact identity, path, and target, not a wildcard.
TARGET_EXCEPTIONS = {
    ("flux-system", "konflate-mcp"): ("kubernetes/apps/flux-system/konflate/mcp", "ai"),
    ("home", "ha-mcp"): ("kubernetes/apps/home/home-assistant/mcp", "ai"),
    ("observability", "victoria-logs-mcp"): ("kubernetes/apps/observability/victoria-logs/mcp", "ai"),
}


def render(root):
    built = subprocess.run(
        ["flux", "build", "kustomization", "cluster-apps", "--namespace", "flux-system",
         "--dry-run", "--in-memory-build", "--kubeconfig", "/dev/null",
         "--path", str(root / "kubernetes/apps"),
         "--kustomization-file", str(root / "kubernetes/flux/cluster/ks.yaml")],
        cwd=root, capture_output=True, check=True,
    )
    # Filter out Secret documents before decoding; never log rendered manifests.
    selected = subprocess.run(
        ["yq", "ea", "-o=json", "-I=0",
         'select(.kind == "Kustomization" and (.apiVersion | test("^kustomize[.]toolkit[.]fluxcd[.]io/")))', "-"],
        input=built.stdout, capture_output=True, check=True,
    )
    children = {}
    for line in selected.stdout.splitlines():
        child = json.loads(line)
        namespace, name = child["metadata"]["namespace"], child["metadata"]["name"]
        if (namespace, name) in children:
            raise ValueError(f"{namespace}/{name}: duplicate Flux child")
        children[namespace, name] = child["spec"]
    if not children:
        raise ValueError("No Flux children found")
    return children


def dependencies(namespace, spec):
    return {(dep.get("namespace", namespace), dep["name"]): dep for dep in spec.get("dependsOn", [])}


def validate(before, after):
    errors = []
    for key, spec in after.items():
        namespace, name = key
        label = f"{namespace}/{name}"
        path = PurePosixPath(spec.get("path", ""))
        target = spec.get("targetNamespace")
        if ".." in path.parts or path.parts[:3] != ("kubernetes", "apps", namespace):
            errors.append(f"{label}: spec.path must stay under kubernetes/apps/{namespace}")
        if target != namespace and TARGET_EXCEPTIONS.get(key) != (str(path), target):
            errors.append(f"{label}: targetNamespace must be {namespace}")
        for dependency, dep in dependencies(namespace, spec).items():
            if dep["name"] == "onepassword-connect" and dep.get("namespace") != "external-secrets":
                errors.append(f"{label}: onepassword-connect requires namespace: external-secrets")
            if dependency not in after:
                errors.append(f"{label}: unresolved dependsOn {dependency[0]}/{dependency[1]}; cross-namespace references require an explicit namespace")
            if name == "network-policies" or dependency[1] == "network-policies":
                if dep.get("readyExpr"):
                    errors.append(f"{label}: readyExpr must not replace a network-policy readiness gate")
        if name == "network-policies":
            if str(path) != f"kubernetes/apps/{namespace}/network-policies/app":
                errors.append(f"{label}: preserve the dedicated network-policies app path")
            if spec.get("suspend", False) or spec.get("wait") is not True or spec.get("prune") is not True:
                errors.append(f"{label}: network-policy child must be active with wait and prune enabled")
            if key in before and spec.get("sourceRef") != before[key].get("sourceRef"):
                errors.append(f"{label}: network-policy sourceRef changed")

    for key, spec in before.items():
        namespace, name = key
        label = f"{namespace}/{name}"
        if key not in after:
            if name == "network-policies":
                errors.append(f"{label}: existing network-policy child removed")
            continue
        current = dependencies(namespace, after[key])
        for dependency in dependencies(namespace, spec):
            if (name == "network-policies" or dependency[1] == "network-policies") and dependency not in current:
                errors.append(f"{label}: removed network-policy gate on {dependency[0]}/{dependency[1]}")
    return errors


def snapshot(revision, directory):
    archive = subprocess.check_output(["git", "archive", "--format=tar", revision, "kubernetes"], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        tree.extractall(directory, filter="data")
    return render(Path(directory))


def check(base, head=None):
    with tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as new:
        if head:
            base = subprocess.check_output(["git", "merge-base", base, head], cwd=ROOT, text=True).strip()
        before = snapshot(base, old)
        after = snapshot(head, new) if head else render(ROOT)
        errors = validate(before, after)
    if errors:
        raise ValueError("\n".join(errors))
    print(f"Kubernetes conventions passed: {len(after)} Flux children")


if __name__ == "__main__":
    try:
        check(*sys.argv[1:])
    except (ValueError, KeyError, subprocess.CalledProcessError) as error:
        # Tool stderr can contain manifest fragments; keep it out of CI logs.
        sys.exit(f"Kubernetes convention check failed: {error}")
