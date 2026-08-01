# Zotero Library Sync Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone, reusable module (`ZoteroLibraryCache`) that gives callers a cheap, repeatable "download all items in this library" call: a full fetch only the first time, version-based incremental syncs (including deletions) every time after.

**Architecture:** A new `backend/zotero/library_cache.py` wraps the existing `ZoteroWebAPI` client (no HTTP/pagination logic duplicated) and persists items in a per-library SQLite file (WAL mode), tracking a high-water-mark `library_version` the same way `backend/services/document_processor.py` already does. Two small additive changes to `ZoteroWebAPI` (`raise_on_error` on `get_library_items_since`/`get_deleted_item_keys`) let the cache detect and abort on a partial fetch instead of silently corrupting its state. A new `zotero_cache_path` setting follows the existing `review_queue_path` pattern.

**Tech Stack:** Python 3.12, `sqlite3` (stdlib), `aiohttp` (already a dependency via `ZoteroWebAPI`), `asyncio.to_thread` for DB calls (per this repo's "never block the event loop" rule), `unittest`/`unittest.mock` (`IsolatedAsyncioTestCase`, `AsyncMock`).

**Spec:** `docs/superpowers/specs/2026-08-01-zotero-library-sync-cache-design.md`

---

## File Structure

- **Modify:** `backend/zotero/web_api.py` — add `LibraryFetchError` exception and an opt-in `raise_on_error` parameter to `get_library_items_since` and `get_deleted_item_keys`. Purely additive; default behavior for every existing caller is unchanged.
- **Modify:** `backend/config/settings.py` — add `zotero_cache_path` setting, following the exact `review_queue_path` pattern (field, `expand_path` validator, `set_derived_paths` fill-in, `ensure_directories` mkdir).
- **Create:** `backend/zotero/library_cache.py` — the new module: `SyncResult` dataclass, `LibrarySyncError` exception, `ZoteroLibraryCache` class (`sync()`, `get_all_items()`).
- **Create:** `backend/tests/test_zotero_cache_settings.py` — tests for the new setting.
- **Create:** `backend/tests/test_zotero_library_cache.py` — tests for the new module.
- **Modify:** `backend/tests/test_web_api.py` — new test cases for `raise_on_error` on both modified methods.

No other files are modified. Wiring `ZoteroLibraryCache` into `ZoteroCatalogMetadataStrategy` is out of scope for this plan (see spec §8) — that happens when the chapter-segmentation strategy-pipeline plan is implemented.

---

### Task 1: `LibraryFetchError` + `raise_on_error` on `get_library_items_since`

**Files:**
- Modify: `backend/zotero/web_api.py`
- Test: `backend/tests/test_web_api.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_web_api.py`, right after `TestZoteroWebAPIGetLibraryItemsSince`:

```python
class TestZoteroWebAPIRaiseOnError(unittest.IsolatedAsyncioTestCase):
    async def test_get_library_items_since_raises_on_http_error_when_requested(self):
        from backend.zotero.web_api import LibraryFetchError

        api = ZoteroWebAPI(api_key="testkey")
        api.session = _make_session(_make_response(500, {}))

        with self.assertRaises(LibraryFetchError):
            await api.get_library_items_since("u12345", "user", raise_on_error=True)

    async def test_get_library_items_since_default_still_returns_partial_on_error(self):
        """Existing callers that don't pass raise_on_error keep today's behavior."""
        api = ZoteroWebAPI(api_key="testkey")
        api.session = _make_session(_make_response(500, {}))

        result = await api.get_library_items_since("u12345", "user")
        self.assertEqual(result, [])

    async def test_get_library_items_since_raises_on_non_list_body_when_requested(self):
        from backend.zotero.web_api import LibraryFetchError

        api = ZoteroWebAPI(api_key="testkey")
        api.session = _make_session(_make_response(200, {"not": "a list"}))

        with self.assertRaises(LibraryFetchError):
            await api.get_library_items_since("u12345", "user", raise_on_error=True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_web_api.py::TestZoteroWebAPIRaiseOnError -v`
Expected: FAIL — `ImportError: cannot import name 'LibraryFetchError'` (or `TypeError: unexpected keyword argument 'raise_on_error'` once the import is fixed manually) since neither exists yet.

- [ ] **Step 3: Implement**

In `backend/zotero/web_api.py`, add the exception class right after the module constants (after `_PAGE_SIZE = 100`, before `class ZoteroWebAPI:`):

```python
class LibraryFetchError(Exception):
    """Raised by ZoteroWebAPI methods when raise_on_error=True and a fetch fails."""
```

Replace the full `get_library_items_since` method with:

```python
    async def get_library_items_since(
        self,
        library_id: str,
        library_type: str = "user",
        since_version: Optional[int] = None,
        limit: Optional[int] = None,
        start: int = 0,
        raise_on_error: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch items with automatic pagination.

        If *limit* is given, at most that many items are returned (no pagination
        beyond the first page). If *limit* is None, all pages are fetched.

        If *raise_on_error* is True, a non-200 response or a non-list response
        body raises LibraryFetchError instead of silently returning whatever
        was accumulated so far. Defaults to False so every existing caller's
        behavior is unchanged.
        """
        await self._ensure_session()
        url = f"{self._base_url(library_id, library_type)}/items"
        page_size = min(limit, _PAGE_SIZE) if limit is not None else _PAGE_SIZE

        params: dict[str, Any] = {"format": "json", "limit": page_size, "start": start}
        if since_version is not None:
            params["since"] = since_version
            logger.info("Fetching items since version %s", since_version)

        all_items: list[dict] = []
        current_start = start

        while True:
            params["start"] = current_start
            async with self.session.get(url, params=params) as resp:
                await self._handle_rate_limit(resp)
                if resp.status != 200:
                    logger.error("get_library_items_since failed: HTTP %s", resp.status)
                    if raise_on_error:
                        raise LibraryFetchError(
                            f"get_library_items_since failed: HTTP {resp.status}"
                        )
                    break
                items = await resp.json()
                if not isinstance(items, list):
                    if raise_on_error:
                        raise LibraryFetchError(
                            "get_library_items_since: non-list response body"
                        )
                    break
                all_items.extend(items)

                if len(items) < page_size:
                    break  # last page
                if limit is not None and len(all_items) >= limit:
                    all_items = all_items[:limit]
                    break
                current_start += len(items)

        logger.info("Retrieved %d items from library %s", len(all_items), library_id)
        return all_items
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_web_api.py -v`
Expected: PASS (all tests in the file, including the 3 new ones and every pre-existing test in `TestZoteroWebAPIGetLibraryItemsSince`).

- [ ] **Step 5: Commit**

```bash
git add backend/zotero/web_api.py backend/tests/test_web_api.py
git commit -m "feat: add raise_on_error option to ZoteroWebAPI.get_library_items_since"
```

---

### Task 2: `raise_on_error` on `get_deleted_item_keys`

**Files:**
- Modify: `backend/zotero/web_api.py`
- Test: `backend/tests/test_web_api.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_web_api.py`, right after `TestZoteroWebAPIGetLibraryItemsSince` (before the class added in Task 1, or after — order doesn't matter):

```python
class TestZoteroWebAPIGetDeletedItemKeys(unittest.IsolatedAsyncioTestCase):
    async def test_get_deleted_item_keys_success(self):
        api = ZoteroWebAPI(api_key="testkey")
        api.session = _make_session(_make_response(200, {"items": ["AAAA", "BBBB"]}))

        result = await api.get_deleted_item_keys("u12345", "user", since_version=5)
        self.assertEqual(result, ["AAAA", "BBBB"])

    async def test_get_deleted_item_keys_error_returns_empty_by_default(self):
        api = ZoteroWebAPI(api_key="testkey")
        api.session = _make_session(_make_response(500, {}))

        result = await api.get_deleted_item_keys("u12345", "user", since_version=5)
        self.assertEqual(result, [])

    async def test_get_deleted_item_keys_raises_on_error_when_requested(self):
        from backend.zotero.web_api import LibraryFetchError

        api = ZoteroWebAPI(api_key="testkey")
        api.session = _make_session(_make_response(500, {}))

        with self.assertRaises(LibraryFetchError):
            await api.get_deleted_item_keys("u12345", "user", since_version=5, raise_on_error=True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_web_api.py::TestZoteroWebAPIGetDeletedItemKeys -v`
Expected: FAIL — the first two tests fail with `TypeError: 'coroutine' object is not subscriptable` or similar only if something's wrong; more precisely, `test_get_deleted_item_keys_raises_on_error_when_requested` fails with `TypeError: get_deleted_item_keys() got an unexpected keyword argument 'raise_on_error'`. (The first two tests exercise pre-existing behavior and should already pass — that's fine, they're regression coverage being added now since no dedicated test class existed for this method before.)

- [ ] **Step 3: Implement**

Replace the full `get_deleted_item_keys` method in `backend/zotero/web_api.py` with:

```python
    async def get_deleted_item_keys(
        self,
        library_id: str,
        library_type: str = "user",
        since_version: int = 0,
        raise_on_error: bool = False,
    ) -> list[str]:
        """Return item keys deleted from Zotero since *since_version*.

        Calls GET /{kind}/{id}/deleted?since=version and returns the "items" list.
        Returns an empty list on error so callers can treat this as best-effort,
        unless *raise_on_error* is True, in which case LibraryFetchError is
        raised instead. Defaults to False so every existing caller's behavior
        is unchanged.
        """
        await self._ensure_session()
        url = f"{self._base_url(library_id, library_type)}/deleted"
        async with self.session.get(url, params={"since": since_version}) as resp:
            await self._handle_rate_limit(resp)
            if resp.status == 200:
                data = await resp.json()
                return data.get("items", [])
            logger.warning("get_deleted_item_keys failed: HTTP %s", resp.status)
            if raise_on_error:
                raise LibraryFetchError(f"get_deleted_item_keys failed: HTTP {resp.status}")
            return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_web_api.py -v`
Expected: PASS (entire file).

- [ ] **Step 5: Commit**

```bash
git add backend/zotero/web_api.py backend/tests/test_web_api.py
git commit -m "feat: add raise_on_error option to ZoteroWebAPI.get_deleted_item_keys"
```

---

### Task 3: `zotero_cache_path` setting

**Files:**
- Modify: `backend/config/settings.py`
- Test: `backend/tests/test_zotero_cache_settings.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_zotero_cache_settings.py`:

```python
"""Unit tests for the zotero_cache_path setting."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.config.settings import Settings


class ZoteroCachePathSettingTest(unittest.TestCase):
    def test_defaults_to_data_path_subdir(self):
        s = Settings(data_path=Path("/tmp/zotero-rag-test-data"))
        self.assertEqual(s.zotero_cache_path, Path("/tmp/zotero-rag-test-data/zotero_cache"))

    def test_explicit_value_from_env_is_respected(self):
        with patch.dict(os.environ, {"ZOTERO_CACHE_PATH": "/tmp/custom-zotero-cache"}):
            s = Settings()
        self.assertEqual(s.zotero_cache_path, Path("/tmp/custom-zotero-cache"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_zotero_cache_settings.py -v`
Expected: FAIL — `pydantic_core._pydantic_core.ValidationError` or `AttributeError: 'Settings' object has no attribute 'zotero_cache_path'` (extra/unknown field, since it doesn't exist yet).

- [ ] **Step 3: Implement**

In `backend/config/settings.py`, add the field right after `review_queue_path` (around line 156):

```python
    zotero_cache_path: Optional[Path] = Field(
        default=None,
        description="Directory for per-library Zotero item sync caches (SQLite). "
                    "Defaults to <data_path>/zotero_cache."
    )
```

Add `"zotero_cache_path"` to the `expand_path` field_validator's field list:

```python
    @field_validator("data_path", "model_weights_path", "vector_db_path", "log_file", "registrations_path", "autoindex_keys_path", "review_queue_path", "zotero_cache_path", mode="before")
```

Add a fill-in line at the end of `set_derived_paths` (after the `review_queue_path` block):

```python
        if self.zotero_cache_path is None:
            self.zotero_cache_path = self.data_path / "zotero_cache"
        return self
```

(i.e. insert the new `if` block before the existing `return self`.)

Add a `mkdir` line at the end of `ensure_directories` (after the `review_queue_path` block):

```python
        if self.zotero_cache_path:
            self.zotero_cache_path.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_zotero_cache_settings.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/config/settings.py backend/tests/test_zotero_cache_settings.py
git commit -m "feat: add zotero_cache_path setting"
```

---

### Task 4: `ZoteroLibraryCache` skeleton — schema & construction

**Files:**
- Create: `backend/zotero/library_cache.py`
- Test: `backend/tests/test_zotero_library_cache.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_zotero_library_cache.py`:

```python
"""Unit tests for backend.zotero.library_cache.ZoteroLibraryCache."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from backend.zotero.library_cache import ZoteroLibraryCache


def _item(key: str, version: int, item_type: str, **data_fields) -> dict:
    """Build a raw Zotero item dict as ZoteroWebAPI.get_library_items_since returns it."""
    return {
        "key": key,
        "version": version,
        "data": {"key": key, "version": version, "itemType": item_type, **data_fields},
    }


class TestZoteroLibraryCacheSchema(unittest.TestCase):
    def test_creates_sqlite_file_with_expected_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp)
            client = AsyncMock()
            cache = ZoteroLibraryCache(
                client=client, library_id="u123", library_type="user", cache_path=cache_path
            )
            db_file = cache_path / "user_u123.sqlite3"
            self.assertTrue(db_file.exists())

            tables = {
                row[0]
                for row in cache._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("items", tables)
            self.assertIn("sync_state", tables)
            cache.close()

    def test_stored_version_is_zero_for_fresh_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = AsyncMock()
            cache = ZoteroLibraryCache(
                client=client, library_id="u123", library_type="user", cache_path=Path(tmp)
            )
            self.assertEqual(cache._stored_version(), 0)
            cache.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_zotero_library_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.zotero.library_cache'`

- [ ] **Step 3: Implement**

Create `backend/zotero/library_cache.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_zotero_library_cache.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/zotero/library_cache.py backend/tests/test_zotero_library_cache.py
git commit -m "feat: add ZoteroLibraryCache skeleton with SQLite schema"
```

---

### Task 5: `sync()` — full and incremental paths

**Files:**
- Modify: `backend/zotero/library_cache.py`
- Test: `backend/tests/test_zotero_library_cache.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_zotero_library_cache.py` (add `from backend.zotero.library_cache import SyncResult` to the existing import line, i.e. change it to `from backend.zotero.library_cache import SyncResult, ZoteroLibraryCache`):

```python
class TestZoteroLibraryCacheSync(unittest.IsolatedAsyncioTestCase):
    def _make_cache(self, client) -> ZoteroLibraryCache:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return ZoteroLibraryCache(
            client=client,
            library_id="u123",
            library_type="user",
            cache_path=Path(tmp.name),
        )

    async def test_first_sync_is_full_and_stores_all_items(self):
        client = AsyncMock()
        client.get_library_items_since.return_value = [
            _item("AAAA", 3, "book"),
            _item("BBBB", 5, "bookSection"),
        ]
        cache = self._make_cache(client)

        result = await cache.sync()

        self.assertEqual(
            result,
            SyncResult(added=2, updated=0, deleted=0, library_version=5, was_full_sync=True),
        )
        client.get_library_items_since.assert_awaited_once_with(
            library_id="u123", library_type="user", since_version=None, raise_on_error=True
        )
        client.get_deleted_item_keys.assert_not_awaited()

    async def test_second_sync_is_incremental_with_additions_and_updates(self):
        client = AsyncMock()
        client.get_library_items_since.return_value = [_item("AAAA", 3, "book")]
        client.get_deleted_item_keys.return_value = []
        cache = self._make_cache(client)
        await cache.sync()  # first, full sync establishes version 3

        client.get_library_items_since.return_value = [
            _item("AAAA", 7, "book", title="Updated Title"),  # existing key, updated
            _item("CCCC", 8, "bookSection"),                   # new key, added
        ]
        result = await cache.sync()

        self.assertEqual(
            result,
            SyncResult(added=1, updated=1, deleted=0, library_version=8, was_full_sync=False),
        )
        client.get_library_items_since.assert_awaited_with(
            library_id="u123", library_type="user", since_version=3, raise_on_error=True
        )
        client.get_deleted_item_keys.assert_awaited_with(
            library_id="u123", library_type="user", since_version=3, raise_on_error=True
        )

        items = await cache.get_all_items()
        titles = {item["data"]["key"]: item["data"].get("title") for item in items}
        self.assertEqual(titles["AAAA"], "Updated Title")

    async def test_sync_applies_deletions(self):
        client = AsyncMock()
        client.get_library_items_since.return_value = [_item("AAAA", 1, "book")]
        client.get_deleted_item_keys.return_value = []
        cache = self._make_cache(client)
        await cache.sync()

        client.get_library_items_since.return_value = []
        client.get_deleted_item_keys.return_value = ["AAAA"]
        result = await cache.sync()

        self.assertEqual(
            result,
            SyncResult(added=0, updated=0, deleted=1, library_version=1, was_full_sync=False),
        )
        items = await cache.get_all_items()
        self.assertEqual(items, [])

    async def test_noop_incremental_sync_reports_zero_changes(self):
        client = AsyncMock()
        client.get_library_items_since.return_value = [_item("AAAA", 4, "book")]
        client.get_deleted_item_keys.return_value = []
        cache = self._make_cache(client)
        await cache.sync()

        client.get_library_items_since.return_value = []
        result = await cache.sync()

        self.assertEqual(
            result,
            SyncResult(added=0, updated=0, deleted=0, library_version=4, was_full_sync=False),
        )

    async def test_force_full_repeats_full_fetch_and_skips_deletion_check(self):
        client = AsyncMock()
        client.get_library_items_since.return_value = [_item("AAAA", 4, "book")]
        client.get_deleted_item_keys.return_value = []
        cache = self._make_cache(client)
        await cache.sync()
        client.get_library_items_since.reset_mock()
        client.get_deleted_item_keys.reset_mock()

        client.get_library_items_since.return_value = [
            _item("AAAA", 4, "book"),
            _item("BBBB", 9, "bookSection"),
        ]
        result = await cache.sync(force_full=True)

        client.get_library_items_since.assert_awaited_once_with(
            library_id="u123", library_type="user", since_version=None, raise_on_error=True
        )
        client.get_deleted_item_keys.assert_not_awaited()
        self.assertTrue(result.was_full_sync)
        self.assertEqual(result.library_version, 9)
```

Note: `get_all_items()` is called in a couple of these tests before Task 7 implements it properly — that's fine, because Task 6 (next) will already need `get_all_items` to exist to verify deletions/updates landed. To keep Task 5 self-contained and runnable on its own, also add this minimal stub at the end of the `ZoteroLibraryCache` class body in Step 3 below (Task 7 will replace it with the filtering version):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_zotero_library_cache.py::TestZoteroLibraryCacheSync -v`
Expected: FAIL — `AttributeError: 'ZoteroLibraryCache' object has no attribute 'sync'`

- [ ] **Step 3: Implement**

Add the following methods to the `ZoteroLibraryCache` class in `backend/zotero/library_cache.py` (after `_stored_version`, and including the `get_all_items`/`_query_items` stub from Step 1 above so the file is complete):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_zotero_library_cache.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add backend/zotero/library_cache.py backend/tests/test_zotero_library_cache.py
git commit -m "feat: implement ZoteroLibraryCache.sync() full and incremental paths"
```

---

### Task 6: `sync()` error handling — abort leaves cache unchanged

**Files:**
- Modify: `backend/zotero/library_cache.py` (no code change expected — this task verifies the `try/except LibraryFetchError` block added in Task 5 behaves correctly; only add if the tests reveal a gap)
- Test: `backend/tests/test_zotero_library_cache.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_zotero_library_cache.py` (add `from backend.zotero.library_cache import LibrarySyncError` to the import line, and `from backend.zotero.web_api import LibraryFetchError` as a new import):

```python
from backend.zotero.web_api import LibraryFetchError
```

```python
class TestZoteroLibraryCacheSyncErrors(unittest.IsolatedAsyncioTestCase):
    async def test_sync_raises_library_sync_error_and_leaves_state_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = AsyncMock()
            client.get_library_items_since.return_value = [_item("AAAA", 3, "book")]
            client.get_deleted_item_keys.return_value = []
            cache = ZoteroLibraryCache(
                client=client, library_id="u123", library_type="user", cache_path=Path(tmp)
            )
            await cache.sync()  # establishes version 3, one item stored

            client.get_library_items_since.side_effect = LibraryFetchError("HTTP 500")
            with self.assertRaises(LibrarySyncError):
                await cache.sync()

            self.assertEqual(cache._stored_version(), 3)
            items = cache._query_items(None)
            self.assertEqual([item["key"] for item in items], ["AAAA"])
            cache.close()

    async def test_sync_raises_when_deleted_keys_fetch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = AsyncMock()
            client.get_library_items_since.return_value = [_item("AAAA", 3, "book")]
            client.get_deleted_item_keys.return_value = []
            cache = ZoteroLibraryCache(
                client=client, library_id="u123", library_type="user", cache_path=Path(tmp)
            )
            await cache.sync()

            client.get_library_items_since.return_value = []
            client.get_deleted_item_keys.side_effect = LibraryFetchError("HTTP 500")
            with self.assertRaises(LibrarySyncError):
                await cache.sync()

            self.assertEqual(cache._stored_version(), 3)
            cache.close()
```

- [ ] **Step 2: Run tests to verify they fail (or pass — see Step 3)**

Run: `uv run pytest backend/tests/test_zotero_library_cache.py::TestZoteroLibraryCacheSyncErrors -v`
Expected: These should already **PASS** given Task 5's implementation, since the `try/except LibraryFetchError: raise LibrarySyncError` block and the fact that `_apply_sync` (which is the only place that writes to the DB) is never reached on an exception already provide this behavior. This task exists to make that guarantee an explicit, tested contract rather than an untested side effect — if either test unexpectedly fails, it indicates Task 5's implementation needs a fix (e.g. an import cycle, or the `except` clause not catching the raised exception type) before proceeding.

- [ ] **Step 3: Fix if needed, otherwise confirm pass**

If Step 2 failed, inspect the failure and correct `ZoteroLibraryCache.sync()` (most likely cause: `LibraryFetchError` not imported correctly in `library_cache.py`, or the mock's `side_effect` not propagating through `AsyncMock` — confirm with `client.get_library_items_since.side_effect = LibraryFetchError(...)` being set on an `AsyncMock`, which does correctly raise when awaited). Re-run until PASS.

- [ ] **Step 4: Run the full test file**

Run: `uv run pytest backend/tests/test_zotero_library_cache.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_zotero_library_cache.py
git commit -m "test: verify ZoteroLibraryCache.sync() leaves cache unchanged on fetch failure"
```

---

### Task 7: `get_all_items()` — item-type filtering

**Files:**
- Modify: `backend/zotero/library_cache.py` (already has a working stub from Task 5 — this task adds test coverage for the filtering behavior it already implements, matching Task 6's pattern of hardening an existing implementation with an explicit contract)
- Test: `backend/tests/test_zotero_library_cache.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_zotero_library_cache.py`:

```python
class TestZoteroLibraryCacheGetAllItems(unittest.IsolatedAsyncioTestCase):
    async def test_get_all_items_returns_everything_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = AsyncMock()
            client.get_library_items_since.return_value = [
                _item("AAAA", 1, "book"),
                _item("BBBB", 2, "bookSection"),
                _item("CCCC", 3, "note"),
            ]
            cache = ZoteroLibraryCache(
                client=client, library_id="u123", library_type="user", cache_path=Path(tmp)
            )
            items = await cache.get_all_items()
            self.assertEqual({item["key"] for item in items}, {"AAAA", "BBBB", "CCCC"})
            cache.close()

    async def test_get_all_items_filters_by_item_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = AsyncMock()
            client.get_library_items_since.return_value = [
                _item("AAAA", 1, "book"),
                _item("BBBB", 2, "bookSection"),
                _item("CCCC", 3, "note"),
            ]
            cache = ZoteroLibraryCache(
                client=client, library_id="u123", library_type="user", cache_path=Path(tmp)
            )
            items = await cache.get_all_items(item_types=["bookSection"])
            self.assertEqual([item["key"] for item in items], ["BBBB"])
            cache.close()

    async def test_get_all_items_syncs_on_every_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = AsyncMock()
            client.get_library_items_since.return_value = []
            client.get_deleted_item_keys.return_value = []
            cache = ZoteroLibraryCache(
                client=client, library_id="u123", library_type="user", cache_path=Path(tmp)
            )
            await cache.get_all_items()
            await cache.get_all_items()
            self.assertEqual(client.get_library_items_since.await_count, 2)
            cache.close()
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_zotero_library_cache.py::TestZoteroLibraryCacheGetAllItems -v`
Expected: PASS — `get_all_items`/`_query_items` were already implemented as part of Task 5's stub. If `test_get_all_items_filters_by_item_type` fails, check that `item_type` is being read from `item["data"]["itemType"]` (not `item["itemType"]`) in `_apply_sync`.

- [ ] **Step 3: Run the full test suite for this module**

Run: `uv run pytest backend/tests/test_zotero_library_cache.py -v`
Expected: PASS (all tests across all classes in the file)

- [ ] **Step 4: Run the full backend test suite to confirm no regressions**

Run: `uv run pytest backend/tests/ -v`
Expected: PASS (all pre-existing tests plus every test added in this plan)

- [ ] **Step 5: Commit**

```bash
git add backend/tests/test_zotero_library_cache.py
git commit -m "test: verify ZoteroLibraryCache.get_all_items() item-type filtering"
```

---

## Self-Review Notes

- **Spec coverage:** §3 (module/API) → Tasks 4-7. §4 (schema) → Task 4. §5 (sync algorithm + `raise_on_error`/`LibraryFetchError` addition) → Tasks 1, 2, 5, 6. §6 (concurrency, `asyncio.Lock` + `check_same_thread=False`) → Task 4's `__init__`. §7 (settings) → Task 3. §8 (integration point) → explicitly out of scope, documented in File Structure. §9 (error handling summary) → Task 6. §10 (testing) → covered across Tasks 1, 2, 4-7; every scenario listed in the spec's testing section has a corresponding test.
- **Placeholder scan:** no TBD/TODO/"add appropriate handling" text; every step shows complete, runnable code.
- **Type consistency:** `SyncResult`, `LibrarySyncError`, `LibraryFetchError`, `ZoteroLibraryCache`, `sync(force_full: bool = False) -> SyncResult`, `get_all_items(item_types: list[str] | None = None) -> list[dict]` are spelled identically everywhere they appear across all 7 tasks.
- **Added implementation detail beyond the spec (not a contradiction):** the spec's §6 says no distributed/cross-process lock is needed; this plan additionally adds an in-process `asyncio.Lock` (Task 4) to serialize concurrent `await`s against the single `sqlite3.Connection` from the same process, since `asyncio.to_thread` calls may land on different worker threads and `sqlite3` connections opened with `check_same_thread=False` are not safe for true concurrent access from multiple threads at once. This is an implementation necessity for correctness within a single process, not a change to the spec's concurrency scope.
