#!/usr/bin/env python3
"""Test local Spegel migration renders and dashboard queries without cluster writes.

Run with python3 tests/manual/check-spegel-migration.py CHART_DIRECTORY.
Supply spegel-0.7.4.tgz, spegel-0.8.0.tgz, and cilium-1.20.4.tgz.
Requires helm, kustomize, yq, and promtool on PATH.
"""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
APPS = ROOT / "kubernetes/apps/kube-system"
CHARTS = Path(sys.argv[1]).resolve()


def run(*args, data=None):
    return subprocess.check_output(args, input=data, text=True, cwd=ROOT)


def documents(data):
    return [json.loads(line) for line in run(
        "yq", "ea", "-o=json", "-I=0", "select(. != null)", "-", data=data).splitlines()]


def resource(items, kind, name):
    return next(item for item in items if item["kind"] == kind and item["metadata"]["name"] == name)


def render(name, version, values, directory):
    path = directory / f"{name}-{version}-values.json"
    path.write_text(json.dumps(values))
    chart = CHARTS / f"{name}-{version}.tgz"
    assert documents(run("helm", "show", "chart", str(chart)))[0]["version"] == version
    return documents(run("helm", "template", name, str(chart), "--namespace", "kube-system", "--values", str(path)))


def labels(component):
    return {"app.kubernetes.io/component": component, "app.kubernetes.io/instance": "spegel",
            "app.kubernetes.io/name": "spegel"}


with tempfile.TemporaryDirectory(prefix="spegel-migration-") as directory:
    directory = Path(directory)
    built = {app: documents(run("kustomize", "build", str(APPS / app / "app"),
                               "--load-restrictor=LoadRestrictionsNone"))
             for app in ("spegel", "cilium", "coredns")}
    release = resource(built["spegel"], "HelmRelease", "spegel")["spec"]
    values = release["values"]
    assert "grafanaDashboard" not in values
    assert values["networkPolicy"]["enabled"] is False
    assert values["service"]["registry"] == {"nodePort": 30021}
    assert resource(built["spegel"], "OCIRepository", "spegel")["spec"]["ref"]["tag"] == "0.8.0"
    previous = copy.deepcopy(values)
    previous["service"]["registry"] = {"hostPort": 29999}
    previous["grafanaDashboard"] = {"enabled": True, "mode": "GrafanaOperator",
                                    "grafanaOperator": {"matchLabels": {"dashboards": "grafana"}}}
    old = render("spegel", "0.7.4", previous, directory)
    new = render("spegel", "0.8.0", values, directory)
    native = resource(new, "DaemonSet", "spegel")
    assert not any("hostPort" in port for c in native["spec"]["template"]["spec"]["containers"] for port in c["ports"])
    # Reproduce the retained live field, then require the post-renderer to clear it.
    native["spec"]["template"]["spec"]["containers"][0]["ports"][0]["hostPort"] = 29999
    (directory / "rendered.json").write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": new}))
    (directory / "kustomization.yaml").write_text(json.dumps({
        "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
        "resources": ["rendered.json"], "patches": release["postRenderers"][0]["kustomize"]["patches"]}))
    new = documents(run("kustomize", "build", str(directory), "--load-restrictor=LoadRestrictionsNone"))
    old_ds = resource(old, "DaemonSet", "spegel")["spec"]
    new_ds = resource(new, "DaemonSet", "spegel")["spec"]
    assert old_ds["selector"] == new_ds["selector"] == {"matchLabels": labels("spegel")}
    old_pod, new_pod = old_ds["template"]["spec"], new_ds["template"]["spec"]
    for key in ("volumes", "securityContext", "nodeSelector", "tolerations", "priorityClassName"):
        assert old_pod.get(key) == new_pod.get(key), key
    assert old_ds.get("updateStrategy") == new_ds.get("updateStrategy")
    for key in ("containers", "initContainers"):
        for before, after in zip(old_pod[key], new_pod[key], strict=True):
            for field in ("name", "volumeMounts", "securityContext", "resources"):
                assert before.get(field) == after.get(field), field
    assert not any(port.get("hostPort", 0) for container in new_pod["containers"] for port in container["ports"])
    assert new_pod["containers"][0]["ports"][0]["hostPort"] == 0
    assert "http://$(NODE_IP):30021" in new_pod["initContainers"][0]["args"]
    assert "http://$(NODE_IP):29999" in old_pod["initContainers"][0]["args"]
    for items in (old, new):
        assert all(item["metadata"].get("namespace") == "kube-system" for item in items)
        assert resource(items, "Service", "spegel-registry")["spec"]["ports"][0]["nodePort"] == 30021
        assert resource(items, "ServiceMonitor", "spegel")["spec"]["endpoints"][0]["port"] == "metrics"
        assert not any(item["kind"] == "NetworkPolicy" for item in items)
        cleanup = resource(items, "DaemonSet", "spegel-cleanup")
        waiter = resource(items, "Pod", "spegel-cleanup-wait")
        assert cleanup["spec"]["template"]["metadata"]["labels"] == labels("cleanup")
        assert all(waiter["metadata"]["labels"][key] == value for key, value in labels("cleanup-wait").items())
        assert cleanup["metadata"]["annotations"]["helm.sh/hook"] == waiter["metadata"]["annotations"]["helm.sh/hook"] == "post-delete"
        assert "--probe-endpoint=spegel-cleanup.kube-system.svc.cluster.local.:8080" in waiter["spec"]["containers"][0]["args"]
    assert resource(new, "Service", "spegel-registry")["spec"]["trafficDistribution"] == "PreferSameNode"
    assert not any(item["kind"] == "GrafanaDashboard" for item in new)

    ingress = resource(built["spegel"], "NetworkPolicy", "spegel-cleanup")["spec"]
    egress = resource(built["spegel"], "NetworkPolicy", "spegel-cleanup-wait")["spec"]
    assert ingress["podSelector"] == {"matchLabels": labels("cleanup")}
    assert egress["podSelector"] == {"matchLabels": labels("cleanup-wait")}
    assert ingress["ingress"] == [{"from": [{"podSelector": {"matchLabels": labels("cleanup-wait")}}],
                                   "ports": [{"port": 8080, "protocol": "TCP"}]}]
    assert egress["egress"] == [{"to": [{"podSelector": {"matchLabels": labels("cleanup")}}],
                                 "ports": [{"port": 8080, "protocol": "TCP"}]}]
    dns = resource(built["spegel"], "CiliumNetworkPolicy", "spegel-cleanup-wait-dns")["spec"]
    assert dns["endpointSelector"] == {"matchLabels": labels("cleanup-wait")}
    for rule, protocol in zip(dns["egress"], ("UDP", "TCP"), strict=True):
        assert rule["toEndpoints"] == [{"matchLabels": {
            "k8s:io.kubernetes.pod.namespace": "kube-system", "k8s:k8s-app": "kube-dns"}}]
        assert rule["toPorts"] == [{"ports": [{"port": "53", "protocol": protocol}], "rules": {
            "dns": [{"matchName": "spegel-cleanup.kube-system.svc.cluster.local"}]}}]
    coredns = resource(built["coredns"], "NetworkPolicy", "coredns")["spec"]
    for protocol in ("UDP", "TCP"):
        assert any({"podSelector": {"matchLabels": labels("cleanup-wait")}} in rule.get("from", [])
                   and {"port": 53, "protocol": protocol} in rule["ports"] for rule in coredns["ingress"])

    cilium_values = resource(built["cilium"], "HelmRelease", "cilium")["spec"]["values"]
    cilium = render("cilium", "1.20.4", cilium_values, directory)
    assert resource(cilium, "ConfigMap", "cilium-config")["data"]["enable-service-topology"] == "true"
    prior_values = copy.deepcopy(cilium_values)
    del prior_values["loadBalancer"]["serviceTopology"]
    prior_cilium = render("cilium", "1.20.4", prior_values, directory)
    config = resource(cilium, "ConfigMap", "cilium-config")["data"].copy()
    config["enable-service-topology"] = "false"
    assert config == resource(prior_cilium, "ConfigMap", "cilium-config")["data"]
    assert resource(cilium, "DaemonSet", "cilium")["spec"]["template"]["spec"] == resource(prior_cilium, "DaemonSet", "cilium")["spec"]["template"]["spec"]
    annotations = resource(cilium, "DaemonSet", "cilium")["spec"]["template"]["metadata"]["annotations"].copy()
    prior_annotations = resource(prior_cilium, "DaemonSet", "cilium")["spec"]["template"]["metadata"]["annotations"]
    checksum = "cilium.io/cilium-configmap-checksum"
    assert annotations[checksum] != prior_annotations[checksum]
    annotations[checksum] = prior_annotations[checksum]
    assert annotations == prior_annotations

    dashboard = json.loads(resource(built["spegel"], "ConfigMap", "spegel-overview-dashboard")["data"]["dashboard.json"])
    cr = resource(built["spegel"], "GrafanaDashboard", "spegel-overview")
    assert cr["spec"]["allowCrossNamespaceImport"] is True
    assert cr["spec"]["instanceSelector"]["matchLabels"] == {"dashboards": "grafana"}
    assert cr["spec"]["datasources"] == [{"datasourceName": "prometheus", "inputName": "DS_PROMETHEUS"}]
    assert dashboard["uid"] != json.loads(resource(old, "GrafanaDashboard", "spegel")["spec"]["json"])["uid"]
    assert cr["spec"]["configMapRef"] == {"name": "spegel-overview-dashboard", "key": "dashboard.json"}
    cm = resource(built["spegel"], "ConfigMap", "spegel-overview-dashboard")
    assert cm["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/substitute"] == "disabled"
    assert "spegel_advertised_content_digests" not in json.dumps(dashboard)
    assert "spegel_advertised_image_digests" not in json.dumps(dashboard)
    for variable in dashboard["templating"]["list"]:
        assert 'service="spegel"' in variable["definition"]
    queries = {panel["id"]: panel["targets"][0]["expr"].replace("$pod", "spegel-.*").replace("$registry", ".*")
               .replace("$__rate_interval", "5m") for panel in dashboard["panels"]}
    assert all('namespace="kube-system"' in expr for expr in queries.values())
    assert all('service="spegel"' in expr for expr in queries.values() if "container_" not in expr)
    assert "registry=" not in queries[2]
    tests = [{"name": f"query {key} without metrics", "interval": "1m", "input_series": [],
              "promql_expr_test": [{"expr": expr, "eval_time": "10m", "exp_samples": []}]}
             for key, expr in queries.items()]
    common = 'namespace="kube-system",service="spegel",pod="spegel-new"'
    tests.append({"name": "mixed-version keys and unrelated service exclusion", "interval": "1m",
                  "input_series": [
                      {"series": "spegel_advertised_keys{" + common + "}", "values": "42x10"},
                      {"series": 'spegel_advertised_keys{namespace="other",service="spegel",pod="spegel-other"}', "values": "999x10"},
                      {"series": 'spegel_advertised_content_digests{namespace="kube-system",service="spegel",pod="spegel-old",registry="ghcr.io"}', "values": "99x10"}],
                  "promql_expr_test": [{"expr": queries[2], "eval_time": "10m", "exp_samples": [
                      {"labels": "spegel_advertised_keys{" + common + "}", "value": 42}]}]})
    for panel_id, cache in ((3, "hit"), (4, "miss")):
        tests.append({"name": f"cache {cache} excludes unrelated services", "interval": "1m",
                      "input_series": [
                          {"series": "spegel_mirror_requests_total{" + common + f',registry="ghcr.io",cache="{cache}"' + "}", "values": "0+60x10"},
                          {"series": 'spegel_mirror_requests_total{namespace="kube-system",service="other",pod="spegel-other",registry="ghcr.io",cache="' + cache + '"}', "values": "0+6000x10"}],
                      "promql_expr_test": [{"expr": queries[panel_id], "eval_time": "10m", "exp_samples": [
                          {"labels": '{pod="spegel-new",registry="ghcr.io"}', "value": 1}]}]})
    for panel_id, metric in ((6, "http_request_duration_seconds_bucket"), (7, "spegel_resolve_duration_seconds_bucket")):
        tests.append({"name": f"histogram quantile {panel_id}", "interval": "1m",
                      "input_series": [{"series": metric + "{" + common + f',le="{bound}"' + "}", "values": values}
                                       for bound, values in (("1", "0+600x10"), ("2", "0+1200x10"), ("+Inf", "0+1200x10"))],
                      "promql_expr_test": [{"expr": queries[panel_id], "eval_time": "10m", "exp_samples": [
                          {"labels": '{pod="spegel-new"}', "value": 1.9}]}]})
    tests.append({"name": "last success excludes uninitialized timestamps", "interval": "1m",
                  "input_series": [
                      {"series": "spegel_mirror_last_success_timestamp_seconds{" + common + "}", "values": "540x10"},
                      {"series": 'spegel_mirror_last_success_timestamp_seconds{namespace="kube-system",service="spegel",pod="spegel-idle"}', "values": "0x10"}],
                  "promql_expr_test": [{"expr": queries[8], "eval_time": "10m", "exp_samples": [
                      {"labels": "{" + common + "}", "value": 60}]}]})
    fixtures = directory / "dashboard-tests.json"
    fixtures.write_text(json.dumps({"rule_files": [], "evaluation_interval": "1m", "tests": tests}))
    subprocess.run(["promtool", "test", "rules", str(fixtures)], check=True)

print("Spegel migration renders, cleanup policy boundaries, topology, and dashboard queries passed.")
