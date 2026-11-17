"""Anthropic list prices for cost estimates (issue #119)."""

import unittest

from backend.providers import Usage, get_providers
from backend.providers.testing import preset_with


class TestAnthropicPricing(unittest.TestCase):
    def test_models_by_family(self):
        p = get_providers(preset_with("anthropic"))["llm"]
        self.assertEqual(p.pricing("claude-haiku-4-5-20251001").input_per_million_tokens, 1.0)
        self.assertEqual(p.pricing("claude-sonnet-4-5").output_per_million_tokens, 15.0)
        self.assertEqual(p.pricing("claude-opus-4-1-20250805").input_per_million_tokens, 15.0)
        self.assertIsNone(p.pricing("claude-unknown"))
        cost = p.estimate_cost(Usage(side="llm", model="claude-sonnet-4-5", input_tokens=1_000_000, output_tokens=100_000))
        self.assertAlmostEqual(cost.amount, 4.5)


if __name__ == "__main__":
    unittest.main()
