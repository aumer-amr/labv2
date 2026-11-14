"""Native scheduling tools preserve bounds, credentials, and retry safety."""
import json
import os
import runpy
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import tomllib
from http.client import HTTPConnection
import unittest
from unittest.mock import Mock, patch

WORKFLOW = Path(__file__).resolve().parents[3] / 'kubernetes/apps/ai/zeroclaw/workflow'
sys.path.insert(0, str(WORKFLOW))
import server
import state


class ToolsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {'AUTOMATION_STATE_DIR': self.directory.name + '/state',
                                         'PUBLICATION_API_KEY': 'fixture-publisher', 'CLOUDFLARE_WAF_API_TOKEN': 'fixture-cloudflare',
                                         'LANGFUSE_SECRET_KEY': 'fixture-tracing', 'LITELLM_API_KEY': 'never-pass'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = Path(self.directory.name, 'config.toml')
        self.config.write_text('[cron.cluster_monitor]\nenabled=true\n[cron.github_hooks]\nenabled=true\n')
        self.config_patch = patch.object(server, 'CONFIG', self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.work = Path(self.directory.name, 'workflow')
        self.work.mkdir()
        self.work_patch = patch.object(server, 'WORKFLOW', self.work)
        self.work_patch.start()
        self.addCleanup(self.work_patch.stop)
        for file in ('monitor.py', 'github_hooks.py'):
            (self.work / file).write_text('import os,json; print(json.dumps({"environment": sorted(os.environ)}))')

    def call(self, path='/monitor', **params):
        return server.rpc(path, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'run', **params}})['result']

    def test_real_subprocess_uses_only_its_credentials(self):
        for path, present, absent in [('/monitor', 'LANGFUSE_SECRET_KEY', 'CLOUDFLARE_WAF_API_TOKEN'),
                                      ('/github-hooks', 'CLOUDFLARE_WAF_API_TOKEN', 'LANGFUSE_SECRET_KEY')]:
            result = self.call(path)
            self.assertFalse(result['isError'])
            environment = result['structuredContent']['result']['environment']
            self.assertIn(present, environment)
            self.assertIn('PUBLICATION_API_KEY', environment)
            self.assertNotIn(absent, environment)
            self.assertNotIn('LITELLM_API_KEY', environment)
        self.assertEqual(state.read('dispatch-cluster_monitor')['status'], 'completed')

    def test_manual_monitor_runs_with_schedule_disabled_and_keeps_retry_guard(self):
        self.config.write_text('[cron.cluster_monitor]\nenabled=false\n')
        result = self.call()
        self.assertFalse(result['isError'])
        self.assertEqual(state.read('dispatch-cluster_monitor')['status'], 'completed')
        self.assertFalse(tomllib.loads(self.config.read_text())['cron']['cluster_monitor']['enabled'])
        with patch.object(server.subprocess, 'Popen') as spawn:
            self.assertEqual(self.call()['structuredContent']['status'], 'skipped')
        spawn.assert_not_called()

    def test_retries_and_disabled_github_schedule_cannot_reexecute(self):
        self.call()
        with patch.object(server.subprocess, 'Popen') as spawn:
            self.assertEqual(self.call()['structuredContent']['status'], 'skipped')
            self.config.write_text('[cron.github_hooks]\nenabled=false\n')
            self.assertTrue(self.call('/github-hooks')['isError'])
        spawn.assert_not_called()

    def test_concurrent_run_is_rejected(self):
        with state.lock('dispatch-cluster_monitor'), patch.object(server.subprocess, 'Popen') as spawn:
            self.assertTrue(self.call()['isError'])
        spawn.assert_not_called()

    def test_failure_is_persisted_and_error_details_are_not_exposed(self):
        (self.work / 'monitor.py').write_text('raise RuntimeError("fixture-secret")')
        result = self.call()
        self.assertTrue(result['isError'])
        self.assertNotIn('fixture-secret', json.dumps(result))
        self.assertEqual(state.read('dispatch-cluster_monitor')['status'], 'failed')
        self.assertEqual(self.call()['structuredContent']['status'], 'skipped')

    def test_timeout_kills_process_group_and_records_failure(self):
        with patch.object(server.subprocess, 'Popen') as spawn, patch.object(server.os, 'killpg') as kill:
            spawn.return_value.pid = 123
            spawn.return_value.wait.side_effect = [subprocess.TimeoutExpired('fixture', 600), 0]
            self.assertTrue(self.call()['isError'])
            kill.assert_called_once_with(123, server.signal.SIGKILL)
        self.assertEqual(state.read('dispatch-cluster_monitor')['status'], 'timeout')

    def test_arguments_paths_and_tools_are_fixed(self):
        for params in ({'name': 'shell'}, {'arguments': {'command': 'echo nope'}}, {'arguments': None}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.call(**params)
        with self.assertRaises(ValueError):
            self.call('/other')
        with self.assertRaises(ValueError):
            server.rpc('/monitor', {'jsonrpc': '2.0', 'method': 'tools/call', 'params': {'name': 'run'}})
        result = server.rpc('/monitor', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        self.assertEqual([tool['name'] for tool in result['result']['tools']], ['run'])

    def test_uninitialized_state_fails_without_writes(self):
        # Exercise the real workflow against an empty state directory.
        with patch.object(server, 'WORKFLOW', WORKFLOW):
            self.assertTrue(self.call('/github-hooks')['isError'])
        self.assertFalse((state.root() / 'github-hooks.json').exists())


class BoundaryTests(unittest.TestCase):
    def test_mcp_deadlines_fit_config_validation_and_allow_job_cleanup(self):
        config = tomllib.loads((WORKFLOW.parent / 'app/config/config.toml').read_text())
        for entry in config['mcp']['servers']:
            with self.subTest(server=entry['name']):
                self.assertLessEqual(entry['tool_timeout_secs'], 600)
                for path, (_, _, timeout, _) in server.JOBS.items():
                    if entry['url'] == 'http://127.0.0.1:42618' + path:
                        self.assertGreaterEqual(entry['tool_timeout_secs'] - timeout, 60)

    def test_server_initializes_state_before_listening(self):
        with patch.object(state, 'initialize') as initialize, patch('http.server.ThreadingHTTPServer') as http:
            def listen(*args):
                initialize.assert_called_once()
                return Mock()
            http.side_effect = listen
            runpy.run_path(str(WORKFLOW / 'server.py'), run_name='__main__')
            http.assert_called_once()

    def test_browser_origin_wrong_host_and_chunked_requests_are_rejected(self):
        http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'run'}})
            for headers in ({'Host': 'evil.example'}, {'Host': '127.0.0.1:42618', 'Origin': 'https://evil.example'},
                            {'Host': '127.0.0.1:42618', 'Transfer-Encoding': 'chunked'}):
                with patch.object(server, 'run_job') as run:
                    connection = HTTPConnection('127.0.0.1', http.server_port, timeout=2)
                    connection.request('POST', '/monitor', body=body, headers=headers)
                    self.assertEqual(connection.getresponse().status, 400)
                    connection.close()
                run.assert_not_called()
        finally:
            http.shutdown()
            http.server_close()

    def test_profiles_grant_only_their_work_and_jobs_have_one_owner(self):
        config = tomllib.loads((WORKFLOW.parent / 'app/config/config.toml').read_text())
        self.assertFalse(config['cron']['cluster_monitor']['enabled'])
        expected = {'main': 'litellm', 'alerts': 'monitor', 'investigator': 'monitor',
                    'monitor': 'monitor_cycle', 'github_hooks': 'github_hooks'}
        self.assertEqual(set(config['agents']), set(expected))
        for name, bundle in expected.items():
            agent = config['agents'][name]
            self.assertEqual(agent['mcp_bundles'], [bundle, 'memini_' + name])
            self.assertEqual(agent['skill_bundles'], ['baseline'])
            risk = config['risk_profiles'][agent['risk_profile']]
            self.assertEqual(risk['allowed_commands'], [])
            self.assertFalse({'shell', 'http_request', 'file_read', 'file_write', 'cron_add'} & set(risk['allowed_tools']))
            self.assertEqual('delegate' in risk['allowed_tools'], name == 'main')
            if name == 'main':
                self.assertFalse(agent['delegate_same_risk_profile'])
                self.assertEqual(agent['delegates'], [{'agent': 'investigator', 'mode': 'independent'}])
                self.assertEqual(risk['delegation_policy']['mode'], 'allow')
                runtime = config['runtime_profiles'][agent['runtime_profile']]
                self.assertEqual(runtime['max_delegation_depth'], 1)
                self.assertLess(config['delegate']['agentic_timeout_secs'], config['gateway']['request_timeout_secs'])
            else:
                self.assertEqual(agent.get('delegates', []), [])
                self.assertEqual(risk.get('delegation_policy', {'mode': 'forbidden'})['mode'], 'forbidden')
        for job in config['cron']:
            owners = [name for name, agent in config['agents'].items() if job in agent.get('cron_jobs', [])]
            self.assertEqual(len(owners), 1)
            self.assertFalse(config['cron'][job]['enabled'], 'Verify credentials before enabling native jobs')
            self.assertFalse(config['cron'][job]['uses_memory'])
        for name in ('monitor', 'github_hooks'):
            risk = config['risk_profiles'][config['agents'][name]['risk_profile']]
            memory_tools = ['memini_' + name + '__' + tool for tool in ('memory_briefing', 'memory_recall', 'memory_remember')]
            self.assertEqual(risk['allowed_tools'], [expected[name] + '__run'] + memory_tools)
            job = config['agents'][name]['cron_jobs'][0]
            self.assertEqual(config['cron'][job]['allowed_tools'], risk['allowed_tools'])

    def test_discord_restricts_sender_and_locations_without_binding_automation_agents(self):
        config = tomllib.loads((WORKFLOW.parent / 'app/config/config.toml').read_text())
        self.assertEqual(set(config['channels']), {'discord'})
        self.assertEqual(set(config['channels']['discord']), {'main'})
        discord = config['channels']['discord']['main']
        self.assertTrue(discord['enabled'])
        self.assertTrue(discord['mention_only'])
        self.assertEqual(discord['guild_ids'], ['759169385658056705'])
        self.assertEqual(discord['channel_ids'], ['1570863482142851103', '1570865870538932226'])
        self.assertFalse(discord.get('bot_token'))
        for feature in ('archive', 'listen_to_bots', 'slash_commands'):
            self.assertFalse(discord.get(feature, False))
        self.assertEqual(config['peer_groups'], {'discord_main': {
            'channel': 'discord.main', 'agents': ['main'], 'external_peers': ['110811348274810880'],
        }})
        bindings = {name: agent['channels'] for name, agent in config['agents'].items() if agent.get('channels')}
        self.assertEqual(bindings, {'main': ['discord.main']})


if __name__ == '__main__':
    unittest.main()
