#!/usr/bin/env python3
"""Check AI routing invariants and alert behavior. Requires yq and promtool on PATH."""

import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
AI = ROOT / "kubernetes/apps/ai"


def read_yaml(path):
    return json.loads(subprocess.check_output(["yq", "-o=json", ".", str(path)], text=True))


models = AI / "litellm/app/models"
local = read_yaml(models / "qwen3-5-9b.yaml")["spec"]
assert local["params"]["apiBase"].startswith("http://qwen3-5-9b.ai.svc.")
fallback = read_yaml(models / "qwen3-5-9b-fallback.yaml")["spec"]
assert fallback["params"]["apiBase"] == "http://qwen3-5-9b-fallback.ai.svc.cluster.local:8080/v1"
assert fallback["modelName"] == "qwen3.5-9b-fallback"
assert fallback["params"]["model"] == "openai/qwen3.5-9b-fallback"
assert local["modelName"] == "qwen3.5-9b"
assert not (models / "local.yaml").exists(), "Reuse the existing Qwen exposure"
router = read_yaml(AI / "litellm/app/litellmproxy.yaml")["spec"]["routerSettings"]
assert not router.get("fallbacks"), "Review local-only routing before adding proxy fallbacks"
config_text = read_yaml(AI / "hermes/app/configmap.yaml")["data"]["config.yaml"]
config = json.loads(subprocess.check_output(
    ["yq", "-o=json", "."], input=config_text, text=True
))
primary = fallback["modelName"]
assert config["model"]["default"] == primary
assert config["model"]["context_length"] == 65536
assert not config.get("fallback_providers"), "ChatGPT must remain opt-in"
assert config["auxiliary"]["title_generation"]["model_upgrade_enabled"] is False
assert config["model_overrides"]["lab-litellm"][fallback["modelName"]]["context_window"] == 65536
assert config["agent"]["reasoning_overrides"][fallback["modelName"]] is False
allowed = read_yaml(AI / "hermes/app/litellmvirtualkey.yaml")["spec"]["models"]
assert {primary, "gpt-5.6-terra", "gpt-6.1-sol"} <= set(allowed), "Hermes must retain manual ChatGPT selection"

webui = read_yaml(AI / "open-webui/app/helmrelease.yaml")["spec"]["values"]["controllers"]["open-webui"]["containers"]["app"]["env"]
assert webui["DEFAULT_MODELS"] == primary
assert webui["TASK_MODEL_EXTERNAL"] == primary
assert webui["ENABLE_PERSISTENT_CONFIG"] == "false", "Stored defaults must not override Git"
memini = read_yaml(AI / "memini/app/helmrelease.yaml")["spec"]["values"]["controllers"]["main"]["containers"]["main"]["env"]
assert memini["MEMINI_LLM_MODEL"] == primary
assert memini["MEMINI_EMBED_MODEL"] == "qwen3-embedding"
assert "MEMINI_LLM_EXTRA_BODY" not in memini, "Qwen must not inherit the ChatGPT Responses workaround"
for app in ("open-webui", "memini"):
    permitted = read_yaml(AI / app / "app/litellmvirtualkey.yaml")["spec"]["models"]
    assert {primary, "gpt-6-luna"} <= set(permitted), f"{app} must allow Qwen and manual ChatGPT selection"

monitor_config = json.loads(subprocess.check_output(
    ["yq", "-o=json", "."], input=read_yaml(AI / "hermes/agents/monitor/configmap.yaml")["data"]["config.yaml"], text=True
))
assert monitor_config["model"]["default"] == local["modelName"]
assert monitor_config["auxiliary"]["title_generation"]["model_upgrade_enabled"] is False
for path in AI.rglob("*virtualkey.yaml"):
    key = read_yaml(path)
    permitted = key["spec"]["models"]
    if key["metadata"]["name"] == "hermes-monitor":
        assert permitted == [local["modelName"]]
    else:
        assert local["modelName"] not in permitted, str(path)
        assert "*" not in permitted and permitted, str(path)
    if key["metadata"]["name"] in ("hermes", "pr-reviewer", "open-webui", "memini"):
        assert fallback["modelName"] in permitted
inference = read_yaml(AI / "llmkube/models/qwen3-5-9b-fallback.yaml")["spec"]
assert inference["modelRef"] == local["modelName"]
assert inference["nodeSelector"]["kubernetes.io/hostname"] == "deathscythe"
assert inference["contextSize"] == 65536 and inference["parallelSlots"] == 1
assert inference["resources"] == {"cpu": "500m", "memory": "12Gi", "memoryLimit": "24Gi"}
assert inference["podSecurityContext"]["runAsUser"] == 101
assert inference["securityContext"]["runAsNonRoot"] is True
cron = read_yaml(AI / "hermes/app/bootstrap/cron-jobs.yaml")["jobs"]
assert all(job.get("enabled", True) is False for job in cron)
assert next(j for j in cron if j["name"] == "Morning exception brief")["model"] == primary
assert "model" not in next(j for j in cron if j["name"] == "Weekly personal review")

# Exercise sustained failures, isolated failures, recovery, and namespace isolation.
cases = [
    ("LlamaCppRequestsDeferred", 'llamacpp:requests_deferred{namespace="ai",service="qwen3.5-9b"}', "1x30", "0x30", 15,
     {"namespace": "ai", "service": "qwen3.5-9b"}),
    ("LlamaCppRequestsDeferred", 'llamacpp:requests_deferred{namespace="ai",service="qwen3-5-9b-fallback"}', "1x30", "0x30", 15,
     {"namespace": "ai", "service": "qwen3-5-9b-fallback"}),
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
    assert "LiteLLMAuthOrQuotaFailures" not in rules
    tests = [
        {
            "name": f"Disabled auth/quota alert: HTTP {status} at {minute}m",
            "interval": "1m",
            "input_series": [{
                "series": 'litellm_deployment_failure_responses_total{namespace="ai",litellm_model_name="gpt-5.6-terra",exception_status="' + status + '"}',
                "values": "0+1x120",
            }],
            "alert_rule_test": [{"eval_time": f"{minute}m", "alertname": "LiteLLMAuthOrQuotaFailures", "exp_alerts": []}],
        }
        for status in ("401", "403", "429") for minute in (20, 120)
    ]
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

print(f"AI model defaults, authorization, local routing, and {len(tests)} alert scenarios passed")
