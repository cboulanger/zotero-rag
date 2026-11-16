"""The RunPod plugin's bundled preset."""

import json
import tempfile
import unittest
from pathlib import Path

from backend.config.presets import HardwarePreset, bundled_preset_path, ensure_default_presets, get_preset
from backend.providers import get_providers


class RunPodPresetTest(unittest.TestCase):
    def test_runpod_is_a_user_scope_preset_with_derived_urls(self):
        """No static base_url: each user's endpoints live in their own RunPod
        account and the URL is found from their key."""
        with tempfile.TemporaryDirectory() as tmp:
            ensure_default_presets(Path(tmp))
            preset = get_preset("runpod", Path(tmp))
        for side in (preset.embedding, preset.llm):
            self.assertEqual(side.provider.scope, "user")
            self.assertEqual(side.model_kwargs["api_key_env"], "RUNPOD_API_KEY")
            self.assertNotIn("shared_base_url_env", side.model_kwargs)

    def test_runpod_json_is_v2_with_per_side_providers_and_validates(self):
        raw = json.loads(bundled_preset_path("runpod").read_text())
        self.assertEqual(raw["version"], 2)
        for side in ("embedding", "llm"):
            self.assertEqual(raw[side]["provider"]["id"], "runpod")
            self.assertIn("options", raw[side]["provider"])
            self.assertNotIn("health_check_provider", raw[side])
            for key in raw[side].get("model_kwargs", {}):
                self.assertFalse(key.endswith("_pattern"), key)  # patterns come from apply_defaults
        self.assertNotIn("provisioning_script", raw)
        providers = get_providers(HardwarePreset(name="runpod", **raw))
        self.assertTrue(all(p.supports_provisioning for p in providers.values()))


if __name__ == "__main__":
    unittest.main()
