"""
Integration tests for end-to-end workflows.

These tests validate the complete pipeline from indexing to querying.
Real Zotero integration tests require a running Zotero instance with the test group.
"""

import unittest
from unittest.mock import AsyncMock, Mock, patch
import tempfile
import shutil
from pathlib import Path

from backend.services.document_processor import DocumentProcessor
from backend.services.rag_engine import RAGEngine
from backend.services.embeddings import LocalEmbeddingService
from backend.services.llm import RemoteLLMService
from backend.db.vector_store import VectorStore
from backend.zotero.local_api import ZoteroLocalAPI
from backend.config.settings import Settings
from backend.config.presets import HardwarePreset, EmbeddingConfig, LLMConfig, RAGConfig


class TestEndToEndWorkflow(unittest.IsolatedAsyncioTestCase):
    """
    Test complete workflow from indexing to querying.

    These tests use mocked Zotero data to validate the pipeline without
    requiring a running Zotero instance.
    """

    def setUp(self):
        """Set up test fixtures."""
        # Create temporary directory for vector store
        self.temp_dir = tempfile.mkdtemp()
        self.vector_store_path = Path(self.temp_dir) / "vector_store"

        # Create test preset
        self.preset = HardwarePreset(
            name="test-integration",
            description="Integration test preset",
            embedding=EmbeddingConfig(
                model_type="local",
                model_name="sentence-transformers/all-MiniLM-L6-v2",
            ),
            llm=LLMConfig(
                model_type="remote",
                model_name="gpt-4o-mini",
            ),
            rag=RAGConfig(),
            memory_budget_gb=2.0,
        )

        # Create services
        self.embedding_service = LocalEmbeddingService(
            config=self.preset.embedding
        )

        # Get embedding dimension from service
        embedding_dim = self.embedding_service.get_embedding_dim()

        self.vector_store = VectorStore(
            storage_path=self.vector_store_path,
            embedding_dim=embedding_dim
        )

        # Mock Zotero client
        self.mock_zotero_client = Mock(spec=ZoteroLocalAPI)

        # Document processor
        self.document_processor = DocumentProcessor(
            zotero_client=self.mock_zotero_client,
            embedding_service=self.embedding_service,
            vector_store=self.vector_store,
            max_chunk_size=512,
            chunk_overlap=50,
        )

    def tearDown(self):
        """Clean up test fixtures."""
        # Clean up in reverse order of creation to avoid dependencies

        # Close embedding service first to release PyTorch models
        if hasattr(self, 'embedding_service') and hasattr(self.embedding_service, 'close'):
            try:
                self.embedding_service.close()
            except Exception as e:
                # Suppress cleanup errors (e.g., PyTorch access violations on Windows)
                pass

        # Close vector store to release database locks
        if hasattr(self, 'vector_store'):
            try:
                self.vector_store.close()
            except Exception as e:
                # Suppress cleanup errors
                pass

        # Remove temporary directory
        if Path(self.temp_dir).exists():
            try:
                shutil.rmtree(self.temp_dir)
            except Exception as e:
                # If files are still locked, try again or give up
                import time
                time.sleep(0.1)
                try:
                    shutil.rmtree(self.temp_dir)
                except Exception:
                    # Give up - temp dir will be cleaned up eventually
                    pass

    async def test_workflow_without_real_zotero(self):
        """
        Test basic workflow with mocked data.

        This test validates that:
        1. The indexing pipeline can process mocked items
        2. Embeddings are generated and stored
        3. Vector search returns relevant results
        4. The complete integration works
        """
        # Setup: Mock Zotero library items
        # Note: Zotero API returns items with key in both root and data
        mock_items = [
            {
                "key": "ITEM1",
                "version": 1,
                "data": {
                    "key": "ITEM1",  # Key must be in data as well
                    "itemType": "journalArticle",
                    "title": "Introduction to Machine Learning",
                    "creators": [{"lastName": "Smith", "firstName": "John"}],
                    "date": "2023",
                },
            }
        ]

        # Mock PDF attachment
        mock_attachments = [
            {
                "key": "ATT1",
                "data": {
                    "key": "ATT1",  # Key must be in data as well
                    "itemType": "attachment",
                    "contentType": "application/pdf",
                    "title": "PDF",
                },
            }
        ]

        # Configure mocks
        self.mock_zotero_client.get_library_items_since = AsyncMock(return_value=mock_items)
        self.mock_zotero_client.get_item_children = AsyncMock(
            return_value=mock_attachments
        )

        # Mock PDF download and extraction
        sample_pdf_content = b"%PDF-1.4 fake content"

        # Create a simple PDF with pypdf for testing
        from io import BytesIO
        from pypdf import PdfWriter

        pdf_writer = PdfWriter()
        pdf_writer.add_blank_page(width=200, height=200)

        pdf_buffer = BytesIO()
        pdf_writer.write(pdf_buffer)
        pdf_buffer.seek(0)

        self.mock_zotero_client.get_attachment_file = AsyncMock(
            return_value=pdf_buffer.read()
        )

        # Mock PDF extraction to return test content
        # Note: We patch the instance, not the class, since it's already created in setUp
        mock_pdf_extractor = Mock()
        mock_pdf_extractor.extract_from_bytes.return_value = [
            Mock(page_number=1, text="Machine learning is a branch of AI."),
            Mock(page_number=2, text="It uses algorithms to learn from data."),
        ]
        self.document_processor.pdf_extractor = mock_pdf_extractor

        # Execute: Index the library
        result = await self.document_processor.index_library(
            library_id="test_lib",
            library_type="user",
            mode="auto",
        )

        # Verify: Indexing succeeded
        self.assertIn("mode", result)  # Should have mode field
        self.assertEqual(result["items_processed"], 1)
        self.assertGreater(result["chunks_added"], 0)

        # Verify: Chunks are in vector store
        # Test vector search with a query about machine learning
        query_text = "What is machine learning?"
        query_embedding = await self.embedding_service.embed_text(query_text)

        search_results = self.vector_store.search(
            query_vector=query_embedding,
            limit=5,
            library_ids=["test_lib"],
        )

        # Should find relevant chunks
        self.assertGreater(len(search_results), 0)

        # Verify chunk metadata
        first_result = search_results[0]
        self.assertIn("machine learning", first_result.chunk.text.lower())
        self.assertEqual(first_result.chunk.metadata.document_metadata.library_id, "test_lib")
        self.assertEqual(first_result.chunk.metadata.document_metadata.item_key, "ITEM1")

    async def test_rag_query_with_mock_llm(self):
        """Test RAG query with mocked LLM service."""
        # Setup: Create mock LLM service
        mock_llm = Mock(spec=RemoteLLMService)
        mock_llm.generate = AsyncMock(
            return_value="Machine learning is a subset of artificial intelligence that enables computers to learn from data."
        )

        # Create RAG engine
        rag_engine = RAGEngine(
            embedding_service=self.embedding_service,
            llm_service=mock_llm,
            vector_store=self.vector_store,
        )

        # Setup: Add a test chunk to vector store
        from backend.models.document import DocumentChunk, ChunkMetadata, DocumentMetadata

        test_text = "Machine learning is a branch of AI that focuses on learning from data."
        test_embedding = await self.embedding_service.embed_text(test_text)

        test_chunk = DocumentChunk(
            text=test_text,
            metadata=ChunkMetadata(
                chunk_id="test_chunk_1",
                document_metadata=DocumentMetadata(
                    library_id="test_lib",
                    item_key="ITEM1",
                    title="ML Introduction",
                    authors=["Smith, J."],
                    year=2023,
                    item_type="article",
                ),
                page_number=1,
                text_preview="Machine learning is a",
                chunk_index=0,
                content_hash="testhash",
            ),
            embedding=test_embedding,
        )

        self.vector_store.add_chunk(test_chunk)

        # Execute: Query the RAG engine
        result = await rag_engine.query(
            question="What is machine learning?",
            library_ids=["test_lib"],
            top_k=5,
            min_score=0.3,
        )

        # Verify: Got an answer
        self.assertIsNotNone(result.answer)
        self.assertIn("Machine learning", result.answer)

        # Verify: Got source citations
        self.assertGreater(len(result.sources), 0)
        self.assertEqual(result.sources[0].item_id, "ITEM1")
        self.assertEqual(result.sources[0].title, "ML Introduction")
        self.assertEqual(result.sources[0].page_number, 1)

        # Verify: LLM was called with context
        mock_llm.generate.assert_called_once()
        call_args = mock_llm.generate.call_args
        prompt = call_args.kwargs["prompt"]

        # Check that context was included
        self.assertIn("Machine learning is a branch", prompt)
        self.assertIn("What is machine learning?", prompt)


class TestRealZoteroIntegration(unittest.IsolatedAsyncioTestCase):
    """
    Tests for real Zotero integration.

    These tests require a running Zotero instance with the test group synced.
    They are skipped if ZOTERO_INTEGRATION_TESTS environment variable is not set.

    To enable:
        export ZOTERO_INTEGRATION_TESTS=1
        # Ensure Zotero is running with test group: https://www.zotero.org/groups/6297749/test-rag-plugin

    These tests are intended for manual validation and may take longer to run.
    """

    @unittest.skip("Requires real Zotero instance - enable manually for integration testing")
    async def test_index_real_library(self):
        """
        Test indexing with real Zotero test group.

        Requirements:
        - Zotero running locally
        - Test group synced: https://www.zotero.org/groups/6297749/test-rag-plugin
        """
        # This is a placeholder for real integration testing
        # Implement when ready to test with actual Zotero instance
        pass

    @unittest.skip("Requires real Zotero instance - enable manually for integration testing")
    async def test_query_real_library(self):
        """
        Test querying indexed real library.

        Requirements:
        - Zotero running locally
        - Test group indexed
        - API key for LLM service
        """
        # This is a placeholder for real integration testing
        # Implement when ready to test with actual Zotero instance
        pass


if __name__ == "__main__":
    unittest.main()
