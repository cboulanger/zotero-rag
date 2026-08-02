"""Unit tests for the Crossref-related Settings fields."""

import unittest

from backend.config.settings import Settings, reset_settings


class TestCrossrefSettings(unittest.TestCase):
    def tearDown(self):
        reset_settings()

    def test_crossref_contact_email_defaults_to_none(self):
        settings = Settings()
        self.assertIsNone(settings.crossref_contact_email)

    def test_crossref_cache_path_defaults_under_data_path(self):
        settings = Settings()
        self.assertEqual(settings.crossref_cache_path, settings.data_path / "system" / "crossref_cache")

    def test_crossref_cache_path_can_be_overridden(self):
        settings = Settings(crossref_cache_path="/tmp/custom-crossref-cache")
        self.assertEqual(str(settings.crossref_cache_path), "/tmp/custom-crossref-cache")
