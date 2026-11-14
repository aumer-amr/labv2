"""Expose bounded memini tools with fixed per-agent write scopes."""
import json
import os
import re
import tomllib
from pathlib import Path

from monitor import http

CONFIG = Path('/config/config.toml')
TOOLS = [
    {'name': 'memory_briefing', 'description': 'Read this agent memory and shared LabV2 context at session start.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'memory_recall', 'description': 'Recall relevant facts from this agent and shared LabV2 context.',
     'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
                     'limit': {'type': 'integer', 'minimum': 1, 'maximum': 5}},
                     'required': ['query'], 'additionalProperties': False}},
    {'name': 'memory_remember', 'description': 'Save one durable fact in this agent memory. Shared context is read-only.',
     'inputSchema': {'type': 'object', 'properties': {'content': {'type': 'string', 'minLength': 1, 'maxLength': 8000},
                     'tags': {'type': 'array', 'maxItems': 10, 'items': {'type': 'string', 'maxLength': 80}}},
                     'required': ['content'], 'additionalProperties': False}},
]


def call(agent, name, arguments):
    if not isinstance(arguments, dict):
        raise ValueError('Memory arguments must be an object')
    if name == 'memory_briefing' and not arguments:
        arguments = {'scope': 'full'}
    elif name == 'memory_recall' and set(arguments) <= {'query', 'limit'}:
        query, limit = arguments.get('query'), arguments.get('limit', 3)
        if not isinstance(query, str) or not 0 < len(query.strip()) <= 2000 or type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError('Invalid recall arguments')
        arguments = {'query': query, 'limit': limit, 'scope': 'full', 'response_format': 'concise'}
    elif name == 'memory_remember' and set(arguments) <= {'content', 'tags'}:
        content, tags = arguments.get('content'), arguments.get('tags', [])
        if (not isinstance(content, str) or not 0 < len(content.strip()) <= 8000 or not isinstance(tags, list)
                or len(tags) > 10 or any(not isinstance(t, str) or not 0 < len(t) <= 80 for t in tags)):
            raise ValueError('Invalid remember arguments')
        # No namespace, visibility, ID, or upsert override can reach memini.
        arguments = {'content': content, 'tags': tags, 'visibility': 'project', 'tier': 'semantic'}
    else:
        raise ValueError('Memory operation is not allowed')
    response = http('http://memini.ai.svc.cluster.local:8080/mcp',
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}},
                    os.environ['MEMINI_API_KEY'], timeout=15,
                    headers={'Accept': 'application/json, text/event-stream', 'X-Memini-Namespace': 'labv2/zeroclaw/' + agent})
    if isinstance(response, str):
        messages = [json.loads('\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:')))
                    for event in response.replace('\r\n', '\n').split('\n\n') if any(line.startswith('data:') for line in event.splitlines())]
        response = next((message for message in messages if message.get('id') == 1), None)
    if not isinstance(response, dict) or 'error' in response or not isinstance(response.get('result'), dict):
        raise ValueError('Invalid memini response')
    return response['result']


def rpc(path, body):
    agent = path.removeprefix('/memory/')
    if (not re.fullmatch(r'[a-z][a-z0-9_\-]*', agent) or agent not in tomllib.loads(CONFIG.read_text())['agents']
            or not isinstance(body, dict) or body.get('jsonrpc') != '2.0'):
        raise ValueError('Invalid memory request')
    method = body.get('method')
    if method == 'notifications/initialized' and 'id' not in body:
        return None
    if 'id' not in body:
        raise ValueError('Request ID required')
    if method == 'initialize':
        result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'zeroclaw-memory', 'version': '1'}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': TOOLS}
    elif method == 'tools/call':
        params = body.get('params', {})
        if not isinstance(params, dict):
            raise ValueError('Invalid memory call')
        try:
            result = call(agent, params.get('name'), params.get('arguments', {}))
        except Exception as error:
            result = {'content': [{'type': 'text', 'text': 'Memory unavailable: ' + type(error).__name__}], 'isError': True}
    else:
        return {'jsonrpc': '2.0', 'id': body['id'], 'error': {'code': -32601, 'message': 'Method not found'}}
    return {'jsonrpc': '2.0', 'id': body['id'], 'result': result}
