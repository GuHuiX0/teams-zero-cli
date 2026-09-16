"""Release archive contains only versioned source, never monitoring runtime data."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile


class ReleaseTests(unittest.TestCase):
    def test_manifest_and_archive_exclude_private_runtime_files(self):
        from tools.release import refresh, verify, build
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'teams_cli.py').write_text('print("hello")\n', encoding='utf-8')
            (root / 'PROVENANCE.json').write_text('{"sources": [], "files_sha256": {}}', encoding='utf-8')
            (root / 'state').mkdir()
            (root / 'state/private.json').write_text('private chats', encoding='utf-8')
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
            refresh(root)
            self.assertEqual(verify(root), [])
            output = root / 'dist/source.zip'
            build(root, output)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(sorted(archive.namelist()), ['PROVENANCE.json', 'teams_cli.py'])
            (root / 'teams_cli.py').write_text('changed', encoding='utf-8')
            self.assertEqual(verify(root), ['teams_cli.py'])
            with self.assertRaises(ValueError):
                build(root, output)


if __name__ == '__main__':
    unittest.main()
