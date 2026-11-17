"""The provider contract (backend.providers.testing) for the kisski provider."""

import unittest

from backend.providers.testing import ContractFixture, ProviderContractSuite


class TestContract(ProviderContractSuite, unittest.TestCase):
    fixture = ContractFixture(
        provider_id="kisski",
        model_kwargs={"api_key_env": "KISSKI_API_KEY", "base_url": "https://chat-ai.academiccloud.de/v1"},
        valid_options=[{}, {"models_url": "https://example.invalid/models"}],
        invalid_options=[{"models_urll": "x"}],
        usage_samples=[
            ({"x-ratelimit-limit-hour": "100", "x-ratelimit-remaining-hour": "40"}, 1),
            ({"x-ratelimit-limit-hour": "100", "x-ratelimit-remaining-hour": "40",
              "x-ratelimit-limit-day": "1000", "x-ratelimit-remaining-day": "10"}, 2),
        ],
    )


if __name__ == "__main__":
    unittest.main()
