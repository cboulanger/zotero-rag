"""Tests for query decomposition with reciprocal rank fusion (#87/#90)."""

import unittest
from unittest.mock import AsyncMock, Mock

from backend.config.settings import Settings
from backend.models.document import ChunkMetadata, DocumentChunk, DocumentMetadata, SearchResult
from backend.services.query_decomposition import fuse_ranked_lists, parse_sub_queries
from backend.services.rag_engine import RAGEngine


def _result(key: str, score: float) -> SearchResult:
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
        ),
        score=score,
    )


class TestParseSubQueries(unittest.TestCase):
    def test_strips_markers_quotes_and_duplicates(self):
        text = '1. "Zotero does not provide user counts"\n- Mendeley API readership data\n\n* zotero does not provide user counts\n'
        self.assertEqual(
            parse_sub_queries(text, 4, "Why?"),
            ["Zotero does not provide user counts", "Mendeley API readership data"],
        )

    def test_drops_question_itself_and_caps_count(self):
        text = "Why Mendeley?\na\nquery one\nquery two\nquery three"
        self.assertEqual(parse_sub_queries(text, 2, "why mendeley?"), ["query one", "query two"])


    def test_drops_degenerate_lines(self):
        text = "same " * 100 + "\nshort valid query"
        self.assertEqual(parse_sub_queries(text, 3, "q"), ["short valid query"])


class TestFuseRankedLists(unittest.TestCase):
    def test_item_ranked_well_in_several_lists_wins(self):
        a, b, c = _result("A", 0.9), _result("B", 0.8), _result("C", 0.7)
        fused = fuse_ranked_lists([[a, b, c], [_result("C", 0.95), _result("B", 0.6)]])
        self.assertEqual([r.chunk.metadata.chunk_id for r in fused], ["C", "B", "A"])
        self.assertEqual(fused[0].score, 1.0)
        self.assertEqual([r.score for r in fused], sorted((r.score for r in fused), reverse=True))

    def test_sub_query_only_hit_is_kept(self):
        fused = fuse_ranked_lists([[_result("A", 0.9)], [_result("Z", 0.5)]])
        self.assertEqual({r.chunk.metadata.chunk_id for r in fused}, {"A", "Z"})

    def test_empty(self):
        self.assertEqual(fuse_ranked_lists([[], []]), [])


class TestEngineQueryDecomposition(unittest.IsolatedAsyncioTestCase):
    def _engine(self, max_queries, decomposition_text="far passage query"):
        settings = Mock(spec=Settings)
        settings.retrieval_lexical_weight = 0.0
        settings.retrieval_embedding_mmr_lambda = 0.0
        settings.retrieval_score_proximity_epsilon = 0.0
        settings.retrieval_near_dup_threshold = 0.0
        settings.retrieval_query_decomposition = max_queries
        preset = Mock()
        preset.llm.max_answer_tokens = 100
        settings.get_hardware_preset.return_value = preset
        embedding = Mock()
        embedding.embed_text = AsyncMock(side_effect=lambda text: [1.0] if text == "question" else [2.0])
        llm = Mock()
        llm.generate = AsyncMock(
            side_effect=lambda prompt, **kw: decomposition_text if "search queries" in prompt else "Answer [S1]"
        )
        store = Mock()
        main = [_result("A", 0.9), _result("B", 0.8), _result("C", 0.7)]
        sub = [_result("FAR", 0.6)]
        store.search = Mock(side_effect=lambda **kw: main if kw["query_vector"] == [1.0] else sub)
        return store, llm, RAGEngine(embedding_service=embedding, llm_service=llm, vector_store=store, settings=settings)

    async def test_disabled_makes_no_extra_calls(self):
        store, llm, engine = self._engine(0)
        result = await engine.query("question", ["1"], top_k=2, search_candidate_pool_size=3, diversity_floor=1)
        self.assertEqual(store.search.call_count, 1)
        self.assertEqual(llm.generate.call_count, 1)
        self.assertEqual([s.item_id for s in result.sources], ["A", "B"])

    async def test_enabled_brings_sub_query_hit_into_window(self):
        store, llm, engine = self._engine(2)
        result = await engine.query("question", ["1"], top_k=2, search_candidate_pool_size=3, diversity_floor=1)
        self.assertEqual(store.search.call_count, 2)
        self.assertIn("FAR", [s.item_id for s in result.sources])

    async def test_empty_decomposition_falls_back_to_question(self):
        store, llm, engine = self._engine(2, decomposition_text="")
        result = await engine.query("question", ["1"], top_k=2, search_candidate_pool_size=3, diversity_floor=1)
        self.assertEqual(store.search.call_count, 1)
        self.assertEqual([s.item_id for s in result.sources], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
