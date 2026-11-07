#!/usr/bin/env python3
"""Assert HTTPS listener scope and connection settings after Kustomize rendering."""

import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
rendered = subprocess.check_output([
    "kustomize", "build", str(ROOT / "kubernetes/apps/network/envoy-gateway/app"),
    "--load-restrictor=LoadRestrictionsNone",
], text=True)
documents = subprocess.check_output(
    ["yq", "ea", "-o=json", "-I=0", ".", "-"], input=rendered, text=True,
)
resources = [json.loads(line) for line in documents.splitlines() if line != "null"]
policies = {r["metadata"]["name"]: r["spec"] for r in resources
            if r["kind"] == "ClientTrafficPolicy"}
base = policies["envoy"]
hsts = policies["envoy-https-hsts"]

# A listener override must not silently drop future additions to the base policy.
assert {k: v for k, v in hsts.items() if k not in ("targetRefs", "headers")} == {
    k: v for k, v in base.items() if k != "targetSelectors"
}
assert hsts["headers"] == {"lateResponseHeaders": {"addIfAbsent": [
    {"name": "Strict-Transport-Security", "value": "max-age=300"},
]}}
assert len(hsts["targetRefs"]) == 2
assert {ref["name"] for ref in hsts["targetRefs"]} == {"envoy-external", "envoy-internal"}
gateways = {r["metadata"]["name"]: r for r in resources if r["kind"] == "Gateway"}
for ref in hsts["targetRefs"]:
    assert ref["group"] == "gateway.networking.k8s.io" and ref["kind"] == "Gateway"
    listener = next(l for l in gateways[ref["name"]]["spec"]["listeners"]
                    if l["name"] == ref["sectionName"])
    assert listener["protocol"] == "HTTPS" and listener["port"] == 443

print("Envoy HSTS listener scope and connection settings passed")
