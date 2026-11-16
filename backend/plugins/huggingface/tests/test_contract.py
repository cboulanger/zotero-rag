"""The provider contract (backend.providers.testing) for the huggingface provider."""

import unittest

from backend.providers.testing import ContractFixture, ProviderContractSuite


class TestContract(ProviderContractSuite, unittest.TestCase):
    fixture = ContractFixture(
        provider_id="huggingface",
        model_kwargs={"api_key_env": "HF_TOKEN"},
        valid_options=[{}, {"namespace": "acme", "region": "us-east-1", "engine": "tgi", "instance": "nvidia-l4",
                            "scale_to_zero_timeout_min": 60, "max_replica": 2}],
        invalid_options=[{"scale_to_zero_timeout_min": 5}, {"engine": "nope"}, {"max_replica": 0}, {"bogus": 1}],
    )


if __name__ == "__main__":
    unittest.main()
