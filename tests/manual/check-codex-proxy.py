#!/usr/bin/env python3
"""Check the Codex proxy manifests. Pass the app-template chart archive as argv[1]."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "kubernetes/apps/ai/codex-proxy/app"
LITELLM = ROOT / "kubernetes/apps/ai/litellm/app"


def documents(path):
    output = subprocess.check_output(["yq", "ea", "-o=json", "-I=0", ".", str(path)])
    return [json.loads(line) for line in output.splitlines() if line != b"null"]


def one(items, kind, name):
    return next(item for item in items if item["kind"] == kind and item["metadata"]["name"] == name)


def matches(selector, labels):
    return all(labels.get(key) == value for key, value in selector.items())


with tempfile.TemporaryDirectory() as directory:
    values = Path(directory) / "values.json"
    values.write_text(json.dumps(documents(APP / "helmrelease.yaml")[0]["spec"]["values"]))
    rendered = Path(directory) / "workload.yaml"
    rendered.write_bytes(subprocess.check_output([
        "helm", "template", "codex-proxy", sys.argv[1], "--namespace", "ai", "-f", str(values),
    ]))
    workload = documents(rendered)

deployment = one(workload, "Deployment", "codex-proxy")["spec"]
pod = deployment["template"]["spec"]
labels = deployment["template"]["metadata"]["labels"]
container, = pod["containers"]
assert {"name": "CODEX_PROXY_DEFAULT_MODEL", "value": "gpt-6.1-sol"} in container["env"]
assert deployment["replicas"] == 1 and deployment["strategy"]["type"] == "Recreate"
assert pod["automountServiceAccountToken"] is False and pod["hostNetwork"] is False
assert pod["securityContext"]["runAsNonRoot"] is True
assert all(pod["securityContext"][key] == 1000 for key in ("runAsUser", "runAsGroup", "fsGroup"))
assert container["securityContext"]["readOnlyRootFilesystem"] is True
assert container["securityContext"]["allowPrivilegeEscalation"] is False
assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
volumes = {volume["name"]: volume for volume in pod["volumes"]}
mounts = {mount["mountPath"]: volumes[mount["name"]] for mount in container["volumeMounts"]}
assert mounts["/home/node/.codex"]["persistentVolumeClaim"]["claimName"] == "codex-proxy"
assert mounts["/tmp"]["emptyDir"] == {}
for probe in ("startupProbe", "livenessProbe", "readinessProbe"):
    assert container[probe]["httpGet"] == {"path": "/health", "port": 3466}
assert all(item["kind"] not in ("Ingress", "HTTPRoute", "ExternalSecret") for item in workload)
service = one(workload, "Service", "codex-proxy")["spec"]
assert service["type"] == "ClusterIP" and matches(service["selector"], labels)
assert service["ports"][0]["port"] == service["ports"][0]["targetPort"] == 3466
pvc = documents(APP / "pvc.yaml")[0]
assert pvc["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"] == "disabled"
assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]

policy = documents(APP / "networkpolicy.yaml")[0]["spec"]
assert matches(policy["podSelector"]["matchLabels"], labels)
assert set(policy["policyTypes"]) == {"Ingress", "Egress"} and policy["egress"] == [{}]
rule, = policy["ingress"]
peer, = rule["from"]
assert set(peer) == {"podSelector"}  # No namespaceSelector: same namespace only.
assert rule["ports"] == [{"port": 3466, "protocol": "TCP"}]
source = peer["podSelector"]["matchLabels"]
assert matches(source, {"app.kubernetes.io/name": "litellm", "app.kubernetes.io/instance": "litellm"})
for name in ("litellm-postgres", "open-webui", "codex-proxy"):
    assert not matches(source, {"app.kubernetes.io/name": name, "app.kubernetes.io/instance": "litellm"})
policies = documents(LITELLM / "networkpolicy.yaml")
egress = one(policies, "NetworkPolicy", "litellm")["spec"]["egress"]
assert any(rule.get("ports") == [{"port": 3466, "protocol": "TCP"}]
           and rule.get("to") == [{"podSelector": {"matchLabels": policy["podSelector"]["matchLabels"]}}]
           for rule in egress)
dns = one(policies, "CiliumNetworkPolicy", "litellm-external")["spec"]["egress"]
for protocol in ("TCP", "UDP"):
    assert any({"port": "53", "protocol": protocol} in port.get("ports", [])
               and {"matchName": "codex-proxy.ai.svc.cluster.local"} in port.get("rules", {}).get("dns", [])
               for rule in dns for port in rule.get("toPorts", []))
coredns = one(documents(ROOT / "kubernetes/apps/kube-system/coredns/app/networkpolicy.yaml"), "NetworkPolicy", "coredns")["spec"]
dns_peer = {
    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ai"}},
    "podSelector": {"matchLabels": policy["podSelector"]["matchLabels"]},
}
for protocol in ("TCP", "UDP"):
    assert any(dns_peer in rule.get("from", [])
               and {"port": 53, "protocol": protocol} in rule.get("ports", [])
               for rule in coredns["ingress"]), f"CoreDNS must accept {protocol} DNS from the proxy"
model = documents(LITELLM / "models/codex-gpt-6-1-sol.yaml")[0]["spec"]
assert model["modelName"] == "codex-gpt-6.1-sol"
assert model["params"] == {"model": "openai/gpt-6.1-sol", "apiBase": "http://codex-proxy.ai.svc.cluster.local:3466/v1", "apiKey": "none"}
assert model["info"]["mode"] == "chat" and model["info"]["supportsVision"] is False
print("PASS: rendered workload, persistent credentials, private Service, network rules, DNS, and model route")
