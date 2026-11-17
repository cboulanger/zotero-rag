"""The openai provider (a small vendor class over the generic base)."""

import unittest

from backend.providers import ProviderConfigError, get_providers
from backend.providers.testing import preset_with


class TestOpenAI(unittest.TestCase):
    def test_key_metadata(self):
        provider = get_providers(preset_with("openai"))["llm"]
        self.assertEqual(provider.default_key_env(), "OPENAI_API_KEY")
        self.assertEqual(provider.key_docs_url("OPENAI_API_KEY"), "https://platform.openai.com/api-keys")
        self.assertIsNone(provider.key_docs_url("OTHER"))
        self.assertEqual(provider.llm_api, "openai")

    def test_serves_both_sides_and_allows_user_and_managed(self):
        for side in ("embedding", "llm"):
            get_providers(preset_with("openai", side))
        get_providers(preset_with("openai", scope="managed", kwargs={"shared_api_key_env": "OPENAI_API_KEY"}))
        with self.assertRaises(ProviderConfigError):
            get_providers(preset_with("openai", scope="shared", kwargs={"shared_api_key_env": "OPENAI_API_KEY"}))


class TestBundledPresetPicksUpProviderDefaults(unittest.TestCase):
    """get_preset() runs the provider's apply_defaults(), so the services read model_kwargs only."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_remote_openai_gets_its_key_name_and_portal_link_from_the_provider(self):
        from backend.config.presets import get_preset
        from backend.services.embeddings import RemoteEmbeddingService
        from backend.services.llm import RemoteLLMService
        preset = get_preset("remote-openai", self.data_path)
        for side in (preset.embedding, preset.llm):
            self.assertEqual(side.model_kwargs["api_key_env"], "OPENAI_API_KEY")
        (emb_field,) = RemoteEmbeddingService.required_client_fields(preset.embedding)
        (llm_field,) = RemoteLLMService.required_client_fields_for_config(preset.llm)
        for field in (emb_field, llm_field):
            self.assertEqual(field["key_name"], "OPENAI_API_KEY")
            self.assertEqual(field["header_name"], "X-Openai-Api-Key")
            self.assertEqual(field["docs_url"], "https://platform.openai.com/api-keys")


if __name__ == "__main__":
    unittest.main()
