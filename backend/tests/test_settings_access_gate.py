"""Unit tests for the Part 2 access-gate settings fields."""

import os
import unittest
from unittest.mock import patch

from backend.config.settings import Settings


class AccessGateSettingsTest(unittest.TestCase):
    def test_authorized_group_id_defaults_none(self):
        s = Settings()
        self.assertIsNone(s.authorized_group_id)

    def test_authorized_user_ids_defaults_empty(self):
        s = Settings()
        self.assertEqual(s.authorized_user_ids, [])

    def test_authorized_group_id_from_env(self):
        with patch.dict(os.environ, {"AUTHORIZED_GROUP_ID": "998877"}):
            s = Settings()
        self.assertEqual(s.authorized_group_id, 998877)

    def test_authorized_user_ids_parses_comma_separated_env(self):
        with patch.dict(os.environ, {"AUTHORIZED_USER_IDS": "123, 456"}):
            s = Settings()
        self.assertEqual(s.authorized_user_ids, [123, 456])

    def test_authorized_user_ids_accepts_list(self):
        s = Settings(authorized_user_ids=[1, 2])
        self.assertEqual(s.authorized_user_ids, [1, 2])


if __name__ == "__main__":
    unittest.main()
