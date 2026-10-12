"""
Unit tests for RAG query engine.
"""

import unittest
from unittest.mock import AsyncMock, Mock, MagicMock

from backend.services.rag_engine import (
    RAGEngine, QueryResult, SourceInfo, _looks_like_leaked_reasoning, _looks_like_wrong_language_answer,
)
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
        llm_answer = "Machine learning is a subset of AI that learns patterns from data. [S1]"
        self.mock_llm_service.generate = AsyncMock(return_value=llm_answer)

        # Execute query
        result = await self.rag_engine.query(
            question=question,
            library_ids=library_ids,
            top_k=5,
            min_score=0.5,
            search_candidate_pool_size=5,
        )

        # Verify embedding was generated
        self.mock_embedding_service.embed_text.assert_called_once_with(question)

        # Verify search was performed
        self.mock_vector_store.search.assert_called_once_with(
            query_vector=query_embedding,
            limit=5,
            score_threshold=0.5,
            library_ids=library_ids,
            filters=None,
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
        # page_number is intentionally None on SourceInfo: page citations are
        # embedded inline in the LLM answer via [N:P] notation rather than on
        # the deduplicated document source record.
        self.assertIsNone(source1.page_number)
        self.assertEqual(source1.text_anchor, "Machine learning is a")
        self.assertEqual(source1.score, 0.95)

        source2 = result.sources[1]
        self.assertEqual(source2.item_id, "DEF456")
        self.assertEqual(source2.title, "ML Algorithms")
        self.assertIsNone(source2.page_number)

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
        self.mock_llm_service.generate = AsyncMock(return_value="Deep learning answer [S1]")

        # Execute query
        result = await self.rag_engine.query(
            question=question,
            library_ids=library_ids,
            top_k=10,
            min_score=0.6,
            search_candidate_pool_size=10,
        )

        # Verify search was performed with both libraries
        self.mock_vector_store.search.assert_called_once_with(
            query_vector=[0.1],
            limit=10,
            score_threshold=0.6,
            library_ids=library_ids,
            filters=None,
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
        self.mock_llm_service.generate = AsyncMock(return_value="AI is the future. [S1]")

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
            search_candidate_pool_size=20,
        )

        # Verify custom parameters were passed to search
        self.mock_vector_store.search.assert_called_once_with(
            query_vector=[0.1],
            limit=20,
            score_threshold=0.8,
            library_ids=library_ids,
            filters=None,
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

        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        # Execute query
        await self.rag_engine.query(question=question, library_ids=library_ids)

        # Verify LLM was called with correct parameters
        self.mock_llm_service.generate.assert_called_once()
        call_kwargs = self.mock_llm_service.generate.call_args.kwargs

        self.assertIn("prompt", call_kwargs)
        self.assertEqual(call_kwargs["max_tokens"], 2048)  # Uses mock preset value
        self.assertEqual(call_kwargs["temperature"], 0.7)

    async def test_source_info_carries_chunk_id(self):
        question = "What is machine learning?"
        library_ids = ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1, 0.2, 0.3])

        chunk = DocumentChunk(
            text="Machine learning is a subset of artificial intelligence.",
            metadata=ChunkMetadata(
                chunk_id="chunk1",
                document_metadata=DocumentMetadata(
                    library_id="12345", item_key="ABC123", attachment_key="ATT1",
                    title="Introduction to ML", authors=["Smith, J."], year=2023,
                    item_type="journalArticle",
                ),
                page_number=5, text_preview="Machine learning is a", chunk_index=0,
                content_hash="h1",
            ),
        )
        self.mock_vector_store.search.return_value = [
            SearchResult(chunk=chunk, score=0.9),
        ]
        self.mock_llm_service.generate = AsyncMock(return_value="An answer [S1].")

        result = await self.rag_engine.query(question, library_ids)
        self.assertEqual(result.sources[0].chunk_id, "chunk1")

    async def _query_with_single_chunk(self):
        """Shared minimal fixture for the tool-call-leak retry tests below."""
        question = "What research trends do authors identify?"
        library_ids = ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1, 0.2, 0.3])

        chunk = DocumentChunk(
            text="Several studies identify a shift toward qualitative methods.",
            metadata=ChunkMetadata(
                chunk_id="chunk1",
                document_metadata=DocumentMetadata(
                    library_id="12345", item_key="ABC123", attachment_key="ATT1",
                    title="Research Trends", authors=["Melles, J."], year=2015,
                    item_type="journalArticle",
                ),
                page_number=5, text_preview="Several studies identify", chunk_index=0,
                content_hash="h1",
            ),
        )
        self.mock_vector_store.search.return_value = [
            SearchResult(chunk=chunk, score=0.9),
        ]
        return question, library_ids

    async def test_query_retries_once_when_answer_looks_like_a_tool_call_leak(self):
        """Some models occasionally hallucinate tool/function-call pseudocode
        instead of a plain-language answer, even though no `tools` parameter
        is ever sent to the LLM. Detect this and retry once."""
        question, library_ids = await self._query_with_single_chunk()
        bad_answer = "tool.call('getResearchTrends', (Melles et al., 2015))"
        good_answer = "Authors identify a shift toward qualitative methods [S1]."
        self.mock_llm_service.generate = AsyncMock(side_effect=[bad_answer, good_answer])

        result = await self.rag_engine.query(question, library_ids)

        self.assertEqual(self.mock_llm_service.generate.call_count, 2)
        self.assertEqual(result.answer, good_answer)

    async def test_query_uses_final_answer_if_retry_still_looks_like_a_tool_call_leak(self):
        """If the retry also looks like a tool-call leak, don't loop forever —
        use the retry's answer anyway (best effort) rather than the original."""
        question, library_ids = await self._query_with_single_chunk()
        bad_answer_1 = "tool.call('getResearchTrends', (Melles et al., 2015))"
        bad_answer_2 = "function_call(getTrends, [S1])"
        self.mock_llm_service.generate = AsyncMock(side_effect=[bad_answer_1, bad_answer_2])

        result = await self.rag_engine.query(question, library_ids)

        self.assertEqual(self.mock_llm_service.generate.call_count, 2)
        self.assertEqual(result.answer, bad_answer_2)

    async def test_query_calls_generate_once_when_answer_looks_normal(self):
        """No regression: a normal prose answer must not trigger a retry."""
        question, library_ids = await self._query_with_single_chunk()
        self.mock_llm_service.generate = AsyncMock(
            return_value="Authors identify a shift toward qualitative methods [S1]."
        )

        result = await self.rag_engine.query(question, library_ids)

        self.mock_llm_service.generate.assert_called_once()
        self.assertEqual(result.answer, "Authors identify a shift toward qualitative methods [S1].")

    async def test_query_retries_once_when_answer_looks_like_leaked_reasoning(self):
        """A reasoning-capable model can leak its raw step-by-step deliberation
        into the final answer instead of a clean conclusion (e.g. repeated
        "Wait," self-contradicting re-derivations) — not as a separate
        <think> block, just as ordinary backtracking prose. Detect this and
        retry once, same as the tool-call-leak case above."""
        question, library_ids = await self._query_with_single_chunk()
        bad_answer = (
            "Authors identify a shift toward qualitative methods [S1]. "
            "Wait, actually the text says quantitative methods [S1]. "
            "Re-evaluating strictly based on the source: Wait, it does say "
            "qualitative after all [S1]."
        )
        good_answer = "Authors identify a shift toward qualitative methods [S1]."
        self.mock_llm_service.generate = AsyncMock(side_effect=[bad_answer, good_answer])

        result = await self.rag_engine.query(question, library_ids)

        self.assertEqual(self.mock_llm_service.generate.call_count, 2)
        self.assertEqual(result.answer, good_answer)

    async def test_query_uses_final_answer_if_retry_still_looks_like_leaked_reasoning(self):
        """If the retry also leaks reasoning, don't loop forever — use the
        retry's answer anyway (best effort) rather than the original."""
        question, library_ids = await self._query_with_single_chunk()
        bad_answer_1 = (
            "Wait, let me reconsider [S1]. Re-evaluating the claim [S1]."
        )
        bad_answer_2 = (
            "Wait, on second thought [S1]. Re-evaluating once more [S1]."
        )
        self.mock_llm_service.generate = AsyncMock(side_effect=[bad_answer_1, bad_answer_2])

        result = await self.rag_engine.query(question, library_ids)

        self.assertEqual(self.mock_llm_service.generate.call_count, 2)
        self.assertEqual(result.answer, bad_answer_2)

    def test_looks_like_leaked_reasoning_ignores_single_incidental_match(self):
        """A single incidental use of a backtracking word (e.g. a paper that
        legitimately discusses "wait times") must not trigger a retry —
        only repeated backtracking language should."""
        self.assertFalse(_looks_like_leaked_reasoning(
            "The study measured patient wait times in the emergency room [S1]."
        ))

    async def test_query_retries_once_when_answer_is_english_but_question_is_not(self):
        """Observed live (qwen3-omni-30b-a3b-instruct, eval run
        20261011T111753Z): the model answered 2 of 3 non-English gold
        questions (French, German) entirely in English despite the prompt's
        explicit "respond in the same language" instruction, while every
        other model matched 100% of the time on the identical prompt.
        Detect the mismatch and retry once, same pattern as the other
        quality-issue checks."""
        question, library_ids = await self._query_with_single_chunk()
        question = "Wie viel kostenlosen Cloud-Speicher bieten Zotero und Mendeley jeweils, und welche Quellen in der Bibliothek nennen das?"
        english_answer = (
            "According to the context, Zotero offers 300 MB of free cloud storage [S1], "
            "and the same source also mentions that this can be increased with a paid plan."
        )
        german_answer = "Laut dem Kontext bietet Zotero 300 MB kostenlosen Speicherplatz [S1]."
        self.mock_llm_service.generate = AsyncMock(side_effect=[english_answer, german_answer])

        result = await self.rag_engine.query(question, library_ids)

        self.assertEqual(self.mock_llm_service.generate.call_count, 2)
        self.assertEqual(result.answer, german_answer)

    async def test_query_uses_final_answer_if_retry_is_still_the_wrong_language(self):
        """Don't loop forever if the retry also comes back in English — use
        it anyway (best effort), same pattern as the other quality checks."""
        question, library_ids = await self._query_with_single_chunk()
        question = "Wie viel kostenlosen Cloud-Speicher bieten Zotero und Mendeley jeweils, und welche Quellen in der Bibliothek nennen das?"
        english_answer_1 = (
            "According to the context, Zotero offers 300 MB of free cloud storage [S1], "
            "and the same source also mentions that this can be increased with a paid plan."
        )
        english_answer_2 = (
            "Based on the context provided, Zotero offers 300 MB of storage for free [S1], "
            "and the same source notes that this limit can be raised with a paid plan too."
        )
        self.mock_llm_service.generate = AsyncMock(side_effect=[english_answer_1, english_answer_2])

        result = await self.rag_engine.query(question, library_ids)

        self.assertEqual(self.mock_llm_service.generate.call_count, 2)
        self.assertEqual(result.answer, english_answer_2)

    def test_looks_like_wrong_language_answer_false_when_question_is_english(self):
        """No regression: an English question answered in English must not
        trigger a retry (nothing to compare against)."""
        self.assertFalse(_looks_like_wrong_language_answer(
            "What research trends do authors identify?",
            "Authors identify a shift toward qualitative methods [S1].",
        ))

    def test_looks_like_wrong_language_answer_false_for_non_english_answer_quoting_english_terms(self):
        """Observed live (deepseek-v4-flash-0731 and others, eval run
        20261011T111753Z, Q10): a genuinely German answer about two
        English-named systems (PyZoBot, HySemRAG) can rack up a handful of
        incidental English-stopword hits ("in", "to", "on", "the"...) from
        quoted technical terms, without actually being an English answer.
        An absolute stopword-count floor alone flagged this; density must
        also be checked (a real English answer is >30% stopwords, this one
        is ~3%)."""
        question = "Vergleiche PyZoBot und HySemRAG: Welche Retrieval-Ansätze setzen sie ein?"
        answer = (
            "Die verfügbaren Kontextabschnitte enthalten nur begrenzte Informationen zu "
            "PyZoBot und HySemRAG und reichen nicht aus, um die Frage vollständig zu "
            "beantworten. Zu PyZoBot ist bekannt: das System verwendet ein "
            "Prompt-Design, das auf einem strukturierten, evidenzbasierten Format "
            "basiert, um Genauigkeit, Transparenz und Rückverfolgbarkeit zu maximieren, "
            "laut der verfügbaren Quelle, die dies im Detail beschreibt und erläutert."
        )
        self.assertFalse(_looks_like_wrong_language_answer(question, answer))

    def test_looks_like_wrong_language_answer_false_when_both_non_english(self):
        """No regression: a non-English question answered in the same
        non-English language must not trigger a retry."""
        self.assertFalse(_looks_like_wrong_language_answer(
            "Combien d'articles ont servi à tester la corrélation?",
            "Mille six cent treize articles ont servi à tester la corrélation [S1].",
        ))

    async def test_prompt_forbids_process_narration_and_ungrounded_speculation(self):
        """A weak model can narrate its process ("I will use the library to find
        relevant information...") or, when the context is thin, supplement its
        answer with unsourced general-knowledge speculation. The prompt must
        explicitly forbid both."""
        question, library_ids = await self._query_with_single_chunk()
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        await self.rag_engine.query(question, library_ids)

        prompt = self.mock_llm_service.generate.call_args.kwargs["prompt"].lower()
        self.assertIn("do not narrate", prompt)
        self.assertIn("stop there", prompt)

    def _make_chunk(self, item_key, attachment_key, title, score, chunk_index=0, text=None):
        if text is None:
            text = f"Content from {title}, chunk {chunk_index}."
        chunk = DocumentChunk(
            text=text,
            metadata=ChunkMetadata(
                chunk_id=f"{item_key}-{chunk_index}",
                document_metadata=DocumentMetadata(
                    library_id="12345", item_key=item_key, attachment_key=attachment_key,
                    title=title, authors=["Author, A."], year=2024,
                    item_type="journalArticle",
                ),
                page_number=1, text_preview=title, chunk_index=chunk_index,
                content_hash=f"hash-{item_key}-{chunk_index}",
            ),
        )
        return SearchResult(chunk=chunk, score=score)

    async def test_query_escalates_retrieval_when_top_k_is_saturated_and_diversity_is_low(self):
        """Observed live: a fixed top_k=10 can be fully saturated by chunks from a
        single dominant document, starving the answer of other relevant sources
        even when they exist in the library. When the raw search hits the top_k
        cap (not the corpus limit) and fewer than 3 distinct documents came back,
        retry once with a larger top_k before generating."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        narrow_results = [self._make_chunk("DOC1", "ATT1", "Dominant Doc", 0.9, i) for i in range(5)]
        diverse_results = (
            [self._make_chunk("DOC1", "ATT1", "Dominant Doc", 0.9, i) for i in range(5)]
            + [self._make_chunk("DOC2", "ATT2", "Second Doc", 0.85)]
            + [self._make_chunk("DOC3", "ATT3", "Third Doc", 0.8)]
        )
        self.mock_vector_store.search = Mock(side_effect=[narrow_results, diverse_results])
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1,S2,S3]")

        result = await self.rag_engine.query(
            question, library_ids, top_k=5, search_candidate_pool_size=5,
        )

        self.assertEqual(self.mock_vector_store.search.call_count, 2)
        second_call_kwargs = self.mock_vector_store.search.call_args_list[1].kwargs
        self.assertGreater(second_call_kwargs["limit"], 5)
        self.assertEqual(len(result.sources), 3)

    async def test_query_escalates_when_one_item_has_multiple_attachments(self):
        """Observed live: a single Zotero item with several indexed attachments
        (e.g. multiple PDF versions of the same paper) must count as ONE document
        for diversity purposes, not one per attachment_key. Otherwise a single
        paper whose attachments happen to split across >= diversity_floor
        attachment_keys can saturate top_k and suppress escalation even though
        the library has only one genuinely relevant item represented."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        narrow_results = (
            [self._make_chunk("DOC1", "ATT1", "Same Paper", 0.9, i) for i in range(4)]
            + [self._make_chunk("DOC1", "ATT2", "Same Paper", 0.88, i) for i in range(3)]
            + [self._make_chunk("DOC1", "ATT3", "Same Paper", 0.86, i) for i in range(3)]
        )
        diverse_results = narrow_results + [
            self._make_chunk("DOC2", "ATT4", "Second Doc", 0.7),
            self._make_chunk("DOC3", "ATT5", "Third Doc", 0.65),
        ]
        self.mock_vector_store.search = Mock(side_effect=[narrow_results, diverse_results])
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1,S2,S3]")

        result = await self.rag_engine.query(
            question, library_ids, top_k=10, search_candidate_pool_size=10,
        )

        self.assertEqual(
            self.mock_vector_store.search.call_count, 2,
            "same item_key across 3 attachment_keys must still count as 1 "
            "document and trigger escalation (1 < diversity_floor=3)",
        )
        self.assertEqual(len(result.sources), 3)

    async def test_query_does_not_escalate_when_diversity_already_sufficient(self):
        """No wasted second search when the first pass already spans enough documents."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        results = [
            self._make_chunk("DOC1", "ATT1", "First Doc", 0.9),
            self._make_chunk("DOC2", "ATT2", "Second Doc", 0.85),
            self._make_chunk("DOC3", "ATT3", "Third Doc", 0.8),
        ]
        self.mock_vector_store.search = Mock(return_value=results)
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1,S2,S3]")

        await self.rag_engine.query(question, library_ids, top_k=3)

        self.mock_vector_store.search.assert_called_once()

    async def test_query_does_not_escalate_when_raw_results_are_below_top_k(self):
        """If the search returned fewer chunks than top_k, the corpus (or the
        min_score threshold) is already exhausted — escalating limit wouldn't
        surface anything new, so don't waste a second search."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        results = [self._make_chunk("DOC1", "ATT1", "Only Doc", 0.9)]
        self.mock_vector_store.search = Mock(return_value=results)
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        await self.rag_engine.query(question, library_ids, top_k=10)

        self.mock_vector_store.search.assert_called_once()

    async def test_query_does_not_escalate_past_the_max_top_k(self):
        """Already at/above the escalation ceiling — don't escalate further even
        if diversity is low, to keep prompt size bounded."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        results = [self._make_chunk("DOC1", "ATT1", "Dominant Doc", 0.9, i) for i in range(30)]
        self.mock_vector_store.search = Mock(return_value=results)
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        await self.rag_engine.query(question, library_ids, top_k=30)

        self.mock_vector_store.search.assert_called_once()

    async def test_query_surfaces_document_crowded_out_of_top_k_by_other_documents(self):
        """Observed live (#87, jin2026): a relevant document's only chunk can
        rank just outside a naive top_k window because a few OTHER documents
        together contribute enough higher-scoring chunks to fill it — even
        though the window already contains exactly diversity_floor distinct
        documents, so the existing diversity-escalation mechanism (which
        only checks DISTINCT document count, not per-document concentration)
        never catches this. Capping each document's contribution to the
        candidate pool BEFORE the top_k cutoff (not just at context-assembly
        time, which only runs on whatever already survived the cutoff) must
        let the crowded-out document back in.

        Fixture: DOC_A supplies 6 of the top-10 raw-score slots (ranks 1-6),
        DOC_B 3 (ranks 7-9), DOC_C 1 (rank 10) — exactly 3 distinct
        documents in the naive top_k=10 window, meeting diversity_floor=3
        exactly, so escalation never fires either. DOC_TARGET's only chunk
        is rank 11, just outside. Capping each document to its top 4 chunks
        drops DOC_A's 2 lowest (ranks 5-6), which is enough to pull
        DOC_TARGET into the resulting top_k=10 window — a real vector store
        honors `limit` by truncating to the N highest-scoring results, so
        the mock must do the same (a plain Mock(return_value=...) would
        hand back everything regardless of `limit`, masking the exact
        truncation behavior this test exercises)."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        crowding_a = [self._make_chunk("DOC_A", "ATT_A", "Crowding Doc A", 0.99 - i * 0.01, i) for i in range(6)]
        crowding_b = [self._make_chunk("DOC_B", "ATT_B", "Crowding Doc B", 0.93 - i * 0.01, i) for i in range(3)]
        crowding_c = [self._make_chunk("DOC_C", "ATT_C", "Crowding Doc C", 0.90)]
        target = self._make_chunk("DOC_TARGET", "ATT_T", "Target Doc", 0.89)
        pool = sorted(crowding_a + crowding_b + crowding_c + [target], key=lambda r: r.score, reverse=True)

        def fake_search(*, query_vector, limit, score_threshold, library_ids, filters):
            return pool[:limit]

        self.mock_vector_store.search = Mock(side_effect=fake_search)
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1,S2,S3,S4]")

        result = await self.rag_engine.query(question, library_ids, top_k=10)

        cited_docs = {s.item_id for s in result.sources}
        self.assertIn(
            "DOC_TARGET", cited_docs,
            "Target document's only chunk (score 0.89, raw rank #11) must "
            "survive the per-document cap applied before the top_k cutoff, "
            "even though the naive top-10 window already contains exactly "
            "diversity_floor (3) distinct documents and so never escalates.",
        )

    async def test_query_caps_chunks_per_document_in_the_assembled_context(self):
        """A single over-chunked document must not be allowed to dominate the
        prompt with many near-duplicate passages at the expense of readability —
        cap how many of its chunks are included, keeping the highest-scoring ones."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        results = [self._make_chunk("DOC1", "ATT1", "Dominant Doc", 0.9 - i * 0.01, i) for i in range(10)]
        self.mock_vector_store.search = Mock(return_value=results)
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        await self.rag_engine.query(question, library_ids, top_k=10)

        prompt = self.mock_llm_service.generate.call_args.kwargs["prompt"]
        self.assertLessEqual(prompt.count("Content from Dominant Doc"), 4)

    async def test_query_custom_max_chunks_per_document_overrides_default(self):
        """The chunk cap must be caller-configurable (plugin Preferences), not
        hardwired to the module default."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        results = [self._make_chunk("DOC1", "ATT1", "Dominant Doc", 0.9 - i * 0.01, i) for i in range(10)]
        self.mock_vector_store.search = Mock(return_value=results)
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        await self.rag_engine.query(question, library_ids, top_k=10, max_chunks_per_document=2)

        prompt = self.mock_llm_service.generate.call_args.kwargs["prompt"]
        self.assertLessEqual(prompt.count("Content from Dominant Doc"), 2)

    async def test_query_per_document_cap_favors_diverse_chunks_over_near_duplicates(self):
        """Observed live (#87 follow-up): a document's own highest-scoring
        chunks can be several near-paraphrases of the same narrow sub-topic
        (e.g. three different sentences all about the survey's response
        rate), each scoring a hair higher than the one chunk that actually
        answers a different part of the question (e.g. which programs were
        used). Plain top-N-by-score caps this document's context to the
        near-duplicate cluster and drops the only chunk with the needed
        fact. The cap must instead prefer a lower-scoring chunk once a
        higher-scoring candidate is too similar to one already kept."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        cluster_1 = self._make_chunk(
            "DOC1", "ATT1", "Doc", 0.95, chunk_index=0,
            text="The survey achieved a high response rate among the authors who were surveyed about the response rate study.",
        )
        cluster_2 = self._make_chunk(
            "DOC1", "ATT1", "Doc", 0.94, chunk_index=1,
            text="Response rate for the survey was high and authors reported a strong response rate overall in the study.",
        )
        cluster_3 = self._make_chunk(
            "DOC1", "ATT1", "Doc", 0.93, chunk_index=2,
            text="Authors surveyed reported a high response rate in this response rate study of systematic reviews.",
        )
        fact_chunk = self._make_chunk(
            "DOC1", "ATT1", "Doc", 0.90, chunk_index=3,
            text="EndNote was used by forty-one authors, Reference Manager by fourteen, and RefWorks by six participants.",
        )

        self.mock_vector_store.search = Mock(return_value=[cluster_1, cluster_2, cluster_3, fact_chunk])
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        await self.rag_engine.query(question, library_ids, top_k=4, max_chunks_per_document=2)

        prompt = self.mock_llm_service.generate.call_args.kwargs["prompt"]
        self.assertIn(
            fact_chunk.chunk.text, prompt,
            "the fact-bearing chunk should survive the cap by virtue of being "
            "topically distinct from the already-selected top-scoring chunk, "
            "even though its raw score is lower than the near-duplicates",
        )

    async def test_query_dedupes_identical_chunk_text_within_one_document(self):
        """Observed live: the same passage can be indexed twice for one
        Zotero item (e.g. extracted from two attachment_keys, or re-chunked
        during a second indexing pass), producing two SearchResults with
        identical text. These must not both consume a context slot — keep
        the higher-scoring copy so a genuinely different chunk can fill the
        slot that would otherwise be wasted on the duplicate."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        duplicate_text = "Identical passage repeated across two attachments."
        chunk_a = self._make_chunk("DOC1", "ATT1", "Doc", 0.9, chunk_index=0, text=duplicate_text)
        chunk_b = self._make_chunk("DOC1", "ATT2", "Doc", 0.85, chunk_index=1, text=duplicate_text)
        unique_chunk = self._make_chunk("DOC1", "ATT3", "Doc", 0.8, chunk_index=2)

        self.mock_vector_store.search = Mock(return_value=[chunk_a, chunk_b, unique_chunk])
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        await self.rag_engine.query(question, library_ids, top_k=3, max_chunks_per_document=2)

        prompt = self.mock_llm_service.generate.call_args.kwargs["prompt"]
        self.assertEqual(prompt.count(duplicate_text), 1)
        self.assertIn(unique_chunk.chunk.text, prompt)

    async def test_query_dedup_keeps_higher_scoring_duplicate(self):
        """When two chunks have identical text, the surviving copy's score
        must be the higher of the two, so it isn't treated as a
        lower-quality hit downstream (e.g. in SourceInfo.score)."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        duplicate_text = "Identical passage."
        low_score_first = self._make_chunk("DOC1", "ATT1", "Doc", 0.7, chunk_index=0, text=duplicate_text)
        high_score_second = self._make_chunk("DOC1", "ATT2", "Doc", 0.95, chunk_index=1, text=duplicate_text)

        self.mock_vector_store.search = Mock(return_value=[low_score_first, high_score_second])
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        result = await self.rag_engine.query(question, library_ids, top_k=2)

        self.assertEqual(result.sources[0].score, 0.95)

    async def test_query_dedup_is_case_and_whitespace_insensitive(self):
        """Observed live: the same passage extracted twice differed only by
        a capitalization/whitespace artifact from re-extraction — this must
        still be recognized as a duplicate."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        chunk_a = self._make_chunk(
            "DOC1", "ATT1", "Doc", 0.9, chunk_index=0,
            text="RechtsVergleichung  und   Rechtsdogmatik",
        )
        chunk_b = self._make_chunk(
            "DOC1", "ATT2", "Doc", 0.85, chunk_index=1,
            text="Rechtsvergleichung und Rechtsdogmatik",
        )

        self.mock_vector_store.search = Mock(return_value=[chunk_a, chunk_b])
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        await self.rag_engine.query(question, library_ids, top_k=2)

        prompt = self.mock_llm_service.generate.call_args.kwargs["prompt"]
        self.assertIn(chunk_a.chunk.text, prompt)
        self.assertNotIn(chunk_b.chunk.text, prompt)

    async def test_query_dedup_does_not_collapse_distinct_chunks(self):
        """No regression: chunks with genuinely different text must not be
        affected by dedup, even within the same document."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        results = [self._make_chunk("DOC1", "ATT1", "Dominant Doc", 0.9 - i * 0.01, i) for i in range(3)]
        self.mock_vector_store.search = Mock(return_value=results)
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        await self.rag_engine.query(question, library_ids, top_k=3, max_chunks_per_document=10)

        prompt = self.mock_llm_service.generate.call_args.kwargs["prompt"]
        self.assertEqual(prompt.count("Content from Dominant Doc"), 3)

    async def test_query_dedupes_identical_chunk_text_across_different_documents(self):
        """Observed live (#87 follow-up): a Zotero library can hold two
        separate items (different item_keys) for the same underlying article
        (e.g. one user adds it twice, or it's imported from two sources) —
        the gold test library's lorenzetti2013 entry is exactly this case.
        Both items' attachments extract to identical chunk text, so the
        existing within-document dedup (keyed by item_key) never sees them
        as duplicates: each item's copy independently consumes its own
        top_k slot with the same content, crowding out a genuinely
        different document that would otherwise have made the window."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        duplicate_text = "The same article, indexed twice under two different library items."
        dup_a = self._make_chunk("DOC_A", "ATT_A", "Duplicate Item A", 0.9, chunk_index=0, text=duplicate_text)
        dup_b = self._make_chunk("DOC_B", "ATT_B", "Duplicate Item B", 0.85, chunk_index=0, text=duplicate_text)
        other_doc = self._make_chunk("DOC_C", "ATT_C", "Other Doc", 0.8, chunk_index=0)
        crowded_out_doc = self._make_chunk("DOC_D", "ATT_D", "Crowded Out Doc", 0.75, chunk_index=0)

        self.mock_vector_store.search = Mock(return_value=[dup_a, dup_b, other_doc, crowded_out_doc])
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1,S2,S3]")

        result = await self.rag_engine.query(question, library_ids, top_k=3)

        cited_docs = {s.item_id for s in result.sources}
        self.assertIn(
            "DOC_D", cited_docs,
            "the cross-item duplicate should have been deduped, freeing the "
            "3rd top_k slot for the genuinely different DOC_D instead of "
            "wasting it on DOC_B's duplicate of DOC_A's text",
        )

    async def test_query_custom_diversity_floor_suppresses_escalation(self):
        """A caller-supplied diversity_floor of 1 means "1 document is already
        enough" — escalation must not fire even though the default floor (3)
        would have triggered it for the same retrieval result."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        results = [self._make_chunk("DOC1", "ATT1", "Dominant Doc", 0.9, i) for i in range(5)]
        self.mock_vector_store.search = Mock(return_value=results)
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        await self.rag_engine.query(question, library_ids, top_k=5, diversity_floor=1)

        self.mock_vector_store.search.assert_called_once()

    async def test_query_custom_low_diversity_floor_suppresses_retry(self):
        """A caller-supplied low_diversity_available_floor higher than the number
        of available documents means the low-diversity retry guard never applies."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        results = [
            self._make_chunk("DOC1", "ATT1", "First Doc", 0.9),
            self._make_chunk("DOC2", "ATT2", "Second Doc", 0.85),
            self._make_chunk("DOC3", "ATT3", "Third Doc", 0.8),
        ]
        self.mock_vector_store.search = Mock(return_value=results)
        self.mock_llm_service.generate = AsyncMock(return_value="Only the first source matters here [S1].")

        await self.rag_engine.query(question, library_ids, top_k=3, low_diversity_available_floor=10)

        self.mock_llm_service.generate.assert_called_once()

    async def test_retrieval_trace_records_whether_escalation_happened(self):
        from backend.services.trace_collector import TraceCollector

        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        narrow_results = [self._make_chunk("DOC1", "ATT1", "Dominant Doc", 0.9, i) for i in range(5)]
        diverse_results = narrow_results + [self._make_chunk("DOC2", "ATT2", "Second Doc", 0.85)] + [
            self._make_chunk("DOC3", "ATT3", "Third Doc", 0.8)
        ]
        self.mock_vector_store.search = Mock(side_effect=[narrow_results, diverse_results])
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1,S2,S3]")
        self.mock_llm_service.model_name = "test-model"

        collector = TraceCollector(question, library_ids, {})
        await self.rag_engine.query(
            question, library_ids, top_k=5, trace=collector, search_candidate_pool_size=5,
        )
        trace = collector.finalize()

        self.assertTrue(trace.agent_executions[0].retrieval.escalated)

    async def test_query_retries_once_when_citing_only_one_of_several_available_sources(self):
        """Observed live: with 6 genuinely distinct documents retrieved, repeated
        attempts at the same question (temperature 0.7) sometimes cited only 1 of
        them despite the others being equally present in context. Retry once with
        a neutral instruction to consider the other sources."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        results = [
            self._make_chunk("DOC1", "ATT1", "First Doc", 0.9),
            self._make_chunk("DOC2", "ATT2", "Second Doc", 0.85),
            self._make_chunk("DOC3", "ATT3", "Third Doc", 0.8),
        ]
        self.mock_vector_store.search = Mock(return_value=results)
        narrow_answer = "Only the first source matters here [S1]."
        diverse_answer = "The first source [S1] and the third source [S3] both matter."
        self.mock_llm_service.generate = AsyncMock(side_effect=[narrow_answer, diverse_answer])

        result = await self.rag_engine.query(question, library_ids, top_k=3)

        self.assertEqual(self.mock_llm_service.generate.call_count, 2)
        self.assertEqual(result.answer, diverse_answer)

    async def test_query_does_not_retry_when_only_one_document_was_retrieved(self):
        """A single relevant document is a legitimate, expected outcome — citing
        only it must not trigger the low-diversity retry."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        results = [self._make_chunk("DOC1", "ATT1", "Only Doc", 0.9)]
        self.mock_vector_store.search = Mock(return_value=results)
        self.mock_llm_service.generate = AsyncMock(return_value="Only the source matters here [S1].")

        await self.rag_engine.query(question, library_ids, top_k=1)

        self.mock_llm_service.generate.assert_called_once()

    async def test_query_does_not_retry_when_diversity_already_sufficient(self):
        """No wasted retry when the answer already cites more than one source."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        results = [
            self._make_chunk("DOC1", "ATT1", "First Doc", 0.9),
            self._make_chunk("DOC2", "ATT2", "Second Doc", 0.85),
            self._make_chunk("DOC3", "ATT3", "Third Doc", 0.8),
        ]
        self.mock_vector_store.search = Mock(return_value=results)
        self.mock_llm_service.generate = AsyncMock(
            return_value="The first [S1] and second [S2] sources both matter."
        )

        await self.rag_engine.query(question, library_ids, top_k=3)

        self.mock_llm_service.generate.assert_called_once()

    async def test_prompt_forbids_page_range_citations(self):
        """Observed live: a weaker model cited a page range ([S1:305-306]) despite
        the prompt asking for a single integer page. The frontend now tolerates
        this defensively, but the prompt should also explicitly discourage it."""
        question, library_ids = await self._query_with_single_chunk()
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        await self.rag_engine.query(question, library_ids)

        prompt = self.mock_llm_service.generate.call_args.kwargs["prompt"].lower()
        self.assertIn("page range", prompt)

    async def test_prompt_requires_a_citation_on_every_factual_sentence(self):
        """Observed live: weaker models frequently drop citations entirely (2 of
        3 repeated attempts at the same question produced zero inline [SN]
        markers) even though the CRITICAL CITATION RULE explains the *format*
        to use. The rule must also mandate that every factual sentence carry
        one — format-only guidance isn't enough to make citations happen."""
        question, library_ids = await self._query_with_single_chunk()
        self.mock_llm_service.generate = AsyncMock(return_value="Answer")

        await self.rag_engine.query(question, library_ids)

        prompt = self.mock_llm_service.generate.call_args.kwargs["prompt"].lower()
        self.assertIn("every sentence", prompt)
        self.assertIn("do not state it", prompt)

    async def test_prompt_instructs_model_to_answer_in_the_questions_language(self):
        """Observed live: a German question retrieved a mix of German and
        English source passages and the model answered in English — the
        prompt must explicitly instruct it to match the question's
        language regardless of what language the context happens to be
        in."""
        question, library_ids = await self._query_with_single_chunk()
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        await self.rag_engine.query(question, library_ids)

        prompt = self.mock_llm_service.generate.call_args.kwargs["prompt"].lower()
        self.assertIn("same language as the question", prompt)

    async def test_query_accepts_enable_quality_self_review_flag(self):
        """Plumbing check: the flag must be accepted without error and must
        not change behavior when nothing triggers the review (a later task
        adds the detector and retry that actually use it)."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])
        results = [self._make_chunk("DOC1", "ATT1", "Doc", 0.9)]
        self.mock_vector_store.search = Mock(return_value=results)
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        await self.rag_engine.query(
            question, library_ids, top_k=1, enable_quality_self_review=True
        )

        self.mock_llm_service.generate.assert_called_once()

    async def test_quality_review_escalates_and_adopts_better_retry(self):
        """When enable_quality_self_review=True and the first answer's
        citation coverage looks thin (signaled by the model emitting
        CONTEXT_INSUFFICIENT_MARKER), retry once with escalated retrieval
        and adopt the retry if it has fewer quality issues."""
        from backend.services.rag_engine import CONTEXT_INSUFFICIENT_MARKER

        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        narrow_results = [self._make_chunk("DOC1", "ATT1", "First Doc", 0.9)]
        escalated_results = narrow_results + [
            self._make_chunk("DOC2", "ATT2", "Second Doc", 0.85),
            self._make_chunk("DOC3", "ATT3", "Third Doc", 0.8),
        ]
        self.mock_vector_store.search = Mock(side_effect=[narrow_results, escalated_results])

        thin_answer = f"I can only find a partial answer here [S1].\n{CONTEXT_INSUFFICIENT_MARKER}"
        better_answer = "The first [S1] and second [S2] sources both address this."
        self.mock_llm_service.generate = AsyncMock(side_effect=[thin_answer, better_answer])

        result = await self.rag_engine.query(
            question, library_ids, top_k=5, enable_quality_self_review=True,
        )

        self.assertEqual(self.mock_vector_store.search.call_count, 2)
        self.assertEqual(self.mock_llm_service.generate.call_count, 2)
        self.assertEqual(result.answer, better_answer)
        self.assertEqual(len(result.sources), 3)

    async def test_quality_review_keeps_original_when_retry_is_not_better(self):
        """If the escalated retry's answer has the same or more quality
        issues, keep the original answer (minus the marker, which is
        always stripped) rather than discarding a decent first attempt for
        a worse one."""
        from backend.services.rag_engine import CONTEXT_INSUFFICIENT_MARKER

        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        narrow_results = [self._make_chunk("DOC1", "ATT1", "First Doc", 0.9)]
        escalated_results = narrow_results + [self._make_chunk("DOC2", "ATT2", "Second Doc", 0.85)]
        self.mock_vector_store.search = Mock(side_effect=[narrow_results, escalated_results])

        thin_answer = f"I can only find a partial answer here [S1].\n{CONTEXT_INSUFFICIENT_MARKER}"
        still_thin_answer = f"I can only find a partial answer here [S1].\n{CONTEXT_INSUFFICIENT_MARKER}"
        self.mock_llm_service.generate = AsyncMock(side_effect=[thin_answer, still_thin_answer])

        result = await self.rag_engine.query(
            question, library_ids, top_k=5, enable_quality_self_review=True,
        )

        self.assertEqual(self.mock_llm_service.generate.call_count, 2)
        self.assertEqual(result.answer, "I can only find a partial answer here [S1].")
        self.assertEqual(len(result.sources), 1)

    async def test_quality_review_does_not_fire_when_disabled(self):
        """Default False — a thin-looking answer must not trigger a retry
        unless the caller explicitly opts in. The marker is still stripped
        from the final answer regardless (the prompt instruction to emit it
        is unconditional; only the escalation RETRY is gated by the flag)."""
        from backend.services.rag_engine import CONTEXT_INSUFFICIENT_MARKER

        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        results = [self._make_chunk("DOC1", "ATT1", "First Doc", 0.9)]
        self.mock_vector_store.search = Mock(return_value=results)
        thin_answer = f"I can only find a partial answer here [S1].\n{CONTEXT_INSUFFICIENT_MARKER}"
        self.mock_llm_service.generate = AsyncMock(return_value=thin_answer)

        result = await self.rag_engine.query(question, library_ids, top_k=1)

        self.mock_llm_service.generate.assert_called_once()
        self.assertEqual(result.answer, "I can only find a partial answer here [S1].")

    async def test_quality_review_does_not_fire_when_answer_already_good(self):
        """No wasted retry when the first answer already looks fine."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        results = [
            self._make_chunk("DOC1", "ATT1", "First Doc", 0.9),
            self._make_chunk("DOC2", "ATT2", "Second Doc", 0.85),
        ]
        self.mock_vector_store.search = Mock(return_value=results)
        good_answer = "The first [S1] and second [S2] sources both address this."
        self.mock_llm_service.generate = AsyncMock(return_value=good_answer)

        await self.rag_engine.query(
            question, library_ids, top_k=2, enable_quality_self_review=True,
        )

        self.mock_llm_service.generate.assert_called_once()

    async def test_quality_review_skips_post_generation_escalation_when_pre_generation_already_escalated(self):
        """Regression test: an earlier version of this feature suppressed
        the pre-existing pre-generation diversity escalation whenever
        enable_quality_self_review was True, which was wrong (the two
        escalation mechanisms detect different failure modes — structural
        retrieval narrowness vs. answer-quality degradation — and aren't a
        strict superset of each other). The fix keeps pre-generation
        escalation unconditional and instead skips the NEW post-generation
        thin-context escalation if pre-generation escalation already fired
        for this query (via the `not escalated` guard), so only one
        escalation retry is ever spent per query. This test sets up a
        first search that saturates top_k with chunks from a single
        dominant document (triggering pre-generation escalation
        unconditionally) and confirms: (1) pre-generation escalation fires
        and consumes the one available extra search result, (2) the
        post-generation thin-context check does NOT also fire even though
        the answer contains CONTEXT_INSUFFICIENT_MARKER, because
        `escalated` is already True — i.e. vector_store.search is called
        exactly twice (the pre-generation escalation's two searches), not
        three, and llm_service.generate is called exactly once, not
        twice."""
        from backend.services.rag_engine import CONTEXT_INSUFFICIENT_MARKER

        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        # top_k=3, first search saturates it with 3 chunks all from DOC1 —
        # triggers pre-generation escalation (len(search_results)==top_k
        # and unique_doc_count=1 < diversity_floor=3) unconditionally,
        # regardless of enable_quality_self_review.
        narrow_results = [self._make_chunk("DOC1", "ATT1", "Dominant Doc", 0.9, i) for i in range(3)]
        escalated_results = narrow_results + [
            self._make_chunk("DOC2", "ATT2", "Second Doc", 0.85),
            self._make_chunk("DOC3", "ATT3", "Third Doc", 0.8),
        ]
        self.mock_vector_store.search = Mock(side_effect=[narrow_results, escalated_results])

        # Even though this answer contains the thin-context marker, the
        # post-generation escalation must NOT fire a second time, because
        # pre-generation escalation already spent this query's one
        # escalation budget.
        answer_with_marker = f"Answer from the escalated context [S1,S2,S3].\n{CONTEXT_INSUFFICIENT_MARKER}"
        self.mock_llm_service.generate = AsyncMock(return_value=answer_with_marker)

        result = await self.rag_engine.query(
            question, library_ids, top_k=3, enable_quality_self_review=True,
            search_candidate_pool_size=3,
        )

        self.assertEqual(
            self.mock_vector_store.search.call_count, 2,
            "pre-generation escalation should have searched twice; "
            "post-generation escalation must not add a third search",
        )
        self.mock_llm_service.generate.assert_called_once()
        self.assertNotIn(CONTEXT_INSUFFICIENT_MARKER, result.answer)

    async def test_quality_review_records_trace_block(self):
        from backend.services.rag_engine import CONTEXT_INSUFFICIENT_MARKER
        from backend.services.trace_collector import TraceCollector

        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])
        self.mock_llm_service.model_name = "test-model"

        narrow_results = [self._make_chunk("DOC1", "ATT1", "First Doc", 0.9)]
        escalated_results = narrow_results + [self._make_chunk("DOC2", "ATT2", "Second Doc", 0.85)]
        self.mock_vector_store.search = Mock(side_effect=[narrow_results, escalated_results])

        thin_answer = f"I can only find a partial answer here [S1].\n{CONTEXT_INSUFFICIENT_MARKER}"
        better_answer = "The first [S1] and second [S2] sources both address this."
        self.mock_llm_service.generate = AsyncMock(side_effect=[thin_answer, better_answer])

        collector = TraceCollector(question, library_ids, {})
        await self.rag_engine.query(
            question, library_ids, top_k=5, enable_quality_self_review=True, trace=collector,
        )
        trace = collector.finalize()

        review = trace.agent_executions[0].quality_review
        self.assertIsNotNone(review)
        self.assertTrue(review["triggered"])
        self.assertTrue(review["retry_improved"])

    async def test_quality_review_strips_marker_from_answer_but_not_from_trace(self):
        """CONTEXT_INSUFFICIENT_MARKER is a detection signal for the model to
        emit, not something the user should see — it must be stripped from
        QueryResult.answer, but the trace should still show the raw model
        output (including the marker) for debuggability."""
        from backend.services.rag_engine import CONTEXT_INSUFFICIENT_MARKER
        from backend.services.trace_collector import TraceCollector

        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])
        self.mock_llm_service.model_name = "test-model"

        results = [
            self._make_chunk("DOC1", "ATT1", "First Doc", 0.9),
            self._make_chunk("DOC2", "ATT2", "Second Doc", 0.85),
        ]
        self.mock_vector_store.search = Mock(return_value=results)
        raw_answer = f"The first source states X [S1].\n{CONTEXT_INSUFFICIENT_MARKER}"
        self.mock_llm_service.generate = AsyncMock(return_value=raw_answer)

        collector = TraceCollector(question, library_ids, {})
        result = await self.rag_engine.query(question, library_ids, top_k=2, trace=collector)
        trace = collector.finalize()

        self.assertNotIn(CONTEXT_INSUFFICIENT_MARKER, result.answer)
        self.assertEqual(result.answer, "The first source states X [S1].")
        self.assertIn(CONTEXT_INSUFFICIENT_MARKER, trace.llm_calls[-1].response)


class TestThinContextCoverage(unittest.TestCase):
    """_thin_context_coverage detects a different failure mode than the
    existing _low_citation_diversity: the answer DOES cite sources, and
    doesn't necessarily cite only one, but the retrieved pool itself looks
    like it didn't actually address the question (either the model says so,
    or it uses little of a small retrieved pool)."""

    def test_true_when_answer_contains_insufficient_context_marker(self):
        """The hedge regex this replaced was language-dependent and missed
        its own motivating phrasing (the system prompt's "doesn't contain"
        contraction didn't match a "does not contain" regex alternative).
        An explicit sentinel the model is instructed to emit is
        language-independent and exact-match, not pattern-match."""
        from backend.services.rag_engine import _thin_context_coverage, CONTEXT_INSUFFICIENT_MARKER
        answer = f"I can only find a partial answer [S1].\n{CONTEXT_INSUFFICIENT_MARKER}"
        self.assertTrue(_thin_context_coverage(answer, documents_grouped=6))

    def test_false_when_marker_absent_and_utilization_is_adequate(self):
        """No regression: without the marker, behavior falls through to the
        citation-utilization check exactly as before."""
        from backend.services.rag_engine import _thin_context_coverage
        answer = "The first [S1], second [S2], and third [S3] sources all matter."
        self.assertFalse(_thin_context_coverage(answer, documents_grouped=6))

    def test_true_when_citation_utilization_is_low_on_a_small_pool(self):
        from backend.services.rag_engine import _thin_context_coverage
        answer = "Only one source is relevant here [S1]."
        self.assertTrue(_thin_context_coverage(answer, documents_grouped=6))

    def test_false_when_citation_utilization_is_adequate(self):
        from backend.services.rag_engine import _thin_context_coverage
        answer = "The first [S1], second [S2], and third [S3] sources all matter."
        self.assertFalse(_thin_context_coverage(answer, documents_grouped=6))

    def test_false_when_pool_is_large_even_if_few_cited(self):
        """A large retrieved pool with few citations is a citation-behavior
        issue (see _low_citation_diversity), not evidence the pool itself
        was thin — don't double-trigger on it here."""
        from backend.services.rag_engine import _thin_context_coverage
        answer = "Only the first source matters here [S1]."
        self.assertFalse(_thin_context_coverage(answer, documents_grouped=20))

    def test_false_on_a_normal_well_cited_short_answer(self):
        from backend.services.rag_engine import _thin_context_coverage
        answer = "The only relevant source states X [S1]."
        self.assertFalse(_thin_context_coverage(answer, documents_grouped=1))

    def test_true_when_marker_is_missing_its_trailing_hashes(self):
        """Observed live (qwen3-omni-30b-a3b-instruct, eval run
        20261011T111753Z, Q09): the model reproduced the instructed sentinel
        imperfectly, emitting "###CONTEXT_INSUFFICIENT" with no trailing
        "###", so the exact-string marker check missed it and it leaked
        into the user-facing answer verbatim. Detection must tolerate a
        missing (or partial) trailing hash sequence.

        documents_grouped=20 isolates this from the citation-utilization
        fallback (see test_false_when_pool_is_large_even_if_few_cited,
        which asserts False for the same low citation count on a pool this
        large) — only the marker check itself can make this True."""
        from backend.services.rag_engine import _thin_context_coverage
        answer = "I can only find a partial answer [S1].\n###CONTEXT_INSUFFICIENT"
        self.assertTrue(_thin_context_coverage(answer, documents_grouped=20))


class TestStripInsufficientContextMarker(unittest.TestCase):
    """_strip_insufficient_context_marker must remove the sentinel even when
    the model reproduces it imperfectly — see CONTEXT_INSUFFICIENT_MARKER's
    near-miss case documented above."""

    def test_strips_well_formed_marker(self):
        from backend.services.rag_engine import _strip_insufficient_context_marker
        answer = "Partial answer [S1].\n###CONTEXT_INSUFFICIENT###"
        self.assertEqual(_strip_insufficient_context_marker(answer), "Partial answer [S1].")

    def test_strips_marker_missing_trailing_hashes(self):
        """The exact case observed live: no trailing ###."""
        from backend.services.rag_engine import _strip_insufficient_context_marker
        answer = "Partial answer [S1].\n###CONTEXT_INSUFFICIENT"
        self.assertEqual(_strip_insufficient_context_marker(answer), "Partial answer [S1].")

    def test_strips_marker_wrapped_in_a_paragraph_tag(self):
        """Observed live: the marker survived markdown->HTML rendering
        wrapped in its own <p> tag because stripping happens on the raw
        text before rendering — this test documents the raw-text shape,
        not the rendered HTML, since stripping must happen pre-render."""
        from backend.services.rag_engine import _strip_insufficient_context_marker
        answer = "Partial answer [S1].\n\n###CONTEXT_INSUFFICIENT"
        self.assertEqual(_strip_insufficient_context_marker(answer), "Partial answer [S1].")

    def test_does_not_strip_unrelated_hash_text(self):
        """No regression: ordinary markdown headings or hashtags must survive."""
        from backend.services.rag_engine import _strip_insufficient_context_marker
        answer = "### Summary\nThe answer is X [S1]."
        self.assertEqual(_strip_insufficient_context_marker(answer), "### Summary\nThe answer is X [S1].")


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
