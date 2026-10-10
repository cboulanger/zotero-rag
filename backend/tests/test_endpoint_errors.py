"""Vendor-neutral 'endpoint unavailable' wording with provider-supplied hints."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from openai import APIConnectionError, BadRequestError

from backend.services import endpoint_errors
from backend.services.embeddings import EmbeddingEndpointUnavailableError, RemoteEmbeddingService
from backend.tests.test_provider_runpod import runpod_preset
from backend.tests.test_providers_registry import make_preset


def _settings(preset):
    s = MagicMock()
    s.get_hardware_preset.return_value = preset
    return s


def _bad_request(body="endpoint is paused"):
    req = httpx.Request("POST", "https://x/v1/embeddings")
    return BadRequestError(body, response=httpx.Response(400, request=req), body=body)


class TestMessages(unittest.TestCase):
    def test_runpod_hint_and_paused_state(self):
        with patch("backend.config.settings.get_settings", return_value=_settings(runpod_preset())):
            msg = endpoint_errors.unavailable_message("llm", "LLM down.", "paused")
        self.assertIn("LLM down.", msg)
        self.assertIn("paused", msg)
        self.assertIn("Preferences", msg)  # the RunPod provider's own advice

    def test_generic_provider_adds_no_vendor_text(self):
        with patch("backend.config.settings.get_settings", return_value=_settings(make_preset())):
            msg = endpoint_errors.unavailable_message("embedding", "Embedding down.")
        self.assertEqual(msg, "Embedding down.")
        self.assertNotIn("RunPod", msg)

    def test_no_preset_never_raises(self):
        s = MagicMock()
        s.get_hardware_preset.side_effect = RuntimeError("boom")
        with patch("backend.config.settings.get_settings", return_value=s):
            self.assertEqual(endpoint_errors.unavailable_message("llm", "x."), "x.")

    def test_classify_uses_the_providers_rules(self):
        exc = MagicMock(status_code=409, body="ENDPOINT_PAUSED")
        with patch("backend.config.settings.get_settings", return_value=_settings(runpod_preset())):
            self.assertEqual(endpoint_errors.classify_status_error("llm", exc), "paused")
            self.assertIsNone(endpoint_errors.classify_status_error("llm", MagicMock(status_code=500, body="x")))
        self.assertIsNone(endpoint_errors.classify_status_error("llm", MagicMock(status_code=None)))


class TestEmbeddingPausedIs400(unittest.IsolatedAsyncioTestCase):
    async def test_paused_400_becomes_endpoint_unavailable_not_a_per_item_error(self):
        preset = runpod_preset()
        preset.embedding.model_type = "remote"
        service = RemoteEmbeddingService(preset.embedding, api_key="k")
        client = MagicMock()
        client.embeddings.with_raw_response.create = AsyncMock(side_effect=_bad_request("ENDPOINT_PAUSED"))
        service._client = client
        with patch("backend.config.settings.get_settings", return_value=_settings(preset)), \
             patch.object(endpoint_errors, "classify_status_error", return_value="paused"), \
             patch("backend.services.embeddings.classify_status_error", return_value="paused"):
            with self.assertRaises(EmbeddingEndpointUnavailableError) as ctx:
                await service._create_embeddings_with_backoff("hello")
        self.assertIn("paused", str(ctx.exception))

    async def test_ordinary_400_is_still_raised_as_is(self):
        preset = make_preset()
        service = RemoteEmbeddingService(preset.embedding, api_key="k")
        client = MagicMock()
        client.embeddings.with_raw_response.create = AsyncMock(side_effect=_bad_request("bad input"))
        service._client = client
        with patch("backend.config.settings.get_settings", return_value=_settings(preset)):
            with self.assertRaises(BadRequestError):
                await service._create_embeddings_with_backoff("hello")


if __name__ == "__main__":
    unittest.main()


class TestLLMUnavailable(unittest.IsolatedAsyncioTestCase):
    async def test_connection_error_is_neutral_and_carries_the_provider_hint(self):
        from openai import APIConnectionError
        from backend.services.llm import LLMEndpointUnavailableError, RemoteLLMService
        from backend.tests.test_llm import _raw  # noqa: F401  (shared fixtures module import check)
        preset = runpod_preset()
        settings = MagicMock()
        settings.get_hardware_preset.return_value = preset
        settings.get_hardware_preset().llm.model_type = "remote"
        service = RemoteLLMService.__new__(RemoteLLMService)
        service.settings = settings
        service.preset = preset
        service.llm_config = preset.llm
        service._model_name = "m"
        service._provider = None
        service._resolved_base_url = None
        service._openai_client = None
        service.api_key = "k"
        service._llm_api = lambda: "openai"
        service._generate_openai = AsyncMock(
            side_effect=APIConnectionError(request=httpx.Request("POST", "https://x/v1/chat/completions"))
        )
        with patch("backend.config.settings.get_settings", return_value=settings):
            with self.assertRaises(LLMEndpointUnavailableError) as ctx:
                await service.generate("hi")
        self.assertNotIn("(e.g. RunPod)", str(ctx.exception))  # core text is vendor-neutral
        self.assertIn("Preferences", str(ctx.exception))
