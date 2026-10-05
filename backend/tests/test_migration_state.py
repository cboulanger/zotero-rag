"""Unit tests for backend/utils/migration_state.py."""

import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.utils.migration_state import (
    delete_state,
    load_state,
    new_state,
    save_state,
    state_path,
)


class StatePathTest(unittest.TestCase):
    def test_same_inputs_produce_same_path(self):
        p1 = state_path("users/1", "http://a", "http://b", Path("/data"))
        p2 = state_path("users/1", "http://a", "http://b", Path("/data"))
        self.assertEqual(p1, p2)

    def test_different_dest_url_produces_different_path(self):
        p1 = state_path("users/1", "http://a", "http://b", Path("/data"))
        p2 = state_path("users/1", "http://a", "http://c", Path("/data"))
        self.assertNotEqual(p1, p2)

    def test_path_is_under_data_path_system_migration_state(self):
        p = state_path("users/1", "http://a", "http://b", Path("/data"))
        self.assertEqual(p.parent, Path("/data/system/migration_state"))
        self.assertTrue(p.name.endswith(".json"))


class SaveLoadStateTest(unittest.TestCase):
    def test_round_trip(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            state = new_state(
                "users/1", "http://a", "http://b", "u1", "model", 8,
                "2026-01-01T00:00:00Z",
            )
            save_state(path, state)
            loaded = load_state(path)
        self.assertEqual(loaded, state)

    def test_atomic_write_leaves_no_tmp_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(path, {"a": 1})
            tmp_files = list(Path(tmp).glob("*.tmp"))
        self.assertEqual(tmp_files, [])

    def test_save_creates_parent_directories(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "dir" / "state.json"
            save_state(path, {"a": 1})
            self.assertTrue(path.exists())

    def test_load_missing_file_returns_none(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            self.assertIsNone(load_state(path))

    def test_load_corrupt_file_returns_none(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("not json{{{")
            self.assertIsNone(load_state(path))

    def test_load_corrupt_file_warns_on_stderr(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("not json{{{")
            with patch("sys.stderr", new_callable=StringIO) as mock_stderr:
                load_state(path)
        self.assertIn(str(path), mock_stderr.getvalue())


class DeleteStateTest(unittest.TestCase):
    def test_delete_removes_existing_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            save_state(path, {"a": 1})
            delete_state(path)
            self.assertFalse(path.exists())

    def test_delete_missing_file_is_noop(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            delete_state(path)  # must not raise


class NewStateTest(unittest.TestCase):
    def test_new_state_shape(self):
        state = new_state(
            "users/1", "http://a", "http://b", "u1", "model", 8,
            "2026-01-01T00:00:00Z",
        )
        self.assertEqual(state["slug"], "users/1")
        self.assertEqual(state["source_url"], "http://a")
        self.assertEqual(state["dest_url"], "http://b")
        self.assertEqual(state["library_id"], "u1")
        self.assertEqual(state["embedding_model_name"], "model")
        self.assertEqual(state["embedding_dim"], 8)
        self.assertFalse(state["begin_done"])
        self.assertEqual(
            state["collections"]["chunks"],
            {"cursor": None, "transferred": 0, "done": False},
        )
        self.assertEqual(
            state["collections"]["dedup"],
            {"cursor": None, "transferred": 0, "done": False},
        )
        self.assertFalse(state["metadata_done"])
        self.assertEqual(state["started_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(state["updated_at"], "2026-01-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
