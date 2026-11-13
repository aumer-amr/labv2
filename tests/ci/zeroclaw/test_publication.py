"""Approval boundaries, evidence gates, recovery, and shared-state serialization."""
import copy
import importlib.util
import json
import os
import unittest
from contextlib import nullcontext
from pathlib import Path
import sys
WORKFLOW = Path(__file__).resolve().parents[3] / 'kubernetes/apps/ai/zeroclaw/workflow'
sys.path.insert(0, str(WORKFLOW))
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('publication_monitor', WORKFLOW / 'monitor.py')
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)

class PublicationTests(unittest.TestCase):

    def setUp(self):
        self.now = 50000
        self.item = {'id': 'observation', 'check': 'memory_pressure', 'labels': {'namespace': 'home', 'pod': 'minecraft', 'container': 'app'}, 'query': monitor.QUERIES['memory_pressure'], 'status': 'observing', 'consecutive': 5, 'first_seen': 46000, 'last_seen': self.now, 'last_investigated': 49900, 'investigation_job_id': 'collection-job', 'trace_id': 'trace', 'assessment': {'verdict': 'actionable', 'summary': 'Persistent memory pressure', 'evidence': ['Six-hour history'], 'next_step': 'Inspect workload', 'zeroclaw_session_id': 'session', 'tool_activity': [{'query_success': True, 'query_arguments': {'query': '', 'start_time': '28300', 'end_time': '49900', 'step': '30m'}}]}}
        self.item['assessment']['tool_activity'][0]['query_arguments']['query'] = monitor.metric_history(self.item)
        self.state = {'version': 1, 'last_run': {'coverage': 'metrics_complete', 'at': self.now}, 'observations': {self.item['id']: self.item}}
        self.settings = {'mode': 'approval_required', 'paused': False, 'reviewer': 'owner'}
        self.job = {'created_by': 'owner', 'script_path': 'f/cluster/monitor', 'job_kind': 'script'}
        self.saved = []
        self.draft = monitor.draft_for(self.state, self.item, self.now)

    def save(self, state):
        self.state = copy.deepcopy(state)
        self.saved.append(copy.deepcopy(state))

    def test_missing_stale_incomplete_and_failed_evidence_cannot_create_drafts(self):
        cases = [{'status': 'resolved'}, {'consecutive': 2}, {'first_seen': 49900}, {'last_seen': 47000}, {'last_investigated': 20000}, {'investigation_error': 'TimeoutError'}, {'trace_error': 'HTTPError'}, {'trace_id': ''}, {'investigation_job_id': ''}]
        for change in cases:
            with self.subTest(change=change):
                item = {**copy.deepcopy(self.item), **change}
                self.assertIsNone(monitor.draft_for(self.state, item, self.now))
        for verdict in ('expected', 'uncertain'):
            item = copy.deepcopy(self.item)
            item['assessment']['verdict'] = verdict
            self.assertIsNone(monitor.draft_for(self.state, item, self.now))
        for last_run in ({'coverage': 'incomplete', 'at': 50000}, {'coverage': 'metrics_complete', 'at': 40000}):
            self.assertIsNone(monitor.draft_for({**self.state, 'last_run': last_run}, self.item, self.now))

    def test_successful_irrelevant_or_wrong_window_query_cannot_validate_publication(self):
        for change in ({'query': 'sum(up)'}, {'end_time': '45000'}, {'start_time': '49000'}, {'step': '1s'}):
            item = copy.deepcopy(self.item)
            item['assessment']['tool_activity'][0]['query_arguments'].update(change)
            self.assertIsNone(monitor.draft_for(self.state, item, self.now))

    def test_query_history_matches_successful_call_id_and_discards_other_arguments(self):
        arguments = {'tool_id': 'query-tool', 'arguments': {'query': 'sum(up)', 'end_time': '50000', 'start_time': '28400', 'private': 'excluded'}}
        messages = [{'role': 'assistant', 'tool_calls': [{'id': 'call-1', 'function': {'arguments': json.dumps(arguments)}}]}, {'role': 'tool', 'tool_call_id': 'call-1', 'tool_name': 'monitor__call_tool', 'content': '{"result":"samples"}'}]
        with patch.object(monitor, 'session_messages', return_value=messages), patch.dict(os.environ, {'HERMES_API_KEY': 'test'}):
            activity = monitor.investigation_tools('session')
        self.assertTrue(activity[0]['query_success'])
        self.assertEqual(activity[0]['query_arguments'], {'query': 'sum(up)', 'end_time': '50000', 'start_time': '28400'})

    def test_all_metric_histories_scope_the_observed_resource(self):
        cases = {'restarts': {'namespace': 'home', 'pod': 'pod', 'container': 'app'}, 'storage_growth': {'namespace': 'home', 'persistentvolumeclaim': 'minecraft'}, 'flux_not_ready': {'exported_namespace': 'ai', 'name': 'windmill', 'customresource_kind': 'Kustomization'}, 'unavailable_replicas': {'namespace': 'ai', 'deployment': 'windmill-app'}, 'failed_scrapes': {'job': 'prometheus', 'instance': 'host:9090'}}
        for check, labels in cases.items():
            with self.subTest(check=check):
                expression = monitor.metric_history({'check': check, 'query': monitor.QUERIES[check], 'labels': labels})
                for key, value in labels.items():
                    self.assertIn(key + '=' + json.dumps(value), expression)
                self.assertNotIn('{}', expression)

    def test_existing_open_or_done_task_is_reused_without_changes(self):
        for done in (False, True):
            with self.subTest(done=done), patch.object(monitor, 'publication_mcp', side_effect=[{'items': [{'id': 2, 'title': 'LabV2'}]}, {'items': [{'id': 101, 'project_id': 2, 'done': done, 'title': 'Existing [monitor:observation]'}]}]) as mcp:
                self.assertEqual(monitor.publish_draft(self.draft, self.state), 101)
                self.assertEqual([c.args[0] for c in mcp.call_args_list], ['projects_list', 'tasks_list'])

    def test_new_task_uses_resolved_project_and_default_board_bucket(self):
        with patch.object(monitor.time, "time", return_value=self.now), patch.object(monitor, 'publication_mcp', side_effect=[{'items': [{'id': 2, 'title': 'LabV2', 'views': [{'view_kind': 'kanban', 'default_bucket_id': 4}]}]}, {'items': [], 'page': 1, 'per_page': 50, 'total': 0, 'total_pages': 0}, {'id': 101, 'project_id': 2}]) as mcp:
            self.assertEqual(monitor.publish_draft(self.draft, self.state), 101)
        payload = mcp.call_args.args[1]
        self.assertEqual(payload['project'], 2)
        self.assertEqual(payload['bucket_id'], 4)
        self.assertFalse(payload['done'])
        self.assertEqual(payload['description'], self.draft['payload']['description'])

    def test_eligibility_is_refreshed_after_slow_project_and_task_lookups(self):
        for expiry in ('collection', 'observation', 'assessment'):
            with self.subTest(expiry=expiry):
                item = copy.deepcopy(self.item)
                state = copy.deepcopy(self.state)
                state['observations'][item['id']] = item
                if expiry == 'collection':
                    state['last_run']['at'] = self.now - 1799
                elif expiry == 'observation':
                    item['last_seen'] = self.now - 1799
                else:
                    item.update(first_seen=self.now - 25000, last_investigated=self.now - 21600 + 1)
                    item['assessment']['tool_activity'][0]['query_arguments'].update(end_time=str(item['last_investigated']), start_time=str(item['last_investigated'] - 21600))
                draft = monitor.draft_for(state, item, self.now)
                self.assertIsNotNone(draft)
                with patch.object(monitor.time, 'time', return_value=self.now) as clock:
                    def mcp(name, arguments):
                        if name == 'projects_list':
                            return {'items': [{'id': 2, 'title': 'LabV2'}]}
                        if name == 'tasks_list':
                            clock.return_value = self.now + 2
                            return {'items': []}
                        self.fail('Expired evidence reached tasks_create')
                    with patch.object(monitor, 'publication_mcp', side_effect=mcp), self.assertRaisesRegex(ValueError, 'eligible'):
                        monitor.publish_draft(draft, state)

    def test_ambiguous_lookup_never_creates(self):
        for response in ({'items': [], 'total_pages': 2}, {'items': [{'id': 1, 'project_id': 2, 'title': '[monitor:observation]'}, {'id': 2, 'project_id': 2, 'title': '[monitor:observation]'}]}):
            with patch.object(monitor, 'publication_mcp', side_effect=[{'items': [{'id': 2, 'title': 'LabV2'}]}, response]) as mcp, self.assertRaisesRegex(ValueError, 'Ambiguous'):
                monitor.publish_draft(self.draft, self.state)
            self.assertEqual(mcp.call_count, 2)

if __name__ == '__main__':
    unittest.main()
