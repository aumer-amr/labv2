#!/usr/bin/env python3
"""Validate DDR isolation with the pinned chart and no cluster writes.

Run: python3 tests/manual/check-unifi-ddr.py /path/to/external-dns-1.23.0.tgz
Requires helm, kustomize, and yq on PATH.
"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
APPS = ROOT / "kubernetes/apps/network"
CHART = Path(sys.argv[1]).resolve()
PROVIDER = "external-dns.home-operations.com/provider"


def run(*args, data=None):
    return subprocess.check_output(args, input=data, text=True, cwd=ROOT)


def documents(data):
    return [json.loads(line) for line in run(
        "yq", "ea", "-o=json", "-I=0", "select(. != null)", "-", data=data).splitlines()]


def resource(items, kind, name):
    return next(item for item in items if item["kind"] == kind and item["metadata"]["name"] == name)


built = {app: documents(run("kustomize", "build", str(APPS / app / "app"),
                           "--load-restrictor=LoadRestrictionsNone"))
         for app in ("unifi-dns", "cloudflare-dns", "cloudflare-tunnel")}
unifi = resource(built["unifi-dns"], "HelmRelease", "unifi-dns")["spec"]["values"]
cloudflare = resource(built["cloudflare-dns"], "HelmRelease", "cloudflare-dns")["spec"]["values"]
tunnel = resource(built["cloudflare-tunnel"], "DNSEndpoint", "cloudflare-tunnel")
ddr = resource(built["unifi-dns"], "DNSEndpoint", "ddr-opt-out")
assert tunnel["metadata"]["labels"][PROVIDER] == "cloudflare"
assert ddr["metadata"].get("labels", {}).get(PROVIDER) != "cloudflare"
assert ddr["spec"]["endpoints"] == [
    {"dnsName": "_dns.resolver.arpa", "recordType": "CNAME", "targets": ["unifi"]}]
assert cloudflare["domainFilters"] == ["aumer.dev"], "DDR must stay out of public DNS"
assert documents(run("helm", "show", "chart", str(CHART)))[0]["version"] == "1.23.0"

with tempfile.TemporaryDirectory(prefix="unifi-ddr-") as directory:
    values = Path(directory) / "values.json"
    values.write_text(json.dumps(unifi))
    rendered = documents(run("helm", "template", "unifi-dns", str(CHART),
                             "--namespace", "network", "--values", str(values)))
deployment = resource(rendered, "Deployment", "unifi-dns")
containers = deployment["spec"]["template"]["spec"]["containers"]
args = next(container for container in containers if container["name"] == "external-dns")["args"]
assert {arg for arg in args if arg.startswith("--source=")} == {
    "--source=crd", "--source=gateway-httproute", "--source=service"}
assert f"--label-filter={PROVIDER} notin (cloudflare)" in args
assert {arg for arg in args if arg.startswith("--domain-filter=")} == {
    "--domain-filter=resolver.arpa", "--domain-filter=aumer.dev"}
assert "--txt-owner-id=main" in args
assert "--txt-prefix=k8s.main.%{record_type}-" in args
rules = resource(rendered, "ClusterRole", "unifi-dns")["rules"]
assert any(rule["apiGroups"] == ["externaldns.k8s.io"]
           and "dnsendpoints" in rule["resources"]
           and {"get", "list", "watch"} <= set(rule["verbs"]) for rule in rules)
assert any(rule["apiGroups"] == ["externaldns.k8s.io"]
           and "dnsendpoints/status" in rule["resources"]
           and "update" in rule["verbs"] for rule in rules)

network = documents(run("kustomize", "build", str(APPS), "--load-restrictor=LoadRestrictionsNone"))
unifi_ks = resource(network, "Kustomization", "unifi-dns")["spec"]
assert {dependency["name"] for dependency in unifi_ks["dependsOn"]} >= {
    "cloudflare-dns", "cloudflare-tunnel", "onepassword-connect"}
assert {"name": "onepassword-connect", "namespace": "external-secrets"} in unifi_ks["dependsOn"]
cloudflare_ks = resource(network, "Kustomization", "cloudflare-dns")["spec"]
assert cloudflare_ks["wait"] is False, "wait=true ignores explicit health checks"
assert {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
        "name": "dnsendpoints.externaldns.k8s.io"} in cloudflare_ks["healthChecks"]
assert {"apiVersion": "helm.toolkit.fluxcd.io/v2", "kind": "HelmRelease",
        "name": "cloudflare-dns", "namespace": "network"} in cloudflare_ks["healthChecks"]
print("PASS: DDR record, provider isolation, chart arguments, CRD permissions, and Flux dependencies")
