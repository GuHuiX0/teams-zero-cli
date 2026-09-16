"""Shared service acceptance tests using a synthetic cache reader."""
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import teams_cli  # Set up bundled parser dependencies, including under -S.
from msteams_local_cli.reader import Account, Message


class FakeSource:
    diagnostics = {'malformed_records': 0}
    skipped = 0
    opened = []
    conversations_data = [
        {'account': 't:u', 'id': 'chat2', 'title': 'Developers extra'},
        {'account': 't:u', 'id': 'chat', 'title': 'Developers'},
        {'account': 't:v', 'id': 'other', 'title': 'Developers'}]

    def __init__(self, path=None):
        self.opened.append(path)
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def accounts(self, account=None, infer_labels=True):
        return [a for a in [Account('t', 'v', 'Two'), Account('t', 'u', 'One')]
                if account is None or a.key == account]
    def conversations(self, account=None):
        return iter(c for c in self.conversations_data if not account or c['account'] == account)
    def messages(self, account=None):
        rows = [Message('t:u', 'chat', 'r', '2', 'Bob', '2026-09-15T12:00:00Z', 'Issue?', 'text'),
                Message('t:u', 'chat', 'r', '1', 'Alice', '2026-09-15T11:00:00Z', 'Hello', 'text'),
                Message('t:v', 'other', 'r', '3', 'Other', '2026-09-15T10:00:00Z', 'No', 'text')]
        return iter(r for r in rows if not account or r.account == account)
    def mentions(self, account=None):
        return iter([{'account': 't:u', 'conversation_id': 'chat', 'message_id': '2',
                      'timestamp': '2026-09-15T12:00:00Z', 'is_read': False}])


class ServiceTests(unittest.TestCase):
    def service(self):
        self.assertIsNotNone(importlib.util.find_spec('msteams_local_cli.service'), 'service required')
        return importlib.import_module('msteams_local_cli.service')

    def test_read_pages_sort_and_filter_and_include_diagnostics(self):
        module = self.service()
        with patch.object(module, 'TeamsCacheReader', FakeSource):
            service = module.TeamsService()
            result = service.call('teams_list_accounts', {'limit': 1})
            self.assertEqual(result['accounts'][0]['key'], 't:u')
            self.assertEqual(result['next_offset'], 1)
            self.assertEqual(result['diagnostics']['malformed_records'], 0)
            result = service.call('teams_load_messages', {'conversation_id': 'chat', 'limit': 1})
            self.assertEqual([m['message_id'] for m in result['messages']], ['1'])
            self.assertEqual(result['total'], 2)
            result = service.call('teams_load_messages', {'conversation_id': 'chat', 'mode': 'since',
                                                        'after': '2026-09-15T11:00:00Z'})
            self.assertEqual([m['message_id'] for m in result['messages']], ['2'])
            self.assertEqual(service.call('teams_get_mentions', {'conversation_id': 'other'})['total'], 0)

    def test_resolution_is_exact_casefold_and_rejects_cross_account_ambiguity(self):
        module = self.service()
        with patch.object(module, 'TeamsCacheReader', FakeSource):
            service = module.TeamsService()
            with self.assertRaisesRegex(ValueError, 'ambiguous'):
                service.call('teams_resolve_conversation', {'name': 'Developers'})
            result = service.call('teams_resolve_conversation', {'name': 'DEVELOPERS', 'account': 't:u'})
            self.assertEqual(result['conversation']['id'], 'chat')
            with self.assertRaises(ValueError):
                service.call('teams_resolve_conversation', {'name': 'Develop', 'account': 't:u'})

    def test_runtime_validation_precedes_cache_access_and_rejects_overrides(self):
        module = self.service()
        FakeSource.opened.clear()
        with patch.object(module, 'TeamsCacheReader', FakeSource):
            service = module.TeamsService()
            cases = [('teams_load_messages', {}), ('teams_load_messages', {'conversation_id': 1}),
                     ('teams_load_messages', {'conversation_id': 'chat', 'mode': 'since'}),
                     ('teams_load_messages', {'conversation_id': 'chat', 'mode': 'since', 'after': '123'}),
                     ('teams_list_accounts', {'limit': True}), ('teams_list_accounts', {'offset': -1}),
                     ('teams_list_accounts', {'limit': 1001}), ('teams_list_accounts', {'unexpected': 1}),
                     ('teams_bootstrap', {'config_path': 'other.json'}), ('teams_bootstrap', {})]
            for name, args in cases:
                with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                    service.call(name, args)
            self.assertEqual(FakeSource.opened, [])

    def test_tool_schema_annotations_are_complete(self):
        module = self.service()
        tools = module.TeamsService().tools
        self.assertEqual(len(tools), 12)
        for tool in tools:
            self.assertEqual(tool['inputSchema']['type'], 'object')
            self.assertIs(tool['inputSchema']['additionalProperties'], False)
            self.assertTrue(tool['description'])
            self.assertEqual(tool['annotations']['readOnlyHint'], tool['name'] not in {
                'teams_bootstrap', 'teams_ingest', 'teams_ack_batch'})
        json.dumps(tools)

    def test_configure_creates_atomic_bound_config_refuses_overwrite(self):
        module = self.service()
        with tempfile.TemporaryDirectory() as directory, patch.object(module, 'TeamsCacheReader', FakeSource):
            path = Path(directory) / 'monitor.json'
            with self.assertRaises(ValueError):
                module.configure_monitor(path, conversation='Developers')
            result = module.configure_monitor(path, conversation='developers', account='t:u', leveldb='synthetic')
            config = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(config['conversation_id'], 'chat')
            self.assertEqual(config['state_path'], 'state/dev-team.json')
            self.assertEqual(config['leveldb'], str(Path('synthetic').resolve()))
            self.assertEqual(result['config'], config)
            with self.assertRaises(FileExistsError):
                module.configure_monitor(path, conversation_id='chat', account='t:u')
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), config)
            service = module.TeamsService(path)
            self.assertEqual(service.call('teams_list_conversations', {})['total'], 2)
            self.assertEqual(FakeSource.opened[-1], config['leveldb'])

    def test_config_rejects_case_insensitive_state_log_and_config_collisions(self):
        from msteams_local_cli.state import load_config
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'Monitor.json'
            path.write_text(json.dumps({'version': 1, 'account': 't:u',
                'conversation_id': 'c', 'state_path': 'STATE.json',
                'log_path': 'state.JSON'}), encoding='utf-8')
            with self.assertRaises(ValueError):
                load_config(path)

    def test_config_rejects_reserved_files_and_file_directory_overlaps(self):
        from msteams_local_cli.state import load_config
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'monitor.json'
            for extra in ({'log_path': 'MONITOR.JSON'},
                          {'state_path': 's.json', 'log_path': 'S.JSON.pending'},
                          {'state_path': 's.json', 'output_path': 's.json'},
                          {'state_path': 's.json', 'log_path': 's.json/monitor.log'}):
                path.write_text(json.dumps({'version': 1, 'account': 't:u',
                    'conversation_id': 'c', **extra}), encoding='utf-8')
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    load_config(path)

    def test_bound_writes_batch_ack_checkpoint_and_workflow_use_real_monitor(self):
        module = self.service()
        with tempfile.TemporaryDirectory() as directory, patch.object(module, 'TeamsCacheReader', FakeSource), \
                patch('msteams_local_cli.ingest.TeamsCacheReader', FakeSource):
            path = Path(directory) / 'monitor.json'
            module.configure_monitor(path, conversation_id='chat', account='t:u', leveldb='synthetic')
            service = module.TeamsService(path)
            boot = service.call('teams_bootstrap', {})
            self.assertEqual(boot['messages_new'], 2)
            self.assertEqual(service.call('teams_ingest', {})['messages_new'], 0)
            self.assertEqual(service.call('teams_get_checkpoint', {})['messages_stored'], 2)
            bid = boot['batch_id']
            self.assertEqual(service.call('teams_list_batches', {})['batches'][0]['batch_id'], bid)
            self.assertEqual(len(service.call('teams_get_batch', {'batch_id': bid, 'limit': 1})['messages']), 1)
            self.assertEqual(service.call('teams_ack_batch', {'batch_id': bid, 'receipt': 'note:123'})['status'], 'delivered')
            self.assertEqual(service.call('teams_list_batches', {})['total'], 0)
            workflow = service.call('teams_analyze_workflow', {'limit': 1})
            self.assertEqual(workflow['total'], 2)
            self.assertEqual(workflow['people'][0]['sender'], 'Alice')

    def test_configure_rejects_unusable_storage_paths_before_creating_config(self):
        module = self.service()
        with tempfile.TemporaryDirectory() as directory, patch.object(module, 'TeamsCacheReader', FakeSource):
            path = Path(directory) / 'monitor.json'
            for args in ({'state_path': None}, {'output_path': None},
                         {'state_path': 'logs/monitor.log'}, {'state_path': 'monitor.json'}):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    module.configure_monitor(path, conversation_id='chat', account='t:u', **args)
                self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
