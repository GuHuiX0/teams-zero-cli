"""Reader correctness tests using synthetic bytes and temporary directories only."""
import pathlib
import struct
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import teams_cli  # sets up the bundled parser imports
from msteams_local_cli import reader
from ccl_chromium_reader import ccl_chromium_indexeddb as idb
from ccl_chromium_reader.storage_formats import ccl_leveldb as leveldb


def raw(seq, value, *, deleted=False, table=False):
    key = idb.IndexedDb.make_prefix(1, 1, 1) + b'\x01\x01\x00a'
    state = leveldb.KeyState.Deleted if deleted else leveldb.KeyState.Live
    if table:
        return leveldb.Record.ldb_record(key + struct.pack('<Q', seq << 8 | state.value), value, 'fake.ldb', 0, False)
    return leveldb.Record.log_record(key, value, seq, state, 'fake.log', 0)


def value(text):
    data = text.encode('ascii')
    return b'\x01\xff\x0f\xff\x0f\x22' + bytes([len(data)]) + data


class ReaderCorrectness(unittest.TestCase):
    def test_accounts_metadata_only_does_not_deserialize_unrelated_records(self):
        r = reader.TeamsCacheReader.__new__(reader.TeamsCacheReader)
        def fail(**kwargs):
            self.fail('metadata-only account discovery decoded messages')
        r._stores = lambda *args: iter([(reader.Account('t', 'u'), SimpleNamespace(iterate_records=fail)),
                                      (reader.Account('t', 'other'), SimpleNamespace(iterate_records=fail))])
        self.assertEqual([a.key for a in r.accounts(account='t:u', infer_labels=False)], ['t:u'])
    def test_empty_body_is_a_message_and_can_become_an_edit(self):
        r = reader.TeamsCacheReader.__new__(reader.TeamsCacheReader)
        r._skipped = 0
        r._diagnostic_counts = {}
        acc = reader.Account('t', 'u')
        store = SimpleNamespace(iterate_records=lambda **kw: iter([SimpleNamespace(
            value={'conversationId': 'c', 'messageMap': {'m': {'content': '<img src="x">'}}})]))
        r._stores = lambda *args: iter([(acc, store)])
        rows = list(r.messages())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].message_id, 'm')
        self.assertEqual(rows[0].content, '')
    def scan(self, rows, live_only=True, bad=None):
        db = idb.IndexedDb.__new__(idb.IndexedDb)
        db._fetched_records = rows
        return list(db.iterate_records(1, 1, live_only=live_only, bad_deserializer_data_handler=bad))

    def test_latest_sequence_wins_across_log_and_table(self):
        rows = [raw(9, value('new'), table=True), raw(2, value('old'))]
        self.assertEqual([r.value for r in self.scan(rows)], ['new'])
        self.assertEqual([r.value for r in self.scan(rows, False)], ['new', 'old'])

    def test_tombstone_suppresses_older_live_value(self):
        self.assertEqual(self.scan([raw(2, value('old')), raw(9, b'', deleted=True)]), [])

    def test_corrupt_latest_does_not_resurrect_old_and_is_counted(self):
        failures = []
        self.assertEqual(self.scan([raw(2, value('old')), raw(9, b'\x01\xff')], bad=lambda *args: failures.append(args)), [])
        self.assertEqual(len(failures), 1)

    def test_html_boundaries_and_escaped_literals(self):
        self.assertEqual(reader._text('<p>A &lt;tag&gt;</p><p>B<br>C &amp; D</p>'), 'A <tag>\nB\nC & D')
        self.assertEqual(reader._text('2 < 3 and 5 > 4'), '2 < 3 and 5 > 4')

    def test_plain_text_message_preserves_literal_tags_and_entities(self):
        r = reader.TeamsCacheReader.__new__(reader.TeamsCacheReader)
        r._skipped = 0
        store = SimpleNamespace(iterate_records=lambda **kw: iter([SimpleNamespace(value={
            'messageMap': {'one': {'content': 'use <tag> &amp; literally', 'contentType': 'text'}}})]))
        r._stores = lambda *args: iter([(reader.Account('t', 'u'), store)])
        self.assertEqual(list(r.messages())[0].content, 'use <tag> &amp; literally')

    def test_latest_empty_value_counts_as_malformed_without_reviving_old(self):
        failures = []
        self.assertEqual(self.scan([raw(1, value('old')), raw(2, b'')], bad=lambda *args: failures.append(args)), [])
        self.assertEqual(len(failures), 1)

    def test_account_scan_reports_unsupported_message_schema(self):
        r = reader.TeamsCacheReader.__new__(reader.TeamsCacheReader)
        r._skipped = 0
        store = SimpleNamespace(iterate_records=lambda **kw: iter([SimpleNamespace(value={'unexpected': {}})]))
        r._stores = lambda *args: iter([(reader.Account('t', 'u'), store)])
        self.assertEqual(r.accounts()[0].key, 't:u')
        self.assertEqual(r.diagnostics['skipped_reasons'], {'invalid_message_map': 1})

    def test_discovery_unique_directories_and_ambiguity(self):
        with tempfile.TemporaryDirectory() as folder:
            base = pathlib.Path(folder)
            (base / 'b').mkdir()
            (base / 'a').mkdir()
            (base / 'file').write_text('not a cache')
            with patch.object(reader, 'default_cache_globs', return_value=[str(base / '*'), str(base / 'a')]):
                # Windows CI may supply an 8.3 alias in the temp directory.
                self.assertEqual(reader.find_caches(),
                                 [str((base / 'a').resolve()), str((base / 'b').resolve())])
                with self.assertRaisesRegex(ValueError, 'ambiguous|multiple|Multiple'):
                    reader.find_cache()

    def test_public_readers_request_live_only(self):
        r = reader.TeamsCacheReader.__new__(reader.TeamsCacheReader)
        r._skipped = 0
        def records(**kwargs):
            if not kwargs.get('live_only'):
                return iter([SimpleNamespace(value={'messageMap': {'old': {'content': 'old'}}})])
            return iter([])
        r._stores = lambda *args: iter([(reader.Account('t', 'u'), SimpleNamespace(iterate_records=records))])
        self.assertEqual(list(r.messages()), [])
        self.assertEqual(list(r.conversations()), [])
        self.assertEqual(list(r.mentions()), [])
        self.assertEqual(r.accounts()[0].label, '')

    def test_diagnostics_distinguish_empty_supported_store_and_missing_schema(self):
        r = reader.TeamsCacheReader.__new__(reader.TeamsCacheReader)
        r._skipped = 0
        r._db = SimpleNamespace(database_ids=[])
        self.assertEqual(list(r.messages()), [])
        self.assertIn('replychain-manager/replychains', r.diagnostics['missing_stores'])
        store = SimpleNamespace(iterate_records=lambda **kw: iter([SimpleNamespace(value={'messageMap': 17}), SimpleNamespace(value=None)]))
        db = SimpleNamespace(get_object_store_by_name=lambda name: store)
        class Wrapped:
            database_ids = [SimpleNamespace(name='Teams:replychain-manager:react-web-client:t:u:en', dbid_no=1)]
            def __getitem__(self, key): return db
        r._db = Wrapped()
        self.assertEqual(list(r.messages()), [])
        self.assertEqual(r.diagnostics['stores_found']['replychain-manager/replychains'], 1)
        self.assertNotIn('replychain-manager/replychains', r.diagnostics['missing_stores'])
        self.assertEqual(r.skipped, 2)
        self.assertEqual(r.diagnostics['skipped_reasons'], {'invalid_message_map': 1, 'invalid_record': 1})


if __name__ == '__main__':
    unittest.main()
