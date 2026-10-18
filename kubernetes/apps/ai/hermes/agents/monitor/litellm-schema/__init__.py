"""Give constrained decoders the parameter schemas discovered through LiteLLM."""

import json


def typed_proxy_request(*, request, **_):
    users = [m.get("content", "") for m in request.get("messages", []) if m.get("role") == "user"]
    log_review = bool(users and '"check": "scheduled_log_review"' in users[-1])
    log_evidence = False
    metric_evidence = False
    metric_review = bool(users and "Use this bounded metric history: " in users[-1])
    if log_review or metric_review:
        for message in request.get("messages", []):
            content = message.get("content", "")
            if message.get("role") != "tool" or not isinstance(content, str):
                continue
            if not content.startswith('<untrusted_tool_result source="mcp__litellm__call_tool">\n'):
                continue
            try:
                payload = content.split("\n\n", 1)[1].rsplit("\n</untrusted_tool_result>", 1)[0]
                result = json.loads(json.loads(payload)["result"])
                if any(result.get(key) for key in ("error", "warnings", "infos")):
                    continue
                log_evidence |= log_review and result.get("status") == "success" and result.get("data", {}).get("resultType") == "vector"
                metric_evidence |= (metric_review and result.get("result") is not None
                                    and "warning: the result was truncated" not in str(result).lower())
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                continue
    if log_evidence:
        request["messages"].append({"role": "user", "content":
            "The bounded periodic aggregate review has completed. Return its JSON assessment now. "
            "This covers sampled error-count aggregates, not raw log contents or every severity. "
            "Do not invent root causes or claim exhaustive coverage. Use uncertain and a concrete "
            "next_step when these counts cannot establish whether a pattern needs action."})
    if metric_evidence:
        request["messages"].append({"role": "user", "content":
            "The bounded metric history query has completed. Return its JSON assessment now. "
            "Use uncertain with a concrete next_step if the sampled history cannot establish actionability. "
            "Do not infer root causes, continuous threshold breaches, or individual workload history "
            "from cluster aggregates."})
    if log_evidence or metric_evidence or (users and "MONITOR_FORMAT_ASSESSMENT_JSON" in users[-1]):
        request.pop("tools", None)
        request.pop("tool_choice", None)
        request["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "monitor_assessment", "strict": True, "schema": {
                "type": "object", "properties": {
                    "verdict": {"type": "string", "enum": ["actionable", "expected", "uncertain"]},
                    "summary": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                    "next_step": {"type": "string"}},
                "required": ["verdict", "summary", "evidence", "next_step"], "additionalProperties": False}}}
        return {"request": request}
    schemas = {}
    for message in request.get("messages", []):
        content = message.get("content", "")
        if message.get("role") != "tool" or not isinstance(content, str):
            continue
        if not content.startswith('<untrusted_tool_result source="mcp__litellm__get_tool_schema">\n'):
            continue
        try:
            payload = content.split("\n\n", 1)[1].rsplit("\n</untrusted_tool_result>", 1)[0]
            tool = json.loads(json.loads(payload)["result"])
            if tool["name"].startswith(("prometheus-", "victorialogs-")):
                schema = tool["inputSchema"]
                if tool["name"] == "prometheus-range_query" and "step" in schema.get("properties", {}):
                    schema["required"] = sorted(set(schema.get("required", [])) | {"step"})
                if tool["name"] == "victorialogs-stats_query" and "time" in schema.get("properties", {}):
                    schema["required"] = sorted(set(schema.get("required", [])) | {"time"})
                schemas[tool["tool_id"]] = schema
        except (ValueError, KeyError, IndexError, TypeError):
            continue
    if schemas:
        properties = {}
        required = set.intersection(*(set(s.get("required", [])) for s in schemas.values()))
        for schema in schemas.values():
            properties.update(schema.get("properties", {}))
        if log_review or metric_review:
            try:
                marker = "validated bounded per-stream comparison: " if log_review else "Use this bounded metric history: "
                supplied = users[-1].split(marker, 1)[1]
                baseline, _ = json.JSONDecoder().raw_decode(supplied)
                for name in baseline:
                    if name in properties:
                        properties[name] = {**properties[name], "enum": [baseline[name]]}
                        required.add(name)
            except (IndexError, ValueError, KeyError, TypeError):
                pass
        for tool in request.get("tools", []):
            function = tool.get("function", {})
            if function.get("name") == "mcp__litellm__call_tool":
                # llama.cpp needs concrete properties at each object level, not top-level oneOf.
                function["parameters"] = {"type": "object", "properties": {
                    "tool_id": {"type": "string", "enum": list(schemas)},
                    "arguments": {"type": "object", "properties": properties,
                                  "required": sorted(required), "additionalProperties": False}},
                    "required": ["tool_id", "arguments"], "additionalProperties": False}
    return {"request": request}


def register(ctx):
    ctx.register_middleware("llm_request", typed_proxy_request)
