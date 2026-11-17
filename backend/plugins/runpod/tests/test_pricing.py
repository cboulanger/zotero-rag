"""RunPod per-second GPU prices for cost estimates (issue #119)."""

import unittest

from backend.plugins.runpod.testing import provider
from backend.providers import Usage


class TestRunPodPricing(unittest.TestCase):
    def test_price_follows_the_configured_gpu(self):
        p = provider(scope="user")  # default gpu: NVIDIA RTX A5000
        self.assertEqual(p.pricing("any-model").per_second, 0.00019)
        cost = p.estimate_cost(Usage(side="llm", model="m", seconds=100))
        self.assertAlmostEqual(cost.amount, 0.019)
        self.assertIn("idle", cost.note)

    def test_gpu_name_is_case_insensitive_and_unknown_gpus_have_no_price(self):
        self.assertEqual(provider(options={"gpu": "nvidia h100 pcie"}, scope="user").pricing("m").per_second, 0.00116)
        self.assertIsNone(provider(options={"gpu": "NVIDIA B200"}, scope="user").pricing("m"))


if __name__ == "__main__":
    unittest.main()
