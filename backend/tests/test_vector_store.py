"""
Unit tests for vector store.
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
    DeduplicationRecord,
)


class TestVectorStore(unittest.TestCase):
    """Test vector store functionality."""

    def setUp(self):
        """Set up test fixtures."""
        # Create temporary directory for test database
        self.temp_dir = tempfile.mkdtemp()
        self.storage_path = Path(self.temp_dir) / "qdrant"

        # Initialize vector store
        self.vector_store = VectorStore(
            storage_path=self.storage_path,
            embedding_dim=384,
            embedding_model_name="test-model",
            distance=Distance.COSINE,
        )

    def tearDown(self):
        """Clean up after tests."""
        # Remove temporary directory
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_init(self):
        """Test vector store initialization."""
        self.assertTrue(self.storage_path.exists())
        self.assertEqual(self.vector_store.embedding_dim, 384)

    def test_collections_created(self):
        """Test that collections are created."""
        info = self.vector_store.get_collection_info()
        self.assertIn("chunks_count", info)
        self.assertIn("dedup_count", info)
        self.assertEqual(info["embedding_dim"], 384)

    def test_add_chunk(self):
        """Test adding a single chunk."""
        chunk = DocumentChunk(
            text="This is a test chunk.",
            metadata=ChunkMetadata(
                chunk_id="chunk-001",
                document_metadata=DocumentMetadata(
                    library_id="1",
                    item_key="ABC123",
                    title="Test Paper",
                    authors=["Author One"],
                    year=2024,
                ),
                page_number=1,
                text_preview="This is a",
                chunk_index=0,
                content_hash="hash123",
            ),
            embedding=[0.1] * 384,  # Dummy embedding
        )

        point_id = self.vector_store.add_chunk(chunk)
        self.assertIsNotNone(point_id)

        # Verify chunk was added
        info = self.vector_store.get_collection_info()
        self.assertEqual(info["chunks_count"], 1)

    def test_add_chunk_without_embedding_raises_error(self):
        """Test that adding chunk without embedding raises error."""
        chunk = DocumentChunk(
            text="Test",
            metadata=ChunkMetadata(
                chunk_id="chunk-001",
                document_metadata=DocumentMetadata(
                    library_id="1",
                    item_key="ABC123",
                ),
                page_number=1,
                text_preview="Test",
                chunk_index=0,
                content_hash="hash",
            ),
            embedding=None,  # No embedding
        )

        with self.assertRaises(ValueError):
            self.vector_store.add_chunk(chunk)

    def test_add_chunks_batch(self):
        """Test adding multiple chunks in batch."""
        chunks = [
            DocumentChunk(
                text=f"Chunk {i}",
                metadata=ChunkMetadata(
                    chunk_id=f"chunk-{i:03d}",
                    document_metadata=DocumentMetadata(
                        library_id="1",
                        item_key="ABC123",
                    ),
                    page_number=1,
                    text_preview=f"Chunk {i}",
                    chunk_index=i,
                    content_hash=f"hash{i}",
                ),
                embedding=[float(i)] * 384,
            )
            for i in range(10)
        ]

        point_ids = self.vector_store.add_chunks_batch(chunks)
        self.assertEqual(len(point_ids), 10)

        # Verify chunks were added
        info = self.vector_store.get_collection_info()
        self.assertEqual(info["chunks_count"], 10)

    def test_search(self):
        """Test similarity search."""
        # Add some chunks
        chunks = [
            DocumentChunk(
                text="Machine learning is great",
                metadata=ChunkMetadata(
                    chunk_id="chunk-001",
                    document_metadata=DocumentMetadata(
                        library_id="1",
                        item_key="ABC123",
                        title="ML Paper",
                    ),
                    page_number=1,
                    text_preview="Machine learning is",
                    chunk_index=0,
                    content_hash="hash1",
                ),
                embedding=[1.0, 0.0] + [0.0] * 382,
            ),
            DocumentChunk(
                text="Deep learning is powerful",
                metadata=ChunkMetadata(
                    chunk_id="chunk-002",
                    document_metadata=DocumentMetadata(
                        library_id="1",
                        item_key="DEF456",
                        title="DL Paper",
                    ),
                    page_number=1,
                    text_preview="Deep learning is",
                    chunk_index=0,
                    content_hash="hash2",
                ),
                embedding=[0.9, 0.1] + [0.0] * 382,
            ),
        ]
        self.vector_store.add_chunks_batch(chunks)

        # Search with similar query
        query_vector = [1.0, 0.0] + [0.0] * 382
        results = self.vector_store.search(query_vector, limit=2)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].chunk.metadata.chunk_id, "chunk-001")
        self.assertGreater(results[0].score, results[1].score)

    def test_search_with_library_filter(self):
        """Test search with library ID filter."""
        # Add chunks from different libraries
        chunks = [
            DocumentChunk(
                text="Text from library 1",
                metadata=ChunkMetadata(
                    chunk_id="chunk-lib1",
                    document_metadata=DocumentMetadata(
                        library_id="1",
                        item_key="ABC123",
                    ),
                    page_number=1,
                    text_preview="Text from library",
                    chunk_index=0,
                    content_hash="hash1",
                ),
                embedding=[1.0] * 384,
            ),
            DocumentChunk(
                text="Text from library 2",
                metadata=ChunkMetadata(
                    chunk_id="chunk-lib2",
                    document_metadata=DocumentMetadata(
                        library_id="2",
                        item_key="DEF456",
                    ),
                    page_number=1,
                    text_preview="Text from library",
                    chunk_index=0,
                    content_hash="hash2",
                ),
                embedding=[1.0] * 384,
            ),
        ]
        self.vector_store.add_chunks_batch(chunks)

        # Search only in library 1
        query_vector = [1.0] * 384
        results = self.vector_store.search(
            query_vector,
            limit=10,
            library_ids=["1"],
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].chunk.metadata.chunk_id, "chunk-lib1")

    def test_check_duplicate(self):
        """Test checking for duplicates."""
        # Add deduplication record
        record = DeduplicationRecord(
            content_hash="unique-hash-123",
            library_id="1",
            item_key="ABC123",
            relation_uri="http://zotero.org/users/1/items/ABC123",
        )
        self.vector_store.add_deduplication_record(record)

        # Check for duplicate
        found = self.vector_store.check_duplicate("unique-hash-123")
        self.assertIsNotNone(found)
        self.assertEqual(found.content_hash, "unique-hash-123")
        self.assertEqual(found.item_key, "ABC123")

        # Check non-existent
        not_found = self.vector_store.check_duplicate("non-existent-hash")
        self.assertIsNone(not_found)

    def test_add_catalog_stub_is_retrievable_via_metadata_scroll(self):
        """A catalog-only stub (no attachment, short abstract) must surface in
        get_items_by_metadata even though it carries no embeddable text."""
        from backend.models.filters import MetadataFilters

        doc_metadata = DocumentMetadata(
            library_id="1",
            item_key="WASSERMANN1",
            title="Der soziale Zivilprozess",
            authors=["Rudolf Wassermann"],
            year=1973,
            item_type="book",
        )
        self.vector_store.add_catalog_stub(doc_metadata, item_version=42)

        results = self.vector_store.get_items_by_metadata(
            library_ids=["1"], filters=MetadataFilters(authors=["wassermann"])
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["item_key"], "WASSERMANN1")
        self.assertEqual(results[0]["title"], "Der soziale Zivilprozess")
        self.assertFalse(results[0]["has_content"])

    def test_add_chunk_stores_tags_and_filters_case_insensitively(self):
        """Tags are stored verbatim for display but matched case-insensitively,
        mirroring the existing author_lastnames filter pattern."""
        from backend.models.filters import MetadataFilters

        chunk = DocumentChunk(
            text="Text about legal sociology.",
            metadata=ChunkMetadata(
                chunk_id="chunk-tags-1",
                document_metadata=DocumentMetadata(
                    library_id="1",
                    item_key="TAGGED1",
                    title="Tagged Item",
                    tags=["Rechtssoziologie", "Zivilprozessrecht"],
                ),
                page_number=1,
                text_preview="Text about legal",
                chunk_index=0,
                content_hash="hash-tags-1",
            ),
            embedding=[0.1] * 384,
        )
        self.vector_store.add_chunk(chunk)

        results = self.vector_store.get_items_by_metadata(
            library_ids=["1"], filters=MetadataFilters(tags=["rechtssoziologie"])
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["item_key"], "TAGGED1")
        # Original casing preserved for display
        self.assertEqual(results[0]["tags"], ["Rechtssoziologie", "Zivilprozessrecht"])

        # A tag that isn't present must not match
        no_match = self.vector_store.get_items_by_metadata(
            library_ids=["1"], filters=MetadataFilters(tags=["unrelatedtag"])
        )
        self.assertEqual(no_match, [])

    def test_search_excludes_catalog_stub(self):
        """Semantic search must never surface a catalog-only stub, even when its
        placeholder vector is the closest match to the query."""
        doc_metadata = DocumentMetadata(
            library_id="1",
            item_key="STUB1",
            title="Stub Item",
            authors=[],
        )
        self.vector_store.add_catalog_stub(doc_metadata, item_version=1)

        real_chunk = DocumentChunk(
            text="Deep learning is powerful",
            metadata=ChunkMetadata(
                chunk_id="chunk-real",
                document_metadata=DocumentMetadata(
                    library_id="1",
                    item_key="REAL1",
                    title="Real Item",
                ),
                page_number=1,
                text_preview="Deep learning is",
                chunk_index=0,
                content_hash="hash-real",
            ),
            embedding=[0.0, 1.0] + [0.0] * 382,
        )
        self.vector_store.add_chunk(real_chunk)

        # Query vector matches the stub's placeholder embedding ([1.0, 0.0, ...])
        # far better than the real chunk's — if exclusion didn't work, the stub
        # would win.
        query_vector = [1.0, 0.0] + [0.0] * 382
        results = self.vector_store.search(query_vector, limit=10)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].chunk.metadata.document_metadata.item_key, "REAL1")

    def test_get_stub_item_keys(self):
        """get_stub_item_keys returns only item_keys whose sole record is a stub."""
        stub_metadata = DocumentMetadata(library_id="1", item_key="STUB1", title="Stub")
        self.vector_store.add_catalog_stub(stub_metadata, item_version=1)

        real_chunk = DocumentChunk(
            text="Real content",
            metadata=ChunkMetadata(
                chunk_id="chunk-real",
                document_metadata=DocumentMetadata(
                    library_id="1", item_key="REAL1", title="Real"
                ),
                page_number=1,
                text_preview="Real content",
                chunk_index=0,
                content_hash="hash-real",
                item_version=1,
            ),
            embedding=[0.5] * 384,
        )
        self.vector_store.add_chunk(real_chunk)

        stub_keys = self.vector_store.get_stub_item_keys("1")
        self.assertEqual(stub_keys, {"STUB1"})

    def test_update_item_bibliographic_metadata_patches_all_chunks(self):
        """update_item_bibliographic_metadata must patch title/authors/tags/
        year/item_type/item_version on every existing chunk without touching
        text or embeddings — used for the metadata-only reindex fast path."""
        chunks = [
            DocumentChunk(
                text=f"Chunk {i}",
                metadata=ChunkMetadata(
                    chunk_id=f"chunk-{i}",
                    document_metadata=DocumentMetadata(
                        library_id="1",
                        item_key="ITEM1",
                        title="Old Title",
                        authors=["Old Author"],
                        year=2000,
                        item_type="book",
                    ),
                    page_number=1,
                    text_preview=f"Chunk {i}",
                    chunk_index=i,
                    content_hash=f"hash{i}",
                    item_version=1,
                ),
                embedding=[0.1] * 384,
            )
            for i in range(3)
        ]
        self.vector_store.add_chunks_batch(chunks)

        updated = self.vector_store.update_item_bibliographic_metadata(
            "1", "ITEM1",
            title="New Title", authors=["New Author"], tags=["Law"],
            year=2020, item_type="journalArticle", item_version=2,
            zotero_modified="2026-01-01T00:00:00Z",
        )

        self.assertEqual(updated, 3)
        results = self.vector_store.get_item_chunks("1", "ITEM1")
        self.assertEqual(len(results), 3)
        for r in results:
            payload = r["payload"]
            self.assertEqual(payload["title"], "New Title")
            self.assertEqual(payload["authors"], ["New Author"])
            self.assertEqual(payload["author_lastnames"], ["author"])
            self.assertEqual(payload["tags"], ["Law"])
            self.assertEqual(payload["tags_lower"], ["law"])
            self.assertEqual(payload["year"], 2020)
            self.assertEqual(payload["item_type"], "journalArticle")
            self.assertEqual(payload["item_version"], 2)
            self.assertEqual(payload["zotero_modified"], "2026-01-01T00:00:00Z")
            # Text and embedding must be untouched by the patch
            self.assertEqual(payload["text"], f"Chunk {payload['chunk_index']}")

    def test_delete_library_chunks(self):
        """Test deleting all chunks for a library."""
        # Add chunks from different libraries
        chunks = [
            DocumentChunk(
                text=f"Chunk from lib {lib_id}",
                metadata=ChunkMetadata(
                    chunk_id=f"chunk-{lib_id}-{i}",
                    document_metadata=DocumentMetadata(
                        library_id=str(lib_id),
                        item_key=f"ITEM{i}",
                    ),
                    page_number=1,
                    text_preview=f"Chunk from lib",
                    chunk_index=i,
                    content_hash=f"hash{lib_id}{i}",
                ),
                embedding=[float(lib_id)] * 384,
            )
            for lib_id in [1, 2]
            for i in range(5)
        ]
        self.vector_store.add_chunks_batch(chunks)

        # Verify 10 chunks added
        info = self.vector_store.get_collection_info()
        self.assertEqual(info["chunks_count"], 10)

        # Delete library 1 chunks
        deleted_count = self.vector_store.delete_library_chunks("1")
        self.assertEqual(deleted_count, 5)

        # Verify only 5 chunks remain
        info = self.vector_store.get_collection_info()
        self.assertEqual(info["chunks_count"], 5)


if __name__ == "__main__":
    unittest.main()
