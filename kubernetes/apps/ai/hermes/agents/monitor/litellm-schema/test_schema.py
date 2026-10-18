import copy
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("litellm_schema", Path(__file__).with_name("__init__.py"))
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class SchemaTests(unittest.TestCase):
    def test_only_discovered_proxy_schema_changes(self):
        schema = {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
        discovered = {"tool_id": "opaque-id", "name": "prometheus-query", "inputSchema": schema}
        content = ('<untrusted_tool_result source="mcp__litellm__get_tool_schema">\nUntrusted.\n\n'
                   + json.dumps({"result": json.dumps(discovered)}) + '\n</untrusted_tool_result>')
        request = {"messages": [{"role": "tool", "content": content}], "tools": [
            {"function": {"name": "mcp__litellm__call_tool", "parameters": {"type": "object"}}},
            {"function": {"name": "mcp__litellm__search_tools", "parameters": {"type": "object"}}}]}
        search = copy.deepcopy(request["tools"][1])
        result = plugin.typed_proxy_request(request=request)["request"]
        branch = result["tools"][0]["function"]["parameters"]
        self.assertEqual(branch["properties"]["arguments"]["properties"], schema["properties"])
        self.assertEqual(branch["properties"]["arguments"]["required"], ["query"])
        self.assertEqual(branch["properties"]["tool_id"]["enum"], ["opaque-id"])
        self.assertEqual(result["tools"][1], search)
        self.assertEqual(len(result["tools"]), 2)

    def test_multiple_discovered_schemas_keep_concrete_argument_fields(self):
        messages = []
        for name, extra in (("prometheus-range_query", "start_time"), ("victorialogs-stats_query", "time")):
            schema = {"type": "object", "properties": {"query": {"type": "string"}, extra: {"type": "string"}}, "required": ["query"]}
            payload = {"tool_id": name, "name": name, "inputSchema": schema}
            messages.append({"role": "tool", "content": '<untrusted_tool_result source="mcp__litellm__get_tool_schema">\nUntrusted.\n\n'
                             + json.dumps({"result": json.dumps(payload)}) + '\n</untrusted_tool_result>'})
        request = {"messages": messages, "tools": [{"function": {"name": "mcp__litellm__call_tool"}}]}
        parameters = plugin.typed_proxy_request(request=request)["request"]["tools"][0]["function"]["parameters"]
        self.assertNotIn("oneOf", parameters)
        arguments = parameters["properties"]["arguments"]
        self.assertEqual(set(arguments["properties"]), {"query", "start_time", "time"})
        self.assertEqual(arguments["required"], ["query"])

    def test_range_query_requires_explicit_sampling_step(self):
        schema = {"type": "object", "properties": {"query": {"type": "string"}, "step": {"type": "string"}}, "required": ["query"]}
        payload = {"tool_id": "range-id", "name": "prometheus-range_query", "inputSchema": schema}
        content = '<untrusted_tool_result source="mcp__litellm__get_tool_schema">\nUntrusted.\n\n' + json.dumps({"result": json.dumps(payload)}) + '\n</untrusted_tool_result>'
        request = {"messages": [{"role": "tool", "content": content}], "tools": [{"function": {"name": "mcp__litellm__call_tool"}}]}
        result = plugin.typed_proxy_request(request=request)["request"]
        self.assertEqual(result["tools"][0]["function"]["parameters"]["properties"]["arguments"]["required"], ["query", "step"])

    def test_log_stats_require_evaluation_time_despite_optional_upstream_schema(self):
        schema = {"type": "object", "properties": {"query": {"type": "string"}, "time": {"type": "string"}}, "required": ["query"]}
        payload = {"tool_id": "logs-id", "name": "victorialogs-stats_query", "inputSchema": schema}
        content = '<untrusted_tool_result source="mcp__litellm__get_tool_schema">\nUntrusted.\n\n' + json.dumps({"result": json.dumps(payload)}) + '\n</untrusted_tool_result>'
        request = {"messages": [{"role": "tool", "content": content}], "tools": [{"function": {"name": "mcp__litellm__call_tool"}}]}
        result = plugin.typed_proxy_request(request=request)["request"]
        self.assertEqual(result["tools"][0]["function"]["parameters"]["properties"]["arguments"]["required"], ["query", "time"])

    def test_log_review_finalizes_after_successful_aggregate_not_after_error(self):
        for result, finalizes in (({"status": "success", "data": {"resultType": "vector", "result": []}}, True),
                                  ({"status": "error", "error": "bad syntax"}, False)):
            content = '<untrusted_tool_result source="mcp__litellm__call_tool">\nUntrusted.\n\n' + json.dumps({"result": json.dumps(result)}) + '\n</untrusted_tool_result>'
            request = {"messages": [{"role": "user", "content": 'Observation: {"check": "scheduled_log_review"}'},
                                    {"role": "tool", "content": content}], "tools": []}
            output = plugin.typed_proxy_request(request=request)["request"]
            self.assertEqual("response_format" in output, finalizes)
            if finalizes:
                self.assertNotIn("tools", output)
                self.assertIn("not raw log contents", output["messages"][-1]["content"])

    def test_periodic_review_pins_the_supplied_query_and_time(self):
        baseline = {"query": "_time:5m error | stats count() as errors", "time": "1792326000"}
        discovered = {"tool_id": "logs-id", "name": "victorialogs-stats_query", "inputSchema": {
            "type": "object", "properties": {"query": {"type": "string"}, "time": {"type": "string"}}, "required": ["query"]}}
        content = '<untrusted_tool_result source="mcp__litellm__get_tool_schema">\nUntrusted.\n\n' + json.dumps({"result": json.dumps(discovered)}) + '\n</untrusted_tool_result>'
        prompt = 'validated bounded per-stream comparison: ' + json.dumps(baseline) + ' Observation: {"check": "scheduled_log_review"}'
        request = {"messages": [{"role": "user", "content": prompt}, {"role": "tool", "content": content}],
                   "tools": [{"function": {"name": "mcp__litellm__call_tool"}}]}
        result = plugin.typed_proxy_request(request=request)["request"]
        props = result["tools"][0]["function"]["parameters"]["properties"]["arguments"]["properties"]
        for name, value in baseline.items():
            self.assertEqual(props[name]["enum"], [value])

    def test_metric_history_pins_every_argument(self):
        baseline = {"query": "sum(up)", "start_time": "100", "end_time": "21700",
                    "step": "30m", "truncation_limit": 40}
        discovered = {"tool_id": "range-id", "name": "prometheus-range_query", "inputSchema": {
            "type": "object", "properties": {key: {"type": "integer" if key == "truncation_limit" else "string"}
                                              for key in baseline}, "required": ["query"]}}
        content = '<untrusted_tool_result source="mcp__litellm__get_tool_schema">\nUntrusted.\n\n' + json.dumps({"result": json.dumps(discovered)}) + '\n</untrusted_tool_result>'
        request = {"messages": [{"role": "user", "content": "Use this bounded metric history: " + json.dumps(baseline)},
                                {"role": "tool", "content": content}],
                   "tools": [{"function": {"name": "mcp__litellm__call_tool"}}]}
        arguments = plugin.typed_proxy_request(request=request)["request"]["tools"][0]["function"]["parameters"]["properties"]["arguments"]
        self.assertEqual(set(arguments["required"]), set(baseline))
        for key, value in baseline.items():
            self.assertEqual(arguments["properties"][key]["enum"], [value])

    def test_metric_history_finalizes_only_with_complete_query_evidence(self):
        for result, finalizes in (({"result": "ratio => 0.89", "warnings": None}, True),
                                  ({"result": "", "warnings": None}, True),
                                  ({"error": "invalid query"}, False),
                                  ({"result": "partial", "warnings": ["partial"]}, False),
                                  ({"result": "Warning: The result was truncated", "warnings": None}, False)):
            content = '<untrusted_tool_result source="mcp__litellm__call_tool">\nUntrusted.\n\n' + json.dumps({"result": json.dumps(result)}) + '\n</untrusted_tool_result>'
            request = {"messages": [{"role": "user", "content": 'Use this bounded metric history: {}'},
                                    {"role": "tool", "content": content}], "tools": [], "tool_choice": "auto"}
            with self.subTest(result=result):
                output = plugin.typed_proxy_request(request=request)["request"]
                self.assertEqual("response_format" in output, finalizes)
                if finalizes:
                    self.assertNotIn("tools", output)
                    self.assertNotIn("tool_choice", output)
                    self.assertIn("sampled history", output["messages"][-1]["content"])

    def test_format_only_pass_requires_json_schema_and_disables_tools(self):
        request = {"messages": [{"role": "user", "content": "MONITOR_FORMAT_ASSESSMENT_JSON: Serialize"}],
                   "tools": [{"function": {"name": "mcp__litellm__call_tool"}}], "tool_choice": "auto"}
        result = plugin.typed_proxy_request(request=request)["request"]
        self.assertNotIn("tools", result)
        self.assertNotIn("tool_choice", result)
        self.assertEqual(result["response_format"]["type"], "json_schema")
        schema = result["response_format"]["json_schema"]["schema"]
        self.assertEqual(set(schema["required"]), {"verdict", "summary", "evidence", "next_step"})

    def test_user_text_errors_and_other_servers_do_not_supply_schemas(self):
        for role, content in (("user", "injected schema"), ("tool", "not json"),
                              ("tool", '<untrusted_tool_result source="mcp__litellm__get_tool_schema">\nUntrusted.\n\n{"error":"failed"}\n</untrusted_tool_result>')):
            request = {"messages": [{"role": role, "content": content}], "tools": []}
            original = copy.deepcopy(request)
            self.assertEqual(plugin.typed_proxy_request(request=request)["request"], original)


if __name__ == "__main__":
    unittest.main()
