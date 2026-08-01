"""Unit tests for backend.zotero.library_cache.ZoteroLibraryCache."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from backend.zotero.library_cache import SyncResult, ZoteroLibraryCache


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


if __name__ == "__main__":
    unittest.main()
