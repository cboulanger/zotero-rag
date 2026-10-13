"""A paused endpoint: services fail fast without calling it, cron skips only the paused owner's
libraries, one user's pause never affects another's, and none of it counts toward the #70 quarantine."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.config.presets import EmbeddingConfig
from backend.services.cron_indexer import CronIndexer
from backend.services.embeddings import EmbeddingEndpointUnavailableError, RemoteEmbeddingService
from backend.services.endpoint_cache import PAUSED_TTL_SECONDS, PausedCache, paused_cache
from backend.tests.test_cron_indexer import _make_target


def fake_provider(paused_keys, supports_suspend=True, calls=None):
    p = MagicMock()
    p.id = "fake"
    p.supports_suspend = supports_suspend
    p.derives_endpoint_url = False

    def is_paused(key):
        if calls is not None:
            calls.append(key)
        return key in paused_keys

    p.is_paused = is_paused
    return p


def service(provider, key="k1"):
    config = EmbeddingConfig(model_type="remote", model_name="m", model_kwargs={"api_key_env": "K", "base_url": "https://x/v1"})
    return RemoteEmbeddingService(config, api_key=key, provider=provider)


class Clock:
    def __init__(self): self.now = 0.0
    def __call__(self): return self.now


class TestPausedCache(unittest.TestCase):
    def test_answers_are_cached_for_the_ttl_and_invalidated(self):
        clock, calls = Clock(), []
        cache = PausedCache(clock)
        p = fake_provider({"k1"}, calls=calls)
        self.assertTrue(cache.is_paused(p, "embedding", "k1"))
        cache.is_paused(p, "embedding", "k1")
        self.assertEqual(len(calls), 1)
        clock.now += PAUSED_TTL_SECONDS + 1
        cache.is_paused(p, "embedding", "k1")
        self.assertEqual(len(calls), 2)
        cache.invalidate("fake", "embedding", "k1")
        cache.is_paused(p, "embedding", "k1")
        self.assertEqual(len(calls), 3)

    def test_keys_are_independent_and_non_suspending_providers_are_never_asked(self):
        cache, calls = PausedCache(Clock()), []
        p = fake_provider({"paused"}, calls=calls)
        self.assertTrue(cache.is_paused(p, "embedding", "paused"))
        self.assertFalse(cache.is_paused(p, "embedding", "other"))
        none = fake_provider({"x"}, supports_suspend=False, calls=calls)
        self.assertFalse(cache.is_paused(none, "embedding", "x"))
        self.assertEqual(calls.count("x"), 0)

    def test_a_provider_that_raises_counts_as_not_paused(self):
        p = fake_provider(set())
        p.is_paused = MagicMock(side_effect=RuntimeError("boom"))
        self.assertFalse(PausedCache(Clock()).is_paused(p, "embedding", "k"))


class TestServicesFailFast(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        paused_cache.clear()
        self.addCleanup(paused_cache.clear)

    async def test_embedding_call_raises_the_paused_error_without_touching_the_endpoint(self):
        svc = service(fake_provider({"k1"}))
        client = MagicMock()
        client.embeddings.with_raw_response.create = AsyncMock(side_effect=AssertionError("must not be called"))
        svc._client = client
        with self.assertRaises(EmbeddingEndpointUnavailableError) as ctx:
            await svc._create_embeddings_with_backoff("hello")
        self.assertIn("paused", str(ctx.exception))
        client.embeddings.with_raw_response.create.assert_not_called()

    async def test_another_user_on_the_same_provider_is_not_affected(self):
        provider = fake_provider({"k1"})
        self.assertTrue(await service(provider, "k1").is_paused())
        self.assertFalse(await service(provider, "k2").is_paused())

    async def test_a_provider_without_pause_support_is_never_asked(self):
        calls = []
        self.assertFalse(await service(fake_provider({"k1"}, supports_suspend=False, calls=calls)).is_paused())
        self.assertEqual(calls, [])

    async def test_a_paused_llm_raises_the_llm_error(self):
        from backend.services.llm import LLMEndpointUnavailableError, RemoteLLMService
        from backend.tests.test_providers_registry import make_preset
        preset = make_preset()
        settings = MagicMock()
        settings.get_hardware_preset.return_value = preset
        svc = RemoteLLMService(settings, api_key="k1")
        svc._provider = fake_provider({"k1"})
        svc._openai_client = MagicMock()
        with self.assertRaises(LLMEndpointUnavailableError):
            await svc.generate("hi")

    async def test_the_paused_error_is_the_systemic_endpoint_error_that_never_counts_toward_quarantine(self):
        # The quarantine rule (#70) keys off this exact class name in cron_indexer.
        from backend.services import cron_indexer
        self.assertIn("EmbeddingEndpointUnavailableError", cron_indexer._SYSTEMIC_EMBEDDING_ERROR_TYPES)


class TestCronSkipsPausedOwnersOnly(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        paused_cache.clear()
        self.addCleanup(paused_cache.clear)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    async def test_the_paused_owners_library_is_skipped_and_the_other_is_indexed(self):
        import logging
        provider = fake_provider({"paused-key"})
        targets = {
            "users/1": _make_target(embedding_key="paused-key", fingerprint="fp1"),
            "users/2": _make_target(embedding_key="live-key", fingerprint="fp2"),
        }
        vector_store = MagicMock()
        vector_store.get_library_metadata.return_value = None
        indexer = CronIndexer(
            targets=targets, vector_store=vector_store, lock_file=self.dir / "lock",
            status_file=self.dir / "status.json", log=logging.getLogger("t"),
        )
        indexed = []

        def make_service(config, api_key=None, **kw):
            return service(provider, api_key)

        async def index_library(**kw):
            indexed.append(kw)
            return {"items_processed": 1, "chunks_added": 1, "mode": "full"}

        processor = MagicMock()
        processor.index_library = AsyncMock(side_effect=index_library)
        with patch("backend.services.cron_indexer.create_embedding_service", side_effect=make_service), \
             patch("backend.services.cron_indexer.ZoteroWebAPI") as web, \
             patch("backend.services.cron_indexer.DocumentProcessor", return_value=processor), \
             patch.object(indexer, "_drain_pending_uploads", new=AsyncMock(return_value=(0, 0))):
            web.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            web.return_value.__aexit__ = AsyncMock(return_value=False)
            await indexer.run()

        status = json.loads((self.dir / "status.json").read_text())
        self.assertEqual(status["slugs"]["users/1"]["status"], "skipped")
        self.assertEqual(status["slugs"]["users/1"]["skip_reason"], "embedding_paused")
        self.assertEqual(status["slugs"]["users/2"]["status"], "done", status["slugs"]["users/2"])
        self.assertEqual(len(indexed), 1)  # only the live owner's library reached the processor

    async def test_a_skipped_paused_slug_never_touches_pending_uploads_or_the_key_status(self):
        import logging
        key_store = MagicMock()
        indexer = CronIndexer(
            targets={"users/1": _make_target(embedding_key="paused-key", fingerprint="fp1")},
            vector_store=MagicMock(), lock_file=self.dir / "lock", status_file=self.dir / "status.json",
            log=logging.getLogger("t"), key_store=key_store,
        )
        drain = AsyncMock(return_value=(0, 0))
        with patch("backend.services.cron_indexer.create_embedding_service",
                   side_effect=lambda config, api_key=None, **kw: service(fake_provider({"paused-key"}), api_key)), \
             patch("backend.services.cron_indexer.ZoteroWebAPI") as web, \
             patch.object(indexer, "_drain_pending_uploads", new=drain):
            web.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
            web.return_value.__aexit__ = AsyncMock(return_value=False)
            await indexer.run()
        drain.assert_not_called()                      # no attempt is recorded against any queued upload
        key_store.set_embedding_key_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
