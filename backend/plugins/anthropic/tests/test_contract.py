"""The provider contract (backend.providers.testing) for the anthropic provider."""

import unittest

from backend.providers.testing import ContractFixture, ProviderContractSuite


class TestContract(ProviderContractSuite, unittest.TestCase):
    fixture = ContractFixture(
        provider_id="anthropic",
        model_kwargs={"api_key_env": "ANTHROPIC_API_KEY"},
        invalid_options=[{"anything": 1}],
    )


if __name__ == "__main__":
    unittest.main()
