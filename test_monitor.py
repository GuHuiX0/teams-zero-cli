"""Durable ingestion acceptance tests using synthetic messages only."""
import dataclasses
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import teams_cli
from msteams_local_cli import reader


def message(mid, text='A bug in `Parser` causes a crash', timestamp='2026-09-15T10:00:00Z'):
    return reader.Message('t:u', 'chat', 'chain', str(mid), 'Alice', timestamp, text, 'text')


class SyntheticReader:
    rows = []
    skipped = 0
    diagnostics = {}

    def __init__(self, path=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def accounts(self, account=None, infer_labels=True):
        return [reader.Account('t', 'u')]

    def messages(self, account=None):
        return iter(self.rows)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        from msteams_local_cli.ingest import Monitor
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.config = self.root / 'monitor.json'
        self.config.write_text(json.dumps({'version': 1, 'account': 't:u',
            'conversation_id': 'chat', 'conversation_name': 'Dev Team',
            'state_path': 'state/chat.json', 'output_path': 'digests'}), encoding='utf-8')
        self.monitor = Monitor(self.config)
        self.patcher = patch('msteams_local_cli.ingest.TeamsCacheReader', SyntheticReader)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        SyntheticReader.rows = []

    def test_bootstrap_all_and_noop_replay(self):
        SyntheticReader.rows = [message(i) for i in range(120)]
        result = self.monitor.collect('bootstrap')
        self.assertEqual(result['messages_new'], 120)
        self.assertTrue(self.monitor.status()['bootstrap_complete'])
        batch = self.monitor.get_batch(result['batch_id'], limit=50)
        self.assertEqual(batch['total'], 120)
        self.assertEqual(len(batch['messages']), 50)
        self.assertEqual(batch['next_offset'], 50)
        replay = self.monitor.collect('incremental')
        self.assertIsNone(replay['batch_id'])
        self.assertEqual(len(self.monitor.list_batches()['batches']), 1)
        self.assertEqual(self.monitor.collect('bootstrap')['messages_new'], 0)

    def test_late_arrivals_edits_equal_times_and_eviction(self):
        SyntheticReader.rows = [message('a')]
        self.monitor.collect('bootstrap')
        SyntheticReader.rows = [message('a', 'Fixed the bug'), message('b'),
                                message('late', timestamp='2025-01-01T00:00:00Z')]
        result = self.monitor.collect('incremental')
        self.assertEqual(result['messages_new'], 2)
        self.assertEqual(result['messages_updated'], 1)
        changes = self.monitor.get_batch(result['batch_id'])['messages']
        self.assertEqual([m['message_id'] for m in changes], ['late', 'a', 'b'])
        SyntheticReader.rows = []
        self.assertEqual(self.monitor.collect('incremental')['messages_new'], 0)
        self.assertEqual(self.monitor.status()['messages_stored'], 3)

    def test_output_failure_does_not_advance_state(self):
        SyntheticReader.rows = [message('a')]
        with patch('msteams_local_cli.ingest.write_batch', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.monitor.collect('bootstrap')
        self.assertFalse(self.monitor.status()['bootstrap_complete'])
        self.assertEqual(self.monitor.collect('bootstrap')['messages_new'], 1)

    def test_state_failure_reuses_immutable_batch(self):
        SyntheticReader.rows = [message('a')]
        with patch('msteams_local_cli.ingest.save_state', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                self.monitor.collect('bootstrap')
        files = list((self.root / 'digests').glob('*.json'))
        self.assertEqual(len(files), 1)
        original = files[0].read_bytes()
        SyntheticReader.rows = []  # Cache eviction must not lose the prepared batch.
        result = self.monitor.collect('bootstrap')
        self.assertEqual(files[0].stem, result['batch_id'])
        self.assertEqual(original, files[0].read_bytes())
        self.assertTrue(result['recovered'])
        self.assertEqual(self.monitor.status()['messages_stored'], 1)

    def test_delivery_pending_until_ack_and_receipt_idempotency(self):
        SyntheticReader.rows = [message('a')]
        bid = self.monitor.collect('bootstrap')['batch_id']
        self.assertEqual(self.monitor.list_batches()['batches'][0]['status'], 'pending')
        self.monitor.ack(bid, 'onenote:page-123')
        self.monitor.ack(bid, 'onenote:page-123')
        self.assertEqual(self.monitor.list_batches()['batches'], [])
        with self.assertRaises(ValueError):
            self.monitor.ack(bid, 'different-page')

    def test_ack_recovers_pending_journal_before_recording_receipt(self):
        SyntheticReader.rows = [message('a')]
        first = self.monitor.collect('bootstrap')['batch_id']
        SyntheticReader.rows = [message('a'), message('b')]
        with patch('msteams_local_cli.ingest.save_state', side_effect=OSError('state unavailable')):
            with self.assertRaises(OSError):
                self.monitor.collect('incremental')
        self.monitor.ack(first, 'onenote:page-a')
        self.monitor.collect('incremental')
        state = self.monitor._load()
        self.assertEqual(state['batches'][first]['status'], 'delivered')
        self.assertEqual(state['batches'][first]['receipt'], 'onenote:page-a')

    def test_incremental_requires_bootstrap_and_config_binding(self):
        with self.assertRaises(ValueError):
            self.monitor.collect('incremental')
        self.monitor.collect('bootstrap')
        cfg = json.loads(self.config.read_text(encoding='utf-8'))
        cfg['conversation_id'] = 'other'
        self.config.write_text(json.dumps(cfg), encoding='utf-8')
        from msteams_local_cli.ingest import Monitor
        with self.assertRaises(ValueError):
            Monitor(self.config).status()

    def test_lock_releases_and_rejects_overlap(self):
        from msteams_local_cli.state import StateLock
        path = self.root / 'state.json'
        with StateLock(path):
            with self.assertRaises(RuntimeError):
                with StateLock(path):
                    self.fail('second writer entered')
        with StateLock(path):
            pass

    def test_rules_and_workflow_are_cited_observations(self):
        SyntheticReader.rows = [message('a', 'Bug: `Parser` crashes. I will investigate.'),
                                message('b', '决定使用 SQLite；待办：修复解析错误')]
        bid = self.monitor.collect('bootstrap')['batch_id']
        batch = self.monitor.get_batch(bid)
        self.assertTrue(batch['insights'])
        for insight in batch['insights']:
            self.assertIn(insight['category'], ['bug', 'issue', 'term', 'decision', 'action', 'question'])
            self.assertTrue(insight['evidence'])
            self.assertEqual(insight['evidence'][0]['conversation_id'], 'chat')
        people = self.monitor.workflow()['people']
        self.assertEqual(people[0]['sender'], 'Alice')
        self.assertEqual(people[0]['message_count'], 2)

    def test_unrelated_accounts_do_not_require_deserializing_their_messages(self):
        class Source(SyntheticReader):
            def accounts(self, account=None, infer_labels=True):
                if infer_labels:
                    self.skipped = 1  # A corrupt message in an unrelated account.
                return [reader.Account('t', 'u')]
        SyntheticReader.rows = [message('a')]
        with patch('msteams_local_cli.ingest.TeamsCacheReader', Source):
            self.assertEqual(self.monitor.collect('bootstrap')['messages_new'], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
