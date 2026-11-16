"""The OpenAI plugin's bundled preset."""

import tempfile
import unittest
from pathlib import Path

from backend.config.presets import ensure_default_presets, get_preset


class OpenAIPresetTest(unittest.TestCase):
    def test_remote_openai(self):
        with tempfile.TemporaryDirectory() as tmp:
            ensure_default_presets(Path(tmp))
            preset = get_preset("remote-openai", Path(tmp))
        self.assertEqual(preset.embedding.model_type, "remote")
        self.assertEqual(preset.llm.model_type, "remote")
        self.assertEqual(preset.llm.provider.id, "openai")


if __name__ == "__main__":
    unittest.main()
