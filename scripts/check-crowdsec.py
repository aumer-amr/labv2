#!/usr/bin/env python3
"""Check CrowdSec contracts against locally rendered chart manifests.

Usage: python3 scripts/check-crowdsec.py CROWDSEC_RENDER.yaml BOUNCER_RENDER.yaml
"""

from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
NETWORK = ROOT / "kubernetes/apps/network"


def documents(path):
    return [doc for doc in yaml.safe_load_all(Path(path).read_text()) if doc]


def resource(docs, kind, name):
    matches = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(matches) == 1, (kind, name, len(matches))
    return matches[0]


def matches(selector, labels):
    assert not selector.get("matchExpressions"), "Extend this check for expression selectors"
    return all(labels.get(k) == v for k, v in selector.get("matchLabels", {}).items())


def allows(policies, local, peer, direction, port, namespace="network", peer_namespace="network"):
    peer_key = "from" if direction == "ingress" else "to"
    for policy in policies:
        if policy["kind"] != "NetworkPolicy" or not matches(policy["spec"]["podSelector"], local):
            continue
        for rule in policy["spec"].get(direction, []):
            if not any(p.get("port") == port and p.get("protocol", "TCP") == "TCP" for p in rule.get("ports", [])):
                continue
            for target in rule.get(peer_key, []):
                if "ipBlock" in target:
                    continue
                if "namespaceSelector" in target:
                    if not matches(target["namespaceSelector"], {"kubernetes.io/metadata.name": peer_namespace}):
                        continue
                elif namespace != peer_namespace:
                    continue
                if matches(target.get("podSelector", {}), peer):
                    return True
    return False


def main():
    crowdsec, bouncer = map(documents, sys.argv[1:])
    pods = {}
    for name, docs in [("crowdsec-agent", crowdsec), ("crowdsec-lapi", crowdsec), ("crowdsec-bouncer", bouncer)]:
        deployment = resource(docs, "Deployment", name)
        template = deployment["spec"]["template"]
        pods[name] = template["metadata"]["labels"]
        spec = template["spec"]
        assert not spec.get("hostNetwork")
        assert all("hostPath" not in volume for volume in spec["volumes"])
        assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
        for container in spec.get("initContainers", []) + spec["containers"]:
            security = container["securityContext"]
            assert security["allowPrivilegeEscalation"] is False
            assert security["capabilities"]["drop"] == ["ALL"]
        if name == "crowdsec-agent":
            assert spec["serviceAccountName"] == "crowdsec-agent"
            assert deployment["spec"]["replicas"] == 1, "Multiple collectors duplicate log events"
        if name == "crowdsec-bouncer":
            assert spec["containers"][0]["securityContext"]["runAsNonRoot"] is True
            key = spec["containers"][0]["env"][0]["valueFrom"]["secretKeyRef"]
            assert key == {"name": "crowdsec-secret", "key": "BOUNCER_KEY_ENVOY"}
        if name == "crowdsec-lapi":
            env = {e["name"]: e for e in spec["containers"][0]["env"]}
            assert env["USE_WAL"]["value"] == "true"
            for key in ["CS_LAPI_SECRET", "REGISTRATION_TOKEN", "BOUNCER_KEY_ENVOY"]:
                assert env[key]["valueFrom"]["secretKeyRef"]["name"] == "crowdsec-secret"
    acquisition = yaml.safe_load(resource(crowdsec, "ConfigMap", "acquis-configmap")["data"]["acquis.yaml"])
    assert acquisition == {"source": "kubernetes", "namespace": "network", "selector": "gateway.envoyproxy.io/owning-gateway-name=envoy-external", "labels": {"type": "envoy"}}
    assert len([d for d in crowdsec + bouncer if d["kind"] == "ServiceMonitor"]) == 3
    assert not any(d["kind"] == "Secret" for d in crowdsec + bouncer), "Credentials must come from ExternalSecret"
    for pvc in [d for d in crowdsec if d["kind"] == "PersistentVolumeClaim"]:
        assert pvc["spec"]["storageClassName"] == "ceph-block"
    role = resource(documents(NETWORK / "crowdsec/app/rbac.yaml"), "Role", "crowdsec-agent")
    assert role["rules"] == [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]}, {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]}]
    policy = documents(NETWORK / "crowdsec-bouncer/app/securitypolicy.yaml")[0]["spec"]
    assert policy["targetRefs"] == [{"group": "gateway.networking.k8s.io", "kind": "Gateway", "name": "envoy-external"}]
    assert policy["extAuth"]["failOpen"] is True
    assert policy["extAuth"]["headersToExtAuth"] == ["X-Envoy-External-Address"], "Do not pass spoofable client IP headers"
    config = yaml.safe_load(resource(bouncer, "ConfigMap", "crowdsec-bouncer-config")["data"]["config.yaml"])
    assert config["trustedIPHeader"] == "X-Envoy-External-Address"
    for app, name, control in [("flux-system/konflate", "konflate-oidc", "oidc"), ("home/mealie", "mealie", "authorization")]:
        route_policy = resource(documents(ROOT / f"kubernetes/apps/{app}/app/securitypolicy.yaml"), "SecurityPolicy", name)["spec"]
        assert route_policy["mergeType"] == "StrategicMerge" and control in route_policy
    policies = documents(NETWORK / "crowdsec/app/networkpolicy.yaml") + documents(NETWORK / "crowdsec-bouncer/app/networkpolicy.yaml")
    envoy = {"gateway.envoyproxy.io/owning-gateway-name": "envoy-external"}
    for source, target in [(envoy, pods["crowdsec-bouncer"]), (pods["crowdsec-bouncer"], pods["crowdsec-lapi"]), (pods["crowdsec-agent"], pods["crowdsec-lapi"])]:
        assert allows(policies, source, target, "egress", 8080)
        assert allows(policies, target, source, "ingress", 8080)
    assert not allows(policies, pods["crowdsec-bouncer"], {"gateway.envoyproxy.io/owning-gateway-name": "envoy-internal"}, "ingress", 8080)
    assert not allows(policies, pods["crowdsec-lapi"], {"app": "unrelated"}, "ingress", 8080)
    prometheus = {"app.kubernetes.io/name": "prometheus"}
    monitoring = documents(ROOT / "kubernetes/apps/observability/kube-prometheus-stack/app/networkpolicy.yaml")
    for name, port in [("crowdsec-agent", 6060), ("crowdsec-lapi", 6060), ("crowdsec-bouncer", 9090)]:
        assert allows(policies, pods[name], prometheus, "ingress", port, peer_namespace="observability")
        assert allows(monitoring, prometheus, pods[name], "egress", port, namespace="observability")
    print("CrowdSec rendered workload, credential, acquisition, policy coexistence and traffic contracts passed")


if __name__ == "__main__":
    main()
