"""The optional re-ranking step (backend/services/rerank.py) and its hooks in the engine and API."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from backend.config.presets import HardwarePreset
from backend.config.settings import Settings
from backend.models.document import ChunkMetadata, DocumentChunk, DocumentMetadata, SearchResult
from backend.providers import get_providers
from backend.services.rag_engine import RAGEngine, _merge_rerank_outcomes
from backend.services.rerank import (
    RemoteReranker,
    Reranker,
    RerankerUnavailable,
    RerankRequest,
    current_rerank_request,
    parse_rerank_response,
    rerank_client_fields,
    rerank_pool,
    reset_rerank_request,
    set_rerank_request,
)


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


def _preset(rerank=None) -> HardwarePreset:
    data = {
        "name": "t", "description": "t", "version": 2,
        "embedding": {"model_type": "remote", "model_name": "m", "model_kwargs": {"api_key_env": "K"}},
        "llm": {"model_type": "remote", "model_names": ["m"], "model_kwargs": {"api_key_env": "K"}},
        "rag": {}, "memory_budget_gb": 1.0,
    }
    if rerank is not None:
        data["rerank"] = rerank
    return HardwarePreset.model_validate(data)


class FakeScorer(Reranker):
    """Scores a passage by the position of its text in ``order`` (first = best)."""

    model_name = "fake"
    provider_id = None

    def __init__(self, order=None, delay=0.0, error=None):
        self.order = order or []
        self.delay = delay
        self.error = error
        self.calls: list[list[str]] = []

    async def score(self, question, texts):
        self.calls.append(list(texts))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        n = len(self.order) or 1
        return [1.0 - (self.order.index(t) / n) if t in self.order else 0.0 for t in texts]


def _keys(results):
    return [r.chunk.metadata.chunk_id for r in results]


class RerankPoolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pool = [_result("A", "a", 0.9), _result("B", "b", 0.8), _result("C", "c", 0.7)]

    async def test_no_section_is_off(self):
        out, outcome = await rerank_pool("q", self.pool, _preset())
        self.assertIs(out, self.pool)
        self.assertEqual(outcome.status, "off")

    async def test_disabled_by_request(self):
        token = set_rerank_request(RerankRequest(enabled=False))
        self.addCleanup(reset_rerank_request, token)
        scorer = FakeScorer()
        out, outcome = await rerank_pool("q", self.pool, _preset({"model_name": "x"}), scorer)
        self.assertIs(out, self.pool)
        self.assertEqual(outcome.status, "disabled")
        self.assertEqual(scorer.calls, [])

    async def test_reorders_and_keeps_vector_scores(self):
        out, outcome = await rerank_pool("q", self.pool, _preset({"model_name": "x"}), FakeScorer(["c", "a", "b"]))
        self.assertEqual(_keys(out), ["C", "A", "B"])
        self.assertEqual(out[0].vector_score, 0.7)
        self.assertEqual(out[0].pre_rerank_rank, 3)
        self.assertEqual((outcome.status, outcome.scored, outcome.model), ("ran", 3, "x"))
        self.assertEqual(self.pool[0].score, 0.9)  # inputs are not mutated

    async def test_tail_beyond_pool_size_stays_below_rescored_head(self):
        preset = _preset({"model_name": "x", "pool_size": 2})
        out, outcome = await rerank_pool("q", self.pool, preset, FakeScorer(["b", "a"]))
        self.assertEqual(_keys(out), ["B", "A", "C"])
        self.assertLess(out[2].score, out[1].score)
        self.assertEqual(out[2].pre_rerank_rank, 3)
        self.assertEqual(outcome.scored, 2)

    async def test_cache_scores_only_new_candidates(self):
        preset = _preset({"model_name": "x"})
        scorer = FakeScorer(["d", "c", "b", "a"])
        cache: dict = {}
        await rerank_pool("q", self.pool, preset, scorer, cache=cache)
        bigger = self.pool + [_result("D", "d", 0.6)]
        out, outcome = await rerank_pool("q", bigger, preset, scorer, cache=cache)
        self.assertEqual(scorer.calls[1], ["d"])
        self.assertEqual(outcome.scored, 1)
        self.assertEqual(_keys(out)[0], "D")

    async def test_timeout_keeps_vector_order(self):
        preset = _preset({"model_name": "x", "timeout_seconds": 0.01})
        out, outcome = await rerank_pool("q", self.pool, preset, FakeScorer(["c"], delay=0.5))
        self.assertIs(out, self.pool)
        self.assertEqual(outcome.status, "timeout")

    async def test_error_and_unavailable_keep_vector_order(self):
        preset = _preset({"model_name": "x"})
        out, outcome = await rerank_pool("q", self.pool, preset, FakeScorer(error=RuntimeError("boom")))
        self.assertIs(out, self.pool)
        self.assertEqual(outcome.status, "error")
        self.assertIn("boom", outcome.detail)
        out, outcome = await rerank_pool("q", self.pool, preset, FakeScorer(error=RerankerUnavailable("not running")))
        self.assertEqual(outcome.status, "not_ready")

    async def test_empty_pool(self):
        out, outcome = await rerank_pool("q", [], _preset({"model_name": "x"}), FakeScorer())
        self.assertEqual((out, outcome.status, outcome.scored), ([], "ran", 0))


class ParseResponseTest(unittest.TestCase):
    def test_results_format_in_any_order(self):
        data = {"results": [{"index": 1, "relevance_score": 0.2}, {"index": 0, "relevance_score": 0.9}]}
        self.assertEqual(parse_rerank_response(data, 2), [0.9, 0.2])

    def test_tei_list_with_logits(self):
        scores = parse_rerank_response([{"index": 0, "score": 3.0}, {"index": 1, "score": -3.0}], 2)
        self.assertGreater(scores[0], 0.9)
        self.assertLess(scores[1], 0.1)

    def test_missing_scores_rejected(self):
        with self.assertRaises(ValueError):
            parse_rerank_response({"results": [{"index": 0, "relevance_score": 0.5}]}, 2)


REMOTE = {
    "model_type": "remote", "model_name": "BAAI/bge-reranker-v2-m3",
    "model_kwargs": {"api_key_env": "RERANK_KEY", "base_url": "https://rerank.example/v1"},
}


class RemoteRerankerTest(unittest.IsolatedAsyncioTestCase):
    def _reranker(self):
        preset = _preset(REMOTE)
        return RemoteReranker(get_providers(preset)["rerank"], "BAAI/bge-reranker-v2-m3", 5.0)

    async def test_generic_provider_uses_callers_key_and_fixed_url(self):
        reranker = self._reranker()
        token = set_rerank_request(RerankRequest(client_keys={"RERANK_KEY": "secret"}))
        self.addCleanup(reset_rerank_request, token)
        response = Mock(status_code=200)
        response.json.return_value = {"results": [{"index": 0, "relevance_score": 0.4}]}
        with patch.object(RemoteReranker, "_post", new=AsyncMock(return_value=response)) as post:
            self.assertEqual(await reranker.score("q", ["t"]), [0.4])
        creds = post.call_args.args[0]
        self.assertEqual((creds.base_url, creds.api_key), ("https://rerank.example/v1", "secret"))

    async def test_without_key_is_unavailable(self):
        with self.assertRaises(RerankerUnavailable):
            await self._reranker().score("q", ["t"])

    async def test_retries_once_on_503(self):
        reranker = self._reranker()
        token = set_rerank_request(RerankRequest(client_keys={"RERANK_KEY": "secret"}))
        self.addCleanup(reset_rerank_request, token)
        bad, good = Mock(status_code=503), Mock(status_code=200)
        good.json.return_value = {"results": [{"index": 0, "relevance_score": 0.7}]}
        with patch.object(RemoteReranker, "_post", new=AsyncMock(side_effect=[bad, good])) as post:
            self.assertEqual(await reranker.score("q", ["t"]), [0.7])
        self.assertEqual(post.call_count, 2)

    def test_client_fields(self):
        fields = rerank_client_fields(_preset(REMOTE).rerank)
        self.assertEqual([(f["key_name"], f["kind"], f["required_for"]) for f in fields],
                         [("RERANK_KEY", "api_key", ["reranking"])])
        self.assertEqual(rerank_client_fields(_preset({"model_name": "x"}).rerank), [])
        self.assertEqual(rerank_client_fields(None), [])


class EngineRerankTest(unittest.IsolatedAsyncioTestCase):
    def _engine(self, preset):
        settings = Mock(spec=Settings)
        settings.retrieval_lexical_weight = 0.0
        settings.retrieval_embedding_mmr_lambda = 0.0
        settings.retrieval_near_dup_threshold = 0.0
        settings.retrieval_query_decomposition = 0
        settings.retrieval_score_proximity_epsilon = 0.0
        settings.get_hardware_preset.return_value = preset
        embedding = Mock()
        embedding.embed_query = AsyncMock(return_value=[0.1])
        llm = Mock()
        llm.generate = AsyncMock(return_value="Answer [S1]")
        store = Mock()
        pool = [_result("GEN", "generic text", 0.95), _result("FACT", "the fact", 0.80)]
        store.search = Mock(side_effect=lambda **kw: pool[: kw["limit"]])
        return RAGEngine(embedding_service=embedding, llm_service=llm, vector_store=store, settings=settings)

    async def test_reranked_chunk_becomes_top_source(self):
        preset = _preset({"model_name": "x"})
        with patch("backend.services.rerank.build_reranker", return_value=FakeScorer(["the fact", "generic text"])):
            result = await self._engine(preset).query("q", ["1"], top_k=1, search_candidate_pool_size=2)
        self.assertEqual(result.sources[0].item_id, "FACT")

    async def test_without_section_keeps_vector_order(self):
        result = await self._engine(_preset()).query("q", ["1"], top_k=1, search_candidate_pool_size=2)
        self.assertEqual(result.sources[0].item_id, "GEN")

    def test_merge_outcomes(self):
        from backend.services.rerank import RerankOutcome

        self.assertIsNone(_merge_rerank_outcomes([]))
        merged = _merge_rerank_outcomes([
            RerankOutcome(status="ran", scored=3, duration_ms=10),
            RerankOutcome(status="ran", scored=1, duration_ms=5),
        ])
        self.assertEqual((merged["passes"], merged["scored"], merged["duration_ms"]), (2, 4, 15))


class RequestContextTest(unittest.TestCase):
    def test_default_is_enabled(self):
        self.assertTrue(current_rerank_request().enabled)


class RerankApiTest(unittest.TestCase):
    """Required keys, client keys and provider descriptors include the rerank side."""

    def setUp(self):
        from fastapi.testclient import TestClient

        from backend.config.presets import ensure_default_presets
        from backend.config.settings import get_settings, reset_settings
        from backend.main import app

        reset_settings()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(reset_settings)
        self.settings = get_settings()
        self.settings.data_path = Path(self.tmp.name)
        ensure_default_presets(self.settings.data_path)
        data = json.loads((self.settings.data_path / "presets" / "remote-kisski.json").read_text())
        data.pop("name", None)
        data["rerank"] = REMOTE
        (self.settings.data_path / "presets" / "with-rerank.json").write_text(json.dumps(data))
        self.settings.model_preset = "with-rerank"
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_required_keys_list_the_rerank_key(self):
        r = self.client.get("/api/required-keys")
        self.assertEqual(r.status_code, 200, r.text)
        entry = next(k for k in r.json()["keys"] if k["key_name"] == "RERANK_KEY")
        self.assertEqual((entry["sides"], entry["required_for"]), (["rerank"], ["reranking"]))

    def test_providers_describe_the_rerank_side(self):
        r = self.client.get("/api/config/providers")
        self.assertEqual(r.status_code, 200, r.text)
        side = r.json()["sides"]["rerank"]
        self.assertEqual((side["model_type"], side["model_name"], side["pool_size"]),
                         ("remote", "BAAI/bge-reranker-v2-m3", 100))
        self.assertEqual(side["provider"]["id"], "generic")

    def test_client_keys_include_the_rerank_header(self):
        from backend.dependencies import get_client_api_keys
        from backend.services.embeddings import env_var_to_header

        request = Mock()
        request.headers = {env_var_to_header("RERANK_KEY"): "secret"}
        self.assertEqual(get_client_api_keys(request).get("RERANK_KEY"), "secret")

    def test_query_request_defaults_to_reranking_on(self):
        from backend.api.query import QueryRequest

        self.assertTrue(QueryRequest(question="q", library_ids=["1"]).enable_reranking)


if __name__ == "__main__":
    unittest.main()
