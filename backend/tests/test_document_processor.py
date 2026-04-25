"""
Unit tests for document processor.
"""

import unittest
from unittest.mock import AsyncMock, Mock, patch

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

        mock_pdf_attachment = {
            "data": {
                "key": "PDF123",
                "itemType": "attachment",
                "contentType": "application/pdf",
            }
        }

        # Mock Zotero API responses
        self.mock_zotero_client.get_library_items_since.return_value = [mock_item]
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
        self.mock_zotero_client.get_attachment_file.return_value = b"fake pdf bytes"

        # Mock duplicate found
        duplicate_record = DeduplicationRecord(
            content_hash="existing_hash",
            library_id="test_lib",
            item_key="OLD_ITEM",
            relation_uri=None,
        )
        self.mock_vector_store.check_duplicate.return_value = duplicate_record

        result = await self.processor.index_library("test_lib")

        self.assertEqual(result["chunks_added"], 0)
        # Extractor should not be called for duplicates
        self.mock_extractor.extract_and_chunk.assert_not_called()

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
        """Test force reindex deletes existing chunks."""
        from backend.models.library import LibraryIndexMetadata
        metadata = LibraryIndexMetadata(
            library_id="test_lib",
            library_type="user",
            library_name="Test Library",
            force_reindex=True
        )
        self.mock_vector_store.get_library_metadata.return_value = metadata

        self.mock_zotero_client.get_library_items_since.return_value = []
        self.mock_vector_store.delete_library_chunks.return_value = 42

        result = await self.processor.index_library("test_lib", mode="auto")

        # Verify deletion was called (full mode due to force_reindex flag)
        self.mock_vector_store.delete_library_chunks.assert_called_once_with("test_lib")

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

        mock_pdf_attachment = {
            "data": {
                "key": "PDF1",
                "itemType": "attachment",
                "contentType": "application/pdf",
            }
        }

        self.mock_zotero_client.get_library_items_since.return_value = mock_items
        self.mock_zotero_client.get_item_children.return_value = [mock_pdf_attachment]
        self.mock_zotero_client.get_attachment_file.return_value = b"fake pdf"
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_vector_store.add_chunks_batch.return_value = ["id1"]

        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(
            ("Test content", 1)
        )
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        progress_calls = []

        def progress_callback(current, total):
            progress_calls.append((current, total))

        result = await self.processor.index_library(
            "test_lib",
            progress_callback=progress_callback,
        )

        # Should have initial + one call per item
        self.assertGreaterEqual(len(progress_calls), 3)
        self.assertEqual(progress_calls[0], (0, 2))  # Initial
        self.assertEqual(progress_calls[1], (1, 2))  # After first item
        self.assertEqual(progress_calls[2], (2, 2))  # After second item

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
            {
                "data": {
                    "key": "PDF1",
                    "itemType": "attachment",
                    "contentType": "application/pdf",
                }
            },
            {
                "data": {
                    "key": "PDF2",
                    "itemType": "attachment",
                    "contentType": "application/pdf",
                }
            },
        ]

        self.mock_zotero_client.get_library_items_since.return_value = [mock_item]
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

        mock_html_attachment = {
            "data": {
                "key": "HTML1",
                "itemType": "attachment",
                "contentType": "text/html",
            }
        }

        self.mock_zotero_client.get_library_items_since.return_value = [mock_item]
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


if __name__ == "__main__":
    unittest.main()
