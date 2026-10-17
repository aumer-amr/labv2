#!/usr/bin/env python3
"""Check AI routing invariants and alert behavior. Requires yq and promtool on PATH."""

import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
AI = ROOT / "kubernetes/apps/ai"


def read_yaml(path):
    return json.loads(subprocess.check_output(["yq", "-o=json", ".", str(path)], text=True))


models = AI / "litellm/app/models"
local = read_yaml(models / "qwen3-5-9b.yaml")["spec"]
assert local["params"]["apiBase"].startswith("http://qwen3-5-9b.ai.svc.")
assert not (models / "local.yaml").exists(), "Reuse the existing Qwen exposure"
router = read_yaml(AI / "litellm/app/litellmproxy.yaml")["spec"]["routerSettings"]
assert not router.get("fallbacks"), "Review local-only routing before adding proxy fallbacks"
config_text = read_yaml(AI / "hermes/app/configmap.yaml")["data"]["config.yaml"]
config = json.loads(subprocess.check_output(
    ["yq", "-o=json", "."], input=config_text, text=True
))
primary = read_yaml(models / "gpt-5-6-terra.yaml")["spec"]["modelName"]
assert config["model"]["default"] == primary
assert config["fallback_providers"][0]["model"] == local["modelName"]
assert config["model_overrides"]["lab-litellm"][local["modelName"]]["context_window"] == 65536
assert config["agent"]["reasoning_overrides"][local["modelName"]] is False
allowed = read_yaml(AI / "hermes/app/litellmvirtualkey.yaml")["spec"]["models"]
assert {primary, local["modelName"]} <= set(allowed), "Hermes cannot use its configured models"

# Exercise sustained failures, isolated failures, recovery, and namespace isolation.
cases = [
    ("LiteLLMAuthOrQuotaFailures", 'litellm_deployment_failure_responses_total{namespace="ai",litellm_model_name="gpt-5.6-terra",exception_status="401"}', "0+1x30", "0x30", 20,
     {"namespace": "ai", "litellm_model_name": "gpt-5.6-terra", "exception_status": "401"}),
    ("LlamaCppRequestsDeferred", 'llamacpp:requests_deferred{namespace="ai",service="qwen3.5-9b"}', "1x30", "0x30", 15,
     {"namespace": "ai", "service": "qwen3.5-9b"}),
    ("MeminiRecallErrors", 'memini_recall_results_total{namespace="ai",result="error"}', "0+1x30", "0x30", 20, {"namespace": "ai"}),
    ("MeminiRerankErrors", 'memini_rerank_results_total{namespace="ai",result="error"}', "0+1x30", "0x30", 20, {"namespace": "ai"}),
    ("MeminiConsolidateErrors", 'memini_consolidate_results_total{namespace="ai",result="error"}', "0+1x30", "0x30", 25, {"namespace": "ai"}),
]
with tempfile.TemporaryDirectory(prefix="ai-alert-tests-") as directory:
    directory = Path(directory)
    rules = {}
    files = []
    for app in ["litellm", "memini"]:
        app_dir = AI / app / "app"
        assert "./prometheusrule.yaml" in read_yaml(app_dir / "kustomization.yaml")["resources"]
        spec = read_yaml(app_dir / "prometheusrule.yaml")["spec"]
        path = directory / f"{app}.json"
        path.write_text(json.dumps(spec))
        files.append(str(path))
        for group in spec["groups"]:
            rules.update({rule["alert"]: rule for rule in group["rules"]})
    tests = []
    for alert, series, failing, healthy, minute, labels in cases:
        annotations = {}
        for key, value in rules[alert]["annotations"].items():
            for label, replacement in labels.items():
                value = value.replace("{{ $labels." + label + " }}", replacement)
            annotations[key] = value
        expected = [{"exp_labels": {**labels, "severity": "warning"}, "exp_annotations": annotations}]
        for name, sample_series, values, at, result in [
            ("sustained", series, failing, f"{minute}m", expected),
            ("healthy", series, healthy, f"{minute}m", []),
            ("pending", series, failing, "8m", []),
            ("isolated failure", series, "0 1x129" if "total" in series else "1 0x129", "20m", []),
            ("other namespace", series.replace('namespace="ai"', 'namespace="other"'), failing, f"{minute}m", []),
            ("recovered", series, failing + " 30x100" if "total" in series else "1x30 0x100", "120m", []),
        ]:
            tests.append({
                "name": f"{alert}: {name}",
                "interval": "1m",
                "input_series": [{"series": sample_series, "values": values}],
                "alert_rule_test": [{"eval_time": at, "alertname": alert, "exp_alerts": result}],
            })
    path = directory / "tests.json"
    path.write_text(json.dumps({"rule_files": files, "evaluation_interval": "1m", "tests": tests}))
    subprocess.run(["promtool", "check", "rules", *files], check=True)
    subprocess.run(["promtool", "test", "rules", str(path)], check=True)

print("AI model authorization, local routing, and 30 alert scenarios passed")
