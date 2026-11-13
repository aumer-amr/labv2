"""Local state durability, runtime deadlines, and evidence boundaries."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

WORKFLOW = Path(__file__).resolve().parents[3] / 'kubernetes/apps/ai/zeroclaw/workflow'
sys.path.insert(0, str(WORKFLOW))
import monitor
import state
import test_publication


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {'AUTOMATION_STATE_DIR': self.directory.name})
        env.start()
        self.addCleanup(env.stop)

    def test_missing_or_corrupt_state_never_silently_resets(self):
        with self.assertRaisesRegex(ValueError, 'Import'):
            state.read('monitor')
        state.write('monitor', {'version': 2})
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            state.read('monitor')
        (state.root() / 'monitor.json').write_text('broken')
        with self.assertRaises(ValueError):
            state.read('monitor')

    def test_round_trip_and_failed_replace_keep_previous_state(self):
        value = {'version': 1, 'observations': {'workload': 'x' * 20000}}
        state.write('monitor', value)
        self.assertEqual(state.read('monitor'), value)
        with patch.object(state.os, 'replace', side_effect=OSError), self.assertRaises(OSError):
            state.write('monitor', {'version': 1})
        self.assertEqual(state.read('monitor'), value)
        self.assertEqual([p.name for p in state.root().iterdir()], ['monitor.json'])

    def test_lock_excludes_contender_and_releases_after_exception(self):
        with self.assertRaisesRegex(RuntimeError, 'body'):
            with state.lock('monitor'):
                with self.assertRaises(BlockingIOError):
                    with state.lock('monitor'):
                        self.fail('Concurrent owner admitted')
                raise RuntimeError('body')
        with state.lock('monitor'):
            pass

    def test_import_preserves_pending_notifications_and_refuses_overwrite(self):
        data = json.dumps({'version': 1, 'pending': {'id': 'notification', 'until': 123}})
        command = [sys.executable, str(WORKFLOW / 'state.py'), 'github-hooks']
        result = subprocess.run(command, input=data, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state.read('github-hooks')['pending']['id'], 'notification')
        result = subprocess.run(command, input=data, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already exists', result.stderr)


class LocalRunTests(unittest.TestCase):
    body = {'messages': [{'role': 'system', 'content': 'Read only'}, {'role': 'user', 'content': 'Observe'}]}

    def test_run_uses_loopback_explicit_agent_and_session(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'AUTOMATION_STATE_DIR': directory}), patch.object(monitor.session, 'turn', return_value={'response': 'done', 'messages': []}) as request:
            self.assertEqual(monitor.zeroclaw_run(self.body, 'session', 30)['choices'][0]['message']['content'], 'done')
        request.assert_called_once_with(monitor.ZEROCLAW, 'session', 'Read only\n\nObserve', 30)

    def test_lost_response_aborts_and_blocks_later_runs_across_restart(self):
        for abort_result in ({'status': 'aborted'}, TimeoutError):
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'AUTOMATION_STATE_DIR': directory}):
                with self.subTest(abort_result=abort_result), patch.object(monitor.session, 'turn', side_effect=TimeoutError), patch.object(monitor, 'http', side_effect=abort_result if isinstance(abort_result, type) else None, return_value=abort_result) as request, self.assertRaises(monitor.ZeroClawRunNotStopped):
                    monitor.zeroclaw_run(self.body, 'session', 1)
                self.assertEqual(request.call_args.args, (monitor.ZEROCLAW + '/api/sessions/session/abort', {}))
                self.assertEqual(state.read('active-investigation')['session_id'], 'session')
                with patch.object(monitor.session, 'turn') as turn, self.assertRaises(monitor.ZeroClawRunNotStopped):
                    monitor.zeroclaw_run(self.body, 'next-session', 30)
                turn.assert_not_called()
                with patch.object(monitor, 'collect_main') as collect, self.assertRaises(monitor.ZeroClawRunNotStopped):
                    monitor.main()
                collect.assert_not_called()

    def test_evidence_write_failure_keeps_recovery_barrier(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'AUTOMATION_STATE_DIR': directory}):
            original = state.write
            def write(name, value):
                if name.startswith('session-'):
                    raise OSError('disk full')
                original(name, value)
            with patch.object(state, 'write', side_effect=write), patch.object(monitor.session, 'turn', return_value={'response': 'done', 'messages': []}), self.assertRaises(OSError):
                monitor.zeroclaw_run(self.body, 'session', 30)
            self.assertEqual(state.read('active-investigation')['session_id'], 'session')

    def test_invalid_session_and_expired_deadline_never_dispatch(self):
        with patch.object(monitor, 'http') as request:
            with self.assertRaises(ValueError):
                monitor.zeroclaw_run(self.body, '../session', 30)
            with self.assertRaises(TimeoutError):
                monitor.zeroclaw_run(self.body, 'session', 0)
        request.assert_not_called()

    def test_saved_evidence_survives_reader_restart_and_rejects_missing_state(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'AUTOMATION_STATE_DIR': directory}):
            with self.assertRaises(ValueError):
                monitor.session_messages('session')
            state.write('session-session', {'version': 1, 'messages': [{'role': 'tool'}]})
            self.assertEqual(monitor.session_messages('session'), [{'role': 'tool'}])
            with self.assertRaises(ValueError):
                monitor.session_messages('../monitor')

    def test_retention_removes_only_old_raw_session_evidence(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'AUTOMATION_STATE_DIR': directory}):
            for name in ('monitor', 'session-old', 'session-new'):
                state.write(name, {'version': 1})
            os.utime(state.root() / 'monitor.json', (1, 1))
            os.utime(state.root() / 'session-old.json', (1, 1))
            monitor.prune_evidence(40 * 86400)
            self.assertEqual({p.name for p in state.root().iterdir()}, {'monitor.json', 'session-new.json'})

    def test_structured_mcp_results_and_errors(self):
        for value, valid in [({'result': 'counts'}, True), ({'result': [], 'warnings': ['partial']}, False), ({'error': True}, False)]:
            self.assertEqual(monitor.successful_query({'role': 'tool', 'tool_name': 'monitor__call_tool', 'content': 'structuredContent:\n' + json.dumps(value)}), valid)


class AutomaticPublicationTests(unittest.TestCase):
    def setUp(self):
        fixture = test_publication.PublicationTests()
        fixture.setUp()
        self.state, self.now, self.draft = fixture.state, fixture.now, fixture.draft
        self.stored = copy.deepcopy(self.state)
        clock = patch.object(monitor.time, 'time', return_value=self.now)
        self.clock = clock.start()
        self.addCleanup(clock.stop)

    def save(self, value):
        self.stored = copy.deepcopy(value)

    def run_publication(self):
        with patch.object(monitor, 'save', side_effect=self.save), patch.object(monitor, 'load_state', side_effect=lambda: copy.deepcopy(self.stored)):
            return monitor.publish_ready(self.state)

    def test_persisted_intent_precedes_automatic_write_and_repeat_is_noop(self):
        def publish(draft, state):
            record = self.stored['publication_audit'][draft['hash']]
            self.assertEqual(record['status'], 'pending')
            self.assertEqual(record['payload'], draft['payload'])
            return 75
        with patch.object(monitor, 'publish_draft', side_effect=publish) as publish:
            self.assertEqual(self.run_publication(), 1)
            self.assertEqual(self.run_publication(), 0)
            publish.assert_called_once()
        self.assertEqual(self.stored['publication_audit'][self.draft['hash']]['task_id'], 75)

    def test_persistence_failure_and_expired_evidence_prevent_external_write(self):
        with patch.object(monitor, 'save', side_effect=OSError), patch.object(monitor, 'publish_draft') as publish, self.assertRaises(OSError):
            monitor.publish_ready(self.state)
        publish.assert_not_called()
        self.clock.return_value = self.now + 22000
        with patch.object(monitor, 'publish_draft') as publish:
            self.assertEqual(monitor.publish_ready(self.state), 0)
        publish.assert_not_called()

    def test_lost_write_response_retains_intent_and_retries_same_identity(self):
        with patch.object(monitor, 'publish_draft', side_effect=[TimeoutError, 75]) as publish:
            with self.assertRaises(TimeoutError):
                self.run_publication()
            self.assertEqual(self.stored['publication_audit'][self.draft['hash']]['status'], 'pending')
            self.assertEqual(self.run_publication(), 1)
            self.assertEqual(publish.call_args_list[0], publish.call_args_list[1])

    def test_later_draft_expires_while_earlier_task_is_published(self):
        later = copy.deepcopy(self.state['observations']['observation'])
        later.update(id='later', first_seen=self.now - 25000, last_investigated=self.now - 21600 + 1)
        query = later['assessment']['tool_activity'][0]['query_arguments']
        query.update(end_time=str(later['last_investigated']), start_time=str(later['last_investigated'] - 21600))
        self.state['observations']['later'] = later
        self.assertIsNotNone(monitor.draft_for(self.state, later, self.now))
        with patch.object(monitor.time, 'time', return_value=self.now) as clock:
            def publish(draft, state):
                clock.return_value = self.now + 2
                return 75
            with patch.object(monitor, 'publish_draft', side_effect=publish) as publish:
                self.assertEqual(self.run_publication(), 1)
            self.assertEqual(publish.call_count, 1)

    def test_expiry_during_intent_persistence_blocks_creation(self):
        def save(value):
            self.save(value)
            self.clock.return_value = self.now + 1801
        replies = [{'items': [{'id': 2, 'title': 'LabV2'}]}, {'items': []}]
        with patch.object(monitor, 'save', side_effect=save), patch.object(monitor, 'load_state', side_effect=lambda: copy.deepcopy(self.stored)), patch.object(monitor, 'publication_mcp', side_effect=replies) as mcp:
            with self.assertRaisesRegex(ValueError, 'eligible'):
                monitor.publish_ready(self.state)
        self.assertEqual([call.args[0] for call in mcp.call_args_list], ['projects_list', 'tasks_list'])
        self.assertEqual(self.stored['publication_audit'][self.draft['hash']]['status'], 'pending')

    def test_existing_human_rejection_survives_migration(self):
        self.state['publication_audit'] = {self.draft['hash']: {'status': 'rejected'}}
        with patch.object(monitor, 'publish_draft') as publish:
            self.assertEqual(self.run_publication(), 0)
        publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
