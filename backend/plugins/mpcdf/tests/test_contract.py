"""The provider contract (backend.providers.testing) for the mpcdf provider."""

import unittest

from backend.providers.testing import ContractFixture, ProviderContractSuite


class TestContract(ProviderContractSuite, unittest.TestCase):
    fixture = ContractFixture(
        provider_id="mpcdf",
        model_kwargs={},
        invalid_options=[{"anything": 1}],
        usage_samples=[({"RateLimit-Limit": "100", "RateLimit-Remaining": "50"}, 1)],
    )


if __name__ == "__main__":
    unittest.main()
