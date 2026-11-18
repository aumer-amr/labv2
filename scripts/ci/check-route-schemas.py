#!/usr/bin/env python3
"""Validate Gateway, HTTPRoute, and DNSEndpoint manifests from LabV2's Flux root."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
FLUX_ROOT = "kubernetes/flux/cluster/ks.yaml"
SCHEMAS = "https://k8s-schemas.home-operations.com/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
ROUTES = 'select(.kind == "Gateway" or .kind == "HTTPRoute" or .kind == "DNSEndpoint")'
SUBSTITUTION_DISABLED = (
    '(.metadata.annotations."kustomize.toolkit.fluxcd.io/substitute" == "disabled" or '
    '.metadata.labels."kustomize.toolkit.fluxcd.io/substitute" == "disabled")'
)
KUSTOMIZATIONS = '[select(.apiVersion == "kustomize.toolkit.fluxcd.io/v1" and .kind == "Kustomization")]'


def build(root, flux, definition, manifest):
    meta = definition["metadata"]
    return subprocess.check_output(
        [flux, "build", "kustomization", meta["name"], "--namespace", meta["namespace"],
         "--dry-run", "--kustomization-file", manifest, "--path", root / definition["spec"]["path"]],
        env={},
        text=True,
    )


def render_routes(root, flux):
    root_manifest = root / FLUX_ROOT
    definition = json.loads(subprocess.check_output(["yq", "-o=json", ".", root_manifest]))
    rendered = build(root, flux, definition, root_manifest)
    children = json.loads(subprocess.check_output(
        ["yq", "eval-all", "-o=json", KUSTOMIZATIONS, "-"], input=rendered, text=True,
    ))
    with tempfile.TemporaryDirectory() as directory:
        manifest = Path(directory) / "child.yaml"
        for child in children:
            child_spec = child["spec"]
            # Keep Flux patches and namespaces, and substitute only the selected resources.
            variables = child_spec.pop("postBuild", {}).get("substitute", {})
            manifest.write_text(json.dumps(child))
            manifests = build(root, flux, child, manifest)
            literal_routes = subprocess.check_output(
                ["yq", "eval-all", f"{ROUTES} | select({SUBSTITUTION_DISABLED})", "-"],
                input=manifests, text=True,
            )
            if literal_routes.strip():
                yield literal_routes
            routes = subprocess.check_output(
                ["yq", "eval-all", f"{ROUTES} | select({SUBSTITUTION_DISABLED} | not)", "-"],
                input=manifests, text=True,
            )
            if routes.strip():
                try:
                    yield subprocess.check_output(
                        [flux, "envsubst", "--strict"], input=routes, text=True, env=variables,
                    )
                except subprocess.CalledProcessError:
                    sys.exit(f"Flux substitution failed in {child_spec['path']}")


def validate_routes(manifests):
    return subprocess.run(
        ["kubeconform", "-strict", "-summary", "-schema-location", SCHEMAS],
        input=manifests,
        text=True,
    ).returncode


def main():
    flux = shutil.which("flux")
    if not flux:
        sys.exit("Flux is required on PATH")
    routes = list(render_routes(ROOT, Path(flux).absolute()))
    if not routes:
        sys.exit("No Gateway, HTTPRoute, or DNSEndpoint manifests found")
    return validate_routes("---\n".join(routes))


if __name__ == "__main__":
    sys.exit(main())
