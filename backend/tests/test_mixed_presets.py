"""Presets that mix providers per side: loadable, only the provisionable side provisions, keys listed once each."""

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.config.presets import HardwarePreset, ensure_default_presets, get_preset
from backend.config.settings import get_settings, reset_settings
from backend.providers import get_providers


def mixed(embedding_side, llm_side):
    base = json.loads((Path(__file__).resolve().parents[1] / "config" / "default_presets" / "huggingface.json").read_text())
    base["embedding"] = {**base["embedding"], **embedding_side}
    base["llm"] = {**base["llm"], **llm_side}
    return base


HF_EMBEDDING = {}
ANTHROPIC_LLM = {"model_names": ["claude-sonnet-4"], "model_kwargs": {}, "provider": {"id": "anthropic"}}
LOCAL_EMBEDDING = {"model_type": "local", "model_name": "nomic-ai/nomic-embed-text-v1.5", "model_kwargs": {}, "provider": {"id": "generic"}}


class MixedPresetsTest(unittest.TestCase):
    def test_hf_embeddings_with_an_anthropic_llm(self):
        providers = get_providers(HardwarePreset(name="m", **mixed(HF_EMBEDDING, ANTHROPIC_LLM)))
        self.assertEqual((providers["embedding"].id, providers["llm"].id), ("huggingface", "anthropic"))
        self.assertEqual((providers["embedding"].supports_provisioning, providers["llm"].supports_provisioning), (True, False))
        self.assertEqual(providers["llm"].llm_api, "anthropic")

    def test_local_embeddings_with_an_anthropic_llm(self):
        providers = get_providers(HardwarePreset(name="m", **mixed(LOCAL_EMBEDDING, ANTHROPIC_LLM)))
        self.assertEqual(providers["llm"].id, "anthropic")
        self.assertFalse(providers["embedding"].supports_provisioning)

    def test_hf_on_the_llm_side_only(self):
        providers = get_providers(HardwarePreset(name="m", **mixed(LOCAL_EMBEDDING, {})))
        self.assertEqual(providers["llm"].id, "huggingface")


class MixedPresetKeysTest(unittest.TestCase):
    def setUp(self):
        from backend.main import app
        reset_settings()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(reset_settings)
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        ensure_default_presets(s.data_path)
        (s.data_path / "presets" / "hf-claude.json").write_text(json.dumps(mixed(HF_EMBEDDING, ANTHROPIC_LLM)))
        s.model_preset = "hf-claude"
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_each_key_is_listed_once_for_its_own_side(self):
        keys = {k["key_name"]: k["sides"] for k in self.client.get("/api/required-keys").json()["keys"]}
        self.assertEqual(keys, {"HF_TOKEN": ["embedding"], "ANTHROPIC_API_KEY": ["llm"]})

    def test_descriptors_show_only_the_hf_side_as_operable(self):
        sides = self.client.get("/api/config/providers").json()["sides"]
        self.assertEqual((sides["embedding"]["provider"]["id"], sides["embedding"]["provider"]["operable_by_caller"]), ("huggingface", True))
        self.assertEqual((sides["llm"]["provider"]["id"], sides["llm"]["provider"]["operable_by_caller"]), ("anthropic", False))

    def test_provisioning_without_sides_runs_only_the_hf_side(self):
        r = self.client.post("/api/config/provision", json={"keys": {"HF_TOKEN": "nope"}})
        self.assertEqual(r.status_code, 400)  # malformed token rejected before any call
        self.assertIn("format", r.json()["detail"])
        r = self.client.post("/api/config/provision", json={"sides": ["llm"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("cannot be provisioned", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
