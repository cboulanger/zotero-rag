"""The RunPod re-ranking side: the user's own endpoint, called through /runsync."""

import unittest
from unittest.mock import AsyncMock, Mock, patch

from backend.config.presets import HardwarePreset
from backend.models.document import ChunkMetadata, DocumentChunk, DocumentMetadata, SearchResult
from backend.providers import get_providers
from backend.services.rerank import (
    RemoteReranker,
    RerankerUnavailable,
    RerankRequest,
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


class RunPodRerankTest(unittest.IsolatedAsyncioTestCase):
    """The RunPod rerank side: the user's own endpoint, called through /runsync."""

    def _provider(self):
        preset = _preset({
            "model_type": "remote", "model_name": "BAAI/bge-reranker-v2-m3",
            "model_kwargs": {"api_key_env": "RUNPOD_API_KEY"}, "provider": {"id": "runpod", "scope": "user"},
        })
        return get_providers(preset)["rerank"]

    def test_side_names_and_worker_env(self):
        p = self._provider()
        self.assertEqual(p.resource_name, "zotero-rag-rerank")
        self.assertEqual(p.worker_env(), {"MODEL_NAME": "BAAI/bge-reranker-v2-m3", "MAX_MODEL_LEN": "1024"})
        url, body = p._warmup_request("https://api.runpod.ai/v2/abc123/openai/v1")
        self.assertEqual(url, "https://api.runpod.ai/v2/abc123/runsync")
        self.assertEqual(body["input"]["route"], "/v1/rerank")

    def test_credentials_from_the_callers_key(self):
        p = self._provider()
        self.assertIsNone(p.rerank_credentials(RerankRequest()))
        with patch("backend.services.endpoint_cache.endpoint_cache.resolve",
                   return_value="https://api.runpod.ai/v2/abc123/openai/v1") as resolve:
            creds = p.rerank_credentials(RerankRequest(client_keys={"RUNPOD_API_KEY": "rpa_x"}))
        self.assertEqual(creds.base_url, "https://api.runpod.ai/v2/abc123/openai/v1")
        self.assertEqual(resolve.call_args.args[2], "rpa_x")

    def test_unwrap(self):
        p = self._provider()
        inner = {"results": [{"index": 0, "relevance_score": 0.3}]}
        self.assertEqual(p.rerank_unwrap({"status": "COMPLETED", "output": [inner]}), inner)
        with self.assertRaises(RerankerUnavailable):
            p.rerank_unwrap({"status": "IN_QUEUE", "id": "j"})
        with self.assertRaises(ValueError):
            p.rerank_unwrap({"status": "COMPLETED", "output": [{"error": {"message": "boom"}}]})

    async def test_remote_reranker_goes_through_runsync(self):
        p = self._provider()
        reranker = RemoteReranker(p, "BAAI/bge-reranker-v2-m3", 5.0)
        token = set_rerank_request(RerankRequest(client_keys={"RUNPOD_API_KEY": "rpa_x"}))
        self.addCleanup(reset_rerank_request, token)
        response = Mock(status_code=200)
        response.json.return_value = {"status": "COMPLETED", "output": [{"results": [{"index": 0, "relevance_score": 0.8}]}]}
        captured = {}

        class Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json, headers):
                captured.update(url=url, json=json, auth=headers.get("Authorization"))
                return response

        with patch("backend.services.endpoint_cache.endpoint_cache.resolve",
                   return_value="https://api.runpod.ai/v2/abc123/openai/v1"), \
             patch("httpx.AsyncClient", Client):
            self.assertEqual(await reranker.score("q", ["t"]), [0.8])
        self.assertEqual(captured["url"], "https://api.runpod.ai/v2/abc123/runsync")
        self.assertEqual(captured["json"]["input"]["body"]["documents"], ["t"])
        self.assertEqual(captured["auth"], "Bearer rpa_x")

    def _paused_response(self):
        import httpx

        request = httpx.Request("POST", "https://api.runpod.ai/v2/abc123/runsync")
        return httpx.Response(409, text='{"error":"ENDPOINT_PAUSED"}', request=request)

    async def test_paused_endpoint_is_unavailable_not_an_error(self):
        """A paused endpoint answers 409 ENDPOINT_PAUSED: that is "not running", not a failure."""
        reranker = RemoteReranker(self._provider(), "BAAI/bge-reranker-v2-m3", 5.0)
        token = set_rerank_request(RerankRequest(client_keys={"RUNPOD_API_KEY": "rpa_x"}))
        self.addCleanup(reset_rerank_request, token)
        with patch("backend.services.endpoint_cache.endpoint_cache.resolve",
                   return_value="https://api.runpod.ai/v2/abc123/openai/v1"), \
             patch.object(RemoteReranker, "_post", new=AsyncMock(return_value=self._paused_response())):
            with self.assertRaisesRegex(RerankerUnavailable, "paused"):
                await reranker.score("q", ["t"])

    async def test_paused_endpoint_keeps_vector_order_as_not_ready(self):
        preset = _preset({
            "model_type": "remote", "model_name": "BAAI/bge-reranker-v2-m3",
            "model_kwargs": {"api_key_env": "RUNPOD_API_KEY"}, "provider": {"id": "runpod", "scope": "user"},
        })
        results = [_result("A", "alpha", 0.9), _result("B", "beta", 0.8)]
        token = set_rerank_request(RerankRequest(client_keys={"RUNPOD_API_KEY": "rpa_x"}))
        self.addCleanup(reset_rerank_request, token)
        with patch("backend.services.endpoint_cache.endpoint_cache.resolve",
                   return_value="https://api.runpod.ai/v2/abc123/openai/v1"), \
             patch.object(RemoteReranker, "_post", new=AsyncMock(return_value=self._paused_response())):
            out, outcome = await rerank_pool("q", results, preset)
        self.assertEqual(outcome.status, "not_ready")
        self.assertIn("paused", outcome.detail)
        self.assertEqual([r.chunk.metadata.document_metadata.item_key for r in out], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
