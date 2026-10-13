"""backend.services.user_settings: per-user preferred preset."""

import json
import tempfile
import unittest
from pathlib import Path

from backend.services.user_settings import get_preferred_preset, set_preferred_preset


class UserSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)

    def test_unset_is_none(self):
        self.assertIsNone(get_preferred_preset(self.data, 1))

    def test_round_trip_and_clear(self):
        set_preferred_preset(self.data, 1, "runpod")
        self.assertEqual(get_preferred_preset(self.data, 1), "runpod")
        set_preferred_preset(self.data, 1, None)
        self.assertIsNone(get_preferred_preset(self.data, 1))

    def test_users_are_independent(self):
        set_preferred_preset(self.data, 1, "runpod")
        set_preferred_preset(self.data, 2, "remote-kisski")
        self.assertEqual(get_preferred_preset(self.data, 1), "runpod")
        self.assertEqual(get_preferred_preset(self.data, 2), "remote-kisski")
        self.assertIsNone(get_preferred_preset(self.data, 3))

    def test_corrupt_or_wrongly_shaped_file_is_tolerated(self):
        path = self.data / "system" / "user_settings.json"
        path.parent.mkdir(parents=True)
        for content in ("{not json", "[]", '{"users": []}', '{"users": {"1": "oops"}}'):
            path.write_text(content)
            self.assertIsNone(get_preferred_preset(self.data, 1))
        path.write_text("{not json")
        set_preferred_preset(self.data, 1, "runpod")  # a write repairs the file
        self.assertEqual(get_preferred_preset(self.data, 1), "runpod")

    def test_file_is_valid_json_and_leaves_no_temp_files(self):
        set_preferred_preset(self.data, 7, "runpod")
        files = {p.name for p in (self.data / "system").iterdir()}
        self.assertEqual(json.loads((self.data / "system" / "user_settings.json").read_text()),
                         {"users": {"7": {"preferred_preset": "runpod"}}})
        self.assertFalse([f for f in files if f.endswith(".tmp")])


if __name__ == "__main__":
    unittest.main()
