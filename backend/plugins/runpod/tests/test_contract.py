"""The provider contract (backend.providers.testing) for the runpod provider."""

import unittest

from backend.providers.testing import ContractFixture, ProviderContractSuite


class TestContract(ProviderContractSuite, unittest.TestCase):
    fixture = ContractFixture(
        provider_id="runpod",
        model_kwargs={"api_key_env": "RUNPOD_API_KEY"},
        valid_options=[{}, {"gpu": "NVIDIA RTX A4000", "workers_max": 2, "idle_timeout": 30, "data_centers": ["EU-RO-1"]}],
        invalid_options=[{"workers_max": 0}, {"gpus": "x"}],
    )


if __name__ == "__main__":
    unittest.main()
