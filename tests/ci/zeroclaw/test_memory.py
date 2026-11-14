"""Memory routes cannot override namespaces or write shared context."""
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / 'kubernetes/apps/ai/zeroclaw/workflow'
sys.path.insert(0, str(WORKFLOW))
import memory
import server

spec = importlib.util.spec_from_file_location('bootstrap', ROOT / 'scripts/zeroclaw-bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.config = WORKFLOW.parent / 'app/config/config.toml'
        self.patch = patch.object(memory, 'CONFIG', self.config)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.env = patch.dict(os.environ, {'MEMINI_API_KEY': 'fixture-secret'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def rpc(self, name, arguments, agent='main'):
        return server.rpc('/memory/' + agent, {'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
                                             'params': {'name': name, 'arguments': arguments}})['result']

    def test_every_agent_has_only_its_memory_endpoint_and_required_tools(self):
        config = tomllib.loads(self.config.read_text())
        for agent, settings in config['agents'].items():
            memory_servers = [s for bundle in settings['mcp_bundles'] for s in config['mcp_bundles'][bundle]['servers'] if s.startswith('memini_')]
            self.assertEqual(memory_servers, ['memini_' + agent])
            connection = next(s for s in config['mcp']['servers'] if s['name'] == memory_servers[0])
            self.assertEqual(connection['url'], 'http://127.0.0.1:42618/memory/' + agent)
            risk = config['risk_profiles'][settings['risk_profile']]
            for tool in memory.TOOLS:
                self.assertIn('memini_' + agent + '__' + tool['name'], risk['allowed_tools'])
            self.assertEqual(settings['skill_bundles'], ['baseline'])

    def test_writes_are_agent_scoped_and_sse_is_decoded(self):
        response = 'event: message\ndata: ' + json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'structuredContent': {'stored': True}}}) + '\n\n'
        with patch.object(memory, 'http', return_value=response) as request:
            for agent in tomllib.loads(self.config.read_text())['agents']:
                result = self.rpc('memory_remember', {'content': 'A confirmed fact', 'tags': ['test']}, agent)
                self.assertTrue(result['structuredContent']['stored'])
                args, kwargs = request.call_args
                self.assertEqual(kwargs['headers']['X-Memini-Namespace'], 'labv2/zeroclaw/' + agent)
                self.assertEqual(args[1]['params']['arguments'], {'content': 'A confirmed fact', 'tags': ['test'], 'visibility': 'project', 'tier': 'semantic'})
                self.assertEqual(args[2], 'fixture-secret')

    def test_unsafe_arguments_and_operations_never_reach_memini(self):
        with patch.object(memory, 'http') as request:
            for name, args in [('memory_remember', {'content': 'x', 'visibility': 'labv2'}),
                               ('memory_remember', {'content': 'x', 'id': 'foreign-id'}),
                               ('memory_remember', {'content': 'x', 'namespace': 'labv2'}),
                               ('memory_remember', {'content': 'x', 'tags': [1]}),
                               ('memory_recall', {'query': 'x', 'scope': 'everywhere'}),
                               ('memory_recall', {'query': 'x', 'limit': True}),
                               ('memory_recall', {'query': 'x', 'limit': 6}),
                               ('memory_briefing', {'scope': 'everywhere'}),
                               ('memory_forget', {'id': 'foreign-id'}), ('memory_remember', [])]:
                with self.subTest(name=name, args=args):
                    self.assertTrue(self.rpc(name, args)['isError'])
            request.assert_not_called()

    def test_unconfigured_or_traversing_agents_are_rejected(self):
        for agent in ('unknown', '../main', 'main?namespace=other'):
            with self.assertRaises(ValueError):
                self.rpc('memory_briefing', {}, agent)

    def test_reads_include_shared_context_without_scope_override(self):
        with patch.object(memory, 'http', return_value={'id': 1, 'result': {}}) as request:
            self.rpc('memory_recall', {'query': 'past decisions'})
            self.assertEqual(request.call_args.args[1]['params']['arguments'],
                             {'query': 'past decisions', 'limit': 3, 'scope': 'full', 'response_format': 'concise'})
            self.rpc('memory_briefing', {})
            self.assertEqual(request.call_args.args[1]['params']['arguments'], {'scope': 'full'})

    def test_errors_do_not_expose_upstream_credentials_or_content(self):
        with patch.object(memory, 'http', side_effect=RuntimeError('fixture-secret private-content')):
            result = self.rpc('memory_briefing', {})
            self.assertTrue(result['isError'])
            self.assertNotIn('fixture-secret', json.dumps(result))
            self.assertNotIn('private-content', json.dumps(result))


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name, 'install')
        self.root.mkdir()
        self.source = Path(self.directory.name, 'config.toml')
        self.source.write_text('schema_version = 4\n')
        content = '---\nname: fixture\ndescription: Fixture\nalways: true\n---\nFixture instructions.\n'
        self.bundle = {'name': 'zeroclaw-policy', 'version': 1, 'files': [
            {'path': path, 'content': content, 'sha256Hash': base64.b64encode(hashlib.sha256(content.encode()).digest()).decode()}
            for path in bootstrap.FILES]}

    def test_pinned_skills_install_and_validated_cache_survives_outage(self):
        with patch.object(bootstrap.private_context, 'request', return_value=self.bundle) as request:
            bootstrap.install(self.root, self.source, 1)
            self.assertIn('version=1', request.call_args.args[0])
        for target in bootstrap.FILES.values():
            path = self.root / 'shared/skills/baseline' / target
            self.assertIn('always: true', path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with patch.object(bootstrap.private_context, 'request', side_effect=bootstrap.private_context.Unavailable()):
            bootstrap.install(self.root, self.source, 1)
            with self.assertRaises(ValueError):
                bootstrap.install(self.root, self.source, 2)

    def test_invalid_bundle_never_replaces_configuration(self):
        for mutation in ('checksum', 'path', 'version', 'always'):
            bundle = copy.deepcopy(self.bundle)
            if mutation == 'checksum': bundle['files'][0]['sha256Hash'] = 'bad'
            elif mutation == 'path': bundle['files'][0]['path'] = '../escaped'
            elif mutation == 'version': bundle['version'] = 2
            else: bundle['files'][0]['content'] = bundle['files'][0]['content'].replace('always: true', 'always: false')
            with patch.object(bootstrap.private_context, 'request', return_value=bundle), self.assertRaises(ValueError):
                bootstrap.install(self.root, self.source, 1)
            self.assertFalse((self.root / 'config.toml').exists())

    def test_corrupt_cache_cannot_bypass_outage_validation(self):
        self.bundle['files'][0]['content'] += 'tampered'
        (self.root / 'private-skills.json').write_text(json.dumps(self.bundle))
        with patch.object(bootstrap.private_context, 'request', side_effect=bootstrap.private_context.Unavailable()), self.assertRaises(ValueError):
            bootstrap.install(self.root, self.source, 1)


if __name__ == '__main__':
    unittest.main()
