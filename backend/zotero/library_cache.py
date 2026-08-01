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

    def _apply_sync(
        self,
        items: list[dict],
        deleted_keys: list[str],
        is_full: bool,
        stored_version: int,
    ) -> SyncResult:
        fetched_keys = [item["key"] for item in items]
        existing: set[str] = set()
        if fetched_keys:
            placeholders = ",".join("?" for _ in fetched_keys)
            rows = self._conn.execute(
                f"SELECT key FROM items WHERE key IN ({placeholders})", fetched_keys
            ).fetchall()
            existing = {row[0] for row in rows}

        added = sum(1 for key in fetched_keys if key not in existing)
        updated = len(fetched_keys) - added

        max_version_seen = max(
            (item.get("version", 0) for item in items),
            default=0 if is_full else stored_version,
        )

        with self._conn:
            for item in items:
                item_type = item.get("data", {}).get("itemType", "")
                self._conn.execute(
                    "INSERT OR REPLACE INTO items (key, version, item_type, data) "
                    "VALUES (?, ?, ?, ?)",
                    (item["key"], item.get("version", 0), item_type, json.dumps(item)),
                )
            for key in deleted_keys:
                self._conn.execute("DELETE FROM items WHERE key = ?", (key,))
            self._conn.execute(
                "INSERT OR REPLACE INTO sync_state (id, library_version, last_synced_at) "
                "VALUES (0, ?, ?)",
                (max_version_seen, datetime.now(timezone.utc).isoformat()),
            )

        return SyncResult(
            added=added,
            updated=updated,
            deleted=len(deleted_keys),
            library_version=max_version_seen,
            was_full_sync=is_full,
        )

    async def sync(self, force_full: bool = False) -> SyncResult:
        async with self._lock:
            stored_version = await asyncio.to_thread(self._stored_version)
            is_full = force_full or stored_version == 0

            try:
                if is_full:
                    items = await self.client.get_library_items_since(
                        library_id=self.library_id,
                        library_type=self.library_type,
                        since_version=None,
                        raise_on_error=True,
                    )
                    deleted_keys: list[str] = []
                else:
                    items = await self.client.get_library_items_since(
                        library_id=self.library_id,
                        library_type=self.library_type,
                        since_version=stored_version,
                        raise_on_error=True,
                    )
                    deleted_keys = await self.client.get_deleted_item_keys(
                        library_id=self.library_id,
                        library_type=self.library_type,
                        since_version=stored_version,
                        raise_on_error=True,
                    )
            except LibraryFetchError as exc:
                raise LibrarySyncError(
                    f"sync failed for {self.library_type}/{self.library_id}: {exc}"
                ) from exc

            return await asyncio.to_thread(
                self._apply_sync, items, deleted_keys, is_full, stored_version
            )

    async def get_all_items(self, item_types: Optional[list[str]] = None) -> list[dict[str, Any]]:
        await self.sync()
        async with self._lock:
            return await asyncio.to_thread(self._query_items, item_types)

    def _query_items(self, item_types: Optional[list[str]]) -> list[dict[str, Any]]:
        if item_types:
            placeholders = ",".join("?" for _ in item_types)
            rows = self._conn.execute(
                f"SELECT data FROM items WHERE item_type IN ({placeholders})", item_types
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT data FROM items").fetchall()
        return [json.loads(row[0]) for row in rows]
