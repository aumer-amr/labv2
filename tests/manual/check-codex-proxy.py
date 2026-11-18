#!/usr/bin/env python3
"""Check the Codex proxy manifests. Pass the app-template chart archive as argv[1]."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
AI = ROOT / "kubernetes/apps/ai"
LITELLM = ROOT / "kubernetes/apps/ai/litellm/app"


def documents(path):
    output = subprocess.check_output(["yq", "ea", "-o=json", "-I=0", ".", str(path)])
    return [json.loads(line) for line in output.splitlines() if line != b"null"]


def one(items, kind, name):
    return next(item for item in items if item["kind"] == kind and item["metadata"]["name"] == name)


def matches(selector, labels):
    return all(labels.get(key) == value for key, value in selector.items())


def check_provider_egress(app, labels):
    cilium = one(documents(app / "networkpolicy.yaml"), "CiliumNetworkPolicy", "cliproxyapi-external")["spec"]
    assert matches(cilium["endpointSelector"]["matchLabels"], labels)
    hosts = [{"matchName": host} for host in ("auth.openai.com", "chatgpt.com")]
    udp, tcp, https = cilium["egress"]
    for rule, protocol in ((udp, "UDP"), (tcp, "TCP")):
        assert rule["toEndpoints"] == [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "kube-system", "k8s:k8s-app": "kube-dns"}}]
        assert rule["toPorts"] == [{"ports": [{"port": "53", "protocol": protocol}], "rules": {"dns": hosts}}]
    assert https == {"toFQDNs": hosts, "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]}


def check_proxy(name):
    app = AI / name / "app"
    candidate = name == "cliproxyapi"
    with tempfile.TemporaryDirectory() as directory:
        values = Path(directory) / "values.json"
        values.write_text(json.dumps(documents(app / "helmrelease.yaml")[0]["spec"]["values"]))
        rendered = Path(directory) / "workload.yaml"
        rendered.write_bytes(subprocess.check_output([
            "helm", "template", name, sys.argv[1], "--namespace", "ai", "-f", str(values),
        ]))
        workload = documents(rendered)

    deployment = one(workload, "Deployment", name)["spec"]
    pod = deployment["template"]["spec"]
    labels = deployment["template"]["metadata"]["labels"]
    container, = pod["containers"]
    assert deployment["replicas"] == 1 and deployment["strategy"]["type"] == "Recreate"
    assert pod["automountServiceAccountToken"] is False and pod["hostNetwork"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert all(pod["securityContext"][key] == 1000 for key in ("runAsUser", "runAsGroup", "fsGroup"))
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    mounts = {mount["mountPath"]: volumes[mount["name"]] for mount in container["volumeMounts"]}
    claims = [volume["persistentVolumeClaim"]["claimName"] for volume in volumes.values() if "persistentVolumeClaim" in volume]
    assert claims == [name]
    if candidate:
        config = documents(app / "config.yaml")[0]
        assert config["server"]["port"] == 3466 and config["management"]["disable-control-panel"]
        assert mounts[config["oauth"]["auth-dir"]]["persistentVolumeClaim"]["claimName"] == name
        assert mounts["/etc/cliproxyapi"]["configMap"]["name"] == "cliproxyapi-config"
        assert container["args"] == ["--config", "/etc/cliproxyapi/config.yaml", "--local-model"]
    else:
        assert {"name": "CODEX_PROXY_DEFAULT_MODEL", "value": "gpt-6.1-sol"} in container["env"]
        assert mounts["/home/node/.codex"]["persistentVolumeClaim"]["claimName"] == name
    assert mounts["/tmp"]["emptyDir"] == {}
    for probe in ("startupProbe", "livenessProbe", "readinessProbe"):
        assert container[probe]["httpGet"] == {"path": "/healthz" if candidate else "/health", "port": 3466}
    assert all(item["kind"] not in ("Ingress", "HTTPRoute", "ExternalSecret") for item in workload)
    service = one(workload, "Service", name)["spec"]
    assert service["selector"]["app.kubernetes.io/instance"] == name
    assert service["type"] == "ClusterIP" and matches(service["selector"], labels)
    assert service["ports"][0]["port"] == service["ports"][0]["targetPort"] == 3466
    pvc = documents(app / "pvc.yaml")[0]
    assert pvc["metadata"]["name"] == name
    assert pvc["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"] == "disabled"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]

    policy = documents(app / "networkpolicy.yaml")[0]["spec"]
    assert policy["podSelector"]["matchLabels"] == {"app.kubernetes.io/instance": name, "app.kubernetes.io/name": name}
    assert matches(policy["podSelector"]["matchLabels"], labels)
    assert set(policy["policyTypes"]) == {"Ingress", "Egress"}
    if candidate:
        assert not policy.get("egress")
        check_provider_egress(app, labels)
    else:
        assert policy["egress"] == [{}]
    rule, = policy["ingress"]
    peer, = rule["from"]
    assert set(peer) == {"podSelector"}  # No namespaceSelector: same namespace only.
    assert rule["ports"] == [{"port": 3466, "protocol": "TCP"}]
    source = peer["podSelector"]["matchLabels"]
    assert matches(source, {"app.kubernetes.io/name": "litellm", "app.kubernetes.io/instance": "litellm"})
    for other in ("litellm-postgres", "open-webui", "codex-proxy", "cliproxyapi"):
        assert not matches(source, {"app.kubernetes.io/name": other, "app.kubernetes.io/instance": "litellm"})
    policy_file = "cliproxyapi-networkpolicy.yaml" if candidate else "networkpolicy.yaml"
    policies = documents(LITELLM / policy_file)
    egress = one(policies, "NetworkPolicy", "litellm-cliproxyapi" if candidate else "litellm")["spec"]["egress"]
    assert any(rule.get("ports") == [{"port": 3466, "protocol": "TCP"}]
               and rule.get("to") == [{"podSelector": policy["podSelector"]}]
               for rule in egress)
    dns = one(policies, "CiliumNetworkPolicy", "litellm-cliproxyapi-dns" if candidate else "litellm-external")["spec"]["egress"]
    for protocol in ("TCP", "UDP"):
        assert any({"port": "53", "protocol": protocol} in port.get("ports", [])
                   and {"matchName": f"{name}.ai.svc.cluster.local"} in port.get("rules", {}).get("dns", [])
                   for rule in dns for port in rule.get("toPorts", []))
    coredns = one(documents(ROOT / "kubernetes/apps/kube-system/coredns/app" / policy_file), "NetworkPolicy", "coredns-cliproxyapi" if candidate else "coredns")["spec"]
    dns_peer = {
        "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ai"}},
        "podSelector": {"matchLabels": policy["podSelector"]["matchLabels"]},
    }
    for protocol in ("TCP", "UDP"):
        assert any(dns_peer in rule.get("from", [])
                   and {"port": 53, "protocol": protocol} in rule.get("ports", [])
                   for rule in coredns["ingress"]), f"CoreDNS must accept {protocol} DNS from the proxy"


for name in ("codex-proxy", "cliproxyapi"):
    check_proxy(name)

model = documents(LITELLM / "models/codex-gpt-6-1-sol.yaml")[0]["spec"]
assert model["modelName"] == "codex-gpt-6.1-sol"
assert model["params"] == {"model": "openai/gpt-6.1-sol", "apiBase": "http://cliproxyapi.ai.svc.cluster.local:3466/v1", "apiKey": "none"}
assert model["info"]["mode"] == "chat" and model["info"]["supportsVision"] is False
print("PASS: rendered workload, persistent credentials, private Service, network rules, DNS, and model route")
