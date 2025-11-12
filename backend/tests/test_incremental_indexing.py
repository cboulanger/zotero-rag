"""
Unit tests for incremental indexing functionality.
"""

import unittest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime

from backend.services.document_processor import DocumentProcessor
from backend.db.vector_store import VectorStore
from backend.models.library import LibraryIndexMetadata
from backend.zotero.local_api import ZoteroLocalAPI
from backend.services.embeddings import EmbeddingService
from backend.services.pdf_extractor import PDFExtractor, PageText
from backend.services.chunking import TextChunker, TextChunk


class TestIncrementalIndexing(unittest.IsolatedAsyncioTestCase):
    """Test incremental indexing functionality."""

    async def asyncSetUp(self):
        """Set up test fixtures."""
        self.mock_vector_store = Mock(spec=VectorStore)
        self.mock_zotero_client = Mock(spec=ZoteroLocalAPI)
        self.mock_embedding_service = Mock(spec=EmbeddingService)

        self.processor = DocumentProcessor(
            zotero_client=self.mock_zotero_client,
            embedding_service=self.mock_embedding_service,
            vector_store=self.mock_vector_store
        )

    async def test_first_time_indexing_uses_full_mode(self):
        """First-time indexing should use full mode."""
        # Mock: No existing metadata
        self.mock_vector_store.get_library_metadata.return_value = None

        # Mock: Library has 10 items
        mock_items = self._create_mock_items(10)
        self.mock_zotero_client.get_library_items_since = AsyncMock(
            return_value=mock_items
        )

        # Mock: No PDFs to avoid complex PDF processing
        self.mock_zotero_client.get_item_children = AsyncMock(return_value=[])

        # Mock: Vector store operations
        self.mock_vector_store.delete_library_chunks.return_value = 0
        self.mock_vector_store.count_library_chunks.return_value = 0
        self.mock_vector_store.update_library_metadata = Mock()

        # Run indexing with auto mode
        stats = await self.processor.index_library(
            library_id="1",
            mode="auto"
        )

        # Assert: Full mode was used
        self.assertEqual(stats["mode"], "full")

        # Assert: Metadata was created and updated
        self.mock_vector_store.update_library_metadata.assert_called_once()

    async def test_incremental_indexing_fetches_only_new_items(self):
        """Incremental mode should only fetch items since last version."""
        # Mock: Existing metadata
        existing_metadata = LibraryIndexMetadata(
            library_id="1",
            library_type="user",
            library_name="Test Library",
            last_indexed_version=100
        )
        self.mock_vector_store.get_library_metadata.return_value = existing_metadata

        # Mock: 3 new items since version 100
        new_items = self._create_mock_items(3, start_version=101)
        self.mock_zotero_client.get_library_items_since = AsyncMock(
            return_value=new_items
        )

        # Mock: No PDFs to avoid complex PDF processing
        self.mock_zotero_client.get_item_children = AsyncMock(return_value=[])

        # Mock: Vector store operations
        self.mock_vector_store.count_library_chunks.return_value = 0
        self.mock_vector_store.update_library_metadata = Mock()

        # Run incremental indexing
        stats = await self.processor.index_library(
            library_id="1",
            mode="incremental"
        )

        # Assert: Only fetched items since version 100
        self.mock_zotero_client.get_library_items_since.assert_called_once()
        call_args = self.mock_zotero_client.get_library_items_since.call_args
        self.assertEqual(call_args[1]["since_version"], 100)

        # Assert: Incremental mode was used
        self.assertEqual(stats["mode"], "incremental")

    async def test_hard_reset_flag_forces_full_reindex(self):
        """force_reindex flag should trigger full mode."""
        # Mock: Metadata with reset flag
        metadata = LibraryIndexMetadata(
            library_id="1",
            library_type="user",
            library_name="Test Library",
            last_indexed_version=100,
            force_reindex=True  # Hard reset requested
        )
        self.mock_vector_store.get_library_metadata.return_value = metadata

        # Mock: All items
        all_items = self._create_mock_items(50)
        self.mock_zotero_client.get_library_items_since = AsyncMock(
            return_value=all_items
        )

        # Mock: No PDFs
        self.mock_zotero_client.get_item_children = AsyncMock(return_value=[])

        # Mock: Vector store operations
        self.mock_vector_store.delete_library_chunks.return_value = 1000
        self.mock_vector_store.count_library_chunks.return_value = 0
        self.mock_vector_store.update_library_metadata = Mock()

        # Run with auto mode (should detect reset flag)
        stats = await self.processor.index_library(
            library_id="1",
            mode="auto"
        )

        # Assert: Full mode was used despite auto mode
        self.assertEqual(stats["mode"], "full")

        # Assert: Chunks were deleted
        self.mock_vector_store.delete_library_chunks.assert_called_once_with("1")

        # Assert: Reset flag was cleared in metadata
        update_call = self.mock_vector_store.update_library_metadata.call_args[0][0]
        self.assertFalse(update_call.force_reindex)

    async def test_version_comparison_detects_updates(self):
        """Items with higher version should be reindexed."""
        # Mock: Existing metadata
        existing_metadata = LibraryIndexMetadata(
            library_id="1",
            library_type="user",
            library_name="Test Library",
            last_indexed_version=100
        )
        self.mock_vector_store.get_library_metadata.return_value = existing_metadata

        # Mock: Item exists with version 50, Zotero returns version 55
        updated_item = self._create_mock_item("ABCD1234", 105)
        self.mock_zotero_client.get_library_items_since = AsyncMock(
            return_value=[updated_item]
        )

        # Mock: Item has PDF
        mock_pdf_attachment = {
            "data": {
                "key": "ATT001",
                "itemType": "attachment",
                "contentType": "application/pdf"
            },
            "version": 105
        }
        self.mock_zotero_client.get_item_children = AsyncMock(
            return_value=[mock_pdf_attachment]
        )

        # Mock: Item is already indexed with older version
        self.mock_vector_store.get_item_version.return_value = 50

        # Mock: PDF processing
        self.mock_zotero_client.get_attachment_file = AsyncMock(
            return_value=b"fake pdf content"
        )
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_vector_store.delete_item_chunks.return_value = 10

        # Mock PDF extractor
        mock_pages = [PageText(page_number=1, text="Test content")]
        self.processor.pdf_extractor.extract_from_bytes = Mock(return_value=mock_pages)

        # Mock chunker
        mock_chunks = [TextChunk(
            text="Test content",
            page_number=1,
            chunk_index=0,
            start_char=0,
            end_char=12
        )]
        self.processor.text_chunker.chunk_pages = Mock(return_value=mock_chunks)

        # Mock embeddings
        self.mock_embedding_service.embed_batch = AsyncMock(return_value=[[0.1, 0.2, 0.3]])

        # Mock vector store operations
        self.mock_vector_store.add_chunks_batch = Mock()
        self.mock_vector_store.add_deduplication_record = Mock()
        self.mock_vector_store.count_library_chunks.return_value = 1
        self.mock_vector_store.update_library_metadata = Mock()

        # Run incremental indexing
        stats = await self.processor.index_library(
            library_id="1",
            mode="incremental"
        )

        # Assert: Item was detected as updated
        self.assertEqual(stats["items_updated"], 1)
        self.assertEqual(stats["items_added"], 0)

        # Assert: Old chunks were deleted
        self.mock_vector_store.delete_item_chunks.assert_called_once_with("1", "ABCD1234")

        # Assert: New chunks were added
        self.mock_vector_store.add_chunks_batch.assert_called_once()

    async def test_auto_mode_selects_incremental_for_indexed_library(self):
        """Auto mode should select incremental if library already indexed."""
        # Mock: Existing metadata with last indexed version > 0
        existing_metadata = LibraryIndexMetadata(
            library_id="1",
            library_type="user",
            library_name="Test Library",
            last_indexed_version=50
        )
        self.mock_vector_store.get_library_metadata.return_value = existing_metadata

        # Mock: No new items
        self.mock_zotero_client.get_library_items_since = AsyncMock(return_value=[])

        # Mock: Vector store operations
        self.mock_vector_store.count_library_chunks.return_value = 100
        self.mock_vector_store.update_library_metadata = Mock()

        # Run with auto mode
        stats = await self.processor.index_library(
            library_id="1",
            mode="auto"
        )

        # Assert: Incremental mode was selected
        self.assertEqual(stats["mode"], "incremental")

    async def test_new_items_are_added_in_incremental_mode(self):
        """New items should be added in incremental mode."""
        # Mock: Existing metadata
        existing_metadata = LibraryIndexMetadata(
            library_id="1",
            library_type="user",
            library_name="Test Library",
            last_indexed_version=100
        )
        self.mock_vector_store.get_library_metadata.return_value = existing_metadata

        # Mock: One new item
        new_item = self._create_mock_item("NEW001", 101)
        self.mock_zotero_client.get_library_items_since = AsyncMock(
            return_value=[new_item]
        )

        # Mock: Item has PDF
        mock_pdf_attachment = {
            "data": {
                "key": "ATT001",
                "itemType": "attachment",
                "contentType": "application/pdf"
            },
            "version": 101
        }
        self.mock_zotero_client.get_item_children = AsyncMock(
            return_value=[mock_pdf_attachment]
        )

        # Mock: Item is NOT indexed yet
        self.mock_vector_store.get_item_version.return_value = None

        # Mock: PDF processing
        self.mock_zotero_client.get_attachment_file = AsyncMock(
            return_value=b"fake pdf content"
        )
        self.mock_vector_store.check_duplicate.return_value = None

        # Mock PDF extractor
        mock_pages = [PageText(page_number=1, text="Test content")]
        self.processor.pdf_extractor.extract_from_bytes = Mock(return_value=mock_pages)

        # Mock chunker
        mock_chunks = [TextChunk(
            text="Test content",
            page_number=1,
            chunk_index=0,
            start_char=0,
            end_char=12
        )]
        self.processor.text_chunker.chunk_pages = Mock(return_value=mock_chunks)

        # Mock embeddings
        self.mock_embedding_service.embed_batch = AsyncMock(return_value=[[0.1, 0.2, 0.3]])

        # Mock vector store operations
        self.mock_vector_store.add_chunks_batch = Mock()
        self.mock_vector_store.add_deduplication_record = Mock()
        self.mock_vector_store.count_library_chunks.return_value = 1
        self.mock_vector_store.update_library_metadata = Mock()

        # Run incremental indexing
        stats = await self.processor.index_library(
            library_id="1",
            mode="incremental"
        )

        # Assert: Item was added
        self.assertEqual(stats["items_added"], 1)
        self.assertEqual(stats["items_updated"], 0)

        # Assert: Chunks were stored
        self.mock_vector_store.add_chunks_batch.assert_called_once()

    def _create_mock_items(self, count: int, start_version: int = 1) -> list[dict]:
        """Create mock Zotero items for testing."""
        return [
            self._create_mock_item(f"ITEM{i:04d}", start_version + i)
            for i in range(count)
        ]

    def _create_mock_item(self, key: str, version: int) -> dict:
        """Create a single mock Zotero item."""
        return {
            "key": key,
            "version": version,
            "data": {
                "key": key,
                "itemType": "journalArticle",
                "title": f"Test Item {key}",
                "dateModified": "2025-01-12T10:00:00Z",
                "creators": [
                    {"creatorType": "author", "firstName": "John", "lastName": "Doe"}
                ]
            }
        }


if __name__ == "__main__":
    unittest.main()
