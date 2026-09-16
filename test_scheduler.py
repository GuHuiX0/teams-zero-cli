"""Scheduler contract tests; schtasks is never executed."""
import datetime
import importlib
import pathlib
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch


class SchedulerTests(unittest.TestCase):
    def scheduler(self):
        spec = importlib.util.find_spec('msteams_local_cli.scheduler')
        self.assertIsNotNone(spec, 'scheduler adapter is required')
        return importlib.import_module('msteams_local_cli.scheduler')

    def test_xml_preserves_paths_and_safe_interactive_repeat_settings(self):
        scheduler = self.scheduler()
        with tempfile.TemporaryDirectory(prefix='Teams & 中文 ') as directory:
            root = pathlib.Path(directory)
            python = root / 'Python Runtime' / 'python.exe'
            script = root / 'Source & Scripts' / 'teams_cli.py'
            config = root / 'Group Chat' / '团队.json'
            before = datetime.datetime.now().astimezone()
            xml = scheduler.task_xml(python, script, config, 17, 'Teams & 中文')
            tree = ET.fromstring(xml)
            ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
            def value(path):
                return tree.find(path, ns).text
            self.assertEqual(value('t:Actions/t:Exec/t:Command'), str(python.resolve()))
            self.assertEqual(value('t:Actions/t:Exec/t:WorkingDirectory'), str(script.parent.resolve()))
            self.assertEqual(value('t:Actions/t:Exec/t:Arguments'),
                             f'-S "{script.resolve()}" monitor run --config "{config.resolve()}" --scheduled')
            self.assertEqual(value('t:Principals/t:Principal/t:LogonType'), 'InteractiveToken')
            self.assertTrue(value('t:Principals/t:Principal/t:UserId'))
            self.assertEqual(value('t:Settings/t:MultipleInstancesPolicy'), 'IgnoreNew')
            self.assertEqual(value('t:Settings/t:StartWhenAvailable'), 'true')
            self.assertEqual(value('t:Triggers/t:TimeTrigger/t:Repetition/t:Interval'), 'PT17M')
            self.assertIsNone(tree.find('t:Triggers/t:TimeTrigger/t:Repetition/t:Duration', ns))
            start = datetime.datetime.fromisoformat(value('t:Triggers/t:TimeTrigger/t:StartBoundary'))
            self.assertGreater(start, before)
            self.assertLess(start - before, datetime.timedelta(minutes=3))

    def test_rejects_invalid_interval_name_and_path(self):
        scheduler = self.scheduler()
        for interval in (0, -1, True, 1.5, '15', 44641):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                scheduler.task_xml('python.exe', 'teams_cli.py', 'config.json', interval)
        for name in ('', '  ', 'leading ', r'folder\task', '/bad', 'bad*', 'bad\nname'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                scheduler.task_xml('python.exe', 'teams_cli.py', 'config.json', task_name=name)
            with self.subTest(remove=name), self.assertRaises(ValueError):
                scheduler.remove_task(name)
        for path in ('', 'bad"path', 'bad\npath'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                scheduler.task_xml(path, 'teams_cli.py', 'config.json')

    def test_install_passes_real_xml_without_overwrite_and_cleans_up(self):
        scheduler = self.scheduler()
        xml_paths = []
        def execute(args, **kwargs):
            self.assertIsInstance(args, list)
            self.assertEqual(args[:3], ['schtasks.exe', '/Create', '/TN'])
            self.assertEqual(args[3], 'Teams & 中文')
            self.assertNotIn('/F', args)
            self.assertFalse(kwargs.get('shell', False))
            self.assertEqual(kwargs.get('stdin'), subprocess.DEVNULL)
            path = pathlib.Path(args[args.index('/XML') + 1])
            xml_paths.append(path)
            ET.parse(path)
            return subprocess.CompletedProcess(args, 0, 'SUCCESS', '')
        with patch('subprocess.run', side_effect=execute):
            result = scheduler.install_task('python.exe', 'teams_cli.py', 'config.json', task_name='Teams & 中文')
        self.assertEqual(result['task_name'], 'Teams & 中文')
        self.assertEqual(result['status'], 'installed')
        self.assertFalse(xml_paths[0].exists())

    def test_remove_exact_name_and_report_command_failure(self):
        scheduler = self.scheduler()
        def execute(args, **kwargs):
            self.assertEqual(args, ['schtasks.exe', '/Delete', '/TN', 'Teams & 中文', '/F'])
            self.assertFalse(kwargs.get('shell', False))
            return subprocess.CompletedProcess(args, 0, 'SUCCESS', '')
        with patch('subprocess.run', side_effect=execute):
            self.assertEqual(scheduler.remove_task('Teams & 中文')['status'], 'removed')
        with patch('subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'Access denied')):
            with self.assertRaisesRegex(RuntimeError, 'Access denied'):
                scheduler.install_task('python.exe', 'teams_cli.py', 'config.json')


if __name__ == '__main__':
    unittest.main()
