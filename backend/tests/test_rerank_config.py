"""The optional ``rerank`` preset section and the ``rerank`` provider side."""

import unittest

from pydantic import ValidationError

from backend.config.presets import HardwarePreset
from backend.providers import ProviderConfigError, get_providers, preset_sides, side_config


def make_preset(rerank=None) -> HardwarePreset:
    data = {
        "name": "t",
        "description": "t",
        "version": 2,
        "embedding": {"model_type": "remote", "model_name": "m", "model_kwargs": {"api_key_env": "K"}},
        "llm": {"model_type": "remote", "model_names": ["m"], "model_kwargs": {"api_key_env": "K"}},
        "rag": {},
        "memory_budget_gb": 1.0,
    }
    if rerank is not None:
        data["rerank"] = rerank
    return HardwarePreset.model_validate(data)


class RerankConfigTest(unittest.TestCase):
    def test_absent_section_means_no_rerank(self):
        preset = make_preset()
        self.assertIsNone(preset.rerank)
        self.assertEqual(preset_sides(preset), ("embedding", "llm"))
        self.assertNotIn("rerank", get_providers(preset))

    def test_local_section_defaults(self):
        preset = make_preset({"model_name": "BAAI/bge-reranker-v2-m3"})
        cfg = preset.rerank
        self.assertEqual(cfg.model_type, "local")
        self.assertEqual(cfg.pool_size, 100)
        self.assertEqual(cfg.timeout_seconds, 15.0)
        self.assertEqual(preset_sides(preset), ("embedding", "llm", "rerank"))
        self.assertIs(side_config(preset, "rerank"), cfg)

    def test_local_section_gets_generic_provider(self):
        providers = get_providers(make_preset({"model_name": "x"}))
        self.assertEqual(providers["rerank"].id, "generic")
        self.assertIs(providers["rerank"].side_config, providers["rerank"].preset.rerank)

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValidationError):
            make_preset({"model_name": "x", "top_n": 3})

    def test_pool_size_bounds(self):
        with self.assertRaises(ValidationError):
            make_preset({"model_name": "x", "pool_size": 0})

    def test_vendor_without_rerank_support_rejected(self):
        preset = make_preset({
            "model_type": "remote", "model_name": "x",
            "model_kwargs": {"api_key_env": "K"}, "provider": {"id": "kisski"},
        })
        with self.assertRaisesRegex(ProviderConfigError, "does not support the rerank side"):
            get_providers(preset)

    def test_remote_rerank_requires_user_scope(self):
        preset = make_preset({
            "model_type": "remote", "model_name": "x",
            "model_kwargs": {"shared_api_key_env": "R"}, "provider": {"id": "generic", "scope": "shared"},
        })
        with self.assertRaisesRegex(ProviderConfigError, "scope 'user'"):
            get_providers(preset)

    def test_remote_generic_rerank_with_user_key(self):
        preset = make_preset({
            "model_type": "remote", "model_name": "x",
            "model_kwargs": {"api_key_env": "RERANK_KEY", "base_url": "https://rerank.example/v1"},
        })
        self.assertEqual(get_providers(preset)["rerank"].scope, "user")


if __name__ == "__main__":
    unittest.main()
