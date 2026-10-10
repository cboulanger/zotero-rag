"""Contract suite every registered provider must pass (spec A6).

Parametrised over the provider registry and over each side a provider supports.
A provider whose id has no fixture in provider_fakes.CONTRACT_FIXTURES fails
``test_every_registered_provider_has_a_fixture`` - that is the definition of
"easy to add": write the module, register a fixture, pass this suite.
"""

import json
import unittest

from backend.config.presets import HardwarePreset
from backend.providers import (
    SIDES,
    PROVIDERS,
    Credentials,
    ProviderConfigError,
    ProvisionContext,
    discover_providers,
    get_providers,
)
from backend.tests.provider_fakes import CONTRACT_FIXTURES

discover_providers()
# Throwaway providers registered by other test modules are not part of the contract.
_REAL_IDS = sorted(pid for pid in PROVIDERS if not pid.startswith("demo-test-only"))


def build_preset(provider_id: str, side: str, fixture, options: dict, scope=None) -> HardwarePreset:
    """A preset where ``side`` uses the provider and the other side is generic."""
    def remote(env: str, side_scope: str) -> dict:
        # The key kind must agree with the scope (user: own key; else admin-set).
        kwargs = {k: v for k, v in fixture.model_kwargs.items() if k not in ("api_key_env", "shared_api_key_env")}
        kwargs["api_key_env" if side_scope == "user" else "shared_api_key_env"] = env
        return kwargs

    own_scope = scope or PROVIDERS[provider_id].default_scope
    emb_scope = own_scope if side == "embedding" else "user"
    llm_scope = own_scope if side == "llm" else "user"
    emb = {"model_type": "remote", "model_name": "m", "model_kwargs": remote("CONTRACT_EMB_KEY", emb_scope)}
    llm = {"model_type": "remote", "model_names": ["m"], "model_kwargs": remote("CONTRACT_LLM_KEY", llm_scope)}
    provider_block = {"id": provider_id, "options": options}
    if scope:
        provider_block["scope"] = scope
    (emb if side == "embedding" else llm)["provider"] = provider_block
    return HardwarePreset.model_validate({
        "name": "contract", "description": "c", "version": 2,
        "embedding": emb, "llm": llm, "rag": {}, "memory_budget_gb": 1.0,
    })


def cases():
    for pid in _REAL_IDS:
        for side in sorted(PROVIDERS[pid].supported_sides):
            yield pid, side


class TestFixtureCoverage(unittest.TestCase):
    def test_every_registered_provider_has_a_fixture(self):
        self.assertEqual(set(_REAL_IDS) - set(CONTRACT_FIXTURES), set(),
                         "Register a ContractFixture in backend/tests/provider_fakes.py for each new provider")

    def test_no_fixture_without_a_provider(self):
        self.assertEqual(set(CONTRACT_FIXTURES) - set(_REAL_IDS), set())


class TestProviderContract(unittest.TestCase):
    def each(self):
        for pid, side in cases():
            fixture = CONTRACT_FIXTURES[pid]
            with self.subTest(provider=pid, side=side):
                yield pid, side, fixture

    def provider_for(self, pid, side, fixture, options=None):
        preset = build_preset(pid, side, fixture, options if options is not None else fixture.valid_options[0])
        return get_providers(preset)[side], preset

    def test_valid_options_load_and_invalid_options_are_rejected(self):
        for pid, side, fixture in self.each():
            for options in fixture.valid_options:
                get_providers(build_preset(pid, side, fixture, options))
            for options in fixture.invalid_options:
                with self.assertRaises(ProviderConfigError):
                    get_providers(build_preset(pid, side, fixture, options))

    def test_unsupported_sides_are_rejected(self):
        for pid in _REAL_IDS:
            fixture = CONTRACT_FIXTURES[pid]
            for side in SIDES:
                if side in PROVIDERS[pid].supported_sides:
                    continue
                with self.subTest(provider=pid, side=side):
                    with self.assertRaises(ProviderConfigError):
                        get_providers(build_preset(pid, side, fixture, fixture.valid_options[0]))

    def test_scopes_outside_key_scopes_are_rejected_and_inside_accepted(self):
        for pid, side, fixture in self.each():
            cls = PROVIDERS[pid]
            for scope in ("user", "shared", "managed"):
                preset = build_preset(pid, side, fixture, fixture.valid_options[0], scope=scope)
                if scope in cls.key_scopes:
                    self.assertEqual(get_providers(preset)[side].scope, scope)
                else:
                    with self.assertRaises(ProviderConfigError):
                        get_providers(preset)

    def test_default_scope_is_an_allowed_scope(self):
        for pid in _REAL_IDS:
            cls = PROVIDERS[pid]
            self.assertIn(cls.default_scope, cls.key_scopes, pid)

    def test_apply_defaults_is_idempotent_and_keeps_explicit_values(self):
        for pid, side, fixture in self.each():
            provider, preset = self.provider_for(pid, side, fixture)
            cfg = provider.side_config
            cfg.model_kwargs["api_key_pattern"] = "^explicit$"
            cfg.model_kwargs["shared_api_key_pattern"] = "^explicit$"
            provider.apply_defaults()
            once = json.dumps(cfg.model_kwargs, sort_keys=True)
            provider.apply_defaults()
            self.assertEqual(json.dumps(cfg.model_kwargs, sort_keys=True), once)
            self.assertEqual(cfg.model_kwargs["api_key_pattern"], "^explicit$")
            self.assertEqual(cfg.model_kwargs["shared_api_key_pattern"], "^explicit$")

    def test_describe_is_well_formed_and_holds_no_secret(self):
        for pid, side, fixture in self.each():
            provider, _ = self.provider_for(pid, side, fixture)
            d = provider.describe()
            self.assertEqual(d.id, pid)
            self.assertTrue(d.label)
            self.assertIn(d.key_scope, ("user", "shared", "managed"))
            self.assertEqual(d.supports_provisioning, provider.supports_provisioning)
            self.assertEqual(d.supports_suspend, provider.supports_suspend)
            blob = d.model_dump_json().lower()
            for forbidden in ("bearer ", "secret", "password"):
                self.assertNotIn(forbidden, blob)
            if provider.supports_provisioning:
                self.assertIsNotNone(d.provisioning, "a provisioning provider must describe its credential and hint")

    def test_health_never_raises_even_when_the_network_is_down(self):
        for pid, side, fixture in self.each():
            provider, _ = self.provider_for(pid, side, fixture)
            fixture.break_network(provider)
            with fixture.patch_http():
                result = provider.health(Credentials(api_key="k", base_url="https://example.invalid/v1"))
                provider.health(Credentials(api_key=None, base_url=None))
            self.assertTrue(result is None or result.status in ("ready", "cold", "throttled", "paused", "unreachable"))

    def test_endpoint_url_and_live_models_never_raise(self):
        for pid, side, fixture in self.each():
            provider, _ = self.provider_for(pid, side, fixture)
            fixture.break_network(provider)
            with fixture.patch_http():
                url = provider.endpoint_url("k")
                models = provider.live_models("https://example.invalid/v1", "k")
            self.assertTrue(url is None or isinstance(url, str))
            self.assertTrue(models is None or isinstance(models, list))

    def test_parse_usage_never_raises_and_is_well_formed(self):
        garbage = [{}, None, {"x-ratelimit-limit-requests": "x"}, {"ratelimit-limit": "-1"}, {"": ""}]
        for pid, side, fixture in self.each():
            provider, _ = self.provider_for(pid, side, fixture)
            for headers in garbage:
                self.assertIsInstance(provider.parse_usage(headers or {}), list)
            self.assertEqual(provider.parse_usage({"content-type": "application/json"}), [])
            for headers, at_least in fixture.usage_samples:
                meters = provider.parse_usage(headers)
                self.assertGreaterEqual(len(meters), at_least)
                for m in meters:
                    self.assertEqual(m.side, side)
                    self.assertGreaterEqual(m.limit, 1)
                    self.assertGreaterEqual(m.remaining, 0)
                    self.assertLessEqual(m.remaining, m.limit)

    def test_unsupported_capabilities_raise_not_implemented(self):
        for pid, side, fixture in self.each():
            provider, preset = self.provider_for(pid, side, fixture)
            ctx = ProvisionContext(side=side, preset=preset, credential="k", data_path=__import__("pathlib").Path("."))
            if not provider.supports_provisioning:
                with self.assertRaises(NotImplementedError):
                    provider.provision(ctx, lambda msg: None)
                with self.assertRaises(NotImplementedError):
                    provider.teardown(ctx)
            if not provider.supports_suspend:
                with self.assertRaises(NotImplementedError):
                    provider.suspend(ctx, lambda msg: None)


if __name__ == "__main__":
    unittest.main()
