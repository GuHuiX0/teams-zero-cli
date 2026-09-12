"""Synthetic-only acceptance tests. Does not discover/read real Teams caches."""
import contextlib
import io
import json
import pathlib
import struct
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import teams_cli
from msteams_local_cli import cli, reader
from ccl_chromium_reader import ccl_chromium_indexeddb as indexeddb
from ccl_chromium_reader.storage_formats import ccl_leveldb
from ccl_chromium_reader.serialization_formats import ccl_v8_value_deserializer as v8
import ccl_simplesnappy


class Acceptance(unittest.TestCase):
    def test_parsers(self):
        self.assertFalse(any('site-packages' in p for p in sys.path))
        self.assertFalse({'brotli', 'zstd', 'mcp'} & sys.modules.keys())
        self.assertEqual(ccl_simplesnappy.decompress(io.BytesIO(b'\x05\x10hello')), b'hello')
        self.assertEqual(indexeddb.read_le_varint(io.BytesIO(b'\xac\x02')), 300)
        self.assertEqual(v8.Deserializer(io.BytesIO(b'\xff\x0f\x22\x05hello'), None).read(), 'hello')
        with tempfile.TemporaryDirectory() as folder:
            batch = struct.pack('<QI', 42, 1) + b'\x01\x03key\x05value'
            crc = ccl_simplesnappy.ccl_simplesnappy.crc32c(b'\x01' + batch)
            crc = (((crc >> 15) | (crc << 17)) + 0xa282ead8) & 0xffffffff
            pathlib.Path(folder, '000001.log').write_bytes(struct.pack('<IHB', crc, len(batch), 1) + batch)
            with ccl_leveldb.RawLevelDb(pathlib.Path(folder)) as db:
                rows = list(db.iterate_records_raw())
                self.assertEqual([(r.key, r.value) for r in rows], [(b'key', b'value')])

    def test_reader_mapping(self):
        r = reader.TeamsCacheReader.__new__(reader.TeamsCacheReader)
        r._skipped = 0
        acc = reader.Account('tenant', 'user')
        value = {'conversationId':'chat', 'replyChainId':'chain', 'messageMap': {
            'one': {'content':'<p>你好 &amp; hello</p>', 'imDisplayName':'Alice (Demo)'},
            'bad': None}}
        store = SimpleNamespace(iterate_records=lambda **kw: iter([SimpleNamespace(value=value)]))
        r._stores = lambda *a: iter([(acc, store)])
        self.assertEqual(r.accounts()[0].label, 'Demo')
        self.assertEqual(list(r.messages())[0].content, '你好 & hello')
        self.assertEqual(list(r.messages(account='other')), [])

    def test_cli_filter_limit_both_option_positions(self):
        class Fake:
            skipped = 0
            def __init__(self, path):
                self.path = path
                assert path == 'synthetic'
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def messages(self, account=None):
                assert account == 't:u'
                for i in range(3):
                    yield reader.Message('t:u','c','r',str(i),'测试','0','你好 HELLO','text')
            def accounts(self): return [reader.Account('t','u'),reader.Account('x','y')]
            def conversations(self, account=None):
                yield {'title': '测试'}
        for arguments in [
            ['--leveldb','synthetic','--limit','1','--account','t:u','search','hello'],
            ['search','hello','--leveldb','synthetic','--limit','1','--account','t:u'],
            ['accounts','--leveldb','synthetic','--limit','1','--account','t:u'],
            ['conversations','--leveldb','synthetic','--limit','1']]:
            output = io.StringIO()
            with patch.object(cli,'TeamsCacheReader',Fake), contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(arguments),0)
            self.assertEqual(len(json.loads(output.getvalue())),1)

    def test_cleanup(self):
        events=[]
        class FakeDB:
            def __init__(self,path): self.path=path
            def close(self):
                self.assertion=pathlib.Path(self.path).is_dir()
                events.append(self.assertion)
        with tempfile.TemporaryDirectory() as source:
            with patch.object(reader._idb,'WrappedIndexDB',FakeDB):
                r=reader.TeamsCacheReader(source)
                copied=r._tmp
                r.close(); r.close()
                self.assertFalse(pathlib.Path(copied).exists())
                self.assertEqual(events,[True])
            real_mkdtemp=tempfile.mkdtemp
            made=[]
            def track(**kw):
                p=real_mkdtemp(**kw); made.append(p); return p
            with patch.object(reader.tempfile,'mkdtemp',track), patch.object(reader._idb,'WrappedIndexDB',side_effect=ValueError('bad fixture')):
                with self.assertRaises(ValueError): reader.TeamsCacheReader(source)
            self.assertTrue(made)
            self.assertTrue(all(not pathlib.Path(p).exists() for p in made))

    def test_subprocess_errors_and_utf8(self):
        entry=str(pathlib.Path(__file__).with_name('teams_cli.py'))
        for args,expected in [(['--help'],0),(['diagnose'],0),(['accounts','--limit','0'],2),(['search','x','--limit','-1'],2),(['accounts','--leveldb','/definitely-missing-synthetic-cache'],1)]:
            result=subprocess.run([sys.executable,'-S',entry,*args],capture_output=True)
            self.assertEqual(result.returncode,expected,result.stderr.decode('utf-8'))
            result.stdout.decode('utf-8')
            if expected: self.assertEqual(result.stdout,b'')

if __name__=='__main__': unittest.main(verbosity=2)
