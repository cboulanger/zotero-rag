"""Tests for stand-in preset cost declarations."""

import tempfile
import unittest
from pathlib import Path

from backend.config.presets import ensure_default_presets, get_preset
from backend.providers import PROVIDERS, discover_providers
from backend.services.standin import preset_cost


class CostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.data_path = Path(cls._tmp.name)
        ensure_default_presets(cls.data_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def preset(self, name: str):
        return get_preset(name, self.data_path)

    def test_free_provider_presets_are_free(self):
        for name in ("remote-kisski", "remote-mpcdf", "kisski-rerank-mpcdf"):
            self.assertEqual(preset_cost(self.preset(name)), "free", name)

    def test_runpod_is_paid(self):
        self.assertEqual(preset_cost(self.preset("runpod")), "paid")

    def test_all_local_preset_is_free(self):
        self.assertEqual(preset_cost(self.preset("cpu-only")), "free")

    def test_explicit_cost_overrides_derived(self):
        preset = self.preset("runpod").model_copy(update={"cost": "free"})
        self.assertEqual(preset_cost(preset), "free")

    def test_defaults(self):
        preset = self.preset("remote-kisski")
        self.assertIs(preset.standin, True)
        self.assertEqual(preset.standin_priority, 0)

    def test_every_provider_declares_cost(self):
        discover_providers()
        for provider_id, cls in PROVIDERS.items():
            self.assertIn(cls.cost, ("free", "paid"), provider_id)


if __name__ == "__main__":
    unittest.main()
