"""Explicit Windows Task Scheduler registration for local collection only."""
from __future__ import annotations

import datetime
import getpass
import os
from pathlib import Path
import subprocess
import tempfile
import xml.etree.ElementTree as ET


def _validate_name(task_name: str) -> None:
    # Only root-level task names: never allow a name to select another folder.
    if (not isinstance(task_name, str) or not task_name.strip()
            or task_name != task_name.strip() or len(task_name) > 238
            or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in task_name)):
        raise ValueError('task_name must be a nonempty root-level Windows task name')


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw.strip() or any(ord(c) < 32 or c == '"' for c in raw):
        raise ValueError('task paths must be nonempty and contain no quotes or control characters')
    return Path(raw).expanduser().resolve()


def _current_user() -> str:
    if os.name == 'nt':
        # Read the actual token identity instead of trusting USERNAME/USERDOMAIN.
        import ctypes
        from ctypes import wintypes
        lookup = ctypes.WinDLL('secur32', use_last_error=True).GetUserNameExW
        lookup.argtypes = [ctypes.c_int, wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG)]
        lookup.restype = wintypes.BOOLEAN
        length = wintypes.ULONG(32768)
        buffer = ctypes.create_unicode_buffer(length.value)
        if not lookup(2, buffer, ctypes.byref(length)):  # NameSamCompatible
            raise OSError(ctypes.get_last_error(), 'Cannot determine current Windows user')
        return buffer.value
    # Allows portable XML previews/tests; schtasks itself is Windows-only.
    return getpass.getuser()


def task_xml(python_path, script_path, config_path, interval_minutes=15,
             task_name='TeamsCacheMonitor') -> str:
    """Build a current-user, indefinitely repeating task without registering it."""
    _validate_name(task_name)
    if type(interval_minutes) is not int or not 1 <= interval_minutes <= 44640:
        raise ValueError('interval_minutes must be an integer between 1 and 44640')
    python, script, config = map(_absolute_path, (python_path, script_path, config_path))
    task = ET.Element('Task', version='1.2', xmlns='http://schemas.microsoft.com/windows/2004/02/mit/task')
    def add(parent, tag, text=None, **attributes):
        node = ET.SubElement(parent, tag, attributes)
        node.text = text
        return node
    registration = add(task, 'RegistrationInfo')
    add(registration, 'Description', f'Local Teams cache monitor: {task_name}')
    triggers = add(task, 'Triggers')
    trigger = add(triggers, 'TimeTrigger')
    repetition = add(trigger, 'Repetition')
    add(repetition, 'Interval', f'PT{interval_minutes}M')
    add(repetition, 'StopAtDurationEnd', 'false')
    start = datetime.datetime.now().astimezone() + datetime.timedelta(minutes=1)
    add(trigger, 'StartBoundary', start.isoformat(timespec='seconds'))
    add(trigger, 'Enabled', 'true')
    principals = add(task, 'Principals')
    principal = add(principals, 'Principal', id='CurrentUser')
    add(principal, 'UserId', _current_user())
    add(principal, 'LogonType', 'InteractiveToken')
    add(principal, 'RunLevel', 'LeastPrivilege')
    settings = add(task, 'Settings')
    add(settings, 'MultipleInstancesPolicy', 'IgnoreNew')
    add(settings, 'DisallowStartIfOnBatteries', 'false')
    add(settings, 'StopIfGoingOnBatteries', 'false')
    add(settings, 'StartWhenAvailable', 'true')
    add(settings, 'Enabled', 'true')
    actions = add(task, 'Actions', Context='CurrentUser')
    action = add(actions, 'Exec')
    add(action, 'Command', str(python))
    add(action, 'Arguments', subprocess.list2cmdline([
        '-S', str(script), 'monitor', 'run', '--config', str(config), '--scheduled']))
    add(action, 'WorkingDirectory', str(script.parent))
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + ET.tostring(task, encoding='unicode')


def _schtasks(arguments: list[str]) -> str:
    try:
        result = subprocess.run(['schtasks.exe', *arguments], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, errors='replace',
                                shell=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'Cannot run Windows Task Scheduler: {exc}') from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or f'exit status {result.returncode}').strip()
        raise RuntimeError(f'Windows Task Scheduler failed: {detail}')
    return result.stdout.strip()


def install_task(python_path, script_path, config_path, interval_minutes=15,
                 task_name='TeamsCacheMonitor') -> dict:
    """Register explicitly; existing tasks are never forcibly overwritten."""
    xml = task_xml(python_path, script_path, config_path, interval_minutes, task_name)
    with tempfile.TemporaryDirectory(prefix='teams-task-') as directory:
        path = Path(directory) / 'task.xml'
        path.write_text(xml, encoding='utf-16')
        message = _schtasks(['/Create', '/TN', task_name, '/XML', str(path)])
    return {'task_name': task_name, 'status': 'installed', 'message': message}


def remove_task(task_name) -> dict:
    """Remove the explicitly named task, without an interactive prompt."""
    _validate_name(task_name)
    message = _schtasks(['/Delete', '/TN', task_name, '/F'])
    return {'task_name': task_name, 'status': 'removed', 'message': message}
