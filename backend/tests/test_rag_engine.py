"""
Unit tests for RAG query engine.
"""

import unittest
from unittest.mock import AsyncMock, Mock, MagicMock

from backend.services.rag_engine import RAGEngine, QueryResult, SourceInfo
from backend.services.embeddings import EmbeddingService
from backend.services.llm import LLMService
from backend.db.vector_store import VectorStore, SearchResult
from backend.models.document import (
    DocumentChunk,
    ChunkMetadata,
    DocumentMetadata,
)
from backend.config.settings import Settings


class TestRAGEngine(unittest.IsolatedAsyncioTestCase):
    """Test RAG query engine."""

    def setUp(self):
        """Set up test fixtures."""
        # Create mock services
        self.mock_embedding_service = Mock(spec=EmbeddingService)
        self.mock_llm_service = Mock(spec=LLMService)
        self.mock_vector_store = Mock(spec=VectorStore)
        self.mock_settings = Mock(spec=Settings)

        # Mock preset configuration
        mock_preset = Mock()
        mock_preset.llm.max_answer_tokens = 2048
        self.mock_settings.get_hardware_preset.return_value = mock_preset

        # Create RAG engine
        self.rag_engine = RAGEngine(
            embedding_service=self.mock_embedding_service,
            llm_service=self.mock_llm_service,
            vector_store=self.mock_vector_store,
            settings=self.mock_settings,
        )

    async def test_query_with_results(self):
        """Test successful query with retrieved chunks."""
        # Setup mocks
        question = "What is machine learning?"
        library_ids = ["12345"]
        query_embedding = [0.1, 0.2, 0.3]

        # Mock embedding generation
        self.mock_embedding_service.embed_text = AsyncMock(return_value=query_embedding)

        # Mock search results
        chunk1 = DocumentChunk(
            text="Machine learning is a subset of artificial intelligence.",
            metadata=ChunkMetadata(
                chunk_id="chunk1",
                document_metadata=DocumentMetadata(
                    library_id="12345",
                    item_key="ABC123",
                    attachment_key="ATT1",
                    title="Introduction to ML",
                    authors=["Smith, J."],
                    year=2023,
                    item_type="journalArticle",
                ),
                page_number=5,
                text_preview="Machine learning is a",
                chunk_index=0,
                content_hash="hash1",
            ),
            embedding=None,
        )

        chunk2 = DocumentChunk(
            text="ML algorithms learn patterns from data.",
            metadata=ChunkMetadata(
                chunk_id="chunk2",
                document_metadata=DocumentMetadata(
                    library_id="12345",
                    item_key="DEF456",
                    attachment_key="ATT2",
                    title="ML Algorithms",
                    authors=["Jones, A."],
                    year=2024,
                    item_type="journalArticle",
                ),
                page_number=12,
                text_preview="ML algorithms learn patterns",
                chunk_index=0,
                content_hash="hash2",
            ),
            embedding=None,
        )

        search_results = [
            SearchResult(chunk=chunk1, score=0.95),
            SearchResult(chunk=chunk2, score=0.87),
        ]

        self.mock_vector_store.search.return_value = search_results

        # Mock LLM response
        llm_answer = "Machine learning is a subset of AI that learns patterns from data."
        self.mock_llm_service.generate = AsyncMock(return_value=llm_answer)

        # Execute query
        result = await self.rag_engine.query(
            question=question,
            library_ids=library_ids,
            top_k=5,
            min_score=0.5,
        )

        # Verify embedding was generated
        self.mock_embedding_service.embed_text.assert_called_once_with(question)

        # Verify search was performed
        self.mock_vector_store.search.assert_called_once_with(
            query_vector=query_embedding,
            limit=5,
            score_threshold=0.5,
            library_ids=library_ids,
        )

        # Verify LLM was called with context
        self.mock_llm_service.generate.assert_called_once()
        call_args = self.mock_llm_service.generate.call_args
        prompt = call_args.kwargs["prompt"]

        # Check that context includes both chunks
        self.assertIn("Machine learning is a subset", prompt)
        self.assertIn("ML algorithms learn patterns", prompt)
        self.assertIn(question, prompt)

        # Check source citations in prompt
        self.assertIn("Introduction to ML", prompt)
        self.assertIn("p. 5", prompt)

        # Verify result
        self.assertIsInstance(result, QueryResult)
        self.assertEqual(result.question, question)
        self.assertEqual(result.answer, llm_answer)
        self.assertEqual(len(result.sources), 2)

        # Verify source information
        source1 = result.sources[0]
        self.assertEqual(source1.item_id, "ABC123")
        self.assertEqual(source1.title, "Introduction to ML")
        self.assertEqual(source1.page_number, 5)
        self.assertEqual(source1.text_anchor, "Machine learning is a")
        self.assertEqual(source1.score, 0.95)

        source2 = result.sources[1]
        self.assertEqual(source2.item_id, "DEF456")
        self.assertEqual(source2.title, "ML Algorithms")
        self.assertEqual(source2.page_number, 12)

    async def test_query_no_results(self):
        """Test query with no matching chunks."""
        question = "What is quantum computing?"
        library_ids = ["12345"]

        # Mock embedding generation
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1, 0.2])

        # Mock empty search results
        self.mock_vector_store.search.return_value = []

        # Execute query
        result = await self.rag_engine.query(
            question=question,
            library_ids=library_ids,
        )

        # Verify embedding was generated
        self.mock_embedding_service.embed_text.assert_called_once()

        # Verify search was performed
        self.mock_vector_store.search.assert_called_once()

        # Verify LLM was NOT called
        self.mock_llm_service.generate.assert_not_called()

        # Verify result contains no-results message
        self.assertEqual(result.question, question)
        self.assertIn("couldn't find any relevant information", result.answer)
        self.assertEqual(len(result.sources), 0)

    async def test_query_multiple_libraries(self):
        """Test query across multiple libraries."""
        question = "What is deep learning?"
        library_ids = ["12345", "67890"]

        # Mock embedding generation
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        # Mock search results
        chunk = DocumentChunk(
            text="Deep learning uses neural networks.",
            metadata=ChunkMetadata(
                chunk_id="chunk1",
                document_metadata=DocumentMetadata(
                    library_id="67890",
                    item_key="XYZ789",
                    title="Deep Learning Basics",
                    item_type="book",
                ),
                page_number=None,
                text_preview="Deep learning uses neural",
                chunk_index=0,
                content_hash="hash1",
            ),
        )

        self.mock_vector_store.search.return_value = [
            SearchResult(chunk=chunk, score=0.92)
        ]

        # Mock LLM response
        self.mock_llm_service.generate = AsyncMock(return_value="Deep learning answer")

        # Execute query
        result = await self.rag_engine.query(
            question=question,
            library_ids=library_ids,
            top_k=10,
            min_score=0.6,
        )

        # Verify search was performed with both libraries
        self.mock_vector_store.search.assert_called_once_with(
            query_vector=[0.1],
            limit=10,
            score_threshold=0.6,
            library_ids=library_ids,
        )

        # Verify result
        self.assertEqual(len(result.sources), 1)
        self.assertEqual(result.sources[0].item_id, "XYZ789")

    async def test_query_empty_library_ids(self):
        """Test query with empty library_ids list."""
        question = "Test question"
        library_ids = []

        # Mock services
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])
        self.mock_vector_store.search.return_value = []

        # Execute query
        result = await self.rag_engine.query(
            question=question,
            library_ids=library_ids,
        )

        # Verify search was called with None (search all libraries)
        self.mock_vector_store.search.assert_called_once()
        call_args = self.mock_vector_store.search.call_args
        self.assertIsNone(call_args.kwargs["library_ids"])

    async def test_query_chunk_without_page_number(self):
        """Test query with chunk that has no page number."""
        question = "What is AI?"
        library_ids = ["12345"]

        # Mock embedding
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        # Mock chunk without page number
        chunk = DocumentChunk(
            text="Artificial intelligence is the future.",
            metadata=ChunkMetadata(
                chunk_id="chunk1",
                document_metadata=DocumentMetadata(
                    library_id="12345",
                    item_key="ABC123",
                    title="AI Overview",
                    item_type="webpage",
                ),
                page_number=None,  # No page number
                text_preview="Artificial intelligence is the",
                chunk_index=0,
                content_hash="hash1",
            ),
        )

        self.mock_vector_store.search.return_value = [
            SearchResult(chunk=chunk, score=0.88)
        ]

        # Mock LLM
        self.mock_llm_service.generate = AsyncMock(return_value="AI is the future.")

        # Execute query
        result = await self.rag_engine.query(question=question, library_ids=library_ids)

        # Verify prompt doesn't include page number
        prompt = self.mock_llm_service.generate.call_args.kwargs["prompt"]
        self.assertNotIn("p. ", prompt)
        self.assertIn("AI Overview", prompt)

        # Verify source has None page_number
        self.assertIsNone(result.sources[0].page_number)

    async def test_query_custom_parameters(self):
        """Test query with custom top_k and min_score."""
        question = "Custom parameters test"
        library_ids = ["12345"]

        # Mock services
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])
        self.mock_vector_store.search.return_value = []

        # Execute query with custom params
        await self.rag_engine.query(
            question=question,
            library_ids=library_ids,
            top_k=20,
            min_score=0.8,
        )

        # Verify custom parameters were passed to search
        self.mock_vector_store.search.assert_called_once_with(
            query_vector=[0.1],
            limit=20,
            score_threshold=0.8,
            library_ids=library_ids,
        )

    async def test_query_llm_parameters(self):
        """Test that LLM is called with correct parameters."""
        question = "Test question"
        library_ids = ["12345"]

        # Mock services
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        chunk = DocumentChunk(
            text="Test content",
            metadata=ChunkMetadata(
                chunk_id="chunk1",
                document_metadata=DocumentMetadata(
                    library_id="12345",
                    item_key="ABC",
                    title="Test",
                    item_type="article",
                ),
                page_number=1,
                text_preview="Test content",
                chunk_index=0,
                content_hash="hash",
            ),
        )

        self.mock_vector_store.search.return_value = [
            SearchResult(chunk=chunk, score=0.9)
        ]

        self.mock_llm_service.generate = AsyncMock(return_value="Answer")

        # Execute query
        await self.rag_engine.query(question=question, library_ids=library_ids)

        # Verify LLM was called with correct parameters
        self.mock_llm_service.generate.assert_called_once()
        call_kwargs = self.mock_llm_service.generate.call_args.kwargs

        self.assertIn("prompt", call_kwargs)
        self.assertEqual(call_kwargs["max_tokens"], 2048)  # Uses mock preset value
        self.assertEqual(call_kwargs["temperature"], 0.7)


class TestSourceInfo(unittest.TestCase):
    """Test SourceInfo model."""

    def test_source_info_with_all_fields(self):
        """Test creating SourceInfo with all fields."""
        source = SourceInfo(
            item_id="ABC123",
            library_id="12345",
            title="Test Document",
            page_number=42,
            text_anchor="This is the beginning",
            score=0.95,
        )

        self.assertEqual(source.item_id, "ABC123")
        self.assertEqual(source.library_id, "12345")
        self.assertEqual(source.title, "Test Document")
        self.assertEqual(source.page_number, 42)
        self.assertEqual(source.text_anchor, "This is the beginning")
        self.assertEqual(source.score, 0.95)

    def test_source_info_without_optional_fields(self):
        """Test creating SourceInfo with only required fields."""
        source = SourceInfo(
            item_id="ABC123",
            library_id="12345",
            title="Test Document",
            score=0.85,
        )

        self.assertEqual(source.item_id, "ABC123")
        self.assertEqual(source.library_id, "12345")
        self.assertEqual(source.title, "Test Document")
        self.assertIsNone(source.page_number)
        self.assertIsNone(source.text_anchor)
        self.assertEqual(source.score, 0.85)


class TestQueryResult(unittest.TestCase):
    """Test QueryResult model."""

    def test_query_result(self):
        """Test creating QueryResult."""
        sources = [
            SourceInfo(
                item_id="ABC123",
                library_id="12345",
                title="Doc 1",
                page_number=5,
                text_anchor="Preview text",
                score=0.95,
            ),
            SourceInfo(
                item_id="DEF456",
                library_id="12345",
                title="Doc 2",
                score=0.88,
            ),
        ]

        result = QueryResult(
            question="What is AI?",
            answer="AI is artificial intelligence.",
            sources=sources,
        )

        self.assertEqual(result.question, "What is AI?")
        self.assertEqual(result.answer, "AI is artificial intelligence.")
        self.assertEqual(len(result.sources), 2)
        self.assertEqual(result.sources[0].item_id, "ABC123")
        self.assertEqual(result.sources[1].item_id, "DEF456")


if __name__ == "__main__":
    unittest.main()
