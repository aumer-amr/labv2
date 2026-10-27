#!/usr/bin/env python3
"""Check etcd fragmentation alert boundaries, persistence, and recovery with promtool."""

import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "kubernetes/apps/observability/kube-prometheus-stack/app"


def read_yaml(path):
    return json.loads(subprocess.check_output(["yq", "-o=json", ".", str(path)], text=True))


spec = read_yaml(APP / "alerts/etcd-fragmentation.yaml")["spec"]
rule = spec["groups"][0]["rules"][0]
assert read_yaml(APP / "helmrelease.yaml")["spec"]["values"]["defaultRules"]["disabled"][rule["alert"]]
assert "./etcd-fragmentation.yaml" in read_yaml(APP / "alerts/kustomization.yaml")["resources"]

cases = [
    ("small fragmented database", "240x150", "100x150", "kube-etcd", [(90, False)]),
    ("small database oscillation", "240x150", "99 101 " * 80, "kube-etcd", [(65, False)]),
    ("live data oscillation with reclaimable space", "600x160", "99 101 " * 80, "kube-etcd", [(65, True)]),
    ("exact reclaim boundary", "384x150", "128x150", "kube-etcd", [(90, False)]),
    ("exact utilization boundary", "600x150", "300x150", "kube-etcd", [(90, False)]),
    ("other job", "600x150", "100x150", "other", [(90, False)]),
    ("sustained fragmentation", "600x150", "100x150", "kube-etcd", [(59, False), (65, True)]),
    ("short spike", "600x30 110x120", "100x150", "kube-etcd", [(90, False)]),
    ("recovery", "600x70 110x80", "100x150", "kube-etcd", [(90, True), (140, False)]),
    ("brief utilization dip", "600x70 190x20 600x60", "100x150", "kube-etcd", [(90, True), (110, True)]),
    ("missing metrics", "600x70 stale _x80", "100x70 stale _x80", "kube-etcd", [(90, True), (140, False)]),
]


def byte_values(values):
    return " ".join(
        token if token in ("stale", "_") or token.startswith("_x")
        else str(int(token.split("x")[0]) * 1048576) + ("x" + token.split("x")[1] if "x" in token else "")
        for token in values.split()
    )


with tempfile.TemporaryDirectory(prefix="etcd-alert-tests-") as directory:
    directory = Path(directory)
    rules_path = directory / "rules.json"
    rules_path.write_text(json.dumps(spec))
    tests = []
    for name, allocated, used, job, checks in cases:
        labels = {"instance": "node:2381", "job": job, "severity": "warning"}
        annotations = {key: value.replace("{{ $labels.instance }}", labels["instance"])
                       for key, value in rule["annotations"].items()}
        expected = {"exp_labels": labels, "exp_annotations": annotations}
        tests.append({
            "name": name,
            "interval": "1m",
            "input_series": [
                {"series": f'etcd_mvcc_db_total_size_in_bytes{{instance="node:2381",job="{job}"}}', "values": byte_values(allocated)},
                {"series": f'etcd_mvcc_db_total_size_in_use_in_bytes{{instance="node:2381",job="{job}"}}', "values": byte_values(used)},
            ],
            "alert_rule_test": [{"eval_time": f"{minute}m", "alertname": rule["alert"],
                                 "exp_alerts": [expected] if firing else []}
                                for minute, firing in checks],
        })
    test_path = directory / "tests.json"
    test_path.write_text(json.dumps({"rule_files": [str(rules_path)], "evaluation_interval": "1m", "tests": tests}))
    subprocess.run(["promtool", "check", "rules", str(rules_path)], check=True)
    subprocess.run(["promtool", "test", "rules", str(test_path)], check=True)

print(f"Passed {len(cases)} etcd fragmentation alert scenarios")
