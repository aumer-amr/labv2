#!/usr/bin/env python3
"""Test the CNPG dashboard transform and its PromQL with Jsonnet and promtool."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "kubernetes/apps/database/cloudnative-pg/app/grafanadashboard.yaml"
spec = json.loads(subprocess.check_output(["yq", "-o=json", ".spec", str(MANIFEST)], text=True))
assert "configMapRef" not in spec
assert spec["envs"] == [{"name": "dashboard", "valueFrom": {"configMapKeyRef": {
    "name": "cnpg-grafana-dashboard", "key": "cnp.json"}}}]


def panel(panel_id, expr, kind="stat"):
    return {"id": panel_id, "type": kind, "targets": [{"expr": expr, "refId": "A"}],
            "fieldConfig": {"defaults": {}, "overrides": []}}


backup = 'cnpg_collector_last_available_backup_timestamp{namespace="$namespace",pod=~"$instances"}'
recovery = 'cnpg_collector_first_recoverability_point{namespace=~"$namespace",pod=~"$instances"}'
controllers = ["cluster", "backup", "pooler", "scheduledbackup|scheduled-backup"]
source = {"uid": "cloudnative-pg", "panels": [
    panel(612, "old WAL query"), panel(360, '-(time() - max(' + backup + '))'),
    panel(364, 'max(' + recovery + ')*1000'),
    {"id": 9000, "panels": [panel(237, recovery + '*1000 > 0', "timeseries"),
                             panel(588, 'time() - max(' + backup + ')')]},
    panel(9001, "unrelated_query"),
] + [panel(700 + i, 'max(controller_runtime_reconcile_total{namespace=~"$operatorNamespace",result="error",controller=~"' + c + '"})', "timeseries")
     for i, c in enumerate(controllers)] + [
         panel(655, 'clamp_max(max(controller_runtime_reconcile_total{namespace=~"$operatorNamespace",result="error",controller="backup"}), 1)')
     ]}
if len(sys.argv) > 1:
    source = json.loads(Path(sys.argv[1]).read_text())


def flatten(panels):
    for p in panels:
        yield p
        yield from flatten(p.get("panels", []))


def concrete(expr):
    return expr.replace("$operatorNamespace", "database").replace("$namespace", "ai").replace("$instances", "demo-[0-9]+")


with tempfile.TemporaryDirectory(prefix="cnpg-dashboard-") as tmp:
    tmp = Path(tmp)
    (tmp / "source.json").write_text(json.dumps(source))
    (tmp / "patch.jsonnet").write_text(spec["jsonnet"])
    rendered = json.loads(subprocess.check_output([
        "jsonnet", "--ext-str-file", f"dashboard={tmp / 'source.json'}", str(tmp / "patch.jsonnet")], text=True))
    original = {p["id"]: p for p in flatten(source["panels"])}
    panels = {p["id"]: p for p in flatten(rendered["panels"])}
    assert rendered["uid"] == source["uid"] == "cloudnative-pg"
    assert panels.keys() == original.keys()
    backup_queries = reconciliation_queries = 0
    for panel_id, old in original.items():
        new = panels[panel_id]
        for before, after in zip(old.get("targets", []), new.get("targets", []), strict=True):
            expr = before.get("expr", "")
            if "cnpg_collector_first_recoverability_point" in expr or "cnpg_collector_last_available_backup_timestamp" in expr:
                expected = expr.replace("cnpg_collector_first_recoverability_point", "barman_cloud_cloudnative_pg_io_first_recoverability_point").replace("cnpg_collector_last_available_backup_timestamp", "barman_cloud_cloudnative_pg_io_last_available_backup_timestamp")
                assert after["expr"] == expected
                backup_queries += 1
            elif "controller_runtime_reconcile_total" in expr:
                assert '[1h]' in after["expr"] and 'job="database/cloudnative-pg"' in after["expr"]
                reconciliation_queries += 1
            elif panel_id != 612:
                assert after == before
        if old.get("targets") and not any("controller_runtime_reconcile_total" in t.get("expr", "") for t in old["targets"]) and panel_id != 612:
            assert new.get("fieldConfig") == old.get("fieldConfig")
    assert backup_queries == 4
    assert reconciliation_queries >= 5
    assert panels[612]["targets"][0]["instant"] and not panels[612]["targets"][0]["range"]
    assert {v["text"] for v in panels[612]["fieldConfig"]["defaults"]["mappings"][0]["options"].values()} == {
        "Idle", "Healthy", "Delayed", "Unsynced", "Unknown", "Failed"}

    wal = concrete(panels[612]["targets"][0]["expr"])
    labels = 'namespace="ai",pod="demo-1"'

    def series(name, values, extra="", series_labels=labels):
        return {"series": name + "{" + series_labels + extra + "}", "values": values}

    def inputs(age=100, activity="0+100x20", ready=0, failures="0x20", missing=None):
        data = {
            "primary": series("cnpg_pg_replication_in_recovery", "0x20"),
            "age": series("cnpg_pg_stat_archiver_seconds_since_last_archival", f"{age}x20"),
            "activity": series("cnpg_collector_wal_bytes", activity),
            "ready": series("cnpg_collector_pg_wal_archive_status", f"{ready}x20", ',value="ready"'),
            "failures": series("cnpg_pg_stat_archiver_failed_count", failures),
        }
        return [v for k, v in data.items() if k != missing]

    tests = []

    def case(name, expr, data, value):
        tests.append({"name": name, "interval": "1m", "input_series": data,
                      "promql_expr_test": [{"expr": expr, "eval_time": "20m", "exp_samples": [
                          {"labels": "{}", "value": value}]}]})

    for name, kwargs, expected in [
        ("fresh archive", {}, 1), ("low WAL activity", {"age": 901, "activity": "0+0.01x20"}, 3), ("six minute boundary", {"age": 360}, 1),
        ("delayed active", {"age": 361}, 2), ("15 minute boundary", {"age": 900}, 2),
        ("unsynced active", {"age": 901}, 3),
        ("old idle", {"age": 80000, "activity": "0x20"}, 0),
        ("idle WAL with queued segment", {"age": 901, "activity": "0x20", "ready": 1}, 3),
        ("recent failure on fresh archive", {"failures": "0x15 1x5"}, 5),
        ("recent failure while idle", {"age": 80000, "activity": "0x20", "failures": "0x15 1x5"}, 5),
        ("historical failures", {"failures": "20x20"}, 1),
        ("counter reset", {"failures": "20x10 0x10"}, 1),
        ("failure with missing age", {"missing": "age", "failures": "0x15 1x5"}, 5),
        ("missing age", {"missing": "age"}, 4), ("missing activity", {"missing": "activity"}, 4),
        ("missing queue", {"missing": "ready"}, 4), ("missing failures", {"missing": "failures"}, 4),
        ("missing primary", {"missing": "primary"}, 4),
        ("missing queue on idle", {"age": 80000, "activity": "0x20", "missing": "ready"}, 4),
    ]:
        case(name, wal, inputs(**kwargs), expected)
    case("no telemetry", wal, [], 4)
    replica = [dict(s, series=s["series"].replace('pod="demo-1"', 'pod="demo-2"')) for s in inputs(age=80000, failures="0+1x20")]
    replica[0]["values"] = "1x20"
    case("replica failures do not affect primary", wal, inputs() + replica, 1)
    excluded = [dict(s, series=s["series"].replace('namespace="ai"', 'namespace="other"')) for s in inputs(age=80000, failures="0+1x20")]
    case("namespace filter", wal, inputs() + excluded, 1)

    queries = [t["expr"] for p in panels.values() for t in p.get("targets", [])
               if "controller_runtime_reconcile_total" in t.get("expr", "") and p["id"] != 655]
    for controller in ("cluster", "backup", "pooler", "scheduled-backup"):
        query = next(q for q in queries if f'controller="{controller}"' in q or
                     f'controller=~"{controller}"' in q or
                     (controller == "scheduled-backup" and "scheduledbackup|scheduled-backup" in q))
        metric = 'controller_runtime_reconcile_total{namespace="database",job="database/cloudnative-pg",controller="' + controller + '",result="error",pod="operator-1"}'
        other = metric.replace('job="database/cloudnative-pg"', 'job="database/other"')
        for name, values, expected in [("historical", "50x20", 0), ("recent", "50x15 51x5", 1.025), ("reset", "50x10 0x10", 0)]:
            case(controller + " " + name, concrete(query), [
                {"series": metric, "values": values}, {"series": other, "values": "0+20x20"}], expected)
    overview = [t["expr"] for t in panels[655]["targets"] if "expr" in t]
    for query in overview:
        controller = next(c for c in controllers if f'controller="{c}"' in query or f'controller=~"{c}"' in query)
        controller = "scheduled-backup" if controller.startswith("scheduledbackup") else controller
        metric = 'controller_runtime_reconcile_total{namespace="database",job="database/cloudnative-pg",controller="' + controller + '",result="error"}'
        for values, expected in [("50x20", 0), ("50x15 51x5", 1)]:
            case("overview " + controller + str(expected), concrete(query), [{"series": metric, "values": values}], expected)
    (tmp / "tests.json").write_text(json.dumps({"rule_files": [], "evaluation_interval": "1m", "tests": tests}))
    subprocess.run(["promtool", "test", "rules", str(tmp / "tests.json")], check=True)
    print(f"Dashboard transform and {len(tests)} PromQL cases passed.")
