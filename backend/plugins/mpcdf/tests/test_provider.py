"""The mpcdf provider (a small vendor class over the generic base)."""

import unittest

from backend.providers import ProviderConfigError, get_providers
from backend.providers.testing import preset_with


class TestMpcdf(unittest.TestCase):
    """Per-user only; session behaviour is tested in test_sessions."""

    def test_scope_is_user_only(self):
        provider = get_providers(preset_with("mpcdf", kwargs={}))["llm"]
        self.assertEqual(provider.scope, "user")
        self.assertTrue(provider.uses_session_endpoints)
        self.assertFalse(provider.provisioning_needs_key)
        for scope in ("shared", "managed"):
            with self.assertRaises(ProviderConfigError):
                get_providers(preset_with("mpcdf", scope=scope, kwargs={}))

    def test_provisions_and_suspends(self):
        provider = get_providers(preset_with("mpcdf", kwargs={}))["llm"]
        self.assertTrue(provider.supports_provisioning and provider.supports_suspend)
        self.assertIsNotNone(provider.describe().provisioning.login)


if __name__ == "__main__":
    unittest.main()
