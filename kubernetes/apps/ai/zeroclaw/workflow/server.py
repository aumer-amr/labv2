"""Two fixed loopback MCP tools for ZeroClaw's native scheduled agents."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import state

WORKFLOW = Path(__file__).resolve().parent
CONFIG = Path('/config/config.toml')
JOBS = {
    '/monitor': ('cluster_monitor', 'monitor.py', 540, 900),
    '/github-hooks': ('github_hooks', 'github_hooks.py', 240, 3600),
}
COMMON_ENV = ('PATH', 'HOME', 'TZ', 'AUTOMATION_STATE_DIR', 'PUBLICATION_API_KEY')
JOB_ENV = {
    '/monitor': ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'),
    '/github-hooks': ('CLOUDFLARE_WAF_API_TOKEN',),
}


def run_job(path):
    job, script, timeout, interval = JOBS[path]
    # Native cron controls automatic dispatch; monitoring also supports manual runs.
    if job != 'cluster_monitor' and not tomllib.loads(CONFIG.read_text())['cron'][job]['enabled']:
        raise ValueError('Enable the native schedule first')
    # The lock and persisted attempt also cover model retries and daemon restarts.
    with state.lock('dispatch-' + job):
        name = 'dispatch-' + job
        previous = state.read(name) if (state.root() / (name + '.json')).exists() else {}
        now = time.time()
        if now - previous.get('started_at', 0) < interval - 30:
            return {'status': 'skipped', 'reason': 'Already attempted this interval'}
        state.write(name, {'version': 1, 'started_at': now, 'status': 'running'})
        environment = {key: os.environ[key] for key in COMMON_ENV + JOB_ENV[path] if key in os.environ}
        # A subprocess owns the existing locks. Kill its process group on timeout.
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen([sys.executable, str(WORKFLOW / script)], env=environment,
                                       stdout=output, stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                state.write(name, {'version': 1, 'started_at': now, 'status': 'timeout'})
                raise TimeoutError('Automation exceeded its deadline') from None
            output.seek(0)
            data = output.read(16001)
        if process.returncode or len(data) > 16000:
            state.write(name, {'version': 1, 'started_at': now, 'status': 'failed'})
            raise RuntimeError('Automation failed; inspect its retained state')
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError('Invalid automation result')
        state.write(name, {'version': 1, 'started_at': now, 'status': 'completed', 'result': value})
        return {'status': 'completed', 'result': value}


def rpc(path, body):
    if path not in JOBS or not isinstance(body, dict) or body.get('jsonrpc') != '2.0':
        raise ValueError('Invalid MCP request')
    method = body.get('method')
    if method == 'notifications/initialized' and 'id' not in body:
        return None
    if 'id' not in body:
        raise ValueError('Request ID required')
    if method == 'initialize':
        result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'zeroclaw-automation', 'version': '1'}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'run', 'description': 'Run the fixed ' + JOBS[path][0] + ' workflow once. No arbitrary commands or inputs.',
                            'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}]}
    elif method == 'tools/call':
        params = body.get('params', {})
        if not isinstance(params, dict) or params.get('name') != 'run' or params.get('arguments', {}) != {}:
            raise ValueError('Only run with empty arguments is allowed')
        try:
            value = run_job(path)
            result = {'content': [{'type': 'text', 'text': json.dumps(value)}], 'structuredContent': value, 'isError': False}
        except Exception as error:
            # Upstream errors, model text and credentials never enter responses or logs.
            result = {'content': [{'type': 'text', 'text': 'Automation failed: ' + type(error).__name__}], 'isError': True}
    else:
        return {'jsonrpc': '2.0', 'id': body['id'], 'error': {'code': -32601, 'message': 'Method not found'}}
    return {'jsonrpc': '2.0', 'id': body['id'], 'result': result}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200 if self.path == '/health' else 405)
        self.end_headers()

    def do_POST(self):
        try:
            self.connection.settimeout(10)
            if self.headers.get('Origin') or self.headers.get('Transfer-Encoding'):
                raise ValueError('Browser and streaming requests are not accepted')
            if self.headers.get('Host') not in ('127.0.0.1:42618', 'localhost:42618'):
                raise ValueError('Loopback host required')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384:
                raise ValueError('Invalid request length')
            result = rpc(self.path, json.loads(self.rfile.read(length)))
        except (ValueError, TypeError, OSError):
            self.send_error(400, 'Invalid MCP request')
            return
        data = json.dumps(result).encode() if result is not None else b''
        self.send_response(200 if result is not None else 202)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == '__main__':
    state.initialize()
    ThreadingHTTPServer(('127.0.0.1', 42618), Handler).serve_forever()
