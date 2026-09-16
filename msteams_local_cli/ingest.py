"""Bootstrap and incremental collection with immutable, acknowledged batches."""
import dataclasses
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from .reader import TeamsCacheReader
from .state import StateLock, atomic_write, load_config, read_json, save_state


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def timestamp(value):
    """Accept ISO timestamps and Unix milliseconds/seconds; return aware UTC or None."""
    try:
        text = str(value).strip()
        if re.fullmatch(r'-?\d+(?:\.\d+)?', text):
            numeric = float(text)
            return datetime.fromtimestamp(numeric / 1000 if abs(numeric) >= 100_000_000_000
                                          else numeric, timezone.utc)
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def message_order(message):
    dt = timestamp(message.get('timestamp', ''))
    return (dt is None, dt or datetime.max.replace(tzinfo=timezone.utc),
            message.get('message_id', ''), message.get('account', ''),
            message.get('conversation_id', ''))


def page(items, offset=0, limit=100):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('offset must be >= 0 and limit must be between 1 and 1000')
    end = offset + limit
    return {'total': len(items), 'offset': offset, 'next_offset': end if end < len(items) else None,
            'items': items[offset:end]}


def write_batch(output_path, batch):
    """An interrupted commit can reuse the exact batch on retry."""
    output = Path(output_path)
    path = output / (batch['batch_id'] + '.json')
    if path.exists():
        saved = read_json(path)
        for key in ('batch_id', 'account', 'conversation_id', 'mode', 'messages'):
            if saved.get(key) != batch.get(key):
                raise ValueError('Existing batch content does not match its identity')
        batch = saved
    else:
        atomic_write(path, json.dumps(batch, ensure_ascii=False, indent=2) + '\n')
    return batch


class Monitor:
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()
        self.config = load_config(self.config_path)
        self.state_path = Path(self.config['state_path'])
        self.journal_path = Path(str(self.state_path) + '.pending')

    def _finish_pending(self, state):
        if not self.journal_path.exists():
            return None
        transaction = read_json(self.journal_path)
        proposed = transaction['state']
        if any(proposed.get(k) != state.get(k) for k in ('account', 'conversation_id')):
            raise ValueError('Recovery journal belongs to another monitor')
        if proposed['generation'] == state['generation'] + 1:
            write_batch(self.config['output_path'], transaction['batch'])
            save_state(self.state_path, proposed)
        elif proposed['generation'] != state['generation']:
            raise ValueError('Recovery journal generation conflicts with state')
        # Equal generation means commit succeeded before the process exited.
        self.journal_path.unlink()
        return transaction['result'] | {'recovered': True}

    def _load(self):
        if self.state_path.exists():
            state = read_json(self.state_path)
            if not isinstance(state, dict) or state.get('version') != 1:
                raise ValueError('Unsupported monitor state version')
            for key in ('account', 'conversation_id'):
                if state.get(key) != self.config[key]:
                    raise ValueError('State belongs to another account/conversation; use a new state_path')
            if not isinstance(state.get('messages'), dict) or not isinstance(state.get('batches'), dict):
                raise ValueError('Malformed monitor state; restore a valid copy')
            return state
        return {'version': 1, 'account': self.config['account'],
                'conversation_id': self.config['conversation_id'], 'bootstrap_complete': False,
                'generation': 0, 'messages': {}, 'batches': {}, 'watermark': None, 'last_run': None}

    def collect(self, mode):
        if mode not in ('bootstrap', 'incremental'):
            raise ValueError('mode must be bootstrap or incremental')
        with StateLock(self.state_path):
            state = self._load()
            recovered = self._finish_pending(state)
            if recovered is not None:
                return recovered
            if mode == 'incremental' and not state['bootstrap_complete']:
                raise ValueError('Run monitor bootstrap before incremental collection')
            # A repeated bootstrap resumes/reconciles rather than resetting history.
            effective_mode = 'incremental' if state['bootstrap_complete'] else 'bootstrap'
            rows = {}
            with TeamsCacheReader(self.config.get('leveldb')) as source:
                if self.config['account'] not in {a.key for a in source.accounts(
                        account=self.config['account'], infer_labels=False)}:
                    raise ValueError('Configured account has no supported replychains store in this cache')
                for message in source.messages(account=self.config['account']):
                    if message.conversation_id != self.config['conversation_id']:
                        continue
                    row = dataclasses.asdict(message)
                    if not row['message_id']:
                        raise ValueError('Message without ID cannot be checkpointed')
                    dt = timestamp(row['timestamp'])
                    if dt:
                        row['timestamp'] = dt.isoformat().replace('+00:00', 'Z')
                    if row['message_id'] in rows and rows[row['message_id']] != row:
                        raise ValueError('Conflicting current records for message ' + row['message_id'])
                    rows[row['message_id']] = row
                diagnostics = getattr(source, 'diagnostics', {})
                if source.skipped:
                    raise ValueError(f'{source.skipped} cache records could not be read; '
                                     'state not advanced. Run diagnose/search to investigate.')
            changes = []
            for mid, row in rows.items():
                previous = state['messages'].get(mid)
                hashed = fingerprint(row)
                if previous is None or previous['fingerprint'] != hashed:
                    changes.append(row | {'change': 'new' if previous is None else 'updated'})
                    state['messages'][mid] = {'fingerprint': hashed, 'message': row}
            changes.sort(key=message_order)
            new_count = sum(m['change'] == 'new' for m in changes)
            updated_count = len(changes) - new_count
            batch_id = None
            if changes or not state['bootstrap_complete']:
                identity = {'account': state['account'], 'conversation_id': state['conversation_id'],
                            'generation': state['generation'] + 1, 'mode': effective_mode,
                            'messages': changes}
                batch_id = fingerprint(identity)
                batch = {'version': 1, 'batch_id': batch_id, 'created_at': now(),
                         'account': state['account'], 'conversation_id': state['conversation_id'],
                         'conversation_name': self.config.get('conversation_name', state['conversation_id']),
                         'mode': effective_mode, 'messages': changes}
                state['generation'] += 1
                state['batches'][batch_id] = {'status': 'pending', 'created_at': batch['created_at'],
                                             'message_count': len(changes), 'receipt': None}
            known = [v['message'] for v in state['messages'].values()
                     if timestamp(v['message']['timestamp']) is not None]
            if known:
                newest = max(known, key=message_order)
                state['watermark'] = {k: newest[k] for k in ('timestamp', 'message_id')}
            result = {'mode': effective_mode, 'batch_id': batch_id, 'messages_loaded': len(rows),
                      'messages_new': new_count, 'messages_updated': updated_count,
                      'watermark': state['watermark'], 'diagnostics': diagnostics}
            state['bootstrap_complete'] = True
            state['last_run'] = {'completed_at': now(), **result}
            if batch_id:
                transaction = {'state': state, 'batch': batch, 'result': result}
                atomic_write(self.journal_path, json.dumps(transaction, ensure_ascii=False, indent=2) + '\n')
                write_batch(self.config['output_path'], batch)
            save_state(self.state_path, state)
            if batch_id:
                self.journal_path.unlink()
            return result

    def status(self):
        state = self._load()
        return {k: state[k] for k in ('version', 'account', 'conversation_id', 'bootstrap_complete',
                                     'watermark', 'last_run')} | {
            'messages_stored': len(state['messages']),
            'pending_batches': sum(b['status'] == 'pending' for b in state['batches'].values())}

    def list_batches(self, pending_only=True, offset=0, limit=100):
        state = self._load()
        rows = [{'batch_id': bid, **value} for bid, value in state['batches'].items()
                if not pending_only or value['status'] == 'pending']
        result = page(rows, offset, limit)
        result['batches'] = result.pop('items')
        return result

    def get_batch(self, batch_id, offset=0, limit=100):
        if batch_id not in self._load()['batches']:
            raise ValueError('Unknown committed batch ID')
        batch = read_json(Path(self.config['output_path']) / (batch_id + '.json'))
        result = page(batch['messages'], offset, limit)
        rows = result.pop('items')
        # Older immutable batches may contain analysis; expose only source data.
        return {k: v for k, v in batch.items() if k not in ('messages', 'insights')} | result | {
            'messages': rows}

    def ack(self, batch_id, receipt):
        if not isinstance(receipt, str) or not receipt.strip():
            raise ValueError('A nonempty external delivery receipt is required')
        with StateLock(self.state_path):
            state = self._load()
            if self.journal_path.exists():
                self._finish_pending(state)
                state = self._load()
            if batch_id not in state['batches']:
                raise ValueError('Unknown committed batch ID')
            delivery = state['batches'][batch_id]
            if delivery['status'] == 'delivered' and delivery['receipt'] != receipt:
                raise ValueError('Batch already acknowledged with a different receipt')
            if delivery['status'] != 'delivered':
                delivery.update(status='delivered', receipt=receipt, delivered_at=now())
                save_state(self.state_path, state)
            return {'batch_id': batch_id, **delivery}
