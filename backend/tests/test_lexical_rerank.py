"""Tests for BM25 fusion over the vector-search candidate pool (#89, #90)."""

import unittest
from unittest.mock import AsyncMock, Mock

from backend.config.settings import Settings
from backend.models.document import ChunkMetadata, DocumentChunk, DocumentMetadata, SearchResult
from backend.services.lexical_rerank import bm25_scores, fuse_lexical, tokenize
from backend.services.rag_engine import RAGEngine


def _result(key: str, text: str, score: float) -> SearchResult:
    return SearchResult(
        chunk=DocumentChunk(
            text=text,
            metadata=ChunkMetadata(
                chunk_id=key,
                document_metadata=DocumentMetadata(
                    library_id="1", item_key=key, attachment_key=f"A{key}", title=key,
                    authors=["X, Y."], year=2020, item_type="journalArticle",
                ),
                page_number=1, text_preview=key, chunk_index=0, content_hash=f"h{key}",
            ),
        ),
        score=score,
    )


class TestTokenize(unittest.TestCase):
    def test_keeps_decimal_numbers_whole(self):
        self.assertEqual(tokenize("EndNote (n=41, 52.6%)"), ["endnote", "n", "41", "52.6"])


class TestBm25(unittest.TestCase):
    def test_term_match_outscores_non_match(self):
        s = bm25_scores("EndNote RefWorks", ["EndNote (n=41), RefWorks (n=6)", "unrelated words here"])
        self.assertGreater(s[0], s[1])
        self.assertEqual(s[1], 0.0)

    def test_empty_inputs(self):
        self.assertEqual(bm25_scores("", ["a"]), [0.0])
        self.assertEqual(bm25_scores("a", []), [])


class TestFuseLexical(unittest.TestCase):
    def setUp(self):
        self.generic = _result("GEN", "reference management software is widely used by researchers", 0.90)
        self.fact = _result("FACT", "EndNote (n=41), Reference Manager (n=14), RefWorks (n=6)", 0.85)
        self.query = "which programs EndNote Reference Manager RefWorks were used"

    def test_zero_weight_is_noop(self):
        pool = [self.generic, self.fact]
        self.assertIs(fuse_lexical(self.query, pool, 0.0), pool)

    def test_lexical_match_overtakes_higher_vector_score(self):
        out = fuse_lexical(self.query, [self.generic, self.fact], 0.6)
        self.assertEqual([r.chunk.metadata.document_metadata.item_key for r in out], ["FACT", "GEN"])
        self.assertTrue(all(0.0 <= r.score <= 1.0 for r in out))

    def test_does_not_mutate_inputs(self):
        fuse_lexical(self.query, [self.generic, self.fact], 0.6)
        self.assertEqual((self.generic.score, self.fact.score), (0.90, 0.85))


class TestEngineLexicalFusion(unittest.IsolatedAsyncioTestCase):
    def _engine(self, weight):
        settings = Mock(spec=Settings)
        settings.retrieval_lexical_weight = weight
        preset = Mock()
        preset.llm.max_answer_tokens = 100
        settings.get_hardware_preset.return_value = preset
        embedding = Mock()
        embedding.embed_text = AsyncMock(return_value=[0.1])
        llm = Mock()
        llm.generate = AsyncMock(return_value="Answer [S1]")
        store = Mock()
        pool = [
            _result("GEN1", "reference management software is widely used", 0.95),
            _result("GEN2", "researchers like reference management tools", 0.92),
            _result("FACT", "EndNote (n=41), Reference Manager (n=14), RefWorks (n=6)", 0.80),
        ]
        store.search = Mock(side_effect=lambda **kw: pool[: kw["limit"]])
        return RAGEngine(embedding_service=embedding, llm_service=llm, vector_store=store, settings=settings)

    async def _top1(self, weight):
        engine = self._engine(weight)
        result = await engine.query(
            "EndNote Reference Manager RefWorks programs", ["1"], top_k=1, search_candidate_pool_size=3
        )
        return result.sources[0].item_id

    async def test_disabled_keeps_vector_order(self):
        self.assertEqual(await self._top1(0.0), "GEN1")

    async def test_enabled_surfaces_lexical_fact_chunk_into_top_k(self):
        self.assertEqual(await self._top1(0.6), "FACT")


if __name__ == "__main__":
    unittest.main()
