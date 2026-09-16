"""Read the *new* Microsoft Teams (v2) message cache from the local disk.

The new Teams client (``com.microsoft.teams2`` on macOS, ``MSTeams`` MSIX on
Windows) is an Edge WebView2 app that keeps recent conversations in an IndexedDB
database backed by Chromium LevelDB, with values serialized in the V8 format.
This module reads that store **locally and read-only** — no Microsoft Graph, no
OAuth, no network. It only exposes data the signed-in user already has on disk.

Schema (react-web-client, observed on Teams 2.x, 2026):
  - one IndexedDB database per (tenant, user) context, named
    ``Teams:<manager>:react-web-client:<tenantId>:<userObjectId>:<locale>``
  - messages live in ``Teams:replychain-manager:…`` → object store
    ``replychains`` → each record has a ``messageMap`` {messageId: message}
  - chat/channel metadata lives in ``Teams:conversation-manager:…`` →
    object store ``conversations``

Low-level LevelDB + V8 parsing is delegated to ``ccl_chromium_reader`` (MIT), the
reference forensic library. This module maps the *current* Teams schema on top of
it (the older ``forensicsim`` mapping targets a previous schema and returns
nothing on 2.x). Nothing here is specific to any tenant or account.
"""
from __future__ import annotations

import dataclasses
import glob
from html.parser import HTMLParser
import os
import pathlib
import re
import shutil
import sys
import tempfile
from collections.abc import Iterator
from typing import Any, Optional

from ccl_chromium_reader import ccl_chromium_indexeddb as _idb

class _MessageHTMLParser(HTMLParser):
    _blocks = {'p', 'div', 'li', 'ul', 'ol', 'blockquote', 'pre', 'tr',
               'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def _boundary(self):
        if self.parts and not self.parts[-1].endswith('\n'):
            self.parts.append('\n')

    def handle_starttag(self, tag, attrs):
        if tag == 'br':
            self.parts.append('\n')
        elif tag in self._blocks:
            self._boundary()

    def handle_endtag(self, tag):
        if tag in self._blocks:
            self._boundary()

    def handle_data(self, data):
        self.parts.append(data)


def _text(value: Any, content_type: str = '') -> str:
    """Best-effort plain text from an HTML/str/bytes message body."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        value = str(value)
    if str(content_type).lower() in {'text', 'text/plain'}:
        return value.strip()
    parser = _MessageHTMLParser()
    parser.feed(value)
    parser.close()
    return ''.join(parser.parts).strip()


# --- Cache discovery ---------------------------------------------------------

def default_cache_globs() -> list[str]:
    """Candidate globs for the Teams v2 IndexedDB LevelDB, per OS."""
    home = pathlib.Path.home()
    if sys.platform == "darwin":
        base = (
            home
            / "Library/Containers/com.microsoft.teams2/Data/Library/"
            "Application Support/Microsoft/MSTeams/EBWebView"
        )
        return [str(base / "*/IndexedDB/https_teams.microsoft.com_0.indexeddb.leveldb")]
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA", str(home / "AppData/Local"))
        return [
            os.path.join(
                local,
                "Packages",
                "MSTeams_*",
                "LocalCache/Microsoft/MSTeams/EBWebView",
                "*/IndexedDB/https_teams.microsoft.com_0.indexeddb.leveldb",
            )
        ]
    # Linux (unofficial clients) — best effort.
    return [
        str(
            home
            / ".config"
            / "*eams*"
            / "*/IndexedDB/https_teams.microsoft.com_0.indexeddb.leveldb"
        )
    ]


def find_caches() -> list[str]:
    """Return sorted, unique existing Teams cache directories."""
    return sorted({str(pathlib.Path(hit).resolve())
                   for pattern in default_cache_globs()
                   for hit in glob.glob(pattern) if os.path.isdir(hit)})


def find_cache() -> Optional[str]:
    """Return the single discovered cache; require explicit choice if ambiguous."""
    hits = find_caches()
    if len(hits) > 1:
        raise ValueError('Multiple Teams cache directories found; pass an explicit leveldb path: ' + ', '.join(hits))
    return hits[0] if hits else None


# --- Model -------------------------------------------------------------------

@dataclasses.dataclass
class Account:
    """One (tenant, user) context found in the cache."""

    tenant_id: str
    user_id: str
    # Human label inferred from the data (org/display names), not hardcoded.
    label: str = ""

    @property
    def key(self) -> str:
        return f"{self.tenant_id}:{self.user_id}"


@dataclasses.dataclass
class Message:
    account: str
    conversation_id: str
    reply_chain_id: str
    message_id: str
    sender: str
    timestamp: str
    content: str
    content_type: str


# --- Reader ------------------------------------------------------------------

class TeamsCacheReader:
    """Read messages/conversations from a Teams v2 IndexedDB LevelDB.

    The database is copied to a temporary directory before reading, because the
    running Teams client holds a lock on the live files.
    """

    def __init__(self, leveldb_path: Optional[str] = None, *, copy: bool = True):
        path = leveldb_path or find_cache()
        if not path or not os.path.isdir(path):
            raise FileNotFoundError(
                "Teams v2 cache not found. Pass a leveldb path explicitly, or make "
                "sure the new Teams client has been signed in at least once. Looked "
                f"in: {default_cache_globs()}"
            )
        self._src = path
        self._tmp: Optional[str] = None
        self._db = None
        self._skipped = 0
        try:
            if copy:
                self._tmp = tempfile.mkdtemp(prefix="msteams-cache-")
                dst = os.path.join(self._tmp, "cache.leveldb")
                shutil.copytree(path, dst)
                path = dst
            self._db = _idb.WrappedIndexDB(path)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        try:
            if self._db is not None:
                self._db.close()
                self._db = None
        finally:
            if self._tmp and os.path.isdir(self._tmp):
                shutil.rmtree(self._tmp)
                self._tmp = None

    def __enter__(self) -> "TeamsCacheReader":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @property
    def skipped(self) -> int:
        """Number of records skipped because they could not be deserialized."""
        return self._skipped

    def _on_bad(self, _key, _raw):  # ccl bad_deserializer_data_handler
        self._skip('deserialization')
        return None

    def _skip(self, reason):
        self._skipped += 1
        if not hasattr(self, '_skipped_reasons'):
            self._skipped_reasons = {}
        self._skipped_reasons[reason] = self._skipped_reasons.get(reason, 0) + 1

    @property
    def diagnostics(self) -> dict:
        """Snapshot of scan diagnostics; missing stores are discovered on access."""
        return {'skipped': self._skipped,
                'skipped_reasons': dict(getattr(self, '_skipped_reasons', {})),
                'stores_found': dict(getattr(self, '_stores_found', {})),
                'missing_stores': sorted(getattr(self, '_missing_stores', set()))}

    def _stores(self, manager: str, store: str):
        """Yield (Account, WrappedObjectStore) for each context of a manager."""
        if not hasattr(self, '_stores_found'):
            self._stores_found = {}
            self._missing_stores = set()
        key = f'{manager}/{store}'
        self._stores_found[key] = 0
        self._missing_stores = {item for item in self._missing_stores
                                if item != key and not item.startswith(key + ':')}
        for meta in self._db.database_ids:
            name = meta.name or ""
            if not name.startswith(f"Teams:{manager}:"):
                continue
            parts = name.split(":")
            # …:react-web-client:<tenantId>:<userObjectId>:<locale>
            if len(parts) != 6 or parts[2] != 'react-web-client' or not all(parts[3:5]):
                self._skip('unsupported_database_name')
                continue
            tenant_id, user_id = parts[-3], parts[-2]
            db = self._db[meta.dbid_no]
            try:
                obj = db.get_object_store_by_name(store)
            except Exception:
                self._missing_stores.add(f'{key}:{tenant_id}:{user_id}')
                continue
            if obj is None:
                self._missing_stores.add(f'{key}:{tenant_id}:{user_id}')
                continue
            self._stores_found[key] += 1
            yield Account(tenant_id=tenant_id, user_id=user_id), obj
        if not self._stores_found[key]:
            self._missing_stores.add(key)

    # -- public API --

    def accounts(self, account: Optional[str] = None, *, infer_labels: bool = True) -> list[Account]:
        """List the (tenant, user) contexts that hold messages, labeled by the
        most frequent org/display name seen in their messages."""
        out: list[Account] = []
        for acc, obj in self._stores("replychain-manager", "replychains"):
            if account and acc.key != account:
                continue
            if not infer_labels:
                out.append(acc)
                continue
            names: dict[str, int] = {}
            seen = 0
            for rec in obj.iterate_records(live_only=True, bad_deserializer_data_handler=self._on_bad):
                if not isinstance(rec.value, dict):
                    self._skip('invalid_record')
                    continue
                mm = rec.value.get("messageMap")
                if isinstance(mm, dict):
                    for msg in mm.values():
                        if not isinstance(msg, dict):
                            self._skip('invalid_message')
                            continue
                        who = _text(msg.get("imDisplayName"))
                        m = re.search(r"\(([^)]+)\)\s*$", who)  # e.g. "X (GP Rubix)"
                        if m:
                            names[m.group(1)] = names.get(m.group(1), 0) + 1
                else:
                    self._skip('invalid_message_map')
                seen += 1
                if seen >= 200:  # enough to infer a label cheaply
                    break
            acc.label = max(names, key=names.get) if names else ""
            out.append(acc)
        return out

    def messages(self, account: Optional[str] = None) -> Iterator[Message]:
        """Yield all messages, optionally restricted to one account key
        (``<tenantId>:<userObjectId>``)."""
        for acc, obj in self._stores("replychain-manager", "replychains"):
            if account and acc.key != account:
                continue
            for rec in obj.iterate_records(live_only=True, bad_deserializer_data_handler=self._on_bad):
                if not isinstance(rec.value, dict):
                    self._skip('invalid_record')
                    continue
                mm = rec.value.get("messageMap")
                if not isinstance(mm, dict):
                    self._skip('invalid_message_map')
                    continue
                for mid, msg in mm.items():
                    if not isinstance(msg, dict):
                        self._skip('invalid_message')
                        continue
                    content = _text(msg.get("content"), msg.get('contentType', ''))
                    yield Message(
                        account=acc.key,
                        conversation_id=str(rec.value.get("conversationId", "")),
                        reply_chain_id=str(rec.value.get("replyChainId", "")),
                        message_id=str(msg.get("id", mid)),
                        sender=_text(msg.get("imDisplayName") or msg.get("from")),
                        timestamp=str(
                            msg.get("originalArrivalTime")
                            or msg.get("composetime")
                            or ""
                        ),
                        content=content,
                        content_type=str(msg.get("messageType") or ""),
                    )

    def conversations(self, account: Optional[str] = None) -> Iterator[dict]:
        """Yield chat/channel metadata records (title, members, id, last time…)."""
        for acc, obj in self._stores("conversation-manager", "conversations"):
            if account and acc.key != account:
                continue
            for rec in obj.iterate_records(live_only=True, bad_deserializer_data_handler=self._on_bad):
                if not isinstance(rec.value, dict):
                    self._skip('invalid_record')
                    continue
                v = rec.value
                tp = v.get("threadProperties") or {}
                last = v.get("lastMessage") if isinstance(v.get("lastMessage"), dict) else {}
                yield {
                    "account": acc.key,
                    "id": str(v.get("id", "")),
                    # The display title lives in threadProperties.topic (channels/named
                    # group chats). 1:1 chats have no topic — the caller can fall back
                    # to member names.
                    "title": _text((tp.get("topic") if isinstance(tp, dict) else "") or ""),
                    "type": str(v.get("type", "")),
                    "last_message_time": str(v.get("lastMessageTimeUtc", "")),
                    "last_message": _text(last.get("content", ""), last.get('contentType', '')),
                }

    def mentions(self, account: Optional[str] = None) -> Iterator[dict]:
        """Yield @-mention entries (read state included). The only reliable read
        marker in the cache: general read/unread is NOT stored locally, but each
        mention carries ``is_read``. Content is not here — join on
        (conversation_id, message_id) with :meth:`messages`."""
        for acc, obj in self._stores("messaging-slice-manager", "mentions-metadata-items"):
            if account and acc.key != account:
                continue
            for rec in obj.iterate_records(live_only=True, bad_deserializer_data_handler=self._on_bad):
                if rec.value is None or not isinstance(rec.value, dict):
                    self._skip('invalid_record')
                    continue
                v = rec.value
                yield {
                    "account": acc.key,
                    "conversation_id": str(v.get("sourceThreadId", "")),
                    "message_id": str(v.get("sourceMessageId", "")),
                    "reply_chain_id": str(v.get("sourceReplyChainId", "")),
                    "timestamp": str(v.get("timestamp", "")),
                    "is_read": str(v.get("isRead")) == "True",
                }
