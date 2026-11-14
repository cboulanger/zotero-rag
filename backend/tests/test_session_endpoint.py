"""Endpoints that run in the caller's own provider session: caller context and service wiring."""

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from backend.config.presets import EmbeddingConfig
from backend.providers.types import Credentials
from backend.services.caller_context import current_user_id, use_caller
from backend.services.embeddings import EmbeddingEndpointUnavailableError, RemoteEmbeddingService
from backend.services.llm import LLMEndpointUnavailableError, RemoteLLMService


class FakeSessionProvider:
    """Answers ``session_endpoint`` from ``endpoints`` (user id -> Credentials) and records activity."""

    id = "fake-session"
    uses_session_endpoints = True
    derives_endpoint_url = False
    supports_suspend = False

    def __init__(self, endpoints=None, error=None):
        self.endpoints = dict(endpoints or {})
        self.error = error
        self.lookups: list = []
        self.touches: list = []

    def session_endpoint(self, user_id):
        self.lookups.append(user_id)
        if self.error:
            raise self.error
        return self.endpoints.get(user_id)

    def session_activity(self, user_id):
        self.touches.append(user_id)

    def unavailable_hint(self):
        return "Log in and start the endpoint."


def embedding_config():
    return EmbeddingConfig(model_type="remote", model_name="e5", model_kwargs={})


def no_hint_settings():
    settings = MagicMock()
    settings.get_hardware_preset.side_effect = RuntimeError("no preset in this test")
    return patch("backend.config.settings.get_settings", return_value=settings)


class TestCallerContext(unittest.IsolatedAsyncioTestCase):
    async def test_binds_and_resets(self):
        self.assertIsNone(current_user_id())
        with use_caller(7):
            self.assertEqual(current_user_id(), 7)
            self.assertEqual(await asyncio.to_thread(current_user_id), 7)
        self.assertIsNone(current_user_id())


class TestEmbeddingSessionEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_uses_the_callers_endpoint_and_key(self):
        provider = FakeSessionProvider({7: Credentials(base_url="https://job7/v1", api_key="k7")})
        service = RemoteEmbeddingService(embedding_config(), provider=provider)
        with use_caller(7), patch("openai.AsyncOpenAI") as cls:
            await service._ensure_endpoint()
            service._get_client()
        self.assertEqual(cls.call_args.kwargs["base_url"], "https://job7/v1")
        self.assertEqual(cls.call_args.kwargs["api_key"], "k7")
        self.assertEqual(provider.touches, [7])

    async def test_a_renewal_rebuilds_the_client(self):
        provider = FakeSessionProvider({7: Credentials(base_url="https://a/v1", api_key="k1")})
        service = RemoteEmbeddingService(embedding_config(), provider=provider)
        with use_caller(7), patch("openai.AsyncOpenAI") as cls:
            await service._ensure_endpoint()
            first = service._get_client()
            await service._ensure_endpoint()
            self.assertIs(service._get_client(), first)
            provider.endpoints[7] = Credentials(base_url="https://b/v1", api_key="k2")
            await service._ensure_endpoint()
            service._get_client()
        self.assertEqual(cls.call_args.kwargs["base_url"], "https://b/v1")
        self.assertEqual(len(provider.lookups), 3)
        self.assertEqual(provider.touches, [7])  # throttled

    async def test_not_running_is_unavailable_and_never_uses_the_default_url(self):
        provider = FakeSessionProvider({})
        service = RemoteEmbeddingService(embedding_config(), provider=provider)
        with use_caller(7), no_hint_settings(), patch("openai.AsyncOpenAI") as cls:
            with self.assertRaises(EmbeddingEndpointUnavailableError) as ctx:
                await service._ensure_endpoint()
            self.assertIn("not running", str(ctx.exception))
            with self.assertRaises(EmbeddingEndpointUnavailableError):
                service._get_client()
        cls.assert_not_called()
        self.assertEqual(provider.touches, [])

    async def test_a_provider_error_is_unavailable(self):
        provider = FakeSessionProvider(error=RuntimeError("library missing"))
        service = RemoteEmbeddingService(embedding_config(), provider=provider)
        with no_hint_settings(), self.assertRaises(EmbeddingEndpointUnavailableError) as ctx:
            await service._ensure_endpoint()
        self.assertIn("library missing", str(ctx.exception))

    async def test_forget_endpoint_asks_again(self):
        provider = FakeSessionProvider({None: Credentials(base_url="https://local/v1", api_key="k")})
        service = RemoteEmbeddingService(embedding_config(), provider=provider)
        with patch("openai.AsyncOpenAI"):
            await service._ensure_endpoint()
            service._get_client()
            service._forget_endpoint()
            self.assertIsNone(service._client)
            await service._ensure_endpoint()
        self.assertEqual(provider.lookups, [None, None])

    async def test_two_users_get_their_own_endpoint(self):
        provider = FakeSessionProvider({
            1: Credentials(base_url="https://one/v1", api_key="a"),
            2: Credentials(base_url="https://two/v1", api_key="b"),
        })
        urls = []
        for uid in (1, 2):
            service = RemoteEmbeddingService(embedding_config(), provider=provider)
            with use_caller(uid), patch("openai.AsyncOpenAI") as cls:
                await service._ensure_endpoint()
                service._get_client()
            urls.append(cls.call_args.kwargs["base_url"])
        self.assertEqual(urls, ["https://one/v1", "https://two/v1"])


class TestLLMSessionEndpoint(unittest.IsolatedAsyncioTestCase):
    def service(self, provider):
        settings = MagicMock()
        settings.get_hardware_preset.return_value.llm.model_kwargs = {}
        settings.get_hardware_preset.return_value.llm.model_name = "gpt-oss"
        service = RemoteLLMService(settings)
        service._provider = provider
        return service

    async def test_uses_the_callers_endpoint_and_key(self):
        provider = FakeSessionProvider({7: Credentials(base_url="https://job7/v1", api_key="k7")})
        service = self.service(provider)
        with use_caller(7), patch("openai.AsyncOpenAI") as cls:
            await service._ensure_endpoint()
            service._get_openai_client()
        self.assertEqual(cls.call_args.kwargs["base_url"], "https://job7/v1")
        self.assertEqual(cls.call_args.kwargs["api_key"], "k7")
        self.assertEqual(provider.touches, [7])

    async def test_not_running_is_unavailable(self):
        service = self.service(FakeSessionProvider({}))
        with use_caller(7), no_hint_settings(), self.assertRaises(LLMEndpointUnavailableError) as ctx:
            await service._ensure_endpoint()
        self.assertIn("not running", str(ctx.exception))

    async def test_a_plain_provider_keeps_the_normal_path(self):
        provider = MagicMock(spec=["id", "derives_endpoint_url"])
        provider.derives_endpoint_url = False
        service = self.service(provider)
        self.assertIsNone(service._session_endpoint())
        self.assertFalse(service._derives_endpoint())


if __name__ == "__main__":
    unittest.main()
