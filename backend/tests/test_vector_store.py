"""
Unit tests for vector store.
"""

import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

from httpx import ReadTimeout
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from qdrant_client.models import Distance

from backend.db.vector_store import VectorStore, VectorStoreError, VectorStoreTimeoutError
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

    def test_chunks_collection_created_with_scalar_quantization(self):
        """New document_chunks collections enable int8 scalar quantization and
        store original vectors on-disk, so the RAM-resident vector set stays a
        quarter of its unquantized size as the collection grows into the
        millions of points (production capacity incident: a 7.24M-point,
        unquantized, RAM-resident collection on a 15GB host caused severe
        Qdrant query latency)."""
        from qdrant_client import QdrantClient
        from qdrant_client.models import ScalarQuantization

        temp_dir = tempfile.mkdtemp()
        storage_path = Path(temp_dir) / "qdrant"
        try:
            with patch.object(
                QdrantClient, "create_collection", autospec=True, side_effect=QdrantClient.create_collection
            ) as mock_create:
                VectorStore(
                    storage_path=storage_path,
                    embedding_dim=384,
                    embedding_model_name="test-model",
                    distance=Distance.COSINE,
                )

            chunks_calls = [
                c for c in mock_create.call_args_list if c.kwargs.get("collection_name") == VectorStore.CHUNKS_COLLECTION
            ]
            self.assertEqual(len(chunks_calls), 1)
            call = chunks_calls[0]
            self.assertIsInstance(call.kwargs.get("quantization_config"), ScalarQuantization)
            self.assertTrue(call.kwargs["vectors_config"].on_disk)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

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

    def test_add_chunk_stores_attachment_title_in_payload(self):
        chunk = DocumentChunk(
            text="Snapshot page text.",
            metadata=ChunkMetadata(
                chunk_id="chunk-snap-1",
                document_metadata=DocumentMetadata(
                    library_id="1",
                    item_key="ABC123",
                    attachment_key="ATT1",
                    attachment_title="Snapshot",
                    title="Test Paper",
                ),
                page_number=1,
                text_preview="Snapshot page",
                chunk_index=0,
                content_hash="hash-snap-1",
            ),
            embedding=[0.1] * 384,
        )
        point_id = self.vector_store.add_chunk(chunk)
        points = self.vector_store.client.retrieve(
            collection_name=self.vector_store.CHUNKS_COLLECTION, ids=[point_id]
        )
        self.assertEqual(points[0].payload.get("attachment_title"), "Snapshot")

    def test_copy_chunks_cross_library_preserves_attachment_title(self):
        source = DocumentChunk(
            text="Snapshot page text.",
            metadata=ChunkMetadata(
                chunk_id="chunk-src-1",
                document_metadata=DocumentMetadata(
                    library_id="1", item_key="SRC1", attachment_key="SA1",
                    attachment_title="Snapshot",
                ),
                page_number=1, text_preview="Snapshot page", chunk_index=0,
                content_hash="hash-cross-1",
            ),
            embedding=[0.1] * 384,
        )
        self.vector_store.add_chunk(source)
        target_meta = DocumentMetadata(
            library_id="2", item_key="TGT1", attachment_key="TA1",
            attachment_title="Snapshot", title="Copied",
        )
        copied = self.vector_store.copy_chunks_cross_library(
            source_library_id="1", source_item_key="SRC1",
            target_library_id="2", target_item_key="TGT1",
            target_attachment_key="TA1", target_doc_metadata=target_meta,
            target_item_version=1, target_attachment_version=1,
            target_item_modified="2026-01-01T00:00:00Z",
        )
        self.assertEqual(copied, 1)
        chunks = self.vector_store.get_item_chunks("2", "TGT1")
        self.assertEqual(chunks[0]["payload"].get("attachment_title"), "Snapshot")

    def _add_snapshot_chunk(self, library_id, item_key, attachment_key, chunk_id):
        chunk = DocumentChunk(
            text="Snapshot text.",
            metadata=ChunkMetadata(
                chunk_id=chunk_id,
                document_metadata=DocumentMetadata(
                    library_id=library_id, item_key=item_key,
                    attachment_key=attachment_key, attachment_title="Snapshot",
                ),
                page_number=1, text_preview="Snapshot text", chunk_index=0,
                content_hash=chunk_id,
            ),
            embedding=[0.1] * 384,
        )
        return self.vector_store.add_chunk(chunk)

    def test_delete_snapshot_chunks_removes_only_snapshot_titled_chunks_across_libraries(self):
        self._add_snapshot_chunk("1", "ITEM1", "SNAP1", "chunk-s1")
        self._add_snapshot_chunk("2", "ITEM2", "SNAP2", "chunk-s2")
        # A normal (non-Snapshot) chunk in library 1 must survive.
        pdf_chunk = DocumentChunk(
            text="PDF text.",
            metadata=ChunkMetadata(
                chunk_id="chunk-pdf",
                document_metadata=DocumentMetadata(
                    library_id="1", item_key="ITEM1", attachment_key="PDF1",
                    attachment_title="Full Paper.pdf",
                ),
                page_number=1, text_preview="PDF text", chunk_index=0,
                content_hash="chunk-pdf",
            ),
            embedding=[0.1] * 384,
        )
        self.vector_store.add_chunk(pdf_chunk)
        # A legacy chunk with no attachment_title at all must also survive.
        legacy_chunk = DocumentChunk(
            text="Legacy text.",
            metadata=ChunkMetadata(
                chunk_id="chunk-legacy",
                document_metadata=DocumentMetadata(
                    library_id="1", item_key="ITEM3", attachment_key="LEG1",
                ),
                page_number=1, text_preview="Legacy text", chunk_index=0,
                content_hash="chunk-legacy",
            ),
            embedding=[0.1] * 384,
        )
        self.vector_store.add_chunk(legacy_chunk)

        deleted_chunks, deleted_attachments = self.vector_store.delete_snapshot_chunks()

        self.assertEqual(deleted_chunks, 2)
        self.assertEqual(deleted_attachments, 2)
        self.assertEqual(len(self.vector_store.get_item_chunks("1", "ITEM1")), 1)  # PDF chunk survives
        self.assertEqual(self.vector_store.get_item_chunks("1", "ITEM1")[0]["payload"]["attachment_key"], "PDF1")
        self.assertEqual(len(self.vector_store.get_item_chunks("2", "ITEM2")), 0)
        self.assertEqual(len(self.vector_store.get_item_chunks("1", "ITEM3")), 1)  # legacy chunk survives

    def test_delete_snapshot_chunks_returns_zero_when_nothing_matches(self):
        deleted_chunks, deleted_attachments = self.vector_store.delete_snapshot_chunks()
        self.assertEqual((deleted_chunks, deleted_attachments), (0, 0))

    def test_get_chunks_by_ids_returns_matching_payloads(self):
        chunk_a = DocumentChunk(
            text="Chunk A text",
            metadata=ChunkMetadata(
                chunk_id="chunk-A", document_metadata=DocumentMetadata(
                    library_id="1", item_key="ITEM1", title="Doc A",
                ),
                page_number=1, text_preview="Chunk A", chunk_index=0, content_hash="hA",
            ),
            embedding=[0.1] * 384,
        )
        chunk_b = DocumentChunk(
            text="Chunk B text",
            metadata=ChunkMetadata(
                chunk_id="chunk-B", document_metadata=DocumentMetadata(
                    library_id="1", item_key="ITEM2", title="Doc B",
                ),
                page_number=1, text_preview="Chunk B", chunk_index=0, content_hash="hB",
            ),
            embedding=[0.2] * 384,
        )
        self.vector_store.add_chunk(chunk_a)
        self.vector_store.add_chunk(chunk_b)

        found = self.vector_store.get_chunks_by_ids(["chunk-A", "chunk-nonexistent"])

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["payload"]["chunk_id"], "chunk-A")
        self.assertEqual(found[0]["payload"]["text"], "Chunk A text")

    def test_get_chunks_by_ids_empty_list_returns_empty(self):
        self.assertEqual(self.vector_store.get_chunks_by_ids([]), [])

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

    def test_search_raises_vector_store_timeout_error_on_read_timeout(self):
        """A raw httpx ReadTimeout from the client is converted to VectorStoreTimeoutError."""
        with patch.object(
            self.vector_store.client, "query_points", side_effect=ReadTimeout("timed out")
        ):
            with self.assertRaises(VectorStoreTimeoutError):
                self.vector_store.search([1.0, 0.0] + [0.0] * 382, limit=2)

    def test_search_raises_vector_store_timeout_error_on_response_handling_timeout(self):
        """A qdrant_client ResponseHandlingException wrapping a timeout is converted too."""
        with patch.object(
            self.vector_store.client,
            "query_points",
            side_effect=ResponseHandlingException("timed out"),
        ):
            with self.assertRaises(VectorStoreTimeoutError):
                self.vector_store.search([1.0, 0.0] + [0.0] * 382, limit=2)

    def test_search_reraises_non_timeout_response_handling_exception(self):
        """A ResponseHandlingException NOT about a timeout must propagate unchanged."""
        with patch.object(
            self.vector_store.client,
            "query_points",
            side_effect=ResponseHandlingException("connection refused"),
        ):
            with self.assertRaises(ResponseHandlingException):
                self.vector_store.search([1.0, 0.0] + [0.0] * 382, limit=2)

    def test_search_raises_vector_store_error_with_real_detail_on_unexpected_response(self):
        """Observed live: a full disk makes Qdrant return a 500 with a specific
        body ("No space left on device: ..."), not a timeout. This must surface
        as a distinct VectorStoreError whose message includes that real detail —
        not get lost or miscategorized as a generic timeout."""
        body = b'{"status":{"error":"No space left on device: WAL buffer size exceeds available disk space"},"time":0.0}'
        with patch.object(
            self.vector_store.client,
            "query_points",
            side_effect=UnexpectedResponse(
                status_code=500, reason_phrase="Internal Server Error", content=body, headers={},
            ),
        ):
            with self.assertRaises(VectorStoreError) as ctx:
                self.vector_store.search([1.0, 0.0] + [0.0] * 382, limit=2)
            self.assertIn("No space left on device", str(ctx.exception))

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

    def test_update_item_metadata_paginates_past_a_single_scroll_page(self):
        """A single-page scroll() call caps at 1000 points; an item with more
        chunks than that must still have every chunk patched, not just the
        first 1000 — regression test for a bug found via manual testing where
        the remaining chunks silently kept a stale payload."""
        chunk_count = 1050
        chunks = [
            DocumentChunk(
                text=f"Chunk {i}",
                metadata=ChunkMetadata(
                    chunk_id=f"chunk-{i}",
                    document_metadata=DocumentMetadata(
                        library_id="1",
                        item_key="BIGITEM",
                        title="Old Title",
                    ),
                    page_number=1,
                    text_preview=f"Chunk {i}",
                    chunk_index=i,
                    content_hash=f"hash{i}",
                ),
                embedding=[0.1] * 384,
            )
            for i in range(chunk_count)
        ]
        self.vector_store.add_chunks_batch(chunks)

        updated = self.vector_store.update_item_metadata("1", "BIGITEM", {"title": "New Title"})

        self.assertEqual(updated, chunk_count)
        results = self.vector_store.get_item_chunks("1", "BIGITEM")
        self.assertEqual(len(results), chunk_count)
        self.assertTrue(all(r["payload"]["title"] == "New Title" for r in results))

    def test_update_item_metadata_raises_vector_store_error_with_real_detail(self):
        """set_payload() had no error handling at all in production — a disk-full
        UnexpectedResponse from it propagated raw. It must now be converted to a
        VectorStoreError carrying the real Qdrant error message, like search()."""
        chunk = DocumentChunk(
            text="Chunk",
            metadata=ChunkMetadata(
                chunk_id="chunk-1",
                document_metadata=DocumentMetadata(library_id="1", item_key="ITEM1", title="Old"),
                page_number=1,
                text_preview="Chunk",
                chunk_index=0,
                content_hash="hash1",
            ),
            embedding=[0.1] * 384,
        )
        self.vector_store.add_chunks_batch([chunk])

        body = b'{"status":{"error":"No space left on device: WAL buffer size exceeds available disk space"},"time":0.0}'
        with patch.object(
            self.vector_store.client,
            "set_payload",
            side_effect=UnexpectedResponse(
                status_code=500, reason_phrase="Internal Server Error", content=body, headers={},
            ),
        ):
            with self.assertRaises(VectorStoreError) as ctx:
                self.vector_store.update_item_metadata("1", "ITEM1", {"title": "New"})
            self.assertIn("No space left on device", str(ctx.exception))

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

    def test_delete_chunks_by_ids_targets_only_given_points(self):
        """delete_chunks_by_ids must remove exactly the given point IDs and
        leave every other point untouched — including other points for the
        same item_key. This is what makes a safe reindex-then-swap possible:
        reprocess into fresh points first, and only delete the old ones (by
        their captured IDs) after confirming the new ones were written,
        instead of deleting-by-item_key upfront and risking an item left with
        zero chunks if reprocessing then fails to produce replacements."""
        chunks = [
            DocumentChunk(
                text=f"Chunk {i}",
                metadata=ChunkMetadata(
                    chunk_id=f"chunk-{i}",
                    document_metadata=DocumentMetadata(library_id="1", item_key="ITEM1"),
                    page_number=1,
                    text_preview="Chunk",
                    chunk_index=i,
                    content_hash=f"hash{i}",
                ),
                embedding=[0.1] * 384,
            )
            for i in range(3)
        ]
        point_ids = self.vector_store.add_chunks_batch(chunks)

        deleted = self.vector_store.delete_chunks_by_ids(point_ids[:2])
        self.assertEqual(deleted, 2)

        remaining = self.vector_store.get_item_chunks("1", "ITEM1")
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], point_ids[2])


class MigrationExportImportTest(unittest.TestCase):
    """Tests for export_points/import_points/count_library_points, which back
    the cross-instance library migration endpoints in backend/api/migration.py.
    See docs/superpowers/specs/2026-10-05-library-rag-migration-design.md."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.vector_store = VectorStore(
            storage_path=Path(self.temp_dir) / "qdrant",
            embedding_dim=4,
            embedding_model_name="test-model",
            distance=Distance.COSINE,
        )
        self._add_chunk("u1", "item-a", [0.1, 0.2, 0.3, 0.4])
        self._add_chunk("u1", "item-b", [0.5, 0.6, 0.7, 0.8])
        self._add_chunk("u2", "item-c", [0.9, 0.9, 0.9, 0.9])
        self.vector_store.add_deduplication_record(
            DeduplicationRecord(content_hash="hash-1", library_id="u1", item_key="item-a")
        )
        self.vector_store.add_deduplication_record(
            DeduplicationRecord(content_hash="hash-2", library_id="u2", item_key="item-c")
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _add_chunk(self, library_id, item_key, embedding):
        chunk = DocumentChunk(
            text="some text",
            embedding=embedding,
            metadata=ChunkMetadata(
                chunk_id=f"{item_key}-chunk-0",
                document_metadata=DocumentMetadata(library_id=library_id, item_key=item_key),
                text_preview="some text",
                chunk_index=0,
                content_hash=f"hash-of-{item_key}",
            ),
        )
        self.vector_store.add_chunk(chunk)

    def test_count_library_points_scopes_to_library(self):
        self.assertEqual(self.vector_store.count_library_points("chunks", "u1"), 2)
        self.assertEqual(self.vector_store.count_library_points("chunks", "u2"), 1)
        self.assertEqual(self.vector_store.count_library_points("dedup", "u1"), 1)

    def test_export_points_paginates_and_filters_by_library(self):
        page1, offset1 = self.vector_store.export_points("chunks", "u1", offset=None, limit=1)
        self.assertEqual(len(page1), 1)
        self.assertIsNotNone(offset1)

        page2, offset2 = self.vector_store.export_points("chunks", "u1", offset=offset1, limit=1)
        self.assertEqual(len(page2), 1)
        self.assertIsNone(offset2)

        all_item_keys = {p["payload"]["item_key"] for p in (page1 + page2)}
        self.assertEqual(all_item_keys, {"item-a", "item-b"})
        self.assertEqual(len(page1[0]["vector"]), 4)

    def test_export_points_unknown_collection_raises(self):
        with self.assertRaises(ValueError):
            self.vector_store.export_points("not-a-collection", "u1", offset=None, limit=10)

    def test_import_points_round_trips_into_a_fresh_store(self):
        points, _ = self.vector_store.export_points("chunks", "u1", offset=None, limit=10)
        dest_dir = tempfile.mkdtemp()
        try:
            dest_store = VectorStore(
                storage_path=Path(dest_dir) / "qdrant",
                embedding_dim=4,
                embedding_model_name="test-model",
                distance=Distance.COSINE,
            )
            imported = dest_store.import_points("chunks", points)
            self.assertEqual(imported, 2)
            self.assertEqual(dest_store.count_library_points("chunks", "u1"), 2)
            dest_points, _ = dest_store.export_points("chunks", "u1", offset=None, limit=10)
            dest_by_id = {p["id"]: p for p in dest_points}
            for original in points:
                self.assertEqual(dest_by_id[original["id"]]["payload"], original["payload"])
                self.assertEqual(dest_by_id[original["id"]]["vector"], original["vector"])
        finally:
            shutil.rmtree(dest_dir, ignore_errors=True)

    def test_import_points_empty_list_is_noop(self):
        self.assertEqual(self.vector_store.import_points("chunks", []), 0)


if __name__ == "__main__":
    unittest.main()
