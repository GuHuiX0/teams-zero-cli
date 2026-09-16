"""Refresh/verify source provenance and build a source-only portable ZIP."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile


TOP_FILES = {'.gitignore', '.gitattributes', 'README.md', 'PROVENANCE.json', 'teams_cli.py'}
SOURCE_DIRS = {'LICENSES', 'msteams_local_cli', 'upstream', 'vendor', 'docs',
               'examples', 'tools', '.github'}


def source_paths(root):
    tracked = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).decode('utf-8').split('\0')
    paths = []
    for name in tracked:
        if not name:
            continue
        path = Path(name)
        if name not in TOP_FILES and path.parts[0] not in SOURCE_DIRS and not (
                len(path.parts) == 1 and name.startswith('test_') and name.endswith('.py')):
            continue
        if '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo'):
            continue
        if (root / path).is_symlink() or not (root / path).is_file():
            raise ValueError('Source must be a regular file: ' + name)
        paths.append(name)
    return sorted(paths)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def refresh(root):
    root = Path(root)
    path = root / 'PROVENANCE.json'
    provenance = json.loads(path.read_text(encoding='utf-8'))
    provenance['files_sha256'] = {name: digest(root / name) for name in source_paths(root)
                                 if name != 'PROVENANCE.json'}
    path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')


def verify(root):
    """Works from a ZIP without Git; compares every listed file byte-for-byte."""
    root = Path(root)
    manifest = json.loads((root / 'PROVENANCE.json').read_text(encoding='utf-8'))['files_sha256']
    failures = []
    for name, expected in manifest.items():
        path = root / name
        if not path.is_file() or digest(path) != expected:
            failures.append(name)
    return sorted(failures)


def build(root, output):
    root, output = Path(root), Path(output)
    failures = verify(root)
    if failures:
        raise ValueError('Provenance mismatch: ' + ', '.join(failures))
    paths = source_paths(root)
    manifest = json.loads((root / 'PROVENANCE.json').read_text(encoding='utf-8'))['files_sha256']
    if set(paths) - {'PROVENANCE.json'} != set(manifest):
        raise ValueError('Staged source set differs from provenance; run --refresh')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in paths:
            archive.write(root / name, name)
    checksum = digest(output)
    output.with_suffix(output.suffix + '.sha256').write_text(checksum + '  ' + output.name + '\n', encoding='utf-8')
    return {'path': str(output.resolve()), 'sha256': checksum, 'files': len(paths)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--refresh', action='store_true')
    action.add_argument('--verify', action='store_true')
    action.add_argument('--build', metavar='ZIP')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    if args.refresh:
        refresh(root)
        print('Provenance refreshed from staged/tracked source files')
    elif args.verify:
        failures = verify(root)
        print(json.dumps({'valid': not failures, 'mismatches': failures}))
        return int(bool(failures))
    else:
        print(json.dumps(build(root, args.build)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
