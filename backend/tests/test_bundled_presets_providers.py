"""Every bundled preset resolves to valid providers from the shipped plugins."""

import unittest


class TestBundledPresetsPickUpProviderDefaults(unittest.TestCase):
    """get_preset() runs each provider's apply_defaults(), so the services read model_kwargs only."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_every_bundled_preset_has_valid_providers(self):
        from backend.config.presets import bundled_preset_files, list_presets
        self.assertEqual(
            set(list_presets(self.data_path)), {p.stem for p in bundled_preset_files()}
        )


if __name__ == "__main__":
    unittest.main()
