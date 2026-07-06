"""Unit tests for the reconcile-count handler's sanity guard.

These exercise backend.api.libraries.reconcile_library_count directly with a mocked
vector store (no running backend required).
"""

import unittest
from unittest.mock import MagicMock

from fastapi import HTTPException

from backend.api.libraries import reconcile_library_count
from backend.models.library import LibraryIndexMetadata


def _meta(total_items_indexed: int, last_full_scan_indexable: int) -> LibraryIndexMetadata:
    return LibraryIndexMetadata(
        library_id="2829873",
        library_type="group",
        library_name="groups/2829873",
        last_indexed_version=46693,
        total_items_indexed=total_items_indexed,
        last_full_scan_indexable=last_full_scan_indexable,
    )


class TestReconcileCountGuard(unittest.TestCase):
    def test_refuses_implausibly_low_count(self):
        """A count far below the scan floor must be refused (409), not persisted.

        Regression for the June 2026 cascade: count_indexed_items returned 195 for a
        library with a scan floor of ~6010, which overwrote the counter and triggered
        an unnecessary full rescan that wiped 217k chunks.
        """
        meta = _meta(total_items_indexed=6010, last_full_scan_indexable=6010)
        vs = MagicMock()
        vs.get_library_metadata.return_value = meta
        vs.count_indexed_items.return_value = 195  # implausibly low

        with self.assertRaises(HTTPException) as ctx:
            reconcile_library_count("2829873", identity=None, vector_store=vs)

        self.assertEqual(ctx.exception.status_code, 409)
        # The counter must not have been overwritten.
        vs.update_library_metadata.assert_not_called()
        self.assertEqual(meta.total_items_indexed, 6010)

    def test_accepts_plausible_count(self):
        """A count within range of the scan floor is accepted and persisted."""
        meta = _meta(total_items_indexed=6010, last_full_scan_indexable=6010)
        vs = MagicMock()
        vs.get_library_metadata.return_value = meta
        vs.count_indexed_items.return_value = 5900  # small, legitimate drop

        result = reconcile_library_count("2829873", identity=None, vector_store=vs)

        vs.update_library_metadata.assert_called_once()
        self.assertEqual(result.total_items_indexed, 5900)

    def test_no_scan_floor_skips_guard(self):
        """Without an established scan floor the guard is inactive (any count accepted)."""
        meta = _meta(total_items_indexed=0, last_full_scan_indexable=0)
        vs = MagicMock()
        vs.get_library_metadata.return_value = meta
        vs.count_indexed_items.return_value = 3

        result = reconcile_library_count("2829873", identity=None, vector_store=vs)

        vs.update_library_metadata.assert_called_once()
        self.assertEqual(result.total_items_indexed, 3)


class TestClearItemChunksCascadesDedup(unittest.TestCase):
    """Clearing an item's chunks must also purge its dedup record.

    Otherwise the dedup record outlives the chunks and the item becomes
    permanently un-reindexable (check_duplicate matches, but no chunks exist).
    """

    def test_clear_item_chunks_also_deletes_dedup_record(self):
        from backend.api.libraries import clear_item_chunks

        vs = MagicMock()
        vs.delete_item_chunks.return_value = 4

        result = clear_item_chunks("6297749", "ITEM001", identity=None, vector_store=vs)

        vs.delete_item_chunks.assert_called_once_with("6297749", "ITEM001")
        vs.delete_item_deduplication_records.assert_called_once_with("6297749", "ITEM001")
        self.assertEqual(result["chunks_deleted"], 4)


if __name__ == "__main__":
    unittest.main()
