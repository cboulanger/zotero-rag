"""
Unit tests for item_vectors and collection_vectors storage in VectorStore.
"""

import unittest
import tempfile
import shutil
from pathlib import Path

from qdrant_client.models import Distance

from backend.db.vector_store import VectorStore
from backend.models.document import (
    DocumentChunk,
    ChunkMetadata,
    DocumentMetadata,
)


# Shared helpers
DIM = 8  # small dimension for fast tests
LIB = "lib1"


def _make_store(tmp_dir: str) -> VectorStore:
    return VectorStore(
        storage_path=Path(tmp_dir) / "qdrant",
        embedding_dim=DIM,
        embedding_model_name="test-model",
        distance=Distance.COSINE,
    )


def _unit_vec(index: int, dim: int = DIM) -> list[float]:
    """Return a unit vector with 1.0 at `index` and 0.0 elsewhere."""
    v = [0.0] * dim
    v[index % dim] = 1.0
    return v


class TestCollectionsCreated(unittest.TestCase):
    """Both new collections must be created by _ensure_collections()."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.store = _make_store(self.temp_dir)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_item_vectors_collection_exists(self):
        names = [c.name for c in self.store.client.get_collections().collections]
        self.assertIn(VectorStore.ITEM_VECTORS_COLLECTION, names)

    def test_collection_vectors_collection_exists(self):
        names = [c.name for c in self.store.client.get_collections().collections]
        self.assertIn(VectorStore.COLLECTION_VECTORS_COLLECTION, names)


class TestItemVectorsCRUD(unittest.TestCase):
    """upsert / get / list / count / delete for item_vectors."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.store = _make_store(self.temp_dir)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_upsert_and_get_round_trip(self):
        vec = _unit_vec(0)
        pid = self.store.upsert_item_vector(
            library_id=LIB,
            item_key="ITEM1",
            vector=vec,
            collection_ids=["col1", "col2"],
            source="abstract",
            title="Test Title",
        )
        self.assertIsNotNone(pid)

        result = self.store.get_item_vector(LIB, "ITEM1")
        self.assertIsNotNone(result)
        retrieved_vec, payload = result
        self.assertEqual(retrieved_vec, vec)
        self.assertEqual(payload["item_key"], "ITEM1")
        self.assertEqual(payload["library_id"], LIB)
        self.assertEqual(payload["collection_ids"], ["col1", "col2"])
        self.assertEqual(payload["source"], "abstract")
        self.assertEqual(payload["title"], "Test Title")
        self.assertEqual(payload["schema_version"], 1)
        self.assertIn("computed_at", payload)

    def test_upsert_is_idempotent(self):
        """Upserting the same item twice should not create duplicates."""
        for _ in range(2):
            self.store.upsert_item_vector(LIB, "ITEM1", _unit_vec(0), [], "abstract", "T")
        self.assertEqual(self.store.count_item_vectors(LIB), 1)

    def test_get_item_vector_not_found(self):
        result = self.store.get_item_vector(LIB, "MISSING")
        self.assertIsNone(result)

    def test_get_item_vectors_for_library(self):
        for i in range(3):
            self.store.upsert_item_vector(LIB, f"ITEM{i}", _unit_vec(i), [f"c{i}"], "abstract", f"T{i}")
        # Add one item in a different library — must not appear
        self.store.upsert_item_vector("lib2", "OTHER", _unit_vec(0), [], "abstract", "X")

        items = self.store.get_item_vectors_for_library(LIB)
        self.assertEqual(len(items), 3)
        keys = {tup[0] for tup in items}
        self.assertEqual(keys, {"ITEM0", "ITEM1", "ITEM2"})
        # Each tuple has (item_key, vector, collection_ids)
        for item_key, vec, col_ids in items:
            self.assertIsInstance(vec, list)
            self.assertEqual(len(vec), DIM)
            self.assertIsInstance(col_ids, list)

    def test_count_item_vectors(self):
        self.assertEqual(self.store.count_item_vectors(LIB), 0)
        self.store.upsert_item_vector(LIB, "ITEM1", _unit_vec(0), [], "abstract", "T")
        self.assertEqual(self.store.count_item_vectors(LIB), 1)
        self.store.upsert_item_vector(LIB, "ITEM2", _unit_vec(1), [], "abstract", "T")
        self.assertEqual(self.store.count_item_vectors(LIB), 2)

    def test_delete_item_vector(self):
        self.store.upsert_item_vector(LIB, "ITEM1", _unit_vec(0), [], "abstract", "T")
        deleted = self.store.delete_item_vector(LIB, "ITEM1")
        self.assertEqual(deleted, 1)
        self.assertIsNone(self.store.get_item_vector(LIB, "ITEM1"))
        self.assertEqual(self.store.count_item_vectors(LIB), 0)

    def test_delete_item_vector_not_found(self):
        deleted = self.store.delete_item_vector(LIB, "GHOST")
        self.assertEqual(deleted, 0)


class TestGetChunkVectorsForItem(unittest.TestCase):
    """get_chunk_vectors_for_item returns vectors from document_chunks."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.store = _make_store(self.temp_dir)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _add_chunk(self, item_key: str, chunk_index: int, vector: list[float]):
        chunk = DocumentChunk(
            text=f"Text {chunk_index}",
            metadata=ChunkMetadata(
                chunk_id=f"{LIB}:{item_key}:{chunk_index}",
                document_metadata=DocumentMetadata(
                    library_id=LIB,
                    item_key=item_key,
                    title="Test",
                ),
                page_number=1,
                text_preview="Text",
                chunk_index=chunk_index,
                content_hash=f"hash-{item_key}-{chunk_index}",
            ),
            embedding=vector,
        )
        self.store.add_chunk(chunk)

    def test_returns_chunk_vectors(self):
        vecs = [_unit_vec(i) for i in range(3)]
        for i, v in enumerate(vecs):
            self._add_chunk("ITEM1", i, v)

        result = self.store.get_chunk_vectors_for_item(LIB, "ITEM1")
        self.assertEqual(len(result), 3)
        for v in result:
            self.assertIsInstance(v, list)
            self.assertEqual(len(v), DIM)

    def test_returns_empty_for_unknown_item(self):
        result = self.store.get_chunk_vectors_for_item(LIB, "GHOST")
        self.assertEqual(result, [])


class TestCollectionVectorsCRUD(unittest.TestCase):
    """upsert / get / search / count / delete for collection_vectors."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.store = _make_store(self.temp_dir)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_upsert_and_get_round_trip(self):
        vec = _unit_vec(0)
        pid = self.store.upsert_collection_vector(
            library_id=LIB,
            collection_id="COL1",
            collection_name="Physics",
            vector=vec,
            item_count=42,
        )
        self.assertIsNotNone(pid)

        result = self.store.get_collection_vector(LIB, "COL1")
        self.assertIsNotNone(result)
        retrieved_vec, payload = result
        self.assertEqual(retrieved_vec, vec)
        self.assertEqual(payload["collection_id"], "COL1")
        self.assertEqual(payload["library_id"], LIB)
        self.assertEqual(payload["collection_name"], "Physics")
        self.assertEqual(payload["item_count"], 42)
        self.assertEqual(payload["schema_version"], 1)
        self.assertIn("computed_at", payload)

    def test_upsert_is_idempotent(self):
        for _ in range(2):
            self.store.upsert_collection_vector(LIB, "COL1", "Physics", _unit_vec(0), 1)
        self.assertEqual(self.store.count_collection_vectors(LIB), 1)

    def test_get_collection_vector_not_found(self):
        self.assertIsNone(self.store.get_collection_vector(LIB, "GHOST"))

    def test_get_all_collection_vectors(self):
        for i in range(3):
            self.store.upsert_collection_vector(LIB, f"COL{i}", f"Col {i}", _unit_vec(i), i)
        self.store.upsert_collection_vector("lib2", "COL_OTHER", "Other", _unit_vec(0), 0)

        all_vecs = self.store.get_all_collection_vectors(LIB)
        self.assertEqual(len(all_vecs), 3)
        col_ids = {t[0] for t in all_vecs}
        self.assertEqual(col_ids, {"COL0", "COL1", "COL2"})
        for col_id, vec, payload in all_vecs:
            self.assertIsInstance(vec, list)
            self.assertEqual(len(vec), DIM)
            self.assertIsInstance(payload, dict)

    def test_search_collection_vectors_top_result(self):
        """The closest collection should score highest."""
        # COL0 vector is (1,0,0,...), COL1 vector is (0,1,0,...)
        self.store.upsert_collection_vector(LIB, "COL0", "A", _unit_vec(0), 5)
        self.store.upsert_collection_vector(LIB, "COL1", "B", _unit_vec(1), 3)

        # Query along dimension 0 — COL0 should be #1
        results = self.store.search_collection_vectors(_unit_vec(0), LIB, limit=2)
        self.assertEqual(len(results), 2)
        col_ids = [r[0] for r in results]
        self.assertEqual(col_ids[0], "COL0")
        # Scores are sorted descending
        self.assertGreaterEqual(results[0][1], results[1][1])

    def test_search_respects_library_filter(self):
        self.store.upsert_collection_vector(LIB, "COL1", "Mine", _unit_vec(0), 1)
        self.store.upsert_collection_vector("lib2", "COL2", "Theirs", _unit_vec(0), 1)

        results = self.store.search_collection_vectors(_unit_vec(0), LIB, limit=10)
        col_ids = [r[0] for r in results]
        self.assertIn("COL1", col_ids)
        self.assertNotIn("COL2", col_ids)

    def test_count_collection_vectors(self):
        self.assertEqual(self.store.count_collection_vectors(LIB), 0)
        self.store.upsert_collection_vector(LIB, "COL1", "A", _unit_vec(0), 1)
        self.assertEqual(self.store.count_collection_vectors(LIB), 1)

    def test_delete_collection_vector(self):
        self.store.upsert_collection_vector(LIB, "COL1", "A", _unit_vec(0), 1)
        deleted = self.store.delete_collection_vector(LIB, "COL1")
        self.assertEqual(deleted, 1)
        self.assertIsNone(self.store.get_collection_vector(LIB, "COL1"))
        self.assertEqual(self.store.count_collection_vectors(LIB), 0)

    def test_delete_collection_vector_not_found(self):
        deleted = self.store.delete_collection_vector(LIB, "GHOST")
        self.assertEqual(deleted, 0)


class TestDeleteLibraryItemVectors(unittest.TestCase):
    """delete_library_item_vectors removes all item vectors for a library."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.store = _make_store(self.temp_dir)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_deletes_all_item_vectors_for_library(self):
        for i in range(3):
            self.store.upsert_item_vector(LIB, f"ITEM{i}", _unit_vec(i), [], "abstract", f"T{i}")
        # Add item in a different library — must not be deleted
        self.store.upsert_item_vector("lib2", "OTHER", _unit_vec(0), [], "abstract", "X")

        deleted = self.store.delete_library_item_vectors(LIB)

        self.assertEqual(deleted, 3)
        self.assertEqual(self.store.count_item_vectors(LIB), 0)
        # Other library untouched
        self.assertEqual(self.store.count_item_vectors("lib2"), 1)

    def test_returns_zero_when_library_has_no_vectors(self):
        deleted = self.store.delete_library_item_vectors("empty_lib")
        self.assertEqual(deleted, 0)


class TestDeleteLibraryCollectionVectors(unittest.TestCase):
    """delete_library_collection_vectors removes all collection vectors for a library."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.store = _make_store(self.temp_dir)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_deletes_all_collection_vectors_for_library(self):
        for i in range(4):
            self.store.upsert_collection_vector(LIB, f"COL{i}", f"Col {i}", _unit_vec(i), i)
        # Add collection in a different library — must not be deleted
        self.store.upsert_collection_vector("lib2", "COL_OTHER", "Other", _unit_vec(0), 0)

        deleted = self.store.delete_library_collection_vectors(LIB)

        self.assertEqual(deleted, 4)
        self.assertEqual(self.store.count_collection_vectors(LIB), 0)
        # Other library untouched
        self.assertEqual(self.store.count_collection_vectors("lib2"), 1)

    def test_returns_zero_when_library_has_no_vectors(self):
        deleted = self.store.delete_library_collection_vectors("empty_lib")
        self.assertEqual(deleted, 0)


if __name__ == "__main__":
    unittest.main()
