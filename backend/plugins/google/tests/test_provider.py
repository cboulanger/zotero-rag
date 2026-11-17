"""The google provider (a small vendor class over the generic base)."""

import unittest

from backend.providers import ProviderConfigError, get_providers
from backend.providers.testing import FakeHTTP, FakeResponse, preset_with


class TestGoogle(unittest.TestCase):
    def test_key_metadata(self):
        provider = get_providers(preset_with("google"))["llm"]
        self.assertEqual(provider.default_key_env(), "GEMINI_API_KEY")
        self.assertEqual(provider.key_docs_url("GEMINI_API_KEY"), "https://aistudio.google.com/apikey")
        self.assertIsNone(provider.key_docs_url("OTHER"))
        self.assertEqual(provider.llm_api, "openai")

    def test_serves_both_sides_and_rejects_shared_scope(self):
        for side in ("embedding", "llm"):
            get_providers(preset_with("google", side))
        with self.assertRaises(ProviderConfigError):
            get_providers(preset_with("google", scope="shared", kwargs={"shared_api_key_env": "GEMINI_API_KEY"}))


class TestGoogleLiveModels(unittest.TestCase):
    BASE = "https://generativelanguage.googleapis.com/v1beta/openai"

    def _provider(self, http, side="llm"):
        provider = get_providers(preset_with("google"))[side]
        provider._http_client = http
        return provider

    def test_lists_only_flash_text_models_newest_first_without_prefix(self):
        data = [{"id": f"models/{i}"} for i in (
            "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.5-flash",
            "gemini-2.5-flash-preview-tts", "gemini-2.5-flash-image", "gemini-3.1-flash-live-preview",
            "gemini-embedding-001", "gemini-3.1-flash-lite",
        )]
        http = FakeHTTP({("GET", r"/openai/models$"): FakeResponse(200, {"data": data})})
        models = self._provider(http).live_models(self.BASE + "/", "k")
        self.assertEqual([m.id for m in models],
                         ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-2.5-flash-lite"])
        self.assertEqual(http.calls[0][:2], ("GET", self.BASE + "/models"))

    def test_failures_return_none_and_embedding_side_has_no_list(self):
        for response in (FakeResponse(403, {}), FakeResponse(200, None), ConnectionError("down")):
            http = FakeHTTP({("GET", r"/models$"): response})
            self.assertIsNone(self._provider(http).live_models(self.BASE, "k"))
        http = FakeHTTP()
        self.assertIsNone(self._provider(http, "embedding").live_models(self.BASE, "k"))
        self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()
