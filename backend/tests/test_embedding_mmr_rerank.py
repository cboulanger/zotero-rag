"""Tests for global embedding-vector MMR reranking over the vector-search
candidate pool (#90 prototype)."""

import unittest
from unittest.mock import AsyncMock, Mock

from backend.config.settings import Settings
from backend.models.document import ChunkMetadata, DocumentChunk, DocumentMetadata, SearchResult
from backend.services.embedding_mmr_rerank import cosine_similarity, rerank_embedding_mmr
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


class TestCosineSimilarity(unittest.TestCase):
    def test_identical_vectors(self):
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [1.0, 0.0]), 1.0)

    def test_orthogonal_vectors(self):
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)

    def test_zero_vector_is_safe(self):
        self.assertEqual(cosine_similarity([0.0, 0.0], [1.0, 0.0]), 0.0)


class TestRerankEmbeddingMMR(unittest.TestCase):
    def setUp(self):
        # DUP1/DUP2 are near-identical vectors (both score highly on relevance);
        # DISTINCT is lower-scoring by raw cosine but points in a very different
        # direction — a plain top-k-by-score pool would pick DUP1 then DUP2,
        # crowding out DISTINCT even though DISTINCT is the one that actually
        # broadens coverage (the #90 failure mode: several similar-to-each-other
        # chunks from different documents outscoring a genuinely distinct one).
        self.dup1 = _result("DUP1", 0.95, [1.0, 0.0])
        self.dup2 = _result("DUP2", 0.93, [0.99, 0.01])
        self.distinct = _result("DISTINCT", 0.80, [0.0, 1.0])

    def test_zero_lambda_is_noop(self):
        pool = [self.dup1, self.dup2, self.distinct]
        self.assertIs(rerank_embedding_mmr(pool, 0.0), pool)

    def test_single_result_is_noop(self):
        pool = [self.dup1]
        self.assertIs(rerank_embedding_mmr(pool, 0.5), pool)

    def test_missing_embedding_is_a_safe_noop(self):
        """Enabling the setting without updating the search call to fetch
        vectors must not silently reorder by garbage — it's a no-op."""
        pool = [self.dup1, _result("NOVEC", 0.90, None)]
        self.assertIs(rerank_embedding_mmr(pool, 0.5), pool)

    def test_distinct_chunk_is_promoted_over_a_near_duplicate(self):
        out = rerank_embedding_mmr([self.dup1, self.dup2, self.distinct], lambda_param=0.5)
        keys = [r.chunk.metadata.document_metadata.item_key for r in out]
        self.assertEqual(keys[0], "DUP1", "highest raw relevance is always selected first")
        self.assertEqual(
            keys[1], "DISTINCT",
            "DUP2 is nearly identical to the already-selected DUP1 (cosine ~1), "
            "so its MMR value is penalized enough that the lower-scoring but "
            "genuinely distinct chunk is preferred next",
        )

    def test_lambda_one_approximates_plain_relevance_order(self):
        """lambda=1 means the similarity penalty is zeroed out — order should
        match plain descending score."""
        out = rerank_embedding_mmr([self.distinct, self.dup1, self.dup2], lambda_param=1.0)
        keys = [r.chunk.metadata.document_metadata.item_key for r in out]
        self.assertEqual(keys, ["DUP1", "DUP2", "DISTINCT"])

    def test_output_scores_are_descending(self):
        out = rerank_embedding_mmr([self.dup1, self.dup2, self.distinct], lambda_param=0.5)
        scores = [r.score for r in out]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_does_not_mutate_inputs(self):
        rerank_embedding_mmr([self.dup1, self.dup2, self.distinct], lambda_param=0.5)
        self.assertEqual((self.dup1.score, self.dup2.score, self.distinct.score), (0.95, 0.93, 0.80))


class TestEngineEmbeddingMMR(unittest.IsolatedAsyncioTestCase):
    def _engine(self, lambda_param):
        settings = Mock(spec=Settings)
        settings.retrieval_lexical_weight = 0.0
        settings.retrieval_embedding_mmr_lambda = lambda_param
        preset = Mock()
        preset.llm.max_answer_tokens = 100
        settings.get_hardware_preset.return_value = preset
        embedding = Mock()
        embedding.embed_text = AsyncMock(return_value=[0.1])
        llm = Mock()
        llm.generate = AsyncMock(return_value="Answer [S1]")
        store = Mock()
        pool = [
            _result("DUP1", 0.95, [1.0, 0.0]),
            _result("DUP2", 0.93, [0.99, 0.01]),
            _result("DISTINCT", 0.80, [0.0, 1.0]),
        ]
        store.search = Mock(side_effect=lambda **kw: pool[: kw["limit"]])
        return store, RAGEngine(
            embedding_service=embedding, llm_service=llm, vector_store=store, settings=settings,
        )

    async def _top2(self, lambda_param):
        store, engine = self._engine(lambda_param)
        result = await engine.query(
            "question", ["1"], top_k=2, search_candidate_pool_size=3,
        )
        return store, [s.item_id for s in result.sources]

    async def test_disabled_keeps_vector_order_and_does_not_fetch_vectors(self):
        store, keys = await self._top2(0.0)
        self.assertEqual(keys, ["DUP1", "DUP2"])
        self.assertFalse(store.search.call_args.kwargs["with_vectors"])

    async def test_enabled_promotes_the_distinct_chunk_into_top_k_and_fetches_vectors(self):
        store, keys = await self._top2(0.5)
        self.assertEqual(keys, ["DUP1", "DISTINCT"])
        self.assertTrue(store.search.call_args.kwargs["with_vectors"])


if __name__ == "__main__":
    unittest.main()
