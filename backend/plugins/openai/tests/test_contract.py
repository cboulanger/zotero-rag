"""The provider contract (backend.providers.testing) for the openai provider."""

import unittest

from backend.providers.testing import ContractFixture, ProviderContractSuite


class TestContract(ProviderContractSuite, unittest.TestCase):
    fixture = ContractFixture(
        provider_id="openai",
        model_kwargs={"api_key_env": "OPENAI_API_KEY"},
        invalid_options=[{"anything": 1}],
        usage_samples=[({"x-ratelimit-limit-requests": "10", "x-ratelimit-remaining-requests": "9"}, 1)],
    )


if __name__ == "__main__":
    unittest.main()
