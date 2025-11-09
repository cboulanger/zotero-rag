"""
Unit tests for document processor.
"""

import unittest
from unittest.mock import AsyncMock, Mock, patch, MagicMock
from pathlib import Path

from backend.services.document_processor import DocumentProcessor
from backend.services.pdf_extractor import PageText
from backend.services.chunking import TextChunk
from backend.models.document import (
    DocumentMetadata,
    ChunkMetadata,
    DocumentChunk,
    DeduplicationRecord,
)


class TestDocumentProcessor(unittest.IsolatedAsyncioTestCase):
    """Test DocumentProcessor class."""

    def setUp(self):
        """Set up test fixtures."""
        # Create mock dependencies
        self.mock_zotero_client = AsyncMock()
        self.mock_embedding_service = AsyncMock()
        self.mock_vector_store = Mock()

        # Create processor
        self.processor = DocumentProcessor(
            zotero_client=self.mock_zotero_client,
            embedding_service=self.mock_embedding_service,
            vector_store=self.mock_vector_store,
            max_chunk_size=512,
            chunk_overlap=50,
        )

    async def test_init(self):
        """Test initialization."""
        self.assertIsNotNone(self.processor.pdf_extractor)
        self.assertIsNotNone(self.processor.text_chunker)
        self.assertEqual(self.processor.text_chunker.max_chunk_size, 512)
        self.assertEqual(self.processor.text_chunker.overlap_size, 50)

    async def test_index_library_no_items(self):
        """Test indexing when library has no items."""
        # Mock empty library
        self.mock_zotero_client.get_library_items.return_value = []

        result = await self.processor.index_library("test_lib")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["items_processed"], 0)
        self.assertEqual(result["chunks_created"], 0)

    async def test_index_library_skip_non_pdf_items(self):
        """Test that non-PDF items are skipped."""
        # Mock library with non-PDF item
        mock_item = {
            "data": {
                "key": "ITEM123",
                "itemType": "book",
                "title": "Test Book",
            }
        }

        self.mock_zotero_client.get_library_items.return_value = [mock_item]
        self.mock_zotero_client.get_item_children.return_value = []  # No attachments

        result = await self.processor.index_library("test_lib")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["items_skipped"], 1)
        self.assertEqual(result["items_processed"], 0)

    async def test_index_library_skip_attachments_and_notes(self):
        """Test that attachment and note items themselves are skipped."""
        mock_items = [
            {"data": {"key": "ATTACH1", "itemType": "attachment"}},
            {"data": {"key": "NOTE1", "itemType": "note"}},
        ]

        self.mock_zotero_client.get_library_items.return_value = mock_items

        result = await self.processor.index_library("test_lib")

        # These should be filtered out before processing
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["items_processed"], 0)

    async def test_index_library_with_pdf_success(self):
        """Test successful indexing of item with PDF attachment."""
        # Mock item with PDF
        mock_item = {
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
        self.mock_zotero_client.get_library_items.return_value = [mock_item]
        self.mock_zotero_client.get_item_children.return_value = [mock_pdf_attachment]
        self.mock_zotero_client.get_attachment_file.return_value = b"fake pdf bytes"

        # Mock vector store - no duplicate
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_vector_store.add_chunks_batch.return_value = ["id1", "id2"]

        # Mock PDF extraction
        mock_pages = [
            PageText(page_number=1, text="This is page one with some content."),
            PageText(page_number=2, text="This is page two with more content."),
        ]

        # Mock chunking
        mock_chunks = [
            TextChunk(
                text="This is page one with some content.",
                page_number=1,
                chunk_index=0,
                start_char=0,
                end_char=37,
            ),
            TextChunk(
                text="This is page two with more content.",
                page_number=2,
                chunk_index=1,
                start_char=0,
                end_char=36,
            ),
        ]

        # Mock embeddings
        mock_embeddings = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        self.mock_embedding_service.embed_batch.return_value = mock_embeddings

        # Patch PDF extractor and chunker
        with patch.object(
            self.processor.pdf_extractor, "extract_from_bytes", return_value=mock_pages
        ), patch.object(
            self.processor.text_chunker, "chunk_pages", return_value=mock_chunks
        ):
            result = await self.processor.index_library("test_lib")

        # Verify results
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["items_processed"], 1)
        self.assertEqual(result["chunks_created"], 2)
        self.assertEqual(result["errors"], 0)

        # Verify embeddings were generated
        self.mock_embedding_service.embed_batch.assert_called_once()

        # Verify chunks were stored
        self.mock_vector_store.add_chunks_batch.assert_called_once()
        stored_chunks = self.mock_vector_store.add_chunks_batch.call_args[0][0]
        self.assertEqual(len(stored_chunks), 2)

        # Verify deduplication record was added
        self.mock_vector_store.add_deduplication_record.assert_called_once()

    async def test_index_library_skip_duplicate(self):
        """Test that duplicate PDFs are skipped."""
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

        self.mock_zotero_client.get_library_items.return_value = [mock_item]
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

        # Should skip processing
        self.assertEqual(result["duplicates_skipped"], 1)
        self.assertEqual(result["chunks_created"], 0)

    async def test_index_library_pdf_extraction_error(self):
        """Test handling of PDF extraction errors."""
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

        self.mock_zotero_client.get_library_items.return_value = [mock_item]
        self.mock_zotero_client.get_item_children.return_value = [mock_pdf_attachment]
        self.mock_zotero_client.get_attachment_file.return_value = b"corrupted pdf"

        # Mock vector store - no duplicate
        self.mock_vector_store.check_duplicate.return_value = None

        # Mock PDF extraction failure
        with patch.object(
            self.processor.pdf_extractor,
            "extract_from_bytes",
            side_effect=ValueError("Invalid PDF"),
        ):
            result = await self.processor.index_library("test_lib")

        # Should handle error gracefully
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["chunks_created"], 0)

    async def test_index_library_force_reindex(self):
        """Test force reindex deletes existing chunks."""
        self.mock_zotero_client.get_library_items.return_value = []
        self.mock_vector_store.delete_library_chunks.return_value = 42

        result = await self.processor.index_library("test_lib", force_reindex=True)

        # Verify deletion was called
        self.mock_vector_store.delete_library_chunks.assert_called_once_with("test_lib")

    async def test_index_library_progress_callback(self):
        """Test that progress callback is invoked."""
        # Use 2 items so we get multiple progress updates
        mock_items = [
            {
                "data": {
                    "key": "ITEM123",
                    "itemType": "journalArticle",
                    "title": "Test Paper 1",
                }
            },
            {
                "data": {
                    "key": "ITEM456",
                    "itemType": "journalArticle",
                    "title": "Test Paper 2",
                }
            },
        ]

        self.mock_zotero_client.get_library_items.return_value = mock_items
        self.mock_zotero_client.get_item_children.return_value = []

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
        # Mock a fatal error
        self.mock_zotero_client.get_library_items.side_effect = Exception("Connection lost")

        result = await self.processor.index_library("test_lib")

        self.assertEqual(result["status"], "failed")
        self.assertIn("error_message", result)
        self.assertEqual(result["error_message"], "Connection lost")

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

        self.mock_zotero_client.get_library_items.return_value = [mock_item]
        self.mock_zotero_client.get_item_children.return_value = mock_pdf_attachments
        self.mock_zotero_client.get_attachment_file.return_value = b"fake pdf"

        self.mock_vector_store.check_duplicate.return_value = None

        mock_pages = [PageText(page_number=1, text="Test content")]
        mock_chunks = [
            TextChunk(
                text="Test content",
                page_number=1,
                chunk_index=0,
                start_char=0,
                end_char=12,
            )
        ]

        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]

        with patch.object(
            self.processor.pdf_extractor, "extract_from_bytes", return_value=mock_pages
        ), patch.object(
            self.processor.text_chunker, "chunk_pages", return_value=mock_chunks
        ):
            result = await self.processor.index_library("test_lib")

        # Should process both PDFs
        self.assertEqual(result["items_processed"], 1)
        # Each PDF creates 1 chunk = 2 total
        self.assertEqual(result["chunks_created"], 2)

    async def test_index_library_pdf_download_failure(self):
        """Test handling when PDF download fails."""
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

        self.mock_zotero_client.get_library_items.return_value = [mock_item]
        self.mock_zotero_client.get_item_children.return_value = [mock_pdf_attachment]
        self.mock_zotero_client.get_attachment_file.return_value = None  # Download failed

        result = await self.processor.index_library("test_lib")

        # Should skip this PDF but not crash
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["chunks_created"], 0)


if __name__ == "__main__":
    unittest.main()
