"""Gemini list prices for cost estimates (issue #119)."""

import unittest

from backend.providers import get_providers
from backend.providers.testing import preset_with


class TestGooglePricing(unittest.TestCase):
    def test_models_by_family(self):
        p = get_providers(preset_with("google"))["llm"]
        self.assertEqual(p.pricing("gemini-2.5-flash").input_per_million_tokens, 0.30)
        self.assertEqual(p.pricing("models/gemini-2.5-flash-lite").input_per_million_tokens, 0.10)
        self.assertIsNone(p.pricing("gemini-unknown"))
        self.assertTrue(p.cost_note)

    def test_embedding_model(self):
        p = get_providers(preset_with("google", "embedding"))["embedding"]
        self.assertEqual(p.pricing("gemini-embedding-001").input_per_million_tokens, 0.15)


if __name__ == "__main__":
    unittest.main()
