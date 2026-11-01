#!/usr/bin/env python3
"""Verify the rendered automation daemon's storage, credential and network boundary."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "kubernetes/apps/ai/paseo-automation/app"


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs)


def documents(text):
    return [json.loads(line) for line in run(
        "yq", "ea", "-o=json", "-I=0", ".", "-", input=text
    ).splitlines() if line.strip() and line.strip() != "null"]


resources = documents(run("kustomize", "build", str(APP),
                          "--load-restrictor=LoadRestrictionsNone"))
release = next(d for d in resources if d["kind"] == "HelmRelease")
chart = next(d for d in resources if d["kind"] == "OCIRepository")
with tempfile.TemporaryDirectory() as directory:
    values = Path(directory) / "values.json"
    values.write_text(json.dumps(release["spec"]["values"]))
    rendered = documents(run("helm", "template", "paseo-automation", chart["spec"]["url"],
                             "--version", chart["spec"]["ref"]["tag"], "--namespace", "ai",
                             "--api-versions", "gateway.networking.k8s.io/v1/HTTPRoute",
                             "--api-versions", "gateway.networking.k8s.io/v1",
                             "--values", str(values)))

deployment = next(d for d in rendered if d["kind"] == "Deployment")
pod = deployment["spec"]["template"]["spec"]
assert deployment["spec"]["replicas"] == 1
assert pod["runtimeClassName"] == "gvisor"
assert pod["automountServiceAccountToken"] is False
assert pod["securityContext"]["runAsNonRoot"] is True
assert pod["securityContext"]["runAsUser"] != 0
assert not any(pod.get(key) for key in ("hostNetwork", "hostPID", "hostIPC", "initContainers"))
assert len(pod["containers"]) == 1
container = pod["containers"][0]
assert container["securityContext"] == {
    "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
    "readOnlyRootFilesystem": True,
}
assert container["command"] == ["/bin/bash", "/etc/paseo/start.sh"]
assert not container.get("envFrom")
env = {e["name"]: e for e in container["env"]}
secrets = [e["valueFrom"]["secretKeyRef"] for e in env.values() if "valueFrom" in e]
assert secrets == [{"name": "paseo-automation-secret", "key": "PASEO_PASSWORD"}]
assert not ({"GH_TOKEN", "GITHUB_TOKEN", "LITELLM_API_KEY", "MEMINI_API_KEY"} & env.keys())
assert env["PASEO_WEB_UI_ENABLED"]["value"] == "false"
assert env["PASEO_RELAY_ENABLED"]["value"] == "false"
assert env["PASEO_CORS_ORIGINS"]["value"] == "https://paseo.aumer.dev"
assert {v["persistentVolumeClaim"]["claimName"] for v in pod["volumes"]
        if "persistentVolumeClaim" in v} == {"paseo-automation"}
assert not any("hostPath" in v or "secret" in v for v in pod["volumes"])
mounts = {v["mountPath"]: v for v in container["volumeMounts"]}
assert mounts["/home/paseo"]["subPath"] == "home"
assert mounts["/workspace"]["subPath"] == "workspace"
assert mounts["/etc/paseo"]["readOnly"] is True

config = next(d for d in resources if d["kind"] == "ConfigMap")
assert config["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/substitute"] == "disabled"
codex = tomllib.loads(config["data"]["codex.toml"])
assert "mcp_servers" not in codex
assert codex["approval_policy"] == "on-request"
assert codex["sandbox_mode"] == "workspace-write"
assert codex["sandbox_workspace_write"]["network_access"] is False
daemon = json.loads(config["data"]["paseo.json"])
assert daemon["daemon"] == {"relay": {"enabled": False}, "mcp": {"enabled": False}}
assert daemon["features"] == {"dictation": {"enabled": False}, "voiceMode": {"enabled": False}}
for password in (None, ""):
    test_env = dict(os.environ)
    test_env.pop("PASEO_PASSWORD", None)
    if password is not None:
        test_env["PASEO_PASSWORD"] = password
    result = subprocess.run(["bash", str(APP / "config/start.sh")], env=test_env,
                            capture_output=True, text=True)
    assert result.returncode != 0 and "PASEO_PASSWORD must be non-empty" in result.stderr

policy = next(d for d in resources if d["kind"] == "NetworkPolicy")["spec"]
labels = {"app.kubernetes.io/instance": "paseo-automation",
          "app.kubernetes.io/name": "paseo-automation"}
assert policy["podSelector"]["matchLabels"] == labels
assert set(policy["policyTypes"]) == {"Ingress", "Egress"} and not policy.get("egress")
assert len(policy["ingress"]) == 1
assert policy["ingress"][0] == {
    "from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "network"}},
              "podSelector": {"matchLabels": {
                  "gateway.envoyproxy.io/owning-gateway-name": "envoy-internal"}}}],
    "ports": [{"port": 6767, "protocol": "TCP"}],
}
external = next(d for d in resources if d["kind"] == "CiliumNetworkPolicy")["spec"]
assert external["endpointSelector"]["matchLabels"] == labels
assert len(external["egress"]) == 3
hosts = {"api.openai.com", "auth.openai.com", "chatgpt.com", "ab.chatgpt.com"}
for rule, protocol in zip(external["egress"][:2], ("UDP", "TCP")):
    assert rule["toEndpoints"] == [{"matchLabels": {
        "k8s:io.kubernetes.pod.namespace": "kube-system", "k8s:k8s-app": "kube-dns"}}]
    assert rule["toPorts"][0]["ports"] == [{"port": "53", "protocol": protocol}]
    assert {r["matchName"] for r in rule["toPorts"][0]["rules"]["dns"]} == hosts
assert {r["matchName"] for r in external["egress"][2]["toFQDNs"]} == hosts
assert external["egress"][2]["toPorts"] == [{"ports": [{"port": "443", "protocol": "TCP"}]}]
route_resource = next(d for d in rendered if d["kind"] == "HTTPRoute")
assert route_resource["apiVersion"] == "gateway.networking.k8s.io/v1"
route = route_resource["spec"]
assert route["hostnames"] == ["paseo-automation.aumer.dev"]
assert route["parentRefs"] == [{"group": "gateway.networking.k8s.io", "kind": "Gateway",
                               "name": "envoy-internal", "namespace": "network"}]
gateway_policies = documents((ROOT / "kubernetes/apps/network/envoy-gateway/app/networkpolicy.yaml").read_text())
gateway = next(d for d in gateway_policies if d["metadata"]["name"] == "allow-from-envoy-internal")
assert any(
    {"k8sService": {"serviceName": "paseo-automation", "namespace": "ai"}} in rule.get("toServices", [])
    and {"ports": [{"port": "6767", "protocol": "TCP"}]} in rule.get("toPorts", [])
    for spec in gateway["specs"] for rule in spec.get("egress", [])
), "Internal Envoy must be allowed to reach the daemon backend"

flux = documents(run("flux", "build", "kustomization", "paseo-automation", "--path", str(APP),
                     "--kustomization-file", str(APP.parent / "ks.yaml"), "--dry-run"))
assert all(d["metadata"]["namespace"] == "ai" for d in flux)
pvc = next(d for d in flux if d["kind"] == "PersistentVolumeClaim")
assert pvc["metadata"]["name"] == "paseo-automation"
assert pvc["spec"]["resources"]["requests"]["storage"] == "10Gi"
assert pvc["spec"]["dataSourceRef"]["name"] == "paseo-automation"
assert next(d for d in flux if d["kind"] == "ConfigMap")["data"] == config["data"]
dns = documents((ROOT / "kubernetes/apps/kube-system/coredns/app/networkpolicy.yaml").read_text())
assert any(
    peer.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name") == "ai"
    and peer.get("podSelector", {}).get("matchLabels") == labels
    and {p["protocol"] for p in rule.get("ports", []) if p["port"] == 53} == {"UDP", "TCP"}
    for policy in dns if policy["kind"] == "NetworkPolicy"
    for rule in policy["spec"].get("ingress", []) for peer in rule.get("from", [])
), "CoreDNS must accept both DNS transports from the isolated daemon"
print("Paseo automation boundary checks passed")
