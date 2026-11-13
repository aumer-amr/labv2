#!/usr/bin/env python3
"""Exercise the pinned image with a local fake model and MCP server, without external access.

Run with Docker available. Model and MCP calls use local fixtures only.
"""
import copy
import socket
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / 'kubernetes/apps/ai/zeroclaw/workflow'
sys.path.insert(0, str(WORKFLOW))
import monitor
import server as automation

IMAGE = "ghcr.io/zeroclaw-labs/zeroclaw:default-features-v0.9.0@sha256:f63c01339d69187af86d3ec9df0d30e9b325c81bb1f558ef0be0bbf683c6ec4f"
PYTHON = "docker.io/python:3.15.2-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928"
if shutil.which("zeroclaw") is None:
    # Local test image only: put the unmodified release binary beside the fixture.
    with tempfile.TemporaryDirectory() as build:
        Path(build, 'Dockerfile').write_text(f"FROM {IMAGE} AS runtime\nFROM {PYTHON}\nCOPY --from=runtime /usr/local/bin/zeroclaw /usr/local/bin/zeroclaw\nCOPY --from=runtime /usr/share/zeroclawlabs/web/dist /usr/share/zeroclawlabs/web/dist\n")
        subprocess.run(['docker', 'build', '-q', '-t', 'labv2-zeroclaw-test:0.9.0', build], check=True)
    subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--user', '1000:1000', '--read-only', '--cap-drop', 'ALL', '--tmpfs', '/tmp',
                    '-v', str(WORKFLOW.parent) + ':/repo/kubernetes/apps/ai/zeroclaw:ro',
                    '-v', str(Path(__file__).resolve()) + ':/repo/tests/manual/check-zeroclaw-runtime.py:ro',
                    'labv2-zeroclaw-test:0.9.0', 'python3', '/repo/tests/manual/check-zeroclaw-runtime.py'], check=True)
    raise SystemExit(0)

calls = []
tool_name = "monitor__call_tool"
query_args = {'query': 'sum(up)', 'start_time': '100', 'end_time': '21700', 'step': '30m'}
assessment = {'verdict': 'expected', 'summary': 'Stable', 'evidence': ['sum(up), 100-21700'], 'next_step': 'Observe'}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_DELETE(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        assert self.headers.get('Authorization') == 'Bearer fixture-key'
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path.startswith('/v1/'):
            if not body.get('tools') and body['messages'][0]['content'].startswith('You are a memory consolidation engine.'):
                # The shared daemon retains the assistant's automatic memory behavior.
                message = {'role': 'assistant', 'content': json.dumps({'history_entry': 'Fixture investigation completed', 'memory_update': ''})}
                reason = 'stop'
            else:
                calls.append(body)
                tool = {'id': 'evidence-call', 'type': 'function', 'function': {'name': tool_name, 'arguments': json.dumps({} if mode in ('cluster_monitor', 'github_hooks') else {'tool_id': 'range', 'arguments': query_args})}}
                if len(calls) == 1:
                    assert tool_name in [t['function']['name'] for t in body['tools']], body.get('tools')
                    if mode in ('cluster_monitor', 'github_hooks'):
                        assert {t['function']['name'] for t in body['tools']} == {tool_name}, body['tools']
                    else:
                        assert not {'monitor_cycle__run', 'github_hooks__run'} & {t['function']['name'] for t in body['tools']}
                    assert not {'shell', 'file_read', 'file_write', 'http_request', 'browser', 'delegate', 'cron_add'} & {t['function']['name'] for t in body['tools']}
                    message = {'role': 'assistant', 'content': None, 'tool_calls': [tool]}
                    reason = 'tool_calls'
                else:
                    message = {'role': 'assistant', 'content': json.dumps({'fixture': 'completed'} if mode in ('cluster_monitor', 'github_hooks') else assessment)}
                    reason = 'stop'
            result = {'id': 'fake-completion', 'object': 'chat.completion', 'created': 1, 'model': 'qwen3.5-9b', 'choices': [{'index': 0, 'message': message, 'finish_reason': reason}], 'usage': {'prompt_tokens': 20, 'completion_tokens': 20, 'total_tokens': 40}}
            if body.get('stream'):
                delta = copy.deepcopy(message)
                for index, call in enumerate(delta.get('tool_calls', [])):
                    call['index'] = index
                chunk = {**result, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}
                ending = {**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': reason}]}
                data = ('data: ' + json.dumps(chunk) + '\n\ndata: ' + json.dumps(ending) + '\n\ndata: [DONE]\n\n').encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        else:
            assert self.path == '/mcp/proxy', self.path
            method = body.get('method')
            if 'id' not in body:
                self.send_response(202)
                self.end_headers()
                return
            if method == 'initialize':
                value = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fixture', 'version': '1'}}
            elif method == 'tools/list':
                value = {'tools': [{'name': 'call_tool', 'description': 'Query read-only evidence', 'inputSchema': {'type': 'object', 'properties': {'tool_id': {'type': 'string'}, 'arguments': {'type': 'object', 'additionalProperties': True}}, 'required': ['tool_id', 'arguments']}}]}
            elif method == 'tools/call':
                assert body['params']['arguments'] == {'tool_id': 'range', 'arguments': query_args}
                value = {'content': [{'type': 'text', 'text': json.dumps({'result': 'counts', 'warnings': None})}], 'structuredContent': {'result': 'counts', 'warnings': None}, 'isError': False}
            else:
                value = {}
            result = {'jsonrpc': '2.0', 'id': body['id'], 'result': value}
        data = json.dumps(result).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


helper = ThreadingHTTPServer(('127.0.0.1', 42618), automation.Handler)
threading.Thread(target=helper.serve_forever, daemon=True).start()
server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    for mode in ('investigator', 'alerts', 'cluster_monitor', 'github_hooks'):
        calls.clear()
        tool_name = {'cluster_monitor': 'monitor_cycle__run', 'github_hooks': 'github_hooks__run'}.get(mode, 'monitor__call_tool')
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o777)
            base = 'http://127.0.0.1:' + str(server.server_port)
            config = (WORKFLOW.parent / 'app/config/config.toml').read_text().replace('http://litellm.ai.svc.cluster.local:4000', base)
            sopdir = Path(directory, 'sops/alertmanager')
            sopdir.mkdir(parents=True)
            shutil.copyfile(WORKFLOW.parent / 'app/config/alertmanager.toml', sopdir / 'SOP.toml')
            config = config.replace('sops_dir = "/sops"', 'sops_dir = "' + directory + '/sops"')
            if mode in ('cluster_monitor', 'github_hooks'):
                # Exercise the actual scheduler tick, not just manual execution.
                start = config.index('[cron.' + mode + ']')
                before, section = config[:start], config[start:]
                section = section.replace('enabled = false', 'enabled = true', 1)
                import re
                at = (datetime.now(timezone.utc) + timedelta(seconds=8)).isoformat()
                section = re.sub(r'schedule = \{[^\n]+\}', 'schedule = { kind = "at", at = "' + at + '" }', section, count=1)
                config = before + section
            Path(directory, 'config.toml').write_text(config)
            stale_lock = Path(directory, 'data/daemon.sock.lock')
            stale_lock.parent.mkdir(parents=True, exist_ok=True)
            stale_lock.touch()
            stale_lock.chmod(0o660)
            automation.CONFIG = Path(directory, 'config.toml')
            automation.WORKFLOW = Path(directory, 'tools')
            automation.WORKFLOW.mkdir()
            for script in ('monitor.py', 'github_hooks.py'):
                (automation.WORKFLOW / script).write_text('print(\'{"fixture": "completed"}\')')
            with socket.socket() as port_socket:
                port_socket.bind(('127.0.0.1', 0))
                port = port_socket.getsockname()[1]
            os.environ['AUTOMATION_STATE_DIR'] = directory + '/evidence'
            environment = dict(os.environ, HOME=directory)
            environment.update({
                'ZEROCLAW_gateway__port': str(port),
                'ZEROCLAW_SOCKET': directory + '/runtime/daemon.sock',
                # Keep the configured channel in schema validation without contacting Discord.
                'ZEROCLAW_channels__discord__main__enabled': 'false',
                'ZEROCLAW_channels__discord__main__bot_token': 'fixture-discord-token',
                'ZEROCLAW_providers__models__openai__monitor__api_key': 'fixture-key',
                'ZEROCLAW_mcp__servers__litellm__headers__authorization': 'Bearer fixture-key',
            })
            environment.update({
                'ZEROCLAW_gateway__host': '127.0.0.1',
                'ZEROCLAW_gateway__webhook_secret': 'Bearer fixture-hook',
                'ZEROCLAW_providers__models__openai__assistant__api_key': 'fixture-key',
                'ZEROCLAW_mcp__servers__monitor__headers__authorization': 'Bearer fixture-key',
            })
            command = ['zeroclaw', '--config-dir', directory, '-v', 'daemon']
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen(command, stdout=output, stderr=output, env=environment)
                try:
                    monitor.ZEROCLAW = 'http://127.0.0.1:' + str(port)
                    for attempt in range(120):
                        if process.poll() is not None:
                            raise RuntimeError('Pinned runtime exited during startup')
                        try:
                            monitor.http(monitor.ZEROCLAW + '/health', timeout=1)
                            break
                        except Exception:
                            time.sleep(0.25)
                    else:
                        raise RuntimeError('Pinned runtime failed readiness')
                    health = monitor.http(monitor.ZEROCLAW + '/health')
                    assert health['runtime']['components']['socket']['status'] == 'ok', health['runtime']['components']['socket']
                    assert stale_lock.stat().st_mode & 0o777 == 0o660
                    assert Path(environment['ZEROCLAW_SOCKET']).is_socket()
                    assert Path(environment['ZEROCLAW_SOCKET'] + '.lock').stat().st_mode & 0o777 == 0o600
                    drift = monitor.http(monitor.ZEROCLAW + '/api/config/drift')
                    assert drift['drifted'] == [], drift
                    loaded = monitor.http(monitor.ZEROCLAW + '/api/config')
                    discord = loaded['channels']['discord']['main']
                    assert discord['enabled'] is False
                    assert discord['mention_only'] is True
                    assert discord['guild_ids'] == ['759169385658056705']
                    assert discord['channel_ids'] == ['1570863482142851103', '1570865870538932226']
                    assert discord['bot_token'] and discord['bot_token'] != 'fixture-discord-token'
                    assert loaded['agents']['main']['channels'] == ['discord.main']
                    assert loaded['peer_groups']['discord_main']['external_peers'] == ['110811348274810880']
                    subprocess.run(['zeroclaw', '--config-dir', directory, 'status', '--format=exit-code'], check=True, env=environment, stdout=subprocess.DEVNULL, stderr=output)
                    if mode == 'alerts':
                        subprocess.run(['zeroclaw', '--config-dir', directory, 'sop', 'validate', 'alertmanager'], check=True, env=environment, stdout=subprocess.DEVNULL, stderr=output)
                        for headers in ({}, {'X-Webhook-Secret': 'wrong'}):
                            try:
                                monitor.http(monitor.ZEROCLAW + '/sop/alertmanager', {'alerts': []}, headers=headers)
                            except HTTPError as error:
                                assert error.code == 401, error.code
                            else:
                                raise AssertionError('Unauthenticated webhook accepted')
                        assert not calls
                        result = monitor.http(monitor.ZEROCLAW + '/sop/alertmanager', {'alerts': [{'status': 'firing', 'labels': {'alertname': 'Fixture'}}]}, headers={'X-Webhook-Secret': 'Bearer fixture-hook'})
                        assert result['results'][0]['status'] == 'started', result
                        for attempt in range(160):
                            runs = monitor.http(monitor.ZEROCLAW + '/api/sops/runs')
                            if any(run.get('status') == 'completed' for run in runs['runs']):
                                break
                            if any(run.get('status') == 'failed' for run in runs['runs']):
                                raise AssertionError(runs)
                            time.sleep(0.25)
                        else:
                            raise AssertionError(runs)
                        assert len(calls) == 2, len(calls)
                        assert Path(directory, 'data/sop/runs.db').is_file()
                    elif mode in ('cluster_monitor', 'github_hooks'):
                        for attempt in range(240):
                            runs = monitor.http(monitor.ZEROCLAW + '/api/cron/' + mode + '/runs')
                            if runs.get('runs'):
                                break
                            time.sleep(0.25)
                        else:
                            raise AssertionError(runs)
                        assert len(calls) == 2, (mode, len(calls), runs)
                        receipt = automation.state.read('dispatch-' + mode)
                        assert receipt['status'] == 'completed', receipt
                        assert receipt['result'] == {'fixture': 'completed'}, receipt
                        assert 'fixture' in json.dumps(runs), runs
                    else:
                        session_id = uuid.uuid4().hex
                        response = monitor.zeroclaw_run({'messages': [{'role': 'user', 'content': 'Query the evidence once, then report the assessment.'}]}, session_id, 90)
                        assert monitor.assessment_json(response['choices'][0]['message']['content']) == assessment
                        activity = monitor.investigation_tools(session_id)
                        assert activity == [{'name': 'monitor__call_tool', 'query_success': True, 'query_arguments': query_args}], activity
                        assert len(calls) == 2, len(calls)
                except Exception:
                    output.seek(0)
                    print(output.read().decode()[-3000:], file=sys.stderr)
                    print([(c.get('model'), [t['function']['name'] for t in c.get('tools', [])], str(c.get('messages', []))[:300]) for c in calls], file=sys.stderr)
                    raise
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
    print('Pinned ZeroClaw profiles, native scheduled tools, evidence persistence, and authenticated alert procedure passed')
finally:
    server.shutdown()
    helper.shutdown()
