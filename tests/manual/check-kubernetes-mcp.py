#!/usr/bin/env python3
"""Check Kubernetes MCP access and configuration rollouts. Requires yq and kustomize."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "kubernetes/apps/ai/kubernetes-mcp/app"


def documents(path):
    output = subprocess.check_output(
        ["yq", "ea", "-o=json", "-I=0", ".", str(path)], text=True
    )
    return [json.loads(line) for line in output.splitlines() if line.strip()]


rules = next(doc for doc in documents(APP / "rbac.yaml") if doc["kind"] == "ClusterRole")["rules"]


def allowed(verb, group, resource):
    return any(
        verb in rule["verbs"] and group in rule["apiGroups"] and resource in rule["resources"]
        for rule in rules
    )


for group, resource in [
    ("", "pods"), ("", "pods/log"), ("", "events"), ("", "nodes"),
    ("apps", "deployments"), ("gateway.networking.k8s.io", "httproutes"),
    ("apiextensions.k8s.io", "customresourcedefinitions"),
    ("kustomize.toolkit.fluxcd.io", "kustomizations"),
    ("helm.toolkit.fluxcd.io", "helmreleases"),
]:
    assert allowed("get", group, resource), (group, resource)

for resource in ["secrets", "configmaps", "pods/exec", "pods/attach", "pods/portforward", "nodes/proxy", "serviceaccounts/token"]:
    assert not any(allowed(verb, "", resource) for verb in ["get", "list", "watch", "create"]), resource

for verb in ["get", "list"]:
    assert allowed(verb, "cilium.io", "ciliumnetworkpolicies"), (verb, "ciliumnetworkpolicies")

for rule in rules:
    assert set(rule["verbs"]) <= {"get", "list", "watch"}
    assert all("*" not in value for field in ["verbs", "apiGroups", "resources"] for value in rule[field])
    assert "nonResourceURLs" not in rule

config = tomllib.loads((APP / "config/config.toml").read_text())
assert config["read_only"] is True
assert config["toolsets"] == ["core"]
assert config["cluster_provider_strategy"] == "in-cluster"
assert {item["kind"] for item in config["denied_resources"]} >= {"Secret", "ConfigMap"}
assert set(config["enabled_tools"]) == {
    "events_list", "namespaces_list", "pods_list", "pods_list_in_namespace",
    "pods_get", "pods_log", "resources_list", "resources_get",
}
server = documents(APP / "mcpserver.yaml")[0]["spec"]
assert set(server["params"]["allowed_tools"]) == set(config["enabled_tools"])
assert server["params"]["allow_all_keys"] is False
assert server["workload"]["serviceAccountName"] == "kubernetes-mcp"
assert server["workload"]["podSecurityContext"]["runAsNonRoot"] is True
assert server["workload"]["securityContext"]["readOnlyRootFilesystem"] is True

policies = documents(APP / "networkpolicy.yaml")
dns = next(policy for policy in policies if policy["metadata"]["name"] == "kubernetes-mcp-dns")
assert len(dns["spec"]["egress"]) == 2
for rule, protocol in zip(dns["spec"]["egress"], ["UDP", "TCP"]):
    assert rule["toEndpoints"] == [{"matchLabels": {
        "k8s:io.kubernetes.pod.namespace": "kube-system", "k8s:k8s-app": "kube-dns",
    }}]
    assert rule["toPorts"][0]["ports"] == [{"port": "53", "protocol": protocol}]
    assert {entry["matchName"] for entry in rule["toPorts"][0]["rules"]["dns"]} == {
        "kubernetes.default.svc", "kubernetes.default.svc.cluster.local",
        "kubernetes.default.svc.ai.svc.cluster.local", "kubernetes.default.svc.svc.cluster.local",
    }
coredns = documents(ROOT / "kubernetes/apps/kube-system/coredns/app/networkpolicy.yaml")[0]
assert any(
    peer.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name") == "ai"
    and peer.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/name") == "kubernetes-mcp"
    and {port["protocol"] for port in rule["ports"] if port["port"] == 53} == {"UDP", "TCP"}
    for rule in coredns["spec"]["ingress"] for peer in rule.get("from", [])
), "CoreDNS must accept both DNS transports from the MCP"

paseo = documents(ROOT / "kubernetes/apps/ai/paseo/app/litellmvirtualkey.yaml")[0]
assert "c53d724b-0d4e-4ffa-98cf-441e37bb809f" in paseo["spec"]["mcpToolsets"]

paseo_external = next(
    policy for policy in documents(ROOT / "kubernetes/apps/ai/paseo/app/networkpolicy.yaml")
    if policy["metadata"]["name"] == "paseo-external"
)
assert paseo_external["spec"] == {
    "endpointSelector": {"matchLabels": {
        "app.kubernetes.io/instance": "paseo",
        "app.kubernetes.io/name": "paseo",
    }},
    "egress": [{"toEntities": ["all"]}],
}, "Only interactive Paseo has unrestricted egress, including DNS and schema downloads"


def config_reference(directory):
    rendered = subprocess.check_output(
        ["kustomize", "build", str(directory), "--load-restrictor=LoadRestrictionsNone"], text=True
    )
    output = subprocess.check_output(
        ["yq", "ea", "-o=json", "-I=0", ".", "-"], input=rendered, text=True
    )
    objects = [json.loads(line) for line in output.splitlines() if line.strip()]
    configmap = next(obj for obj in objects if obj["kind"] == "ConfigMap")
    mcp = next(obj for obj in objects if obj["kind"] == "LiteLLMMCPServer")
    reference = mcp["spec"]["workload"]["volumes"][0]["configMap"]["name"]
    assert reference == configmap["metadata"]["name"], "MCP must mount the generated ConfigMap"
    return reference


with tempfile.TemporaryDirectory(prefix="kubernetes-mcp-config-") as temporary:
    copied = Path(temporary) / "app"
    shutil.copytree(APP, copied)
    before = config_reference(copied)
    config_path = copied / "config/config.toml"
    config_path.write_text(config_path.read_text() + "\n# Rollout regression check\n")
    assert config_reference(copied) != before, "Config changes must change the pod volume reference"

print("Kubernetes MCP: RBAC, tools, Paseo grant and configuration rollout checks passed")
