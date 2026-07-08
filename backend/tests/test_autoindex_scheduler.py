"""Unit tests for backend.services.autoindex_scheduler."""

import unittest

from pydantic import ValidationError

from backend.config.settings import Settings


class SettingsValidatorTest(unittest.TestCase):
    def test_autoindex_interval_minutes_defaults_none(self):
        self.assertIsNone(Settings().autoindex_interval_minutes)

    def test_autoindex_interval_minutes_accepts_positive_int(self):
        s = Settings(autoindex_interval_minutes=60)
        self.assertEqual(s.autoindex_interval_minutes, 60)

    def test_autoindex_interval_minutes_rejects_zero(self):
        with self.assertRaises(ValidationError):
            Settings(autoindex_interval_minutes=0)

    def test_autoindex_interval_minutes_rejects_negative(self):
        with self.assertRaises(ValidationError):
            Settings(autoindex_interval_minutes=-5)


if __name__ == "__main__":
    unittest.main()
