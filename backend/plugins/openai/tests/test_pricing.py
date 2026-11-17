"""OpenAI list prices for cost estimates (issue #119)."""

import unittest

from backend.providers import Usage, get_providers
from backend.providers.testing import preset_with


class TestOpenAIPricing(unittest.TestCase):
    def provider(self, side="llm", kwargs=None):
        return get_providers(preset_with("openai", side, kwargs=kwargs))[side]

    def test_answering_models_by_family(self):
        p = self.provider()
        self.assertEqual(p.pricing("gpt-4o-mini").input_per_million_tokens, 0.15)
        self.assertEqual(p.pricing("gpt-4o-mini-2024-07-18").output_per_million_tokens, 0.60)
        self.assertEqual(p.pricing("gpt-4o").input_per_million_tokens, 2.50)
        self.assertIsNone(p.pricing("some-future-model"))

    def test_embedding_sentinel_is_text_embedding_3_small(self):
        p = self.provider("embedding")
        self.assertEqual(p.pricing("openai").input_per_million_tokens, 0.02)
        cost = p.estimate_cost(Usage(side="embedding", model="text-embedding-3-small", input_tokens=1_000_000))
        self.assertAlmostEqual(cost.amount, 0.02)

    def test_preset_prices_override_the_list(self):
        p = self.provider(kwargs={"api_key_env": "SIDE_KEY", "pricing": {"input_per_million_tokens": 9}})
        self.assertEqual(p.pricing("gpt-4o-mini").input_per_million_tokens, 9)


if __name__ == "__main__":
    unittest.main()
