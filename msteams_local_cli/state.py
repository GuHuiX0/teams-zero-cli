"""Versioned JSON storage, config-relative paths and crash-released writer locks."""
import contextlib
import json
import os
from pathlib import Path
import tempfile


def read_json(path):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream)


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def save_state(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


class StateLock:
    """An OS file lock released on process exit, including crashes.

    Keep the lock file: deleting it would let another process lock a different
    inode while a writer is still active on POSIX.
    """
    def __init__(self, state_path):
        self.path = Path(str(state_path) + '.lock')
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.stream.write(b'\0')
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise RuntimeError('Another process is using this monitor state') from exc
        return self

    def __exit__(self, *args):
        if self.stream:
            self.stream.close()
            self.stream = None


def load_config(path):
    return validate_config(read_json(path), path)


def validate_config(config, path):
    """Validate in-memory or persisted configuration with portable path rules."""
    path = Path(path).resolve()
    if not isinstance(config, dict) or type(config.get('version')) is not int or config['version'] != 1:
        raise ValueError('Unsupported monitor config; expected version 1')
    config = dict(config)
    for key in ('account', 'conversation_id'):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f'Config requires nonempty {key}')
    for key, default in [('state_path', 'state/dev-team.json'), ('output_path', 'digests'),
                         ('log_path', 'logs/monitor.log')]:
        value = config.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'Invalid config {key}')
        config[key] = str((path.parent / value).resolve())
    if config.get('leveldb'):
        if not isinstance(config['leveldb'], str):
            raise ValueError('Invalid config leveldb')
        config['leveldb'] = str((path.parent / config['leveldb']).resolve())
    def key(value):
        # Configurations must remain valid when copied from POSIX to Windows.
        return Path(str(Path(value).resolve()).casefold())
    files = [key(path), key(str(path) + '.lock'), key(config['state_path']),
             key(config['state_path'] + '.lock'), key(config['state_path'] + '.pending'),
             key(config['log_path'])]
    files.extend(key(config['log_path'] + '.' + str(i)) for i in range(1, 4))
    output = key(config['output_path'])
    if (len(set(files)) != len(files) or output in files
            or any(f in other.parents for f in files for other in [*files, output])):
        raise ValueError('Config, state, log, recovery and output paths must be distinct and usable')
    return config
