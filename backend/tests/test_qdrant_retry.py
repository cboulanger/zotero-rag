"""
Unit tests for:
- _ensure_chunks_indexes payload index creation

Uses a mock Qdrant client so no real Qdrant server is needed.
"""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from qdrant_client.models import Distance


def _make_store(timeout: int = 1) -> "VectorStore":
    """Return a VectorStore whose Qdrant client is a MagicMock."""
    from backend.db.vector_store import VectorStore

    with patch("backend.db.vector_store.QdrantClient") as MockClient:
        store = VectorStore.__new__(VectorStore)
        store.storage_path = Path("/tmp/test-qdrant")
        store.embedding_dim = 384
        store.embedding_model_name = "test-model"
        store.distance = Distance.COSINE
        store.qdrant_timeout = timeout
        store.client = MagicMock()
        return store


class TestEnsureChunksIndexes(unittest.TestCase):
    """Tests for _ensure_chunks_indexes — payload index creation on CHUNKS_COLLECTION."""

    def test_creates_indexes_for_library_id_and_item_key(self):
        """Both library_id and item_key indexes are created on CHUNKS_COLLECTION."""
        store = _make_store()
        store._ensure_chunks_indexes()

        calls = store.client.create_payload_index.call_args_list
        indexed_fields = {c[1]["field_name"] for c in calls}
        self.assertIn("library_id", indexed_fields)
        self.assertIn("item_key", indexed_fields)

    def test_indexes_target_chunks_collection(self):
        """create_payload_index is called with the CHUNKS_COLLECTION name."""
        store = _make_store()
        store._ensure_chunks_indexes()

        for c in store.client.create_payload_index.call_args_list:
            self.assertEqual(c[1]["collection_name"], store.CHUNKS_COLLECTION)

    def test_indexes_use_correct_schema(self):
        """library_id and item_key use keyword schema; year uses integer; authors/title use text."""
        from qdrant_client.models import TextIndexParams
        store = _make_store()
        store._ensure_chunks_indexes()

        field_schemas = {c[1]["field_name"]: c[1]["field_schema"]
                         for c in store.client.create_payload_index.call_args_list}
        self.assertEqual(field_schemas.get("library_id"), "keyword")
        self.assertEqual(field_schemas.get("item_key"), "keyword")
        self.assertEqual(field_schemas.get("item_type"), "keyword")
        self.assertEqual(field_schemas.get("year"), "integer")
        self.assertIsInstance(field_schemas.get("authors"), TextIndexParams)
        self.assertIsInstance(field_schemas.get("title"), TextIndexParams)

    def test_idempotent_when_index_already_exists(self):
        """A second call does not raise even if create_payload_index returns an error."""
        store = _make_store()
        store.client.create_payload_index.side_effect = Exception("already exists")

        # Must not propagate the exception.
        try:
            store._ensure_chunks_indexes()
        except Exception as exc:
            self.fail(f"_ensure_chunks_indexes raised unexpectedly: {exc}")

    def test_called_during_ensure_collections(self):
        """_ensure_chunks_indexes is invoked as part of _ensure_collections."""
        store = _make_store()

        # Stub the parts of _ensure_collections that need a real Qdrant response.
        store.client.get_collections.return_value.collections = []
        store.client.get_collection.return_value = MagicMock()

        with patch.object(store, "_ensure_chunks_indexes") as mock_ensure, \
             patch.object(store, "_ensure_dedup_indexes"), \
             patch.object(store, "_save_embedding_config"), \
             patch.object(store, "_load_embedding_config", return_value=None):
            store._ensure_collections()

        mock_ensure.assert_called_once()


if __name__ == "__main__":
    unittest.main()
