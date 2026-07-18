"""
Unit tests for document processor.
"""

import unittest
import hashlib
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from backend.services.document_processor import DocumentProcessor
from backend.services.extraction.base import DocumentExtractor, ExtractionChunk
from backend.models.document import (
    DocumentMetadata,
    ChunkMetadata,
    DocumentChunk,
    DeduplicationRecord,
)


def _make_extraction_chunks(*texts_and_pages) -> list[ExtractionChunk]:
    """Helper: build ExtractionChunk list from (text, page) tuples."""
    return [
        ExtractionChunk(text=text, page_number=page, chunk_index=i)
        for i, (text, page) in enumerate(texts_and_pages)
    ]


def _attachment(key: str, parent_key: str, content_type: str = "application/pdf") -> dict:
    """Build an attachment item as Zotero's full /items fetch returns it.

    The real Zotero API returns attachments as top-level items carrying a
    ``parentItem`` link — not only via /items/{key}/children.  Full-sync builds
    its parent→children map from this list, so test fixtures must include
    attachments here with ``parentItem`` set.
    """
    return {
        "data": {
            "key": key,
            "itemType": "attachment",
            "contentType": content_type,
            "parentItem": parent_key,
        }
    }


class TestDocumentProcessor(unittest.IsolatedAsyncioTestCase):
    """Test DocumentProcessor class."""

    def setUp(self):
        """Set up test fixtures."""
        # Create mock dependencies
        self.mock_zotero_client = AsyncMock()
        self.mock_embedding_service = AsyncMock()
        self.mock_vector_store = Mock()
        self.mock_vector_store.find_cross_library_duplicate.return_value = None

        # Mock document extractor (replaces pdf_extractor + text_chunker)
        self.mock_extractor = AsyncMock(spec=DocumentExtractor)

        # Create processor with explicit mock extractor
        self.processor = DocumentProcessor(
            zotero_client=self.mock_zotero_client,
            embedding_service=self.mock_embedding_service,
            vector_store=self.mock_vector_store,
            document_extractor=self.mock_extractor,
        )

        # Default stubs for new methods added by the sync-deletion changes
        self.mock_vector_store.get_all_indexed_item_versions.return_value = {}
        self.mock_zotero_client.get_deleted_item_keys.return_value = []

        # Default stubs for catalog-only stub records (non-indexable items)
        self.mock_vector_store.get_stub_item_keys.return_value = set()

        # Default: no existing chunks, so _try_metadata_only_update returns
        # False immediately and every existing test keeps exercising the
        # normal delete+reindex path unless it explicitly overrides this.
        self.mock_vector_store.get_item_chunks.return_value = []

    async def test_init(self):
        """Test initialization."""
        self.assertIsNotNone(self.processor.document_extractor)

    async def test_index_library_no_items(self):
        """Test indexing when library has no items."""
        self.mock_zotero_client.get_library_items_since.return_value = []

        result = await self.processor.index_library("test_lib")

        self.assertIn("mode", result)
        self.assertEqual(result["items_processed"], 0)
        self.assertEqual(result["chunks_added"], 0)

    async def test_index_library_skip_non_pdf_items(self):
        """Test that items without indexable attachments are skipped."""
        mock_item = {
            "data": {
                "key": "ITEM123",
                "itemType": "book",
                "title": "Test Book",
            }
        }

        self.mock_zotero_client.get_library_items_since.return_value = [mock_item]
        self.mock_zotero_client.get_item_children.return_value = []  # No attachments

        result = await self.processor.index_library("test_lib")

        self.assertIn("mode", result)
        self.assertEqual(result["items_processed"], 0)

    async def test_index_library_skip_attachments_and_notes(self):
        """Test that attachment and note items themselves are skipped."""
        mock_items = [
            {"data": {"key": "ATTACH1", "itemType": "attachment"}},
            {"data": {"key": "NOTE1", "itemType": "note"}},
        ]

        self.mock_zotero_client.get_library_items_since.return_value = mock_items

        result = await self.processor.index_library("test_lib")

        self.assertIn("mode", result)
        self.assertEqual(result["items_processed"], 0)

    async def test_index_library_with_pdf_success(self):
        """Test successful indexing of item with PDF attachment."""
        mock_item = {
            "version": 1,
            "data": {
                "key": "ITEM123",
                "itemType": "journalArticle",
                "title": "Test Paper",
                "creators": [
                    {"creatorType": "author", "firstName": "John", "lastName": "Doe"}
                ],
                "date": "2024-01-15",
            }
        }

        mock_pdf_attachment = _attachment("PDF123", "ITEM123")

        # Full /items fetch returns the attachment as a top-level item too.
        self.mock_zotero_client.get_library_items_since.return_value = [mock_item, mock_pdf_attachment]
        self.mock_zotero_client.get_item_children.return_value = [mock_pdf_attachment]
        self.mock_zotero_client.get_attachment_file.return_value = b"fake pdf bytes"

        # Mock vector store - no duplicate
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_vector_store.add_chunks_batch.return_value = ["id1", "id2"]

        # Mock extractor returning two chunks
        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(
            ("This is page one with some content.", 1),
            ("This is page two with more content.", 2),
        )

        # Mock embeddings
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

        result = await self.processor.index_library("test_lib")

        # Verify results
        self.assertIn("mode", result)
        self.assertEqual(result["items_processed"], 1)
        self.assertEqual(result["chunks_added"], 2)

        # Verify extractor was called with correct args
        self.mock_extractor.extract_and_chunk.assert_called_once_with(
            b"fake pdf bytes", "application/pdf"
        )

        # Verify embeddings were generated
        self.mock_embedding_service.embed_batch.assert_called_once()

        # Verify chunks were stored
        self.mock_vector_store.add_chunks_batch.assert_called_once()
        stored_chunks = self.mock_vector_store.add_chunks_batch.call_args[0][0]
        self.assertEqual(len(stored_chunks), 2)

        # Verify deduplication record was added
        self.mock_vector_store.add_deduplication_record.assert_called_once()

    async def test_index_library_skip_duplicate(self):
        """Test that duplicate attachments are skipped."""
        mock_item = {
            "version": 1,
            "data": {
                "key": "ITEM123",
                "itemType": "journalArticle",
                "title": "Test Paper",
            }
        }

        mock_pdf_attachment = _attachment("PDF123", "ITEM123")

        self.mock_zotero_client.get_library_items_since.return_value = [mock_item, mock_pdf_attachment]
        self.mock_zotero_client.get_item_children.return_value = [mock_pdf_attachment]
        self.mock_zotero_client.get_attachment_file.return_value = b"fake pdf bytes"

        # Mock duplicate found: same item already has this exact content indexed.
        duplicate_record = DeduplicationRecord(
            content_hash="existing_hash",
            library_id="test_lib",
            item_key="ITEM123",
            relation_uri=None,
        )
        self.mock_vector_store.check_duplicate.return_value = duplicate_record
        self.mock_vector_store.get_item_version.return_value = 1

        result = await self.processor.index_library("test_lib")

        self.assertEqual(result["chunks_added"], 0)
        # Extractor should not be called for duplicates
        self.mock_extractor.extract_and_chunk.assert_not_called()

    async def test_index_library_copies_duplicate_from_different_item(self):
        """A hash match belonging to a *different* item in the same library must
        get its own chunks copied, not be silently skipped — otherwise it can
        never be counted as indexed (regression test for the permanently-stuck
        "not indexed" bug)."""
        mock_item = {
            "version": 1,
            "data": {
                "key": "ITEM123",
                "itemType": "journalArticle",
                "title": "Test Paper",
            }
        }
        mock_pdf_attachment = _attachment("PDF123", "ITEM123")
        self.mock_zotero_client.get_library_items_since.return_value = [mock_item, mock_pdf_attachment]
        self.mock_zotero_client.get_item_children.return_value = [mock_pdf_attachment]
        self.mock_zotero_client.get_attachment_file.return_value = b"fake pdf bytes"

        duplicate_record = DeduplicationRecord(
            content_hash="existing_hash",
            library_id="test_lib",
            item_key="OTHER_ITEM",
            relation_uri=None,
        )
        self.mock_vector_store.check_duplicate.return_value = duplicate_record
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "chunk1", "payload": {"chunk_index": 0, "text": "shared content"}},
        ]
        self.mock_vector_store.copy_chunks_cross_library.return_value = 1

        result = await self.processor.index_library("test_lib")

        self.assertEqual(result["chunks_added"], 1)
        # Chunks are copied, not freshly extracted/embedded.
        self.mock_extractor.extract_and_chunk.assert_not_called()
        self.mock_embedding_service.embed_batch.assert_not_called()
        self.mock_vector_store.copy_chunks_cross_library.assert_called_once_with(
            "test_lib", "OTHER_ITEM", "test_lib", "ITEM123",
            "PDF123", unittest.mock.ANY, unittest.mock.ANY, unittest.mock.ANY, unittest.mock.ANY,
        )
        # The current item gets its own dedup record so it's no longer
        # dependent on OTHER_ITEM's record surviving.
        added_record = self.mock_vector_store.add_deduplication_record.call_args[0][0]
        self.assertEqual(added_record.item_key, "ITEM123")
        self.assertEqual(added_record.content_hash, "existing_hash")

    async def test_index_library_reindexes_when_duplicate_source_has_no_chunks(self):
        """If the matching item's chunks are gone (orphaned dedup record across
        items), fall through to fresh extraction instead of leaving this item
        unindexed forever."""
        mock_item = {
            "version": 1,
            "data": {
                "key": "ITEM123",
                "itemType": "journalArticle",
                "title": "Test Paper",
            }
        }
        mock_pdf_attachment = _attachment("PDF123", "ITEM123")
        self.mock_zotero_client.get_library_items_since.return_value = [mock_item, mock_pdf_attachment]
        self.mock_zotero_client.get_item_children.return_value = [mock_pdf_attachment]
        self.mock_zotero_client.get_attachment_file.return_value = b"fake pdf bytes"

        duplicate_record = DeduplicationRecord(
            content_hash="existing_hash",
            library_id="test_lib",
            item_key="OTHER_ITEM",
            relation_uri=None,
        )
        self.mock_vector_store.check_duplicate.return_value = duplicate_record
        self.mock_vector_store.get_item_chunks.return_value = []
        self.mock_vector_store.add_chunks_batch.return_value = ["id1"]
        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(
            ("Some content.", 1),
        )
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2, 0.3]]

        result = await self.processor.index_library("test_lib")

        self.assertEqual(result["chunks_added"], 1)
        self.mock_extractor.extract_and_chunk.assert_called_once()
        self.mock_vector_store.copy_chunks_cross_library.assert_not_called()

    async def test_index_from_abstract_copies_duplicate_from_different_item(self):
        """Abstract-based indexing must also copy (not silently skip) when the
        duplicate hash belongs to a different item in the same library."""
        abstract_text = "word " * 150  # exceeds default min_abstract_words=100
        doc_metadata = DocumentMetadata(
            library_id="test_lib",
            item_key="ITEM123",
            title="Test Paper",
            item_type="journalArticle",
        )
        duplicate_record = DeduplicationRecord(
            content_hash="existing_hash",
            library_id="test_lib",
            item_key="OTHER_ITEM",
            relation_uri=None,
        )
        self.mock_vector_store.check_duplicate.return_value = duplicate_record
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "chunk1", "payload": {"chunk_index": 0}},
        ]
        self.mock_vector_store.copy_chunks_cross_library.return_value = 1

        chunks_written = await self.processor._index_from_abstract(
            abstract_text=abstract_text,
            doc_metadata=doc_metadata,
            item_version=1,
            item_modified="2024-01-01T00:00:00Z",
        )

        self.assertEqual(chunks_written, 1)
        self.mock_vector_store.copy_chunks_cross_library.assert_called_once_with(
            "test_lib", "OTHER_ITEM", "test_lib", "ITEM123",
            "ITEM123:abstract", unittest.mock.ANY, 1, 0, "2024-01-01T00:00:00Z",
        )
        added_record = self.mock_vector_store.add_deduplication_record.call_args[0][0]
        self.assertEqual(added_record.item_key, "ITEM123")

    async def test_index_library_extraction_error(self):
        """Test handling of extraction errors."""
        mock_item = {
            "data": {
                "key": "ITEM123",
                "itemType": "journalArticle",
                "title": "Test Paper",
            }
        }

        mock_pdf_attachment = {
            "data": {
                "key": "PDF123",
                "itemType": "attachment",
                "contentType": "application/pdf",
            }
        }

        self.mock_zotero_client.get_library_items_since.return_value = [mock_item]
        self.mock_zotero_client.get_item_children.return_value = [mock_pdf_attachment]
        self.mock_zotero_client.get_attachment_file.return_value = b"corrupted pdf"

        self.mock_vector_store.check_duplicate.return_value = None

        # Mock extraction failure
        self.mock_extractor.extract_and_chunk.side_effect = ValueError("Extraction failed")

        result = await self.processor.index_library("test_lib")

        # Should handle error gracefully
        self.assertIn("mode", result)
        self.assertEqual(result["chunks_added"], 0)

    async def test_index_library_force_reindex(self):
        """Test force_reindex triggers full sync (smart sync, not a wipe-and-rebuild)."""
        from backend.models.library import LibraryIndexMetadata
        metadata = LibraryIndexMetadata(
            library_id="test_lib",
            library_type="user",
            library_name="Test Library",
            force_reindex=True
        )
        self.mock_vector_store.get_library_metadata.return_value = metadata

        self.mock_zotero_client.get_library_items_since.return_value = []

        result = await self.processor.index_library("test_lib", mode="auto")

        # Smart sync: upfront wipe must NOT happen
        self.mock_vector_store.delete_library_chunks.assert_not_called()
        # Full sync path: get_all_indexed_item_versions must be called
        self.mock_vector_store.get_all_indexed_item_versions.assert_called_once_with("test_lib")
        self.assertEqual(result["mode"], "full")

    async def test_index_library_progress_callback(self):
        """Test that progress callback is invoked."""
        mock_items = [
            {
                "version": 1,
                "data": {
                    "key": "ITEM123",
                    "itemType": "journalArticle",
                    "title": "Test Paper 1",
                }
            },
            {
                "version": 2,
                "data": {
                    "key": "ITEM456",
                    "itemType": "journalArticle",
                    "title": "Test Paper 2",
                }
            },
        ]

        mock_items = mock_items + [
            _attachment("PDF1", "ITEM123"),
            _attachment("PDF2", "ITEM456"),
        ]

        self.mock_zotero_client.get_library_items_since.return_value = mock_items
        self.mock_zotero_client.get_item_children.side_effect = (
            lambda library_id, item_key, library_type: [
                a for a in mock_items if a["data"].get("parentItem") == item_key
            ]
        )
        self.mock_zotero_client.get_attachment_file.return_value = b"fake pdf"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_vector_store.add_chunks_batch.return_value = ["id1"]

        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(
            ("Test content", 1)
        )
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        progress_calls = []

        def progress_callback(current, total, chunks_added):
            progress_calls.append((current, total, chunks_added))

        result = await self.processor.index_library(
            "test_lib",
            progress_callback=progress_callback,
        )

        # Should have initial + one call per item
        self.assertGreaterEqual(len(progress_calls), 3)
        self.assertEqual(progress_calls[0][:2], (0, 2))  # Initial
        self.assertEqual(progress_calls[1][:2], (1, 2))  # After first item
        self.assertEqual(progress_calls[2][:2], (2, 2))  # After second item
        # chunks_added must be non-negative and non-decreasing
        chunk_counts = [c for _, _, c in progress_calls]
        self.assertTrue(all(c >= 0 for c in chunk_counts))
        self.assertEqual(chunk_counts, sorted(chunk_counts))

    async def test_index_library_fatal_error(self):
        """Test handling of fatal errors during indexing."""
        self.mock_zotero_client.get_library_items_since.side_effect = Exception("Connection lost")

        with self.assertRaises(Exception) as context:
            await self.processor.index_library("test_lib")

        self.assertIn("Connection lost", str(context.exception))

    async def test_extract_authors(self):
        """Test author extraction from Zotero item data."""
        item_data = {
            "creators": [
                {"creatorType": "author", "firstName": "John", "lastName": "Doe"},
                {"creatorType": "author", "firstName": "Jane", "lastName": "Smith"},
                {"creatorType": "editor", "firstName": "Bob", "lastName": "Jones"},
                {"creatorType": "translator", "firstName": "Alice", "lastName": "Brown"},
            ]
        }

        authors = self.processor._extract_authors(item_data)

        # Should extract authors and editors, not translators
        self.assertEqual(len(authors), 3)
        self.assertIn("John Doe", authors)
        self.assertIn("Jane Smith", authors)
        self.assertIn("Bob Jones", authors)
        self.assertNotIn("Alice Brown", authors)

    async def test_extract_authors_empty(self):
        """Test author extraction with no creators."""
        item_data = {"creators": []}

        authors = self.processor._extract_authors(item_data)

        self.assertEqual(len(authors), 0)

    async def test_extract_tags(self):
        """Zotero tags come as a list of {"tag": ..., "type": ...} dicts; extract the names."""
        item_data = {
            "tags": [
                {"tag": "Rechtssoziologie"},
                {"tag": "Zivilprozessrecht", "type": 1},
            ]
        }

        tags = self.processor._extract_tags(item_data)

        self.assertEqual(tags, ["Rechtssoziologie", "Zivilprozessrecht"])

    async def test_extract_tags_empty(self):
        self.assertEqual(self.processor._extract_tags({"tags": []}), [])
        self.assertEqual(self.processor._extract_tags({}), [])

    async def test_metadata_only_update_skips_standalone_attachment(self):
        """Standalone attachments are out of scope — Zotero bumps their own
        version for both a metadata-only edit and a real file re-upload, and
        there's no cheap way to tell those apart."""
        item = {"version": 5, "data": {"key": "ATT1", "itemType": "attachment"}}

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertFalse(result)
        self.mock_vector_store.get_item_chunks.assert_not_called()

    async def test_metadata_only_update_returns_false_when_no_existing_chunks(self):
        item = {"version": 5, "data": {"key": "ITEM1", "itemType": "book"}}
        self.mock_vector_store.get_item_chunks.return_value = []

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertFalse(result)

    async def test_metadata_only_update_returns_false_for_catalog_stub(self):
        item = {"version": 5, "data": {"key": "ITEM1", "itemType": "book"}}
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {"has_content": False}},
        ]

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertFalse(result)

    async def test_metadata_only_update_abstract_fallback_unchanged(self):
        """Abstract-fallback item whose abstractNote text hasn't changed —
        must patch metadata in place, not re-chunk/re-embed the abstract."""
        abstract_text = "word " * 150
        abstract_hash = hashlib.sha256(abstract_text.encode("utf-8")).hexdigest()
        item = {
            "version": 10,
            "data": {
                "key": "ITEM1",
                "itemType": "journalArticle",
                "title": "New Title",
                "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Doe"}],
                "tags": [{"tag": "Law"}],
                "date": "2020",
                "abstractNote": abstract_text,
                "dateModified": "2026-01-01T00:00:00Z",
            },
        }
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "ITEM1:abstract",
                "content_hash": abstract_hash,
                "has_content": True,
            }},
        ]
        self.mock_zotero_client.get_item_children.return_value = []  # still no attachment

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertTrue(result)
        self.mock_vector_store.update_item_bibliographic_metadata.assert_called_once_with(
            "test_lib", "ITEM1",
            title="New Title", authors=["Jane Doe"], tags=["Law"],
            year=2020, item_type="journalArticle", item_version=10,
            zotero_modified="2026-01-01T00:00:00Z",
        )
        # The abstract-vs-attachment-backed check now always queries current
        # children first, to detect a transition to attachment-backed.
        self.mock_zotero_client.get_item_children.assert_called_once_with(
            library_id="test_lib", item_key="ITEM1", library_type="user"
        )

    async def test_metadata_only_update_abstract_fallback_changed(self):
        """If the abstract text itself changed, that's a content change —
        must fall through to the normal reindex path."""
        item = {
            "version": 10,
            "data": {
                "key": "ITEM1", "itemType": "journalArticle",
                "abstractNote": "a completely different abstract " * 20,
            },
        }
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "ITEM1:abstract",
                "content_hash": "stale-hash-from-before",
                "has_content": True,
            }},
        ]

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertFalse(result)
        self.mock_vector_store.update_item_bibliographic_metadata.assert_not_called()

    async def test_metadata_only_update_abstract_fallback_gained_attachment(self):
        """Abstract-fallback item that has since had a real indexable
        attachment attached — this is a state transition (abstract-only ->
        attachment-backed), not a metadata-only edit, even though the
        abstract text itself is unchanged. Must fall through to full
        reindex so the new attachment gets extracted."""
        abstract_text = "word " * 150
        abstract_hash = hashlib.sha256(abstract_text.encode("utf-8")).hexdigest()
        item = {
            "version": 10,
            "data": {
                "key": "ITEM1",
                "itemType": "journalArticle",
                "title": "New Title",
                "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Doe"}],
                "tags": [{"tag": "Law"}],
                "date": "2020",
                "abstractNote": abstract_text,
                "dateModified": "2026-01-01T00:00:00Z",
            },
        }
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "ITEM1:abstract",
                "content_hash": abstract_hash,
                "has_content": True,
            }},
        ]
        self.mock_zotero_client.get_item_children.return_value = [
            {"data": {"key": "PDF1", "itemType": "attachment", "contentType": "application/pdf"},
             "version": 1},
        ]

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertFalse(result)
        self.mock_vector_store.update_item_bibliographic_metadata.assert_not_called()

    async def test_metadata_only_update_attachment_unchanged(self):
        """Attachment-backed item whose attachment version(s) haven't
        changed — must patch metadata in place."""
        item = {
            "version": 10,
            "data": {
                "key": "ITEM1", "itemType": "journalArticle", "title": "New Title",
                "creators": [], "tags": [],
            },
        }
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "PDF1", "attachment_version": 7, "has_content": True,
            }},
        ]
        self.mock_zotero_client.get_item_children.return_value = [
            {"data": {"key": "PDF1", "itemType": "attachment", "contentType": "application/pdf"},
             "version": 7},
        ]

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertTrue(result)
        self.mock_vector_store.update_item_bibliographic_metadata.assert_called_once_with(
            "test_lib", "ITEM1",
            title="New Title", authors=[], tags=[],
            year=None, item_type="journalArticle", item_version=10,
            zotero_modified="",
        )

    async def test_metadata_only_update_attachment_version_changed(self):
        item = {"version": 10, "data": {"key": "ITEM1", "itemType": "journalArticle"}}
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "PDF1", "attachment_version": 7, "has_content": True,
            }},
        ]
        self.mock_zotero_client.get_item_children.return_value = [
            {"data": {"key": "PDF1", "itemType": "attachment", "contentType": "application/pdf"},
             "version": 8},
        ]

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertFalse(result)

    async def test_metadata_only_update_attachment_added(self):
        """A new indexable attachment appeared — content changed, not just metadata."""
        item = {"version": 10, "data": {"key": "ITEM1", "itemType": "journalArticle"}}
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "PDF1", "attachment_version": 7, "has_content": True,
            }},
        ]
        self.mock_zotero_client.get_item_children.return_value = [
            {"data": {"key": "PDF1", "itemType": "attachment", "contentType": "application/pdf"},
             "version": 7},
            {"data": {"key": "PDF2", "itemType": "attachment", "contentType": "application/pdf"},
             "version": 1},
        ]

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertFalse(result)

    async def test_metadata_only_update_attachment_removed(self):
        """An indexed attachment is no longer present — content changed."""
        item = {"version": 10, "data": {"key": "ITEM1", "itemType": "journalArticle"}}
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "PDF1", "attachment_version": 7, "has_content": True,
            }},
            {"id": "p2", "payload": {
                "attachment_key": "PDF2", "attachment_version": 1, "has_content": True,
            }},
        ]
        self.mock_zotero_client.get_item_children.return_value = [
            {"data": {"key": "PDF1", "itemType": "attachment", "contentType": "application/pdf"},
             "version": 7},
        ]

        result = await self.processor._try_metadata_only_update(item, "test_lib", "user")

        self.assertFalse(result)

    async def test_extract_year_various_formats(self):
        """Test year extraction from various date formats."""
        test_cases = [
            ("2024", 2024),
            ("2024-01-15", 2024),
            ("January 2024", 2024),
            ("15 Jan 2024", 2024),
            ("1999", 1999),
            ("2000-12-31", 2000),
            ("", None),
            ("unknown", None),
        ]

        for date_str, expected_year in test_cases:
            item_data = {"date": date_str}
            year = self.processor._extract_year(item_data)
            self.assertEqual(
                year,
                expected_year,
                f"Failed for date string: {date_str}",
            )

    async def test_index_library_multiple_pdf_attachments(self):
        """Test indexing item with multiple PDF attachments."""
        mock_item = {
            "version": 1,
            "data": {
                "key": "ITEM123",
                "itemType": "journalArticle",
                "title": "Test Paper",
            }
        }

        mock_pdf_attachments = [
            _attachment("PDF1", "ITEM123"),
            _attachment("PDF2", "ITEM123"),
        ]

        self.mock_zotero_client.get_library_items_since.return_value = [mock_item] + mock_pdf_attachments
        self.mock_zotero_client.get_item_children.return_value = mock_pdf_attachments
        # Return different bytes so dedup doesn't kick in
        self.mock_zotero_client.get_attachment_file.side_effect = [b"fake pdf 1", b"fake pdf 2"]

        self.mock_vector_store.check_duplicate.return_value = None

        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(
            ("Test content", 1)
        )
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        result = await self.processor.index_library("test_lib")

        # Should process both PDFs
        self.assertEqual(result["items_processed"], 1)
        # Each PDF creates 1 chunk = 2 total
        self.assertEqual(result["chunks_added"], 2)

    async def test_index_library_pdf_download_failure(self):
        """Test handling when attachment download fails."""
        mock_item = {
            "data": {
                "key": "ITEM123",
                "itemType": "journalArticle",
                "title": "Test Paper",
            }
        }

        mock_pdf_attachment = {
            "data": {
                "key": "PDF123",
                "itemType": "attachment",
                "contentType": "application/pdf",
            }
        }

        self.mock_zotero_client.get_library_items_since.return_value = [mock_item]
        self.mock_zotero_client.get_item_children.return_value = [mock_pdf_attachment]
        self.mock_zotero_client.get_attachment_file.return_value = None  # Download failed

        result = await self.processor.index_library("test_lib")

        # Should skip this PDF but not crash
        self.assertIn("mode", result)
        self.assertEqual(result["chunks_added"], 0)

    async def test_index_item_records_download_failure(self):
        """_index_item must append a record to self._download_failures when an
        attachment can't be downloaded, so full sync can later surface these to
        the plugin as potentially-fixable (unlike parse errors, which recur
        regardless of how the bytes are obtained and are never recorded here)."""
        mock_item = {
            "version": 1,
            "data": {"key": "ITEM123", "itemType": "journalArticle", "title": "Test Paper"},
        }
        mock_pdf_attachment = {
            "data": {"key": "PDF123", "itemType": "attachment", "contentType": "application/pdf"},
        }
        self.mock_zotero_client.get_item_children.return_value = [mock_pdf_attachment]
        self.mock_zotero_client.get_attachment_file.return_value = None  # Download failed

        await self.processor._index_item(mock_item, "test_lib", "user")

        self.assertEqual(self.processor._download_failures, [
            {"item_key": "ITEM123", "attachment_key": "PDF123"},
        ])

    async def test_full_sync_persists_download_failures_on_metadata(self):
        """Full sync must persist up to 100 download-failure records onto
        LibraryIndexMetadata.last_full_scan_failed_downloads, capped, and reset
        cleanly between runs."""
        mock_item = {
            "version": 1,
            "data": {"key": "ITEM123", "itemType": "journalArticle", "title": "Test Paper"},
        }
        mock_pdf_attachment = _attachment("PDF123", "ITEM123")

        self.mock_zotero_client.get_library_items_since.return_value = [mock_item, mock_pdf_attachment]
        self.mock_zotero_client.get_item_children.return_value = [mock_pdf_attachment]
        self.mock_zotero_client.get_attachment_file.return_value = None  # Download failed

        result = await self.processor.index_library("test_lib")

        self.assertIn("mode", result)
        saved_metadata = self.mock_vector_store.update_library_metadata.call_args.args[0]
        self.assertEqual(saved_metadata.last_full_scan_failed_downloads, [
            {"item_key": "ITEM123", "attachment_key": "PDF123"},
        ])

    async def test_index_library_html_attachment(self):
        """Test that HTML snapshot attachments are indexed."""
        mock_item = {
            "version": 1,
            "data": {
                "key": "ITEM123",
                "itemType": "webpage",
                "title": "A Web Article",
            }
        }

        mock_html_attachment = _attachment("HTML1", "ITEM123", content_type="text/html")

        self.mock_zotero_client.get_library_items_since.return_value = [mock_item, mock_html_attachment]
        self.mock_zotero_client.get_item_children.return_value = [mock_html_attachment]
        self.mock_zotero_client.get_attachment_file.return_value = b"<html><body>Hello</body></html>"

        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(
            ("Hello", None)
        )
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        result = await self.processor.index_library("test_lib")

        self.assertEqual(result["items_processed"], 1)
        self.assertEqual(result["chunks_added"], 1)

        # Verify extractor was called with correct mime type
        self.mock_extractor.extract_and_chunk.assert_called_once_with(
            b"<html><body>Hello</body></html>", "text/html"
        )


    async def test_full_mode_deletes_orphaned_items(self):
        """Chunks for items no longer in Zotero are removed during a full sync."""
        mock_item = {
            "version": 3,
            "data": {
                "key": "CURRENT",
                "itemType": "journalArticle",
                "title": "Still here",
            },
        }
        mock_pdf = _attachment("PDF1", "CURRENT")

        self.mock_zotero_client.get_library_items_since.return_value = [mock_item, mock_pdf]
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(
            ("Content", 1)
        )
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        # Indexed state: CURRENT + ORPHAN both have chunks
        self.mock_vector_store.get_all_indexed_item_versions.return_value = {
            "CURRENT": 3,
            "ORPHAN": 1,
        }
        self.mock_vector_store.delete_item_chunks.return_value = 5

        result = await self.processor.index_library("test_lib")

        # Orphaned item must have its chunks deleted
        self.mock_vector_store.delete_item_chunks.assert_any_call("test_lib", "ORPHAN")
        self.mock_vector_store.delete_item_deduplication_records.assert_any_call(
            "test_lib", "ORPHAN"
        )
        # CURRENT item has same version → skipped, no second delete_item_chunks call for it
        self.assertEqual(result["orphaned_items"], 1)
        self.assertEqual(result["items_skipped"], 1)

    async def test_incremental_mode_handles_deleted_items(self):
        """Chunks for items deleted from Zotero are purged in incremental mode."""
        from backend.models.library import LibraryIndexMetadata

        metadata = LibraryIndexMetadata(
            library_id="test_lib",
            library_type="user",
            library_name="Test Library",
            last_indexed_version=5,
        )
        self.mock_vector_store.get_library_metadata.return_value = metadata

        # No modified items, but one key was deleted from Zotero
        self.mock_zotero_client.get_library_items_since.return_value = []
        self.mock_zotero_client.get_deleted_item_keys.return_value = ["DELETED_KEY"]
        self.mock_vector_store.delete_item_chunks.return_value = 3

        result = await self.processor.index_library("test_lib", mode="incremental")

        # Must have called get_deleted_item_keys with the version we had
        self.mock_zotero_client.get_deleted_item_keys.assert_called_once_with(
            library_id="test_lib",
            library_type="user",
            since_version=5,
        )
        # Chunks for the deleted item must be removed
        self.mock_vector_store.delete_item_chunks.assert_called_once_with(
            "test_lib", "DELETED_KEY"
        )
        self.mock_vector_store.delete_item_deduplication_records.assert_called_once_with(
            "test_lib", "DELETED_KEY"
        )
        self.assertEqual(result["chunks_deleted"], 3)

    async def test_full_sync_writes_catalog_stub_for_non_indexable_item(self):
        """Full sync must write a catalog-only stub for a bibliographic item with
        no attachment and no substantial abstract, so it still surfaces in
        metadata search even though there is no text to embed."""
        mock_item = {
            "version": 7,
            "data": {
                "key": "WASSERMANN1",
                "itemType": "book",
                "title": "Der soziale Zivilprozess",
                "creators": [
                    {"creatorType": "author", "firstName": "Rudolf", "lastName": "Wassermann"}
                ],
                "date": "1973",
                "abstractNote": "Short abstract, far below the threshold.",
            },
        }
        self.mock_zotero_client.get_library_items_since.return_value = [mock_item]

        result = await self.processor.index_library("test_lib")

        self.mock_vector_store.add_catalog_stub.assert_called_once()
        call_args = self.mock_vector_store.add_catalog_stub.call_args
        doc_metadata = call_args.args[0]
        self.assertEqual(doc_metadata.item_key, "WASSERMANN1")
        self.assertEqual(doc_metadata.title, "Der soziale Zivilprozess")
        self.assertEqual(doc_metadata.authors, ["Rudolf Wassermann"])
        self.assertEqual(doc_metadata.year, 1973)
        self.assertEqual(doc_metadata.item_type, "book")
        self.assertEqual(result["items_cataloged"], 1)
        # Unchanged semantics: catalog-only items don't go through the embedding
        # pipeline, so they're still excluded from items_processed.
        self.assertEqual(result["items_processed"], 0)

    async def test_full_sync_does_not_stub_notes_or_bare_attachments(self):
        """Notes and non-indexable standalone attachments aren't bibliographic
        catalog entries — they must never get a stub record."""
        mock_items = [
            {"data": {"key": "ATTACH1", "itemType": "attachment", "contentType": "image/png"}},
            {"data": {"key": "NOTE1", "itemType": "note"}},
        ]
        self.mock_zotero_client.get_library_items_since.return_value = mock_items

        result = await self.processor.index_library("test_lib")

        self.mock_vector_store.add_catalog_stub.assert_not_called()
        self.assertEqual(result.get("items_cataloged", 0), 0)

    async def test_full_sync_catalog_stub_is_idempotent(self):
        """An already-stubbed item with an unchanged Zotero version must not be
        rewritten on every full sync run."""
        mock_item = {
            "version": 7,
            "data": {
                "key": "WASSERMANN1",
                "itemType": "book",
                "title": "Der soziale Zivilprozess",
            },
        }
        self.mock_zotero_client.get_library_items_since.return_value = [mock_item]
        self.mock_vector_store.get_all_indexed_item_versions.return_value = {"WASSERMANN1": 7}
        self.mock_vector_store.get_stub_item_keys.return_value = {"WASSERMANN1"}

        result = await self.processor.index_library("test_lib")

        self.mock_vector_store.add_catalog_stub.assert_not_called()
        self.mock_vector_store.delete_item_chunks.assert_not_called()
        self.assertEqual(result["items_cataloged"], 0)

    async def test_full_sync_orphan_purge_spares_catalog_stub_items(self):
        """A previously-stubbed item that's still present (but still
        non-indexable) in Zotero must not be purged as orphaned."""
        mock_item = {
            "version": 7,
            "data": {
                "key": "WASSERMANN1",
                "itemType": "book",
                "title": "Der soziale Zivilprozess",
            },
        }
        self.mock_zotero_client.get_library_items_since.return_value = [mock_item]
        self.mock_vector_store.get_all_indexed_item_versions.return_value = {"WASSERMANN1": 7}
        self.mock_vector_store.get_stub_item_keys.return_value = {"WASSERMANN1"}

        await self.processor.index_library("test_lib")

        self.mock_vector_store.delete_item_chunks.assert_not_called()

    async def test_full_sync_reindexes_item_that_gained_content_over_stub(self):
        """An item whose Zotero version didn't change but that now has a real
        PDF attachment (stub -> real transition) must have its stale stub
        cleared and be indexed with real content — not skipped as
        'already indexed', since Zotero doesn't bump a parent item's own
        version when a child attachment is added."""
        mock_item = {
            "version": 7,
            "data": {
                "key": "WASSERMANN1",
                "itemType": "book",
                "title": "Der soziale Zivilprozess",
            },
        }
        mock_pdf = _attachment("PDF1", "WASSERMANN1")
        self.mock_zotero_client.get_library_items_since.return_value = [mock_item, mock_pdf]
        self.mock_zotero_client.get_item_children.return_value = [mock_pdf]
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(
            ("Content", 1)
        )
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        self.mock_vector_store.get_all_indexed_item_versions.return_value = {"WASSERMANN1": 7}
        self.mock_vector_store.get_stub_item_keys.return_value = {"WASSERMANN1"}

        result = await self.processor.index_library("test_lib")

        self.mock_vector_store.delete_item_chunks.assert_any_call("test_lib", "WASSERMANN1")
        self.assertEqual(result["items_added"], 1)
        self.assertEqual(result["chunks_added"], 1)

    async def test_full_sync_metadata_only_update_skips_reindex(self):
        """Full sync (inline path) must also take the metadata-only fast path."""
        abstract_text = "word " * 150
        abstract_hash = hashlib.sha256(abstract_text.encode("utf-8")).hexdigest()
        mock_item = {
            "version": 10,
            "data": {
                "key": "ITEM1",
                "itemType": "journalArticle",
                "title": "New Title",
                "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Doe"}],
                "abstractNote": abstract_text,
            },
        }
        self.mock_zotero_client.get_library_items_since.return_value = [mock_item]
        self.mock_zotero_client.get_item_children.return_value = []
        self.mock_vector_store.get_all_indexed_item_versions.return_value = {"ITEM1": 5}
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "ITEM1:abstract",
                "content_hash": abstract_hash,
                "has_content": True,
            }},
        ]

        result = await self.processor.index_library("test_lib")

        self.mock_vector_store.delete_item_chunks.assert_not_called()
        self.mock_extractor.extract_and_chunk.assert_not_called()
        self.mock_vector_store.update_item_bibliographic_metadata.assert_called_once_with(
            "test_lib", "ITEM1",
            title="New Title", authors=["Jane Doe"], tags=[],
            year=None, item_type="journalArticle", item_version=10,
            zotero_modified="",
        )
        self.assertEqual(result["items_updated"], 1)
        self.assertEqual(result["chunks_added"], 0)

    async def test_incremental_writes_catalog_stub_for_non_indexable_item(self):
        """Incremental sync must also write a catalog-only stub for a changed
        item that has no indexable attachment and no substantial abstract."""
        from backend.models.library import LibraryIndexMetadata

        metadata = LibraryIndexMetadata(
            library_id="test_lib", library_type="user", library_name="Test Library",
            last_indexed_version=5,
        )
        self.mock_vector_store.get_library_metadata.return_value = metadata
        mock_item = {
            "version": 7,
            "data": {
                "key": "WASSERMANN1",
                "itemType": "book",
                "title": "Der soziale Zivilprozess",
                "creators": [
                    {"creatorType": "author", "firstName": "Rudolf", "lastName": "Wassermann"}
                ],
                "date": "1973",
            },
        }
        self.mock_zotero_client.get_library_items_since.return_value = [mock_item]
        self.mock_zotero_client.get_item_children.return_value = []
        self.mock_vector_store.get_item_version.return_value = None
        self.mock_vector_store.get_stub_item_keys.return_value = set()

        result = await self.processor.index_library("test_lib", mode="incremental")

        self.mock_vector_store.add_catalog_stub.assert_called_once()
        doc_metadata = self.mock_vector_store.add_catalog_stub.call_args.args[0]
        self.assertEqual(doc_metadata.item_key, "WASSERMANN1")
        self.assertEqual(doc_metadata.authors, ["Rudolf Wassermann"])
        self.assertEqual(result["items_cataloged"], 1)

    async def test_incremental_metadata_only_update_skips_reindex(self):
        """When only item-level metadata changed (content untouched),
        incremental sync must patch payload fields in place instead of
        re-downloading and re-embedding the attachment."""
        from backend.models.library import LibraryIndexMetadata

        metadata = LibraryIndexMetadata(
            library_id="test_lib", library_type="user", library_name="Test Library",
            last_indexed_version=5,
        )
        self.mock_vector_store.get_library_metadata.return_value = metadata
        abstract_text = "word " * 150
        abstract_hash = hashlib.sha256(abstract_text.encode("utf-8")).hexdigest()
        mock_item = {
            "version": 10,
            "data": {
                "key": "ITEM1",
                "itemType": "journalArticle",
                "title": "New Title",
                "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Doe"}],
                "abstractNote": abstract_text,
            },
        }
        self.mock_zotero_client.get_library_items_since.return_value = [mock_item]
        self.mock_zotero_client.get_item_children.return_value = []
        self.mock_vector_store.get_item_version.return_value = 5
        self.mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "ITEM1:abstract",
                "content_hash": abstract_hash,
                "has_content": True,
            }},
        ]

        result = await self.processor.index_library("test_lib", mode="incremental")

        self.mock_vector_store.delete_item_chunks.assert_not_called()
        self.mock_extractor.extract_and_chunk.assert_not_called()
        self.mock_embedding_service.embed_batch.assert_not_called()
        self.mock_vector_store.update_item_bibliographic_metadata.assert_called_once_with(
            "test_lib", "ITEM1",
            title="New Title", authors=["Jane Doe"], tags=[],
            year=None, item_type="journalArticle", item_version=10,
            zotero_modified="",
        )
        self.assertEqual(result["items_updated"], 1)
        self.assertEqual(result["chunks_added"], 0)

    async def test_full_sync_propagates_embedding_auth_error(self):
        """A fatal embedding auth error must abort the run, not be swallowed per-item.

        Regression for the June 2026 incident: an expired KISSKI key made every
        embedding call fail, but the per-item ``except Exception`` swallowed it and
        the scan reported success with zero chunks.
        """
        from backend.services.embeddings import EmbeddingAuthenticationError

        item = {"version": 1, "data": {"key": "AAA", "itemType": "journalArticle", "title": "A"}}
        pdf = _attachment("PDF", "AAA")
        self.mock_zotero_client.get_library_items_since.return_value = [item, pdf]
        self.mock_zotero_client.get_item_children.return_value = [pdf]
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(("content", 1))
        self.mock_embedding_service.embed_batch.side_effect = EmbeddingAuthenticationError("bad key")

        with self.assertRaises(EmbeddingAuthenticationError):
            await self.processor.index_library("test_lib", mode="full")

    async def test_incremental_propagates_embedding_auth_error(self):
        """Incremental mode must also abort on a fatal embedding auth error."""
        from backend.models.library import LibraryIndexMetadata
        from backend.services.embeddings import EmbeddingAuthenticationError

        metadata = LibraryIndexMetadata(
            library_id="test_lib", library_type="user", library_name="t",
            last_indexed_version=5,
        )
        self.mock_vector_store.get_library_metadata.return_value = metadata
        self.mock_vector_store.get_item_version.return_value = None

        item = {"version": 6, "data": {"key": "AAA", "itemType": "journalArticle", "title": "A"}}
        pdf = {"data": {"key": "PDF", "itemType": "attachment", "contentType": "application/pdf"}}
        self.mock_zotero_client.get_library_items_since.return_value = [item]
        self.mock_zotero_client.get_item_children.return_value = [pdf]
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(("content", 1))
        self.mock_embedding_service.embed_batch.side_effect = EmbeddingAuthenticationError("bad key")

        with self.assertRaises(EmbeddingAuthenticationError):
            await self.processor.index_library("test_lib", mode="incremental")

    async def test_full_sync_filter_does_not_call_get_item_children_per_item(self):
        """Full sync must derive attachments from the item list, not probe each item.

        Pins the N+1 fix: _filter_indexed_attachments builds its parent->children map
        from the full /items fetch.  get_item_children is only called while *processing*
        the items that actually have indexable attachments — never once per library item
        during filtering (the OOM regression for library 2829873).
        """
        items = [
            {"version": 1, "data": {"key": "WITHPDF", "itemType": "journalArticle", "title": "A"}},
            {"version": 1, "data": {"key": "PLAIN1", "itemType": "book", "title": "B"}},
            {"version": 1, "data": {"key": "PLAIN2", "itemType": "book", "title": "C"}},
            {"version": 1, "data": {"key": "PLAIN3", "itemType": "book", "title": "D"}},
            _attachment("PDF", "WITHPDF"),
        ]
        self.mock_zotero_client.get_library_items_since.return_value = items
        self.mock_zotero_client.get_item_children.side_effect = (
            lambda library_id, item_key, library_type: [
                a for a in items if a["data"].get("parentItem") == item_key
            ]
        )
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(("content", 1))
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        result = await self.processor.index_library("test_lib", mode="full")

        # Only the one item with an indexable attachment is processed...
        self.assertEqual(result["items_processed"], 1)
        # ...and get_item_children is called only for that item (processing), not for
        # the 3 plain items during filtering.  Pre-fix this was 4 (one per library item).
        self.assertEqual(self.mock_zotero_client.get_item_children.call_count, 1)

    async def test_full_sync_total_items_indexed_counts_only_successes(self):
        """total_items_indexed must reflect items actually indexed, not merely attempted.

        A full scan where some items fail to process must record only the successes,
        otherwise a run that fails to embed everything looks complete and blocks the
        cron under-indexed auto-recovery.
        """
        item_a = {"version": 1, "data": {"key": "AAA", "itemType": "journalArticle", "title": "A"}}
        item_b = {"version": 1, "data": {"key": "BBB", "itemType": "journalArticle", "title": "B"}}

        all_items = [item_a, item_b, _attachment("PDFA", "AAA"), _attachment("PDFB", "BBB")]
        self.mock_zotero_client.get_library_items_since.return_value = all_items
        self.mock_zotero_client.get_item_children.side_effect = (
            lambda library_id, item_key, library_type: [
                a for a in all_items if a["data"].get("parentItem") == item_key
            ]
        )
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        self.mock_vector_store.check_duplicate.return_value = None
        # First item extracts fine; second fails extraction (non-fatal, swallowed per-item).
        self.mock_extractor.extract_and_chunk.side_effect = [
            _make_extraction_chunks(("content", 1)),
            ValueError("boom"),
        ]
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        result = await self.processor.index_library("test_lib", mode="full")

        self.assertEqual(result["items_added"], 1)
        saved = self.mock_vector_store.update_library_metadata.call_args[0][0]
        # Persisted count is successes (1), not attempts (2).
        self.assertEqual(saved.total_items_indexed, 1)
        # last_full_scan_indexable still records how many items had indexable content.
        self.assertEqual(saved.last_full_scan_indexable, 2)
        # The failure is persisted too, so the plugin can tell a permanent
        # per-item failure apart from a still-incomplete index.
        self.assertEqual(saved.last_full_scan_items_failed, 1)

    async def test_full_sync_indexes_standalone_attachment_without_parent(self):
        """A PDF/HTML/DOCX/EPUB attachment with no parentItem is itself an indexable item.

        Zotero allows dropping a file directly into a collection with no bibliographic
        parent record. Regression: the full-sync filter used to unconditionally skip
        every itemType=="attachment" item, so these files were silently and permanently
        never indexed (found via production library groups/2829873, e.g. item Z22WB65S).
        """
        standalone = {
            "version": 1,
            "data": {
                "key": "STANDALONE_PDF",
                "itemType": "attachment",
                "contentType": "application/pdf",
            },
        }
        self.mock_zotero_client.get_library_items_since.return_value = [standalone]
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(("content", 1))
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        result = await self.processor.index_library("test_lib", mode="full")

        self.assertEqual(result["items_processed"], 1)
        self.assertEqual(result["items_added"], 1)
        # Standalone attachments have no children to fetch.
        self.mock_zotero_client.get_item_children.assert_not_called()
        # The attachment's own key is downloaded directly (item_key == attachment_key).
        self.mock_zotero_client.get_attachment_file.assert_called_once_with(
            library_id="test_lib", item_key="STANDALONE_PDF", library_type="user"
        )

    async def test_full_sync_ignores_parented_attachment_as_standalone(self):
        """An attachment that DOES have a parentItem must not be treated as its own
        item — it's only relevant via its parent's children_by_parent lookup."""
        child = _attachment("CHILD_PDF", "SOME_PARENT_NOT_IN_LIST")
        self.mock_zotero_client.get_library_items_since.return_value = [child]

        result = await self.processor.index_library("test_lib", mode="full")

        self.assertEqual(result["items_processed"], 0)

    async def test_incremental_indexes_standalone_attachment_without_parent(self):
        """Incremental sync must also pick up standalone attachments (no parentItem)."""
        from backend.models.library import LibraryIndexMetadata

        metadata = LibraryIndexMetadata(
            library_id="test_lib", library_type="user", library_name="t",
            last_indexed_version=5,
        )
        self.mock_vector_store.get_library_metadata.return_value = metadata
        self.mock_vector_store.get_item_version.return_value = None

        standalone = {
            "version": 6,
            "data": {"key": "STANDALONE", "itemType": "attachment", "contentType": "application/pdf"},
        }
        self.mock_zotero_client.get_library_items_since.return_value = [standalone]
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(("content", 1))
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        result = await self.processor.index_library("test_lib", mode="incremental")

        self.assertEqual(result["items_added"], 1)
        self.mock_zotero_client.get_item_children.assert_not_called()

    async def test_index_item_standalone_attachment_skips_get_item_children(self):
        """_index_item must treat a standalone attachment as its own single attachment,
        not fetch children for it (attachments don't have children in Zotero), and must
        not attempt an abstract-fallback (attachments carry no abstractNote of their own)."""
        standalone = {
            "version": 3,
            "data": {
                "key": "STANDALONE",
                "itemType": "attachment",
                "contentType": "application/pdf",
                "title": "Some Standalone File",
            },
        }
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(("content", 1))
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        chunks = await self.processor._index_item(standalone, "test_lib", "user")

        self.assertEqual(chunks, 1)
        self.mock_zotero_client.get_item_children.assert_not_called()
        self.mock_zotero_client.get_attachment_file.assert_called_once_with(
            library_id="test_lib", item_key="STANDALONE", library_type="user"
        )

    async def test_filter_indexed_attachments_includes_standalone_attachment(self):
        """_filter_indexed_attachments (used by incremental sync) must not skip every
        itemType=="attachment" unconditionally — a standalone attachment with no
        parentItem is itself indexable; a parented one is not included on its own."""
        standalone = {
            "version": 1,
            "data": {"key": "STANDALONE", "itemType": "attachment", "contentType": "application/pdf"},
        }
        child_attachment = _attachment("CHILD_PDF", "PARENT_KEY")

        result = await self.processor._filter_indexed_attachments(
            [standalone, child_attachment], "test_lib", "user"
        )

        self.assertIn(standalone, result)
        self.assertNotIn(child_attachment, result)

    async def test_filter_indexed_attachments_excludes_standalone_non_indexable_type(self):
        """A standalone attachment whose contentType isn't in INDEXABLE_MIME_TYPES
        (e.g. an image) must not be included."""
        standalone_image = {
            "version": 1,
            "data": {"key": "IMG", "itemType": "attachment", "contentType": "image/png"},
        }

        result = await self.processor._filter_indexed_attachments(
            [standalone_image], "test_lib", "user"
        )

        self.assertEqual(result, [])

    async def test_full_sync_reports_items_failed(self):
        """A per-item processing failure (e.g. corrupted/broken attachment download)
        must be counted as items_failed, not silently disappear from every stat."""
        item_a = {"version": 1, "data": {"key": "AAA", "itemType": "journalArticle", "title": "A"}}
        item_b = {"version": 1, "data": {"key": "BBB", "itemType": "journalArticle", "title": "B"}}

        all_items = [item_a, item_b, _attachment("PDFA", "AAA"), _attachment("PDFB", "BBB")]
        self.mock_zotero_client.get_library_items_since.return_value = all_items
        self.mock_zotero_client.get_item_children.side_effect = (
            lambda library_id, item_key, library_type: [
                a for a in all_items if a["data"].get("parentItem") == item_key
            ]
        )
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_extractor.extract_and_chunk.side_effect = [
            _make_extraction_chunks(("content", 1)),
            ValueError("boom"),
        ]
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        result = await self.processor.index_library("test_lib", mode="full")

        self.assertEqual(result["items_added"], 1)
        self.assertEqual(result["items_failed"], 1)

    async def test_incremental_reports_items_failed(self):
        """Incremental sync must also count per-item failures instead of dropping them."""
        from backend.models.library import LibraryIndexMetadata

        metadata = LibraryIndexMetadata(
            library_id="test_lib", library_type="user", library_name="t",
            last_indexed_version=5,
        )
        self.mock_vector_store.get_library_metadata.return_value = metadata
        self.mock_vector_store.get_item_version.return_value = None

        item = {"version": 6, "data": {"key": "AAA", "itemType": "journalArticle", "title": "A"}}
        pdf = {"data": {"key": "PDF", "itemType": "attachment", "contentType": "application/pdf"}}
        self.mock_zotero_client.get_library_items_since.return_value = [item]
        self.mock_zotero_client.get_item_children.return_value = [pdf]
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_extractor.extract_and_chunk.side_effect = ValueError("boom")

        result = await self.processor.index_library("test_lib", mode="incremental")

        self.assertEqual(result["items_added"], 0)
        self.assertEqual(result["items_failed"], 1)

    async def test_incremental_sync_preserves_last_full_scan_items_failed(self):
        """Incremental sync only ever sees items that changed since last_indexed_version
        — an item that failed in a prior full scan and hasn't changed since is never
        re-attempted, so incremental sync must not overwrite the last full scan's
        items-failed floor with its own much narrower (and here, zero) count."""
        from backend.models.library import LibraryIndexMetadata

        metadata = LibraryIndexMetadata(
            library_id="test_lib", library_type="user", library_name="t",
            last_indexed_version=5, last_full_scan_items_failed=5,
        )
        self.mock_vector_store.get_library_metadata.return_value = metadata
        self.mock_vector_store.get_item_version.return_value = None

        item = {"version": 6, "data": {"key": "AAA", "itemType": "journalArticle", "title": "A"}}
        pdf = {"data": {"key": "PDF", "itemType": "attachment", "contentType": "application/pdf"}}
        self.mock_zotero_client.get_library_items_since.return_value = [item]
        self.mock_zotero_client.get_item_children.return_value = [pdf]
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(("content", 1))
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        result = await self.processor.index_library("test_lib", mode="incremental")

        self.assertEqual(result["items_failed"], 0)
        saved = self.mock_vector_store.update_library_metadata.call_args[0][0]
        self.assertEqual(saved.last_full_scan_items_failed, 5)

    async def test_full_sync_reports_items_failed_for_dead_download_link(self):
        """A candidate whose only attachment download returns no bytes (e.g. a dead
        Zotero storage link) produces zero chunks and no exception — it must still
        be counted as items_failed, not silently counted as items_added with zero
        content indexed."""
        item = {"version": 1, "data": {"key": "AAA", "itemType": "journalArticle", "title": "A"}}
        self.mock_zotero_client.get_library_items_since.return_value = [
            item, _attachment("PDF", "AAA"),
        ]
        self.mock_zotero_client.get_item_children.return_value = [_attachment("PDF", "AAA")]
        self.mock_zotero_client.get_attachment_file.return_value = None  # dead link
        self.mock_vector_store.check_duplicate.return_value = None
        # No existing content for this item either — a genuine failure, not a
        # duplicate skip (see test_full_sync_treats_duplicate_skip_as_added below).
        self.mock_vector_store.get_item_version.return_value = None

        result = await self.processor.index_library("test_lib", mode="full")

        self.assertEqual(result["items_added"], 0)
        self.assertEqual(result["items_failed"], 1)

    async def test_full_sync_treats_duplicate_skip_as_added_not_failed(self):
        """A same-item duplicate skip (content already indexed under this exact
        item_key) also returns zero chunks from _index_item, but it is NOT a
        failure — the item already has valid indexed content, just not written
        again this run. Regression test for the false positive this would
        otherwise create once zero-chunk results start counting as items_failed."""
        item = {"version": 1, "data": {"key": "AAA", "itemType": "journalArticle", "title": "A"}}
        self.mock_zotero_client.get_library_items_since.return_value = [
            item, _attachment("PDF", "AAA"),
        ]
        self.mock_zotero_client.get_item_children.return_value = [_attachment("PDF", "AAA")]
        self.mock_zotero_client.get_attachment_file.return_value = b"pdf bytes"
        duplicate_record = DeduplicationRecord(
            content_hash="existing_hash", library_id="test_lib", item_key="AAA", relation_uri=None,
        )
        self.mock_vector_store.check_duplicate.return_value = duplicate_record
        # Already has indexed content under this item_key -> _handle_same_library_duplicate
        # takes the "skipped_duplicate" branch (zero new chunks, not a failure).
        self.mock_vector_store.get_item_version.return_value = 1

        result = await self.processor.index_library("test_lib", mode="full")

        self.assertEqual(result["items_added"], 1)
        self.assertEqual(result["items_failed"], 0)
        self.mock_extractor.extract_and_chunk.assert_not_called()

    async def test_incremental_reports_items_failed_for_dead_download_link(self):
        """Incremental sync must also count a zero-chunk dead-download-link result
        as items_failed, not items_added."""
        from backend.models.library import LibraryIndexMetadata

        metadata = LibraryIndexMetadata(
            library_id="test_lib", library_type="user", library_name="t",
            last_indexed_version=5,
        )
        self.mock_vector_store.get_library_metadata.return_value = metadata
        self.mock_vector_store.get_item_version.return_value = None

        item = {"version": 6, "data": {"key": "AAA", "itemType": "journalArticle", "title": "A"}}
        pdf = {"data": {"key": "PDF", "itemType": "attachment", "contentType": "application/pdf"}}
        self.mock_zotero_client.get_library_items_since.return_value = [item]
        self.mock_zotero_client.get_item_children.return_value = [pdf]
        self.mock_zotero_client.get_attachment_file.return_value = None  # dead link
        self.mock_vector_store.check_duplicate.return_value = None

        result = await self.processor.index_library("test_lib", mode="incremental")

        self.assertEqual(result["items_added"], 0)
        self.assertEqual(result["items_failed"], 1)


class TestSubprocessBatchIndexing(unittest.IsolatedAsyncioTestCase):
    """Tests for the subprocess-isolated batch processing path in _index_library_full."""

    def setUp(self):
        self.mock_zotero_client = AsyncMock()
        self.mock_embedding_service = AsyncMock()
        self.mock_vector_store = Mock()
        self.mock_vector_store.find_cross_library_duplicate.return_value = None
        self.mock_vector_store.get_all_indexed_item_versions.return_value = {}
        self.mock_vector_store.get_stub_item_keys.return_value = set()
        self.mock_vector_store.get_item_chunks.return_value = []
        self.mock_zotero_client.get_deleted_item_keys.return_value = []
        self.mock_extractor = AsyncMock(spec=DocumentExtractor)
        self.processor = DocumentProcessor(
            zotero_client=self.mock_zotero_client,
            embedding_service=self.mock_embedding_service,
            vector_store=self.mock_vector_store,
            document_extractor=self.mock_extractor,
        )

    @patch("backend.services.document_processor.SUBPROCESS_BATCH_SIZE", 1)
    @patch("backend.services.document_processor.Process")
    @patch("backend.services.document_processor.MPQueue")
    async def test_subprocess_batch_fatal_error_aborts_run(self, mock_queue_cls, mock_process_cls):
        """A fatal embedding error reported by a subprocess must abort _index_library_full."""
        from backend.services.embeddings import EmbeddingAuthenticationError

        item = {"version": 1, "data": {"key": "AAA", "itemType": "journalArticle", "title": "A"}}
        pdf = _attachment("PDF", "AAA")
        self.mock_zotero_client.get_library_items_since.return_value = [item, pdf]

        mock_q = MagicMock()
        mock_q.empty.return_value = False
        mock_q.get_nowait.return_value = {
            "fatal": True,
            "error": "bad key",
            "error_type": "EmbeddingAuthenticationError",
        }
        mock_queue_cls.return_value = mock_q

        mock_proc = MagicMock()
        mock_proc.exitcode = 0
        mock_process_cls.return_value = mock_proc

        with patch("backend.services.document_processor.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                testing=False,
                min_abstract_words=5,
                zotero_api_key="dummy",
            )
            with self.assertRaises(EmbeddingAuthenticationError):
                await self.processor._index_library_full(
                    library_id="test_lib",
                    library_type="user",
                    metadata=MagicMock(last_indexed_version=0),
                )

    @patch("backend.services.document_processor.SUBPROCESS_BATCH_SIZE", 1)
    @patch("backend.services.document_processor.Process")
    @patch("backend.services.document_processor.MPQueue")
    async def test_subprocess_oom_kill_skips_batch_and_continues(self, mock_queue_cls, mock_process_cls):
        """An OOM-killed subprocess (exitcode -9) must be logged and skipped, not abort the run."""
        item1 = {"version": 1, "data": {"key": "AAA", "itemType": "journalArticle", "title": "A"}}
        item2 = {"version": 1, "data": {"key": "BBB", "itemType": "journalArticle", "title": "B"}}
        pdf1 = _attachment("PDF1", "AAA")
        pdf2 = _attachment("PDF2", "BBB")
        self.mock_zotero_client.get_library_items_since.return_value = [item1, item2, pdf1, pdf2]

        # First batch OOM-killed (empty queue, exitcode -9); second succeeds
        mock_q1 = MagicMock()
        mock_q1.empty.return_value = True
        mock_q2 = MagicMock()
        mock_q2.empty.return_value = False
        mock_q2.get_nowait.return_value = {
            "fatal": False, "chunks_added": 3, "items_added": 1,
            "items_updated": 0, "items_skipped": 0,
        }
        mock_queue_cls.side_effect = [mock_q1, mock_q2]

        mock_proc1 = MagicMock()
        mock_proc1.exitcode = -9
        mock_proc2 = MagicMock()
        mock_proc2.exitcode = 0
        mock_process_cls.side_effect = [mock_proc1, mock_proc2]

        with patch("backend.services.document_processor.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                testing=False,
                min_abstract_words=5,
                zotero_api_key="dummy",
            )
            result = await self.processor._index_library_full(
                library_id="test_lib",
                library_type="user",
                metadata=MagicMock(last_indexed_version=0),
            )

        self.assertEqual(result["chunks_added"], 3)

    @patch("backend.services.document_processor.SUBPROCESS_BATCH_SIZE", 1)
    @patch("backend.services.document_processor.Process")
    @patch("backend.services.document_processor.MPQueue")
    async def test_subprocess_dispatch_passes_client_api_key(self, mock_queue_cls, mock_process_cls):
        """The subprocess batch worker must receive the per-request Zotero and embedding
        API keys that self.zotero_client / self.embedding_service were constructed with —
        not fall back to global (and, under per-user auto-indexing, unset) settings/env
        vars. Regression test for items being silently dropped and their version cursor
        advanced past them anyway when the global keys are unset (see cron_indexer.log
        2026-07-07..11 for the Zotero-key case, and 2026-07-16 08:10 for the embedding-key
        case: 'API key not found. Set the KISSKI_API_KEY environment variable.')."""
        self.mock_zotero_client.api_key = "per-user-key-abc"
        self.mock_embedding_service.api_key = "per-user-embed-key-xyz"

        item = {"version": 1, "data": {"key": "AAA", "itemType": "journalArticle", "title": "A"}}
        pdf = _attachment("PDF", "AAA")
        self.mock_zotero_client.get_library_items_since.return_value = [item, pdf]

        mock_q = MagicMock()
        mock_q.empty.return_value = False
        mock_q.get_nowait.return_value = {
            "fatal": False, "chunks_added": 1, "items_added": 1,
            "items_updated": 0, "items_skipped": 0,
        }
        mock_queue_cls.return_value = mock_q

        mock_proc = MagicMock()
        mock_proc.exitcode = 0
        mock_process_cls.return_value = mock_proc

        with patch("backend.services.document_processor.get_settings") as mock_settings:
            # Global key deliberately unset, as in production auto-indexing.
            mock_settings.return_value = MagicMock(
                testing=False,
                min_abstract_words=5,
                zotero_api_key=None,
            )
            await self.processor._index_library_full(
                library_id="test_lib",
                library_type="user",
                metadata=MagicMock(last_indexed_version=0),
            )

        _, kwargs = mock_process_cls.call_args
        self.assertIn("per-user-key-abc", kwargs["args"])
        self.assertIn("per-user-embed-key-xyz", kwargs["args"])


class TestSubprocessIndexBatchFunction(unittest.TestCase):
    """Direct tests of the module-level subprocess worker function (no multiprocessing).

    _subprocess_index_batch is synchronous — it spins up its own event loop via
    asyncio.run() internally (it's designed to run inside a forked child process
    with no event loop of its own), so this must be a plain sync test, not async.
    """

    def test_uses_passed_zotero_api_key_not_global_setting(self):
        """_subprocess_index_batch must use its zotero_api_key argument to build the
        ZoteroWebAPI client, not backend.config.settings.zotero_api_key. Under
        per-user auto-indexing that global setting is unset, so falling back to it
        raised RuntimeError for every item in the batch (see bug this test guards)."""
        from backend.services import document_processor as dp_module

        captured = {}

        class FakeWebAPI:
            def __init__(self, api_key):
                captured["api_key"] = api_key

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

        with patch("backend.services.document_processor.get_settings") as mock_settings, \
             patch("backend.zotero.web_api.ZoteroWebAPI", FakeWebAPI), \
             patch("backend.services.embeddings.create_embedding_service", return_value=MagicMock()), \
             patch("backend.dependencies.make_vector_store", return_value=MagicMock()):
            mock_settings.return_value = MagicMock(
                zotero_api_key=None,
                testing=False,
                extractor_backend="kreuzberg",
                ocr_enabled=True,
                kreuzberg_url="http://kreuzberg.test",
            )

            result = dp_module._subprocess_index_batch(
                items=[],
                library_id="lib1",
                library_type="group",
                indexed_versions={},
                zotero_api_key="per-user-key-123",
                embedding_api_key="irrelevant-for-this-test",
            )

        self.assertEqual(captured["api_key"], "per-user-key-123")
        self.assertEqual(result["items_added"], 0)

    def test_uses_passed_embedding_api_key_not_global_env_var(self):
        """_subprocess_index_batch must use its embedding_api_key argument, not fall
        back to a global env var (e.g. KISSKI_API_KEY/OPENAI_API_KEY) that per-user
        auto-indexing never sets. Regression test: production logs showed 'API key
        not found. Set the KISSKI_API_KEY environment variable.' for every item in
        subprocess batches even after the Zotero-key fix, because the embedding
        client had the same unfixed fallback."""
        from backend.services import document_processor as dp_module

        captured = {}

        class FakeWebAPI:
            def __init__(self, api_key):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

        def fake_create_embedding_service(config, cache_dir=None, api_key=None, hf_token=None):
            captured["api_key"] = api_key
            return MagicMock()

        with patch("backend.services.document_processor.get_settings") as mock_settings, \
             patch("backend.zotero.web_api.ZoteroWebAPI", FakeWebAPI), \
             patch("backend.services.embeddings.create_embedding_service", side_effect=fake_create_embedding_service), \
             patch("backend.dependencies.make_vector_store", return_value=MagicMock()):
            mock_settings.return_value = MagicMock(
                zotero_api_key=None,
                testing=False,
                extractor_backend="kreuzberg",
                ocr_enabled=True,
                kreuzberg_url="http://kreuzberg.test",
            )

            dp_module._subprocess_index_batch(
                items=[],
                library_id="lib1",
                library_type="group",
                indexed_versions={},
                zotero_api_key="irrelevant-for-this-test",
                embedding_api_key="per-user-embed-key-456",
            )

        self.assertEqual(captured["api_key"], "per-user-embed-key-456")

    def test_reports_items_failed_for_per_item_exception(self):
        """_subprocess_index_batch must count a per-item exception as items_failed,
        not just log it and move on with no trace in the returned stats."""
        from backend.services import document_processor as dp_module

        class FakeWebAPI:
            def __init__(self, api_key):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

        item = {"version": 1, "data": {"key": "BAD", "itemType": "journalArticle", "title": "A"}}

        with patch("backend.services.document_processor.get_settings") as mock_settings, \
             patch("backend.zotero.web_api.ZoteroWebAPI", FakeWebAPI), \
             patch("backend.services.embeddings.create_embedding_service", return_value=MagicMock()), \
             patch("backend.dependencies.make_vector_store", return_value=MagicMock()), \
             patch.object(
                 dp_module.DocumentProcessor, "_index_item",
                 AsyncMock(side_effect=ValueError("corrupted attachment")),
             ):
            mock_settings.return_value = MagicMock(
                zotero_api_key=None,
                testing=False,
                extractor_backend="kreuzberg",
                ocr_enabled=True,
                kreuzberg_url="http://kreuzberg.test",
            )

            result = dp_module._subprocess_index_batch(
                items=[item],
                library_id="test_lib",
                library_type="user",
                indexed_versions={},
                zotero_api_key="fake-key",
                embedding_api_key="fake-embed-key",
            )

        self.assertEqual(result["items_added"], 0)
        self.assertEqual(result["items_failed"], 1)

    def test_reports_items_failed_for_zero_chunk_result(self):
        """_subprocess_index_batch must count a zero-chunk result (e.g. a dead
        attachment download link, which _index_item handles without raising) as
        items_failed too — not every failure surfaces as an exception."""
        from backend.services import document_processor as dp_module

        class FakeWebAPI:
            def __init__(self, api_key):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

        item = {"version": 1, "data": {"key": "DEADLINK", "itemType": "journalArticle", "title": "A"}}
        mock_vector_store = MagicMock()
        mock_vector_store.get_item_version.return_value = None  # no existing content either

        with patch("backend.services.document_processor.get_settings") as mock_settings, \
             patch("backend.zotero.web_api.ZoteroWebAPI", FakeWebAPI), \
             patch("backend.services.embeddings.create_embedding_service", return_value=MagicMock()), \
             patch("backend.dependencies.make_vector_store", return_value=mock_vector_store), \
             patch.object(
                 dp_module.DocumentProcessor, "_index_item",
                 AsyncMock(return_value=0),
             ):
            mock_settings.return_value = MagicMock(
                zotero_api_key=None,
                testing=False,
                extractor_backend="kreuzberg",
                ocr_enabled=True,
                kreuzberg_url="http://kreuzberg.test",
            )

            result = dp_module._subprocess_index_batch(
                items=[item],
                library_id="test_lib",
                library_type="user",
                indexed_versions={},
                zotero_api_key="fake-key",
                embedding_api_key="fake-embed-key",
            )

        self.assertEqual(result["items_added"], 0)
        self.assertEqual(result["items_failed"], 1)

    def test_reports_failed_downloads_in_result(self):
        """_subprocess_index_batch must return the batch's own DocumentProcessor
        instance's _download_failures so the parent process can aggregate them
        across all batches of a full sync."""
        from backend.services import document_processor as dp_module

        class FakeWebAPI:
            def __init__(self, api_key):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

        item = {"version": 1, "data": {"key": "DEADLINK", "itemType": "journalArticle", "title": "A"}}
        mock_vector_store = MagicMock()
        mock_vector_store.get_item_version.return_value = None  # no existing content either

        def fake_index_item(self, item, library_id, library_type):
            # Simulates what the real _index_item does on a download failure
            # (Task 1) — append to the instance's own accumulator and return 0.
            self._download_failures.append({"item_key": "DEADLINK", "attachment_key": "ATT1"})
            return 0

        with patch("backend.services.document_processor.get_settings") as mock_settings, \
             patch("backend.zotero.web_api.ZoteroWebAPI", FakeWebAPI), \
             patch("backend.services.embeddings.create_embedding_service", return_value=MagicMock()), \
             patch("backend.dependencies.make_vector_store", return_value=mock_vector_store), \
             patch.object(dp_module.DocumentProcessor, "_index_item", side_effect=fake_index_item, autospec=True):
            mock_settings.return_value = MagicMock(
                zotero_api_key=None,
                testing=False,
                extractor_backend="kreuzberg",
                ocr_enabled=True,
                kreuzberg_url="http://kreuzberg.test",
            )

            result = dp_module._subprocess_index_batch(
                items=[item],
                library_id="test_lib",
                library_type="user",
                indexed_versions={},
                zotero_api_key="fake-key",
                embedding_api_key="fake-embed-key",
            )

        self.assertEqual(result["failed_downloads"], [
            {"item_key": "DEADLINK", "attachment_key": "ATT1"},
        ])

    def test_metadata_only_update_skips_reindex_in_subprocess_batch(self):
        """The subprocess worker must also take the metadata-only fast path —
        production always uses this code path for full sync
        (settings.testing=False)."""
        from backend.services import document_processor as dp_module

        class FakeWebAPI:
            def __init__(self, api_key):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def get_item_children(self, library_id, item_key, library_type):
                return []  # still no indexable attachment

        abstract_text = "word " * 150
        abstract_hash = hashlib.sha256(abstract_text.encode("utf-8")).hexdigest()
        item = {
            "version": 10,
            "data": {
                "key": "ITEM1", "itemType": "journalArticle", "title": "New Title",
                "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Doe"}],
                "abstractNote": abstract_text,
            },
        }
        mock_vector_store = MagicMock()
        mock_vector_store.get_item_chunks.return_value = [
            {"id": "p1", "payload": {
                "attachment_key": "ITEM1:abstract",
                "content_hash": abstract_hash,
                "has_content": True,
            }},
        ]

        with patch("backend.services.document_processor.get_settings") as mock_settings, \
             patch("backend.zotero.web_api.ZoteroWebAPI", FakeWebAPI), \
             patch("backend.services.embeddings.create_embedding_service", return_value=MagicMock()), \
             patch("backend.dependencies.make_vector_store", return_value=mock_vector_store):
            mock_settings.return_value = MagicMock(
                zotero_api_key=None, testing=False, extractor_backend="kreuzberg",
                ocr_enabled=True, kreuzberg_url="http://kreuzberg.test",
            )

            result = dp_module._subprocess_index_batch(
                items=[item],
                library_id="test_lib",
                library_type="user",
                indexed_versions={"ITEM1": 5},
                zotero_api_key="fake-key",
                embedding_api_key="fake-embed-key",
            )

        mock_vector_store.delete_item_chunks.assert_not_called()
        mock_vector_store.update_item_bibliographic_metadata.assert_called_once_with(
            "test_lib", "ITEM1",
            title="New Title", authors=["Jane Doe"], tags=[],
            year=None, item_type="journalArticle", item_version=10,
            zotero_modified="",
        )
        self.assertEqual(result["items_updated"], 1)
        self.assertEqual(result["chunks_added"], 0)


if __name__ == "__main__":
    unittest.main()
