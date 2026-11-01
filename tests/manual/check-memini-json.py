#!/usr/bin/env python3
"""Regress Memini JSON mode through LiteLLM, with all provider HTTP mocked.

Run in the installed LiteLLM container:
  kubectl exec -i --namespace ai <litellm-pod> -- python < tests/manual/check-memini-json.py
"""

import asyncio
import json
from unittest.mock import patch

import httpx
import litellm


async def check():
    for name in ("baseline", "developer", "appended"):
        seen = []

        async def send(client, request, *args, **kwargs):
            body = json.loads(request.content)
            seen.append(body)
            assert request.url.path.endswith("/responses"), request.url.path
            return httpx.Response(200, request=request, json={
                "id": "resp_test", "object": "response", "created_at": 1,
                "model": "gpt-6-luna", "status": "completed",
                "output": [{"type": "message", "id": "msg_test", "role": "assistant",
                            "status": "completed", "content": [{"type": "output_text",
                            "text": "{\"scores\":[0.6]}", "annotations": []}]}],
                "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20},
            })

        messages = [{"role": "system", "content": "Score these memories. Return JSON."},
                    {"role": "user", "content": '["Prefer small changes"]'}]
        if name == "developer":
            messages[0]["role"] = "developer"
        if name == "appended":
            messages.append({"role": "system", "content": "When JSON output is requested, return only JSON."})
        with patch.object(httpx.AsyncClient, "send", send):
            result = await litellm.acompletion(
                model="openai/responses/gpt-6-luna", messages=messages,
                response_format={"type": "json_object"}, temperature=0, max_tokens=4096,
                additional_drop_params=["temperature"], api_key="synthetic-test-key",
                api_base="https://example.invalid/v1", num_retries=0,
            )
        assert len(seen) == 1, len(seen)
        body = seen[0]
        assert body["text"]["format"]["type"] == "json_object"
        assert "temperature" not in body
        assert body["instructions"] == "Score these memories. Return JSON."
        assert body["input"][0]["content"][0]["text"] == '["Prefer small changes"]'
        assert len(body["input"]) == (2 if name == "appended" else 1)
        has_json = "json" in json.dumps(body["input"]).lower()
        assert has_json == (name == "appended"), (name, has_json)
        assert json.loads(result.choices[0].message.content) == {"scores": [0.6]}
        print(f"PASS {name}: JSON instruction in provider input = {has_json}")


asyncio.run(check())
