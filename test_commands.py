"""Public command behavior without discovering private caches."""
import contextlib
import io
import json
import pathlib
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch

import teams_cli
from msteams_local_cli import cli, reader


class Commands(unittest.TestCase):
    def test_monitor_status_without_bootstrap_and_scheduled_failure_log(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            config = root / 'monitor.json'
            config.write_text(json.dumps({'version': 1, 'account': 't:u', 'conversation_id': 'c'}), encoding='utf-8')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = cli.main(['monitor', 'status', '--config', str(config)])
            self.assertEqual(result, 0)
            self.assertFalse(json.loads(output.getvalue())['bootstrap_complete'])
            with contextlib.redirect_stderr(io.StringIO()):
                result = cli.main(['monitor', 'run', '--config', str(config), '--scheduled'])
            self.assertEqual(result, 1)
            self.assertIn('bootstrap', (root / 'logs/monitor.log').read_text(encoding='utf-8'))

    def test_search_sorts_before_limit_and_filters_conversation(self):
        class Source:
            skipped = 0
            def __init__(self, path): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def messages(self, account=None):
                return iter([reader.Message('a','c','r','old','A','2020-01-01T00:00:00Z','bug','text'),
                             reader.Message('a','c','r','new','A','2026-01-01T00:00:00Z','bug','text'),
                             reader.Message('a','other','r','other','A','2027-01-01T00:00:00Z','bug','text')])
        output = io.StringIO()
        with patch.object(cli, 'TeamsCacheReader', Source), contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            result = cli.main(['search', 'bug', '--conversation-id', 'c', '--order', 'newest', '--limit', '1'])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())[0]['message_id'], 'new')

    def test_serve_routes_to_transport_without_logging_stdout(self):
        with patch('msteams_local_cli.mcp_server.serve', return_value=0) as serve:
            self.assertEqual(cli.main(['serve']), 0)
            self.assertEqual(len(serve.call_args.args), 1)

    def test_real_mcp_entrypoint_serves_bound_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            config = pathlib.Path(folder) / 'monitor.json'
            config.write_text(json.dumps({'version': 1, 'account': 't:u',
                'conversation_id': 'dev-chat'}), encoding='utf-8')
            requests = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                    'protocolVersion': '2025-11-25', 'capabilities': {},
                    'clientInfo': {'name': 'integration-test', 'version': '1'}}},
                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
                    'name': 'teams_get_checkpoint', 'arguments': {}}},
            ]
            entry = pathlib.Path(__file__).with_name('teams_cli.py')
            result = subprocess.run([sys.executable, '-S', str(entry), 'serve', '--config', str(config)],
                input=''.join(json.dumps(r) + '\n' for r in requests).encode('utf-8'),
                capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8'))
            responses = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([r['id'] for r in responses], [1, 2])
            checkpoint = responses[1]['result']['structuredContent']
            self.assertEqual(checkpoint['conversation_id'], 'dev-chat')
            self.assertFalse(checkpoint['bootstrap_complete'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
