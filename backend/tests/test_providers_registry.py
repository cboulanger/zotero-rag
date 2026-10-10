"""Registry, discovery and load-time validation of a preset's providers."""

import copy
import unittest

from backend.config.presets import HardwarePreset
from backend.providers import (
    PROVIDERS,
    GenericProvider,
    Provider,
    ProviderConfigError,
    ProviderOptions,
    discover_providers,
    get_provider,
    get_providers,
    provider_config_error,
)
from backend.providers.base import Provider as BaseProvider


def make_preset(**overrides) -> HardwarePreset:
    data = {
        "name": "t",
        "description": "t",
        "version": 2,
        "embedding": {"model_type": "remote", "model_name": "m", "model_kwargs": {"api_key_env": "K"}},
        "llm": {"model_type": "remote", "model_names": ["m"], "model_kwargs": {"api_key_env": "K"}},
        "rag": {},
        "memory_budget_gb": 1.0,
    }
    for key, value in overrides.items():
        side, _, field = key.partition("__")
        if field:
            data[side][field] = value
        else:
            data[key] = value
    return HardwarePreset.model_validate(data)


class _DemoOptions(ProviderOptions):
    gpu: str = "small"


DEMO = None  # the demo provider class, registered only while this module's tests run


def setUpModule():
    """Register a throwaway provider; it must not leak into other test modules."""
    global DEMO

    class _DemoProvider(Provider):
        id = "demo-test-only"
        label = "Demo"
        Options = _DemoOptions
        supported_sides = frozenset({"llm"})
        key_scopes = frozenset({"user", "managed"})

    DEMO = _DemoProvider


def tearDownModule():
    PROVIDERS.pop("demo-test-only", None)


class TestRegistry(unittest.TestCase):
    def test_discovery_registers_generic_and_is_idempotent(self):
        first = discover_providers()
        second = discover_providers()
        self.assertIs(first, second)
        self.assertIn("generic", first)

    def test_generic_provider_is_the_base_class(self):
        self.assertIs(PROVIDERS["generic"], BaseProvider)
        self.assertIs(GenericProvider, BaseProvider)

    def test_subclass_registers_by_its_own_id_only(self):
        self.assertIs(PROVIDERS["demo-test-only"], DEMO)

        class _NoOwnId(DEMO):  # inherits the id, must not re-register
            pass

        self.assertIs(PROVIDERS["demo-test-only"], DEMO)

    def test_duplicate_id_is_rejected(self):
        with self.assertRaises(ValueError):

            class _Dup(Provider):
                id = "demo-test-only"


class TestGetProviders(unittest.TestCase):
    def test_valid_default_preset_gets_one_generic_instance_per_side(self):
        providers = get_providers(make_preset())
        self.assertEqual(set(providers), {"embedding", "llm"})
        self.assertEqual(providers["embedding"].side, "embedding")
        self.assertEqual(providers["llm"].side, "llm")
        self.assertIsInstance(providers["llm"], GenericProvider)
        self.assertEqual(providers["llm"].scope, "user")

    def test_get_provider_returns_one_side(self):
        self.assertEqual(get_provider(make_preset(), "llm").side, "llm")

    def test_unknown_provider_id_is_rejected_naming_side_and_id(self):
        preset = make_preset()
        preset.llm.provider.id = "nope"
        with self.assertRaisesRegex(ProviderConfigError, r"llm: unknown provider id 'nope'"):
            get_providers(preset)

    def test_invalid_options_are_rejected(self):
        preset = make_preset()
        preset.llm.provider.id = "demo-test-only"
        preset.llm.provider.options = {"gpu": ["not", "a", "string"]}
        with self.assertRaisesRegex(ProviderConfigError, "invalid options"):
            get_providers(preset)

    def test_unknown_option_is_rejected_not_ignored(self):
        preset = make_preset()
        preset.llm.provider.options = {"gpus": 4}
        with self.assertRaisesRegex(ProviderConfigError, "invalid options"):
            get_providers(preset)

    def test_valid_options_are_validated_into_the_options_model(self):
        preset = make_preset()
        preset.llm.provider.id = "demo-test-only"
        preset.llm.provider.options = {"gpu": "big"}
        # shared key env var between differing providers would fail; give llm its own key
        preset.llm.model_kwargs = {"api_key_env": "OTHER"}
        provider = get_providers(preset)["llm"]
        self.assertEqual(provider.options.gpu, "big")

    def test_side_outside_supported_sides_is_rejected(self):
        preset = make_preset()
        preset.embedding.provider.id = "demo-test-only"
        with self.assertRaisesRegex(ProviderConfigError, "does not support the embedding side"):
            get_providers(preset)

    def test_scope_outside_key_scopes_is_rejected(self):
        preset = make_preset()
        preset.llm.provider.id = "demo-test-only"
        preset.llm.provider.scope = "shared"
        preset.llm.model_kwargs = {"api_key_env": "OTHER"}
        with self.assertRaisesRegex(ProviderConfigError, "does not allow scope 'shared'"):
            get_providers(preset)

    def test_scope_inside_key_scopes_is_accepted_and_applied(self):
        preset = make_preset()
        preset.llm.provider.id = "demo-test-only"
        preset.llm.provider.scope = "managed"
        preset.llm.model_kwargs = {"shared_api_key_env": "OTHER"}
        self.assertEqual(get_providers(preset)["llm"].scope, "managed")

    def test_non_generic_provider_on_a_local_side_is_rejected(self):
        preset = make_preset(llm__model_type="local")
        preset.llm.provider.id = "demo-test-only"
        with self.assertRaisesRegex(ProviderConfigError, "cannot serve a local model"):
            get_providers(preset)

    def test_generic_on_a_local_side_is_fine(self):
        preset = make_preset(embedding__model_type="local")
        self.assertEqual(get_providers(preset)["embedding"].id, "generic")

    def test_one_key_used_by_two_different_providers_is_rejected(self):
        preset = make_preset()
        preset.llm.provider.id = "demo-test-only"  # embedding stays generic, both use env "K"
        with self.assertRaisesRegex(ProviderConfigError, "key 'K' is used by both sides"):
            get_providers(preset)

    def test_one_key_used_by_the_same_provider_on_both_sides_is_fine(self):
        self.assertEqual(len(get_providers(make_preset())), 2)

    def test_provider_config_error_returns_message_or_none(self):
        self.assertIsNone(provider_config_error(make_preset()))
        bad = make_preset()
        bad.llm.provider.id = "nope"
        self.assertIn("unknown provider id", provider_config_error(bad))

    def test_get_providers_does_not_mutate_the_preset(self):
        preset = make_preset()
        before = copy.deepcopy(preset.model_dump())
        get_providers(preset)
        self.assertEqual(preset.model_dump(), before)


if __name__ == "__main__":
    unittest.main()


class TestScopeMatchesKeyFields(unittest.TestCase):
    def test_user_scope_with_a_shared_key_field_is_rejected(self):
        preset = make_preset()
        preset.llm.model_kwargs = {"shared_api_key_env": "K"}  # generic defaults to scope user
        with self.assertRaisesRegex(ProviderConfigError, "takes each user's own key"):
            get_providers(preset)

    def test_shared_or_managed_scope_with_a_personal_key_field_is_rejected(self):
        for scope in ("shared", "managed"):
            preset = make_preset()
            preset.llm.provider.scope = scope
            with self.assertRaisesRegex(ProviderConfigError, "admin-set key"):
                get_providers(preset)

    def test_matching_kinds_are_accepted(self):
        preset = make_preset()
        preset.llm.provider.scope = "shared"
        preset.llm.model_kwargs = {"shared_api_key_env": "K"}
        self.assertEqual(get_providers(preset)["llm"].scope, "shared")
