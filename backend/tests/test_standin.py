"""Tests for stand-in preset cost declarations."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.config.presets import ensure_default_presets, get_preset
from backend.providers import PROVIDERS, discover_providers
from backend.services.standin import ExhaustedSide, find_standins, preset_cost


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


class FindStandinsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)
        ensure_default_presets(self.data_path)
        self.when = datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def preset(self, name: str):
        return get_preset(name, self.data_path)

    def settings(self, default: str = "remote-kisski"):
        return SimpleNamespace(data_path=self.data_path, get_default_preset=lambda: self.preset(default))

    def add_variant(self, base: str, name: str, **extra) -> None:
        data = json.loads((self.data_path / "presets" / f"{base}.json").read_text())
        data.update(name=name, **extra)
        (self.data_path / "presets" / f"{name}.json").write_text(json.dumps(data))

    def find(self, current: str, side: str, provider: str, keys=None, default="remote-kisski"):
        exhausted = [ExhaustedSide(side, provider, self.when)]
        with patch("backend.providers.mpcdf.MpcdfProvider.session_status", return_value=None):
            return find_standins(self.settings(default), self.preset(current), exhausted, keys or {}, 1)

    def names(self, standins):
        return [s.preset for s in standins]

    def test_embedding_exhausted_free_before_paid(self):
        names = self.names(self.find("kisski-rerank-mpcdf", "embedding", "kisski"))
        self.assertIn("remote-mpcdf", names)
        self.assertIn("runpod", names)
        self.assertLess(names.index("remote-mpcdf"), names.index("runpod"))
        for excluded in ("kisski-gemini", "remote-kisski", "kisski-docling", "kisski-rerank-mpcdf"):
            self.assertNotIn(excluded, names)

    def test_llm_exhausted_offers_other_llm_provider(self):
        names = self.names(self.find("remote-kisski", "llm", "kisski"))
        self.assertIn("kisski-gemini", names)
        self.assertNotIn("remote-kisski", names)

    def test_standin_false_is_never_offered(self):
        self.add_variant("runpod", "runpod-hidden", standin=False)
        self.assertNotIn("runpod-hidden", self.names(self.find("remote-kisski", "embedding", "kisski")))

    def test_priority_orders_within_cost(self):
        self.add_variant("runpod", "aaa-runpod", standin_priority=0)
        self.add_variant("runpod", "zzz-runpod", standin_priority=5)
        names = self.names(self.find("remote-kisski", "embedding", "kisski"))
        self.assertLess(names.index("zzz-runpod"), names.index("aaa-runpod"))

    def test_different_embedding_model_never_offered(self):
        data = json.loads((self.data_path / "presets" / "runpod.json").read_text())
        data["name"] = "other-model"
        data["embedding"]["model_name"] = "some-org/other-embedder"
        (self.data_path / "presets" / "other-model.json").write_text(json.dumps(data))
        self.assertNotIn("other-model", self.names(self.find("remote-kisski", "embedding", "kisski")))

    def test_key_need(self):
        by = {s.preset: s for s in self.find("remote-kisski", "embedding", "kisski")}
        self.assertIn("key:RUNPOD_API_KEY", by["runpod"].needs)
        by = {s.preset: s for s in self.find("remote-kisski", "embedding", "kisski", {"RUNPOD_API_KEY": "x"})}
        self.assertFalse([n for n in by["runpod"].needs if n.startswith("key:")])

    def test_login_need(self):
        exhausted = [ExhaustedSide("embedding", "kisski", self.when)]
        for status, expected in ((None, ["login:mpcdf"]), ({"state": "login_required"}, ["login:mpcdf"]),
                                 ({"state": "ok"}, []), ({"state": "active"}, [])):
            with patch("backend.providers.mpcdf.MpcdfProvider.session_status", return_value=status):
                result = find_standins(self.settings(), self.preset("remote-kisski"), exhausted, {}, 1)
            by = {s.preset: s for s in result}
            self.assertEqual([n for n in by["remote-mpcdf"].needs if n.startswith("login:")], expected, status)

    def test_to_dict(self):
        item = self.find("remote-kisski", "embedding", "kisski")[0]
        self.assertEqual(set(item.to_dict()), {"preset", "cost", "label", "needs"})


if __name__ == "__main__":
    unittest.main()
