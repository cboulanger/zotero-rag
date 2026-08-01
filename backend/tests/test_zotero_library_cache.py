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
