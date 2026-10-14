"""Tests for embedding-based near-duplicate removal over the candidate pool (#87/#90)."""

import unittest
from unittest.mock import AsyncMock, Mock

from backend.config.settings import Settings
from backend.models.document import ChunkMetadata, DocumentChunk, DocumentMetadata, SearchResult
from backend.services.near_dup_filter import drop_near_duplicates
from backend.services.rag_engine import RAGEngine


def _result(key: str, score: float, embedding: list[float] | None) -> SearchResult:
    return SearchResult(
        chunk=DocumentChunk(
            text=f"text for {key}",
            metadata=ChunkMetadata(
                chunk_id=key,
                document_metadata=DocumentMetadata(
                    library_id="1", item_key=key, attachment_key=f"A{key}", title=key,
                    authors=["X, Y."], year=2020, item_type="journalArticle",
                ),
                page_number=1, text_preview=key, chunk_index=0, content_hash=f"h{key}",
            ),
            embedding=embedding,
        ),
        score=score,
    )


class TestDropNearDuplicates(unittest.TestCase):
    def setUp(self):
        self.a = _result("A", 0.95, [1.0, 0.0])
        self.a_copy = _result("ACOPY", 0.94, [0.99, 0.02])  # cosine ~0.9998 with A
        self.b = _result("B", 0.90, [0.6, 0.8])  # cosine 0.6 with A
        self.c = _result("C", 0.85, [0.0, 1.0])

    def test_drops_near_copy_of_higher_scoring_candidate(self):
        out = drop_near_duplicates([self.b, self.a_copy, self.c, self.a], threshold=0.95)
        self.assertEqual([r.chunk.metadata.chunk_id for r in out], ["A", "B", "C"])

    def test_keeps_scores_unchanged(self):
        out = drop_near_duplicates([self.a, self.a_copy, self.b], threshold=0.95)
        self.assertEqual([r.score for r in out], [0.95, 0.90])

    def test_threshold_above_similarity_keeps_everything(self):
        out = drop_near_duplicates([self.a, self.a_copy, self.b], threshold=0.99999)
        self.assertEqual(len(out), 3)

    def test_unnormalized_vectors_compare_by_cosine(self):
        big = _result("BIG", 0.80, [10.0, 0.1])
        out = drop_near_duplicates([self.a, big], threshold=0.99)
        self.assertEqual([r.chunk.metadata.chunk_id for r in out], ["A"])

    def test_disabled_or_missing_vectors_is_noop(self):
        pool = [self.a, self.a_copy]
        self.assertIs(drop_near_duplicates(pool, threshold=0.0), pool)
        no_vec = [self.a, _result("N", 0.5, None)]
        self.assertIs(drop_near_duplicates(no_vec, threshold=0.9), no_vec)


class TestEngineNearDupFilter(unittest.IsolatedAsyncioTestCase):
    def _engine(self, threshold):
        settings = Mock(spec=Settings)
        settings.retrieval_lexical_weight = 0.0
        settings.retrieval_embedding_mmr_lambda = 0.0
        settings.retrieval_score_proximity_epsilon = 0.0
        settings.retrieval_near_dup_threshold = threshold
        preset = Mock()
        preset.llm.max_answer_tokens = 100
        settings.get_hardware_preset.return_value = preset
        embedding = Mock()
        embedding.embed_text = AsyncMock(return_value=[0.1])
        llm = Mock()
        llm.generate = AsyncMock(return_value="Answer [S1]")
        store = Mock()
        pool = [
            _result("A", 0.95, [1.0, 0.0]),
            _result("ACOPY", 0.94, [0.99, 0.02]),
            _result("C", 0.80, [0.0, 1.0]),
        ]
        store.search = Mock(side_effect=lambda **kw: pool[: kw["limit"]])
        return store, RAGEngine(embedding_service=embedding, llm_service=llm, vector_store=store, settings=settings)

    async def _top2(self, threshold):
        store, engine = self._engine(threshold)
        result = await engine.query("question", ["1"], top_k=2, search_candidate_pool_size=3)
        return store, [s.item_id for s in result.sources]

    async def test_disabled_keeps_near_copy_and_does_not_fetch_vectors(self):
        store, keys = await self._top2(0.0)
        self.assertEqual(keys, ["A", "ACOPY"])
        self.assertFalse(store.search.call_args.kwargs["with_vectors"])

    async def test_enabled_frees_slot_for_distinct_candidate(self):
        store, keys = await self._top2(0.95)
        self.assertEqual(keys, ["A", "C"])
        self.assertTrue(store.search.call_args.kwargs["with_vectors"])


if __name__ == "__main__":
    unittest.main()
