"""Unit tests for the admin settings JSON state store."""

import tempfile
import unittest
from pathlib import Path

from backend.services.admin_settings_store import read_admin_settings, write_admin_settings


class TestAdminSettingsStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_returns_index_snapshots_false_when_file_missing(self):
        settings = read_admin_settings(self.data_path)
        self.assertEqual(settings, {"index_snapshots": False})

    def test_write_then_read_round_trips(self):
        write_admin_settings(self.data_path, {"index_snapshots": True})
        self.assertEqual(read_admin_settings(self.data_path), {"index_snapshots": True})

    def test_write_then_read_false_round_trips(self):
        write_admin_settings(self.data_path, {"index_snapshots": True})
        write_admin_settings(self.data_path, {"index_snapshots": False})
        self.assertEqual(read_admin_settings(self.data_path), {"index_snapshots": False})

    def test_read_returns_default_on_corrupt_file(self):
        system_dir = self.data_path / "system"
        system_dir.mkdir(parents=True)
        (system_dir / "admin_settings.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(read_admin_settings(self.data_path), {"index_snapshots": False})


if __name__ == "__main__":
    unittest.main()
