"""The KISSKI plugin's bundled presets."""

import tempfile
import unittest
from pathlib import Path

from backend.config.presets import ensure_default_presets, get_preset, list_presets


class KisskiPresetsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)
        ensure_default_presets(self.data_path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_remote_kisski(self):
        preset = get_preset("remote-kisski", self.data_path)
        self.assertEqual(preset.embedding.model_type, "remote")  # fully remote, no local torch
        self.assertEqual(preset.embedding.model_name, "multilingual-e5-large-instruct")
        for side in (preset.embedding, preset.llm):
            self.assertEqual(side.provider.id, "kisski")
            self.assertEqual(side.model_kwargs["base_url"], "https://chat-ai.academiccloud.de/v1")
            self.assertEqual(side.model_kwargs["api_key_env"], "KISSKI_API_KEY")

    def test_kisski_docling_converts_documents_with_saia_docling(self):
        import json

        preset = get_preset("kisski-docling", self.data_path)
        self.assertEqual(preset.extraction.backend, "docling")
        self.assertEqual(preset.extraction.options["api_flavor"], "saia")
        self.assertEqual(preset.embedding.model_name, "multilingual-e5-large-instruct")
        json.dumps(preset.model_dump())  # serialisable for the config API

    def test_windows_test_is_listed_on_windows_only(self):
        self.assertIn("windows-test", list_presets(self.data_path, platform="windows"))
        self.assertNotIn("windows-test", list_presets(self.data_path, platform="linux"))


if __name__ == "__main__":
    unittest.main()
