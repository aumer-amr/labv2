#!/usr/bin/env python3
"""Render a supplied Tetragon 1.8.0 chart and assert the pilot trust boundaries."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "kubernetes/apps/security/tetragon/app"


def yaml_docs(path):
    return json.loads(subprocess.check_output(
        ["yq", "eval-all", "-o=json", "-I=0", "[.]", str(path)], text=True))


def run(*args):
    return subprocess.check_output(args, text=True)



def test_projected_policies(agent):
    mount = next(m for m in agent["volumeMounts"] if m["name"] == "tetragon-policies")
    argument = next(a for a in agent["args"] if a.startswith("--tracing-policy-dir="))
    relative = Path(argument.split("=", 1)[1]).relative_to(mount["mountPath"])
    expected = {p.name for p in (APP / "policies").glob("*.yaml")}
    with tempfile.TemporaryDirectory() as directory:
        volume = Path(directory)
        snapshot = volume / "..2026_01"
        snapshot.mkdir()
        for name in expected:
            (snapshot / name).write_text((APP / "policies" / name).read_text())
            (volume / name).symlink_to("..data/" + name)
        (volume / "..data").symlink_to(snapshot.name, target_is_directory=True)
        # Tetragon 1.8 walks one subdirectory and follows file symlinks with os.Stat.
        candidates = []
        for entry in (volume / relative).iterdir():
            if entry.is_dir() and not entry.is_symlink():
                candidates.extend(p for p in entry.iterdir() if p.is_file())
            elif entry.is_file():
                candidates.append(entry)
        assert len(candidates) == len(expected), "ConfigMap projection loads static policies more than once"
        assert {p.name for p in candidates} == expected


def test_alerts(work):
    rules = yaml_docs(APP / "prometheusrule.yaml")[0]["spec"]
    (work / "tetragon-rules.yaml").write_text(json.dumps(rules))
    alerts = {r["alert"]: r for g in rules["groups"] for r in g["rules"]}
    def expected(name, labels=None):
        return {"exp_labels": {**alerts[name]["labels"], **(labels or {})},
                "exp_annotations": alerts[name]["annotations"]}
    policy = 'tetragon_tracingpolicy_selector_actions_total{job="tetragon",policy_namespace="network",policy="echo-unexpected-exec",action="post"}'
    tests = [
        {"name": "first sparse event alerts and then clears", "interval": "1m",
         "input_series": [{"series": policy, "values": "_ 1+0x9"}],
         "alert_rule_test": [
             {"eval_time": "1m", "alertname": "TetragonEchoUnexpectedExec", "exp_alerts": [expected("TetragonEchoUnexpectedExec")]},
             {"eval_time": "7m", "alertname": "TetragonEchoUnexpectedExec", "exp_alerts": []}]},
        {"name": "no matching executions", "interval": "1m",
         "input_series": [{"series": policy, "values": "0+0x10"}],
         "alert_rule_test": [{"eval_time": "7m", "alertname": "TetragonEchoUnexpectedExec", "exp_alerts": []}]},
        {"name": "event loss alerts", "interval": "1m",
         "input_series": [{"series": 'tetragon_observer_ringbuf_events_lost_total{job="tetragon"}', "values": "0 0 1+0x5"}],
         "alert_rule_test": [{"eval_time": "2m", "alertname": "TetragonPilotEventLoss", "exp_alerts": [expected("TetragonPilotEventLoss")]}]},
        {"name": "missing scrape target", "interval": "1m", "input_series": [],
         "alert_rule_test": [{"eval_time": "6m", "alertname": "TetragonPilotUnavailable", "exp_alerts": [expected("TetragonPilotUnavailable", {"job": "tetragon"})]}]},
    ]
    (work / "test.yaml").write_text(json.dumps({"rule_files": [str(work / "tetragon-rules.yaml")], "evaluation_interval": "1m", "tests": tests}))
    subprocess.run([sys.argv[2], "check", "rules", str(work / "tetragon-rules.yaml")], check=True)
    subprocess.run([sys.argv[2], "test", "rules", str(work / "test.yaml")], check=True)


def main(chart):
    release = yaml_docs(APP / "helmrelease.yaml")[0]["spec"]
    assert release["releaseName"] == "tetragon"
    assert release["chart"]["spec"]["version"] == "1.8.0"
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        (work / "values.json").write_text(json.dumps(release["values"]))
        (work / "helm.yaml").write_text(run(
            "helm", "template", "tetragon", chart, "--namespace", "security",
            "--kube-version", "1.37.2", "--values", str(work / "values.json")))
        (work / "kustomization.yaml").write_text(json.dumps({
            "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
            "resources": ["helm.yaml"],
            "patches": release["postRenderers"][0]["kustomize"]["patches"],
        }))
        (work / "final.yaml").write_text(run(
            "kustomize", "build", str(work), "--load-restrictor=LoadRestrictionsNone"))
        docs = yaml_docs(work / "final.yaml")
        test_alerts(work)
    assert {d["kind"] for d in docs} == {"ConfigMap", "DaemonSet", "Service", "ServiceMonitor"}
    daemon = next(d for d in docs if d["kind"] == "DaemonSet")
    pod = daemon["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "deathscythe"}
    assert pod["hostNetwork"] is True
    assert not pod.get("initContainers")
    assert pod["terminationGracePeriodSeconds"] == 30
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert "cilium-run" not in volumes
    assert volumes["tetragon-run"].get("emptyDir") == {}
    assert volumes["export-logs"]["emptyDir"]["sizeLimit"] == "16Mi"
    assert volumes["tetragon-policies"]["configMap"]["name"] == "tetragon-policies"
    for container in pod["containers"]:
        for mount in container.get("volumeMounts", []):
            assert mount["name"] in volumes, "mount references missing volume"
        assert container["resources"]["requests"] and container["resources"]["limits"]
        assert not container["securityContext"].get("privileged", False)
    agent = next(c for c in pod["containers"] if c["name"] == "tetragon")
    test_projected_policies(agent)
    assert next(m for m in agent["volumeMounts"] if m["name"] == "host-proc")["readOnly"]
    assert next(m for m in agent["volumeMounts"] if m["name"] == "sys-kernel-tracing")["readOnly"]
    sidecar = next(c for c in pod["containers"] if c["name"] == "export-stdout")
    assert sidecar["securityContext"]["runAsNonRoot"]
    assert set(m["name"] for m in sidecar["volumeMounts"]) == {"export-logs"}
    cfg = next(d for d in docs if d["kind"] == "ConfigMap")["data"]
    assert cfg["server-address"] == "unix:///var/run/tetragon/tetragon.sock"
    assert cfg["enable-tracing-policy-crd"] == "false"
    assert cfg["enable-pod-info"] == "false"
    assert cfg["export-rate-limit"] == "60"
    assert cfg["export-file-perm"] == "640"
    allow = json.loads(cfg["export-allowlist"])
    assert allow == {"namespace": ["network"], "labels": ["app.kubernetes.io/name=echo,app.kubernetes.io/instance=echo"], "event_set": ["PROCESS_EXEC", "PROCESS_KPROBE"]}
    rbac = yaml_docs(APP / "rbac.yaml")
    role = next(d for d in rbac if d["kind"] == "ClusterRole")
    assert role["rules"] == [{"apiGroups": [""], "resources": ["namespaces", "nodes", "pods", "services"], "verbs": ["get", "list", "watch"]}]
    assert all(d.get("metadata", {}).get("namespace", "security") == "security" for d in docs)
    print("PASS: rendered pilot placement, access, API, resources, export scope, and RBAC")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: check-tetragon-pilot.py /path/to/tetragon-1.8.0.tgz /path/to/promtool")
    main(sys.argv[1])
