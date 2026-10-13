"""Per-key endpoint lookup: cache semantics and the service wiring."""

import unittest
from unittest.mock import MagicMock, patch

from backend.providers import get_providers
from backend.services.embeddings import EmbeddingEndpointUnavailableError, RemoteEmbeddingService
from backend.services.endpoint_cache import FOUND_TTL_SECONDS, MISSING_TTL_SECONDS, EndpointCache
from backend.tests.test_provider_runpod import provider as runpod_provider, runpod_preset
from backend.tests.provider_fakes import FakeHTTP, FakeResponse


class Clock:
    def __init__(self): self.now = 1000.0
    def __call__(self): return self.now


def fake_provider(urls):
    """A provider whose endpoint_url answers from ``urls`` (key -> url) and counts calls."""
    p = MagicMock()
    p.id = "fake"
    p.calls = []
    def endpoint_url(key):
        p.calls.append(key)
        return urls.get(key)
    p.endpoint_url = endpoint_url
    return p


class TestEndpointCache(unittest.TestCase):
    def test_found_url_is_cached_until_ttl(self):
        clock, p = Clock(), fake_provider({"k1": "https://a/v1"})
        cache = EndpointCache(clock)
        self.assertEqual(cache.resolve(p, "llm", "k1"), "https://a/v1")
        self.assertEqual(cache.resolve(p, "llm", "k1"), "https://a/v1")
        self.assertEqual(len(p.calls), 1)
        clock.now += FOUND_TTL_SECONDS + 1
        cache.resolve(p, "llm", "k1")
        self.assertEqual(len(p.calls), 2)

    def test_miss_is_cached_only_briefly(self):
        clock, p = Clock(), fake_provider({})
        cache = EndpointCache(clock)
        self.assertIsNone(cache.resolve(p, "llm", "k1"))
        cache.resolve(p, "llm", "k1")
        self.assertEqual(len(p.calls), 1)
        clock.now += MISSING_TTL_SECONDS + 1
        cache.resolve(p, "llm", "k1")
        self.assertEqual(len(p.calls), 2)

    def test_two_keys_and_two_sides_resolve_independently(self):
        p = fake_provider({"k1": "https://a/v1", "k2": "https://b/v1"})
        cache = EndpointCache(Clock())
        self.assertEqual(cache.resolve(p, "llm", "k1"), "https://a/v1")
        self.assertEqual(cache.resolve(p, "llm", "k2"), "https://b/v1")
        cache.resolve(p, "embedding", "k1")
        self.assertEqual(len(p.calls), 3)

    def test_invalidate_forces_a_new_lookup(self):
        p = fake_provider({"k1": "https://a/v1"})
        cache = EndpointCache(Clock())
        cache.resolve(p, "llm", "k1")
        cache.invalidate("fake", "llm", "k1")
        cache.resolve(p, "llm", "k1")
        self.assertEqual(len(p.calls), 2)

    def test_a_provider_that_raises_counts_as_no_endpoint(self):
        p = MagicMock(); p.id = "fake"; p.endpoint_url.side_effect = RuntimeError("boom")
        self.assertIsNone(EndpointCache(Clock()).resolve(p, "llm", "k"))


class TestRunPodEndpointUrl(unittest.TestCase):
    def test_finds_the_endpoint_by_name(self):
        http = FakeHTTP()
        http.add("GET", r"/endpoints$", FakeResponse(200, [
            {"id": "other", "name": "something-else"}, {"id": "ep9", "name": "zotero-rag-llm"}]))
        self.assertEqual(runpod_provider("llm", http=http).endpoint_url("k"), "https://api.runpod.ai/v2/ep9/openai/v1")

    def test_none_when_absent_or_on_any_error(self):
        http = FakeHTTP()
        http.add("GET", r"/endpoints$", FakeResponse(200, []))
        self.assertIsNone(runpod_provider("llm", http=http).endpoint_url("k"))
        http = FakeHTTP()
        http.add("GET", r"/endpoints$", FakeResponse(403, {"error": "no"}))
        self.assertIsNone(runpod_provider("llm", http=http).endpoint_url("k"))


def derived_preset():
    """A runpod preset in the per-key shape: the user's own key, no URL in the preset."""
    preset = runpod_preset(scope="user")
    for side in (preset.embedding, preset.llm):
        side.model_kwargs = {"api_key_env": "RUNPOD_API_KEY"}
    return preset


class TestServiceWiring(unittest.IsolatedAsyncioTestCase):
    async def test_embedding_service_builds_its_client_from_the_derived_url(self):
        preset = derived_preset()
        provider = get_providers(preset)["embedding"]
        provider.endpoint_url = lambda key: f"https://derived/{key}/v1"
        service = RemoteEmbeddingService(preset.embedding, api_key="userkey", provider=provider)
        with patch("openai.AsyncOpenAI") as cls:
            await service._ensure_endpoint()
            service._get_client()
        self.assertEqual(cls.call_args.kwargs["base_url"], "https://derived/userkey/v1")

    async def test_two_users_get_two_urls(self):
        preset = derived_preset()
        provider = get_providers(preset)["embedding"]
        provider.endpoint_url = lambda key: f"https://derived/{key}/v1"
        urls = []
        for key in ("alice", "bob"):
            service = RemoteEmbeddingService(preset.embedding, api_key=key, provider=provider)
            with patch("openai.AsyncOpenAI") as cls:
                await service._ensure_endpoint()
                service._get_client()
            urls.append(cls.call_args.kwargs["base_url"])
        self.assertEqual(urls, ["https://derived/alice/v1", "https://derived/bob/v1"])

    async def test_a_missing_endpoint_is_reported_as_not_provisioned_with_the_hint(self):
        preset = derived_preset()
        provider = get_providers(preset)["embedding"]
        provider.endpoint_url = lambda key: None
        service = RemoteEmbeddingService(preset.embedding, api_key="nobody", provider=provider)
        settings = MagicMock(); settings.get_hardware_preset.return_value = preset
        with patch("backend.config.settings.get_settings", return_value=settings):
            with self.assertRaises(EmbeddingEndpointUnavailableError) as ctx:
                await service._ensure_endpoint()
        self.assertIn("not provisioned", str(ctx.exception))
        self.assertIn("Preferences", str(ctx.exception))

    async def test_a_preset_url_wins_and_no_lookup_happens(self):
        preset = derived_preset()
        preset.embedding.model_kwargs["base_url"] = "https://fixed/v1"
        provider = get_providers(preset)["embedding"]
        provider.endpoint_url = MagicMock(side_effect=AssertionError("must not be called"))
        service = RemoteEmbeddingService(preset.embedding, api_key="k", provider=provider)
        with patch("openai.AsyncOpenAI") as cls:
            await service._ensure_endpoint()
            service._get_client()
        self.assertEqual(cls.call_args.kwargs["base_url"], "https://fixed/v1")


if __name__ == "__main__":
    unittest.main()


class TestLLMWiring(unittest.IsolatedAsyncioTestCase):
    async def test_llm_service_builds_its_client_from_the_derived_url(self):
        from backend.services.llm import RemoteLLMService
        preset = derived_preset()
        provider = get_providers(preset)["llm"]
        provider.endpoint_url = lambda key: f"https://derived/{key}/v1"
        settings = MagicMock(); settings.get_hardware_preset.return_value = preset
        service = RemoteLLMService(settings, api_key="userkey")
        service._provider = provider
        with patch("openai.AsyncOpenAI") as cls:
            await service._ensure_endpoint()
            service._get_openai_client()
        self.assertEqual(cls.call_args.kwargs["base_url"], "https://derived/userkey/v1")
