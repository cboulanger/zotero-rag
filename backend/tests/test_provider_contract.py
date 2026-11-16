"""Provider contract (spec A6) for core's generic provider, and the rule that every
plugin bundles the contract for the providers it registers.

The suite itself is ``backend.providers.testing.ProviderContractSuite``; each plugin
runs it for its own providers in ``<plugin>/tests/test_contract.py``.
"""

import unittest

from backend.pluginhost import SHIPPED_PLUGIN_DIR, get_plugin_host
from backend.providers.testing import ContractFixture, ProviderContractSuite


class TestGenericProviderContract(ProviderContractSuite, unittest.TestCase):
    fixture = ContractFixture(
        provider_id="generic",
        valid_options=[{}],
        invalid_options=[{"anything": 1}],
        usage_samples=[
            ({"x-ratelimit-limit-requests": "10", "x-ratelimit-remaining-requests": "5"}, 1),
            ({"RateLimit-Limit": "100", "RateLimit-Remaining": "50"}, 1),
        ],
    )


class TestShippedPluginsBundleTheirTests(unittest.TestCase):
    def test_every_shipped_plugin_has_a_tests_package(self):
        for directory in sorted(SHIPPED_PLUGIN_DIR.iterdir()):
            if (directory / "__init__.py").is_file():
                with self.subTest(plugin=directory.name):
                    self.assertTrue((directory / "tests" / "__init__.py").is_file(),
                                    f"add backend/plugins/{directory.name}/tests/")

    def test_every_shipped_provider_plugin_runs_the_contract(self):
        for cap in get_plugin_host().capabilities("inference.provider"):
            if cap.plugin_id == "core" or cap.provider.id.startswith("demo-test-only"):
                continue
            path = SHIPPED_PLUGIN_DIR / cap.plugin_id / "tests" / "test_contract.py"
            if not path.parent.parent.is_dir():
                continue  # an external plugin
            with self.subTest(provider=cap.id):
                self.assertTrue(path.is_file(), f"{path} must run ProviderContractSuite for {cap.id!r}")
                self.assertIn(f'"{cap.id}"', path.read_text())


if __name__ == "__main__":
    unittest.main()
