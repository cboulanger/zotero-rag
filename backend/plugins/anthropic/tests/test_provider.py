"""The anthropic provider (a small vendor class over the generic base)."""

import unittest

from backend.providers import ProviderConfigError, get_providers
from backend.providers.testing import preset_with


class TestAnthropic(unittest.TestCase):
    def test_protocol_and_key_metadata(self):
        provider = get_providers(preset_with("anthropic"))["llm"]
        self.assertEqual(provider.llm_api, "anthropic")
        self.assertEqual(provider.default_key_env(), "ANTHROPIC_API_KEY")
        self.assertEqual(provider.key_docs_url("ANTHROPIC_API_KEY"), "https://console.anthropic.com/settings/keys")

    def test_embedding_side_is_rejected(self):
        with self.assertRaisesRegex(ProviderConfigError, "does not support the embedding side"):
            get_providers(preset_with("anthropic", side="embedding"))

    def test_mixed_with_another_providers_embedding_side(self):
        preset = preset_with("anthropic")
        preset.embedding.provider.id = "openai"
        providers = get_providers(preset)
        self.assertEqual((providers["embedding"].id, providers["llm"].id), ("openai", "anthropic"))


if __name__ == "__main__":
    unittest.main()
