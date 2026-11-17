#!/usr/bin/env python3
"""Test shared CNPG alerts. Requires flux, yq, and promtool on PATH."""

import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "kubernetes/components/cnpg/prometheusrule.yaml"
METRICS = {
    "collector": "cnpg_collector_up",
    "success": "barman_cloud_cloudnative_pg_io_last_available_backup_timestamp",
    "failure": "barman_cloud_cloudnative_pg_io_last_failed_backup_timestamp",
}
HEALTHY = {"collector": "1x1600", "success": "60+60x1600", "failure": "0x1600"}
CLUSTERS = [("ai", "langfuse"), ("ai", "litellm"), ("home", "langfuse")]


def series(values, namespace="ai", app="langfuse", pod="1"):
    labels = f'namespace="{namespace}",job="{namespace}/{app}-postgres",pod="{app}-{pod}"'
    return [
        {"series": f'{METRICS[key]}{{{labels}'
                   + (f',cluster="{app}"' if key == "collector" else "") + "}",
         "values": value}
        for key, value in values.items() if value is not None
    ]


# Each case keeps two other databases healthy to prove namespace and job isolation.
cases = [
    ("healthy", {}, [], [(30, [])]),
    ("offline", {"collector": "0x30"}, [], [(4, []), (5, ["ClusterOffline"])]),
    ("collector absent", {"collector": None}, [], [(4, []), (5, ["ClusterOffline"])]),
    ("collector disappears", {"collector": "1x4 stale"}, [], [(9, []), (10, ["ClusterOffline"])]),
    ("offline recovery", {"collector": "0x5 1x25"}, [], [(5, ["ClusterOffline"]), (6, [])]),
    ("brief outage", {"collector": "0x3 1x26"}, [], [(5, []), (30, [])]),
    ("one healthy replica", {"collector": "0x30"}, series(HEALTHY, pod="2"), [(30, [])]),
    ("all replicas offline", {"collector": "0x30"}, series({"collector": "0x30"}, pod="2"),
     [(5, ["ClusterOffline"])]),
    ("failed backup", {"success": "60x30", "failure": "120x30"}, [],
     [(4, []), (5, ["BackupFailed"])]),
    ("backup failure recovery", {"success": "60x5 180x25", "failure": "120x30"}, [],
     [(5, ["BackupFailed"]), (6, [])]),
    ("equal backup timestamps", {"success": "60x30", "failure": "60x30"}, [], [(30, [])]),
    ("failure on another pod", {}, series({"failure": "99999x30"}, pod="2"),
     [(5, ["BackupFailed"])]),
    ("newer successful replica", {"success": "60x30", "failure": "120x30"},
     series({"success": "180x30"}, pod="2"), [(30, [])]),
    ("never backed up", {"success": "0x30"}, [], [(14, []), (15, ["BackupStale"])]),
    ("stale backup boundary", {"success": "60x1600"}, [],
     [(1561, []), (1576, []), (1577, ["BackupStale"])]),
    ("missing successful timestamp", {"success": None}, [],
     [(14, []), (15, ["BackupMetricsMissing"])]),
    ("missing failed timestamp", {"failure": None}, [], [(15, ["BackupMetricsMissing"])]),
    ("both timestamps missing", {"success": None, "failure": None}, [],
     [(15, ["BackupMetricsMissing"])]),
    ("all metrics absent", dict.fromkeys(METRICS), [],
     [(5, ["ClusterOffline"]), (15, ["ClusterOffline", "BackupMetricsMissing"])]),
    ("timestamps disappear", {"success": "60x4 stale", "failure": "0x4 stale"}, [],
     [(19, []), (20, ["BackupMetricsMissing"])]),
    ("missing timestamps recover", {"success": "_x16 60x14"}, [],
     [(15, ["BackupMetricsMissing"]), (16, [])]),
    ("missing collector becomes offline", {"collector": "_x3 0x27"}, [],
     [(4, []), (5, ["ClusterOffline"])]),
    ("offline collector disappears", {"collector": "0x2 stale"}, [],
     [(4, []), (5, ["ClusterOffline"])]),
    ("failed backup pod replacement", {"success": "60x2 stale", "failure": "120x2 stale"},
     series({"success": "_x3 60x27", "failure": "_x3 120x27"}, pod="2"),
     [(4, []), (5, ["BackupFailed"])]),
    ("healthy backup pod replacement", {"success": "60x2 stale", "failure": "0x2 stale"},
     series({"success": "_x3 180x27", "failure": "_x3 0x27"}, pod="2"), [(30, [])]),
]

with tempfile.TemporaryDirectory(prefix="cnpg-alert-tests-") as directory:
    directory = Path(directory)
    groups = []
    for namespace, app in CLUSTERS:
        rendered = subprocess.check_output(
            ["flux", "envsubst", "--strict"], input=SOURCE.read_text(), text=True,
            env={**os.environ, "NAMESPACE": namespace, "APP": app},
        )
        resource = json.loads(subprocess.check_output(
            ["yq", "-o=json", "."], input=rendered, text=True,
        ))
        group = resource["spec"]["groups"][0]
        group["name"] = f"{namespace}/{app}"
        groups.append(group)
    rules = {rule["alert"]: rule for rule in groups[0]["rules"]}
    assert set(rules) == {"CNPGClusterOffline", "CNPGBackupFailed", "CNPGBackupStale",
                          "CNPGBackupMetricsMissing"}
    rules_path = directory / "rules.json"
    rules_path.write_text(json.dumps({"groups": groups}))
    tests = []
    for name, overrides, extra, checks in cases:
        inputs = series({**HEALTHY, **overrides}) + extra
        for namespace, app in CLUSTERS[1:]:
            inputs.extend(series(HEALTHY, namespace, app))
        tests.append({
            "name": name,
            "interval": "1m",
            "input_series": inputs,
            "alert_rule_test": [
                {"eval_time": f"{minute}m", "alertname": alert,
                 "exp_alerts": [{
                     "exp_labels": {"namespace": "ai", "cluster": "langfuse",
                                    "severity": "critical" if alert in
                                    ("CNPGClusterOffline", "CNPGBackupFailed") else "warning"},
                     "exp_annotations": rule["annotations"],
                 }] if alert.removeprefix("CNPG") in firing else []}
                for minute, firing in checks for alert, rule in rules.items()
            ],
        })
    tests_path = directory / "tests.json"
    tests_path.write_text(json.dumps({"rule_files": [str(rules_path)],
                                     "evaluation_interval": "1m", "tests": tests}))
    subprocess.run(["promtool", "check", "rules", str(rules_path)], check=True)
    subprocess.run(["promtool", "test", "rules", str(tests_path)], check=True)

print(f"Passed {len(cases)} CNPG alert scenarios across {len(CLUSTERS)} isolated databases")
