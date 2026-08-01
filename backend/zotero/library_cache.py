"""
Local SQLite cache of a single Zotero library's items, synced incrementally
via Zotero's version-based sync primitives (since=, /deleted,
Last-Modified-Version).

Wraps a ZoteroWebAPI instance rather than duplicating its HTTP/pagination
logic — this module only adds caching and sync-state bookkeeping on top.
"""

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backend.zotero.web_api import LibraryFetchError, ZoteroWebAPI

logger = logging.getLogger(__name__)


class LibrarySyncError(Exception):
    """Raised when sync() cannot complete a fetch; the cache is left unchanged."""


@dataclass(frozen=True)
class SyncResult:
    """Summary of a single sync() call."""

    added: int
    updated: int
    deleted: int
    library_version: int
    was_full_sync: bool


class ZoteroLibraryCache:
    """Local SQLite cache of a single Zotero library's items."""

    def __init__(
        self,
        client: ZoteroWebAPI,
        library_id: str,
        library_type: str,
        cache_path: Path,
    ) -> None:
        self.client = client
        self.library_id = library_id
        self.library_type = library_type
        cache_path.mkdir(parents=True, exist_ok=True)
        self.db_path = cache_path / f"{library_type}_{library_id}.sqlite3"
        # check_same_thread=False: sync()/get_all_items() run DB work via
        # asyncio.to_thread (see below), which may use a different worker
        # thread per call. self._lock serializes all access to this
        # connection so it is never touched from two threads at once.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = asyncio.Lock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                key TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                item_type TEXT NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_item_type ON items(item_type)"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_state (
                id INTEGER PRIMARY KEY CHECK (id = 0),
                library_version INTEGER NOT NULL DEFAULT 0,
                last_synced_at TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _stored_version(self) -> int:
        row = self._conn.execute(
            "SELECT library_version FROM sync_state WHERE id = 0"
        ).fetchone()
        return row[0] if row else 0
