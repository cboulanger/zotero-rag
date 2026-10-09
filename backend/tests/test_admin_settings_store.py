"""Unit tests for the admin settings JSON state store."""

import tempfile
import unittest
from pathlib import Path

from backend.services.admin_settings_store import (
    read_admin_settings,
    write_admin_settings,
    get_active_preset_override,
    set_active_preset_override,
    get_remote_config_value,
    update_remote_config,
    normalize_base_url,
)


class TestAdminSettingsStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_returns_defaults_when_file_missing(self):
        settings = read_admin_settings(self.data_path)
        self.assertEqual(settings, {
            "index_snapshots": False,
            "active_preset_override": None,
            "remote_config": {},
        })

    def test_write_then_read_round_trips(self):
        write_admin_settings(self.data_path, {"index_snapshots": True})
        self.assertEqual(read_admin_settings(self.data_path), {
            "index_snapshots": True,
            "active_preset_override": None,
            "remote_config": {},
        })

    def test_write_then_read_false_round_trips(self):
        write_admin_settings(self.data_path, {"index_snapshots": True})
        write_admin_settings(self.data_path, {"index_snapshots": False})
        self.assertEqual(read_admin_settings(self.data_path), {
            "index_snapshots": False,
            "active_preset_override": None,
            "remote_config": {},
        })

    def test_read_returns_default_on_corrupt_file(self):
        system_dir = self.data_path / "system"
        system_dir.mkdir(parents=True)
        (system_dir / "admin_settings.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(read_admin_settings(self.data_path), {
            "index_snapshots": False,
            "active_preset_override": None,
            "remote_config": {},
        })

    def test_read_returns_default_when_file_contains_valid_but_non_dict_json(self):
        system_dir = self.data_path / "system"
        system_dir.mkdir(parents=True)
        (system_dir / "admin_settings.json").write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(read_admin_settings(self.data_path), {
            "index_snapshots": False,
            "active_preset_override": None,
            "remote_config": {},
        })

    def test_active_preset_override_defaults_to_none(self):
        self.assertIsNone(get_active_preset_override(self.data_path))

    def test_set_then_get_active_preset_override_round_trips(self):
        set_active_preset_override(self.data_path, "remote-mpcdf")
        self.assertEqual(get_active_preset_override(self.data_path), "remote-mpcdf")

    def test_set_active_preset_override_none_clears_it(self):
        set_active_preset_override(self.data_path, "remote-mpcdf")
        set_active_preset_override(self.data_path, None)
        self.assertIsNone(get_active_preset_override(self.data_path))

    def test_set_active_preset_override_preserves_index_snapshots(self):
        write_admin_settings(self.data_path, {"index_snapshots": True})
        set_active_preset_override(self.data_path, "remote-mpcdf")
        state = read_admin_settings(self.data_path)
        self.assertTrue(state["index_snapshots"])
        self.assertEqual(state["active_preset_override"], "remote-mpcdf")

    def test_get_remote_config_value_defaults_to_none(self):
        self.assertIsNone(get_remote_config_value("MPCDF_LLM_BASE_URL", data_path=self.data_path))

    def test_update_remote_config_then_get_round_trips(self):
        update_remote_config({"MPCDF_LLM_BASE_URL": "https://llm.mpcdf.mpg.de/abc123/v1"}, data_path=self.data_path)
        self.assertEqual(
            get_remote_config_value("MPCDF_LLM_BASE_URL", data_path=self.data_path),
            "https://llm.mpcdf.mpg.de/abc123/v1",
        )

    def test_update_remote_config_merges_without_dropping_other_keys(self):
        update_remote_config({"MPCDF_LLM_BASE_URL": "https://llm.mpcdf.mpg.de/abc123/v1"}, data_path=self.data_path)
        update_remote_config({"MPCDF_EMBEDDING_BASE_URL": "https://llm.mpcdf.mpg.de/def456/v1"}, data_path=self.data_path)
        self.assertEqual(
            get_remote_config_value("MPCDF_LLM_BASE_URL", data_path=self.data_path),
            "https://llm.mpcdf.mpg.de/abc123/v1",
        )
        self.assertEqual(
            get_remote_config_value("MPCDF_EMBEDDING_BASE_URL", data_path=self.data_path),
            "https://llm.mpcdf.mpg.de/def456/v1",
        )

    def test_update_remote_config_overwrites_same_key(self):
        update_remote_config({"MPCDF_LLM_BASE_URL": "https://llm.mpcdf.mpg.de/old/v1"}, data_path=self.data_path)
        update_remote_config({"MPCDF_LLM_BASE_URL": "https://llm.mpcdf.mpg.de/new/v1"}, data_path=self.data_path)
        self.assertEqual(
            get_remote_config_value("MPCDF_LLM_BASE_URL", data_path=self.data_path),
            "https://llm.mpcdf.mpg.de/new/v1",
        )

    def test_read_admin_settings_does_not_leak_mutable_state_across_data_paths(self):
        """DEFAULT_ADMIN_SETTINGS["remote_config"] is one module-level dict; a
        shallow dict(DEFAULT_ADMIN_SETTINGS) copy still shares that nested dict
        object, so mutating one data_path's returned remote_config in place
        (bypassing update_remote_config) must not leak into another, unrelated
        data_path's defaults."""
        with tempfile.TemporaryDirectory() as other_tmp:
            other_data_path = Path(other_tmp)
            update_remote_config({"LEAKED_KEY": "leaked_value"}, data_path=self.data_path)
            other_state = read_admin_settings(other_data_path)
            self.assertEqual(other_state["remote_config"], {})


class TestNormalizeBaseUrl(unittest.TestCase):
    def test_appends_v1_when_missing(self):
        self.assertEqual(
            normalize_base_url("https://llm.mpcdf.mpg.de/abc123"),
            "https://llm.mpcdf.mpg.de/abc123/v1",
        )

    def test_leaves_v1_untouched_when_already_present(self):
        self.assertEqual(
            normalize_base_url("https://llm.mpcdf.mpg.de/abc123/v1"),
            "https://llm.mpcdf.mpg.de/abc123/v1",
        )

    def test_strips_trailing_slash_before_appending_v1(self):
        self.assertEqual(
            normalize_base_url("https://llm.mpcdf.mpg.de/abc123/"),
            "https://llm.mpcdf.mpg.de/abc123/v1",
        )

    def test_strips_trailing_slash_when_v1_already_present(self):
        self.assertEqual(
            normalize_base_url("https://llm.mpcdf.mpg.de/abc123/v1/"),
            "https://llm.mpcdf.mpg.de/abc123/v1",
        )


if __name__ == "__main__":
    unittest.main()
