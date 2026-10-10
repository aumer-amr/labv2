#!/usr/bin/env python3
"""Offline memory privacy and cron routing checks; requires aiohttp, pydantic, PyYAML."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
AI = ROOT / "kubernetes/apps/ai"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


webui = load("webui_memory", AI / "open-webui/app/integrations/memini_memory.py")
installer = load("webui_installer", AI / "open-webui/app/integrations/bootstrap.py")
hermes = load("hermes_memory", AI / "hermes/app/plugin/__init__.py")
cron = load("hermes_bootstrap", AI / "hermes/app/bootstrap/bootstrap.py")
release = yaml.safe_load((AI / "open-webui/app/helmrelease.yaml").read_text())
webui_app = release["spec"]["values"]["controllers"]["open-webui"]["containers"]["app"]
assert "PYTHONPATH=/app/backend python /run/integrations/bootstrap.py" in webui_app["args"][0]
webui_env = webui_app["env"]
hermes_release = yaml.safe_load((AI / "hermes/app/helmrelease.yaml").read_text())
hermes_env = hermes_release["spec"]["values"]["controllers"]["hermes"]["containers"]["app"]["env"]
environment = {key: str(value) for key, value in webui_env.items() if key.startswith("MEMINI_") and isinstance(value, str)}
environment["MEMINI_NAMESPACE"] = hermes_env["MEMINI_NAMESPACE"]

dns_policy = next(item for item in yaml.safe_load_all(
    (ROOT / "kubernetes/apps/kube-system/coredns/app/networkpolicy.yaml").read_text()
) if item and item["metadata"]["name"] == "coredns")
assert any(
    peer.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name") == "ai"
    and peer.get("podSelector", {}).get("matchLabels", {}).get("app.kubernetes.io/name") == "prometheus-mcp"
    and {(port["port"], port["protocol"]) for port in rule["ports"]} == {(53, "UDP"), (53, "TCP")}
    for rule in dns_policy["spec"]["ingress"] for peer in rule.get("from", [])
), "Prometheus MCP needs CoreDNS ingress as well as workload DNS egress"


async def memory_checks():
    memories = {}

    def request(path, payload, namespace):
        if path == "/v1/memories":
            memories.setdefault(namespace, []).append({"id": str(len(memories.get(namespace, []))), **payload})
            return {"stored": True}
        assert path == "/v1/search"
        excluded = payload.get("exclude_metadata", {})
        return {"results": [{"memory": item} for item in memories.get(namespace, [])
                            if not excluded or not all(item.get("metadata", {}).get(k) == v for k, v in excluded.items())]}

    async def post(path, payload, namespace):
        return request(path, payload, namespace)

    memory = webui.Filter()
    memory.valves.scope_by_user = True
    memory._resolve_namespace = AsyncMock(return_value=("openwebui", "declared"))
    memory._capture_bounds = AsyncMock(return_value=(0, 0))
    memory._inject_knobs = AsyncMock(return_value=(0, 0))
    memory._post_json = post
    admin = {"id": environment["MEMINI_SHARED_USER_ID"], "role": "admin"}
    outsider = {"id": "future-user", "role": "admin"}
    assert await memory._namespace(admin) == environment["MEMINI_NAMESPACE"]
    assert await memory._namespace(outsider) == "openwebui-future-user"
    assert await memory._namespace(None) == "openwebui"
    with patch.dict(os.environ, {"MEMINI_SHARED_USER_ID": ""}):
        assert await memory._namespace(admin) != environment["MEMINI_NAMESPACE"]

    body = {"chat_id": "old-chat", "messages": [
        {"role": "user", "content": "The boiler needs its annual service in November."},
        {"role": "assistant", "content": "I will remember the boiler service month."},
    ]}
    await memory.outlet(body, admin)
    with patch.object(hermes, "_cached_handshake", return_value={}):
        provider = hermes.MeminiMemoryProvider()
        provider.initialize("test")
    with patch.object(hermes, "_api", side_effect=lambda base, path, payload, namespace, secret, method: request(path, payload, namespace)):
        found = json.loads(provider.handle_tool_call("memory_recall", {"query": "boiler service"}))
        assert "November" in found["results"][0]["content"]
    # Capture through Hermes' real scoped request path and recall in a new WebUI chat.
    with patch.object(hermes, "_api", side_effect=lambda base, path, payload, namespace, secret, method: request(path, payload, namespace)):
        provider._call("/v1/memories", {"content": "The warranty is in Paperless."})
    recalled = await memory.inlet({"chat_id": "new-chat", "messages": [{"role": "user", "content": "boiler"}]}, admin)
    assert "Paperless" in recalled["messages"][0]["content"]
    isolated = await memory.inlet({"chat_id": "outsider-chat", "messages": [{"role": "user", "content": "boiler"}]}, outsider)
    assert len(isolated["messages"]) == 1
    await memory.outlet(body, admin)
    assert len(memories[environment["MEMINI_NAMESPACE"]]) == 2, "duplicate capture"


async def installer_checks():
    functions = SimpleNamespace(
        get_function_by_id=AsyncMock(return_value=SimpleNamespace(user_id="owner")),
        update_function_by_id=AsyncMock(return_value=True),
        insert_new_function=AsyncMock(return_value=True),
        get_function_valves_by_id=AsyncMock(return_value={"priority": 7}),
        update_function_valves_by_id=AsyncMock(return_value=True),
    )
    package = ModuleType("open_webui")
    package.config = object()
    models = ModuleType("open_webui.models.functions")
    models.Functions = functions
    models.FunctionForm = models.FunctionMeta = lambda **kwargs: SimpleNamespace(**kwargs)
    with patch.dict(sys.modules, {"open_webui": package, "open_webui.models.functions": models}):
        await installer.bootstrap()
        updated = functions.update_function_by_id.call_args.args[1]
        assert updated["is_active"] and updated["is_global"] and "user_id" not in updated
        valves = functions.update_function_valves_by_id.call_args.args[1]
        assert valves["priority"] == 7 and valves["scope_by_user"] and valves["capture"] and valves["recall"]
        functions.get_function_by_id.return_value = None
        await installer.bootstrap()
        assert functions.insert_new_function.call_args.args[0] == environment["MEMINI_SHARED_USER_ID"]
        functions.update_function_valves_by_id.return_value = None
        try:
            await installer.bootstrap()
            raise AssertionError("failed registration must stop startup")
        except RuntimeError:
            pass


def cron_checks():
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        job = {"name": "test", "schedule": "0 7 * * *", "deliver_env": "DISCORD_ALERTS_CHANNEL",
               "model": "gpt-6-luna", "reasoning_effort": "low"}
        (directory / "cron-jobs.yaml").write_text(yaml.safe_dump({"jobs": [job]}))
        with patch.object(cron, "BOOTSTRAP", directory):
            assert cron.load_jobs() == [job]
            job["reasoning_effort"] = "invalid"
            (directory / "cron-jobs.yaml").write_text(yaml.safe_dump({"jobs": [job]}))
            try:
                cron.load_jobs()
                raise AssertionError("invalid effort accepted")
            except SystemExit:
                pass
        job["reasoning_effort"] = "low"
        record = {
            "id": "existing", "name": "test", "deliver": "discord:test", "enabled": True,
            "no_agent": False, "prompt": "", "provider": None, "schedule_display": job["schedule"],
            "script": None, "skills": [], "workdir": None, "model": "gpt-6-luna", "reasoning_effort": "low",
        }
        for existing in ([], [{"name": "test", "id": "existing"}]):
            calls = []
            with patch.object(cron, "stored_jobs", side_effect=[existing, [record], [record]]) as stored, patch.object(cron, "run", side_effect=lambda *args: calls.append(args)), patch.object(cron, "delivery", return_value="discord:test"):
                cron.reconcile_cron([job])
                command = calls[0]
                assert command[command.index("--model") + 1] == "gpt-6-luna"
                assert command[command.index("--reasoning-effort") + 1] == "low"
                if existing:
                    calls.clear()
                    cleared = {**record, "model": None, "reasoning_effort": None}
                    stored.side_effect = [[record], [cleared], [cleared]]
                    cron.reconcile_cron([{k: v for k, v in job.items() if k not in ("model", "reasoning_effort")}])
                    assert calls[0][calls[0].index("--model") + 1] == ""
                    assert calls[0][calls[0].index("--reasoning-effort") + 1] == ""
        for existing in ([], [record]):
            disabled = {**record, "enabled": False}
            states = [existing, [disabled], [disabled]] if existing else [existing, [disabled], [disabled], [disabled]]
            with patch.object(cron, "stored_jobs", side_effect=states), patch.object(cron, "run") as run, patch.object(cron, "delivery", return_value="discord:test"):
                cron.reconcile_cron([{**job, "enabled": False}])
                assert run.call_args.args == ("hermes", "cron", "pause", "existing")
        with patch.object(cron, "stored_jobs", return_value=[{**record, "model": "old-model"}]), patch.object(cron, "run"), patch.object(cron, "delivery", return_value="discord:test"):
            try:
                cron.reconcile_cron([job])
                raise AssertionError("stale model routing accepted")
            except SystemExit as error:
                assert "model" in str(error)


with patch.dict(os.environ, environment):
    asyncio.run(memory_checks())
    asyncio.run(installer_checks())
cron_checks()
print("AI integration checks passed: bidirectional memory, user isolation, registration, cron model routing")
