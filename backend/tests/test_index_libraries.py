"""Unit tests for bin/index_libraries.py's argument parsing and target scoping."""

import importlib.util
import logging
import tempfile
import unittest
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "bin" / "index_libraries.py"
_SPEC = importlib.util.spec_from_file_location("index_libraries_script", _SCRIPT_PATH)
index_libraries = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(index_libraries)


class ParseArgsTest(unittest.TestCase):
    def test_fingerprint_defaults_to_none(self):
        args = index_libraries._parse_args([])
        self.assertIsNone(args.fingerprint)

    def test_fingerprint_accepted(self):
        args = index_libraries._parse_args(["--fingerprint", "abc123"])
        self.assertEqual(args.fingerprint, "abc123")


class FilterTargetsTest(unittest.TestCase):
    def setUp(self):
        self.targets = {
            "users/1": {"fingerprint": "fp-a", "zotero_key": "KA"},
            "groups/2": {"fingerprint": "fp-b", "zotero_key": "KB"},
        }

    def test_no_fingerprint_returns_all_targets(self):
        result = index_libraries._filter_targets(self.targets, None)
        self.assertEqual(result, self.targets)

    def test_fingerprint_restricts_to_owner(self):
        result = index_libraries._filter_targets(self.targets, "fp-a")
        self.assertEqual(result, {"users/1": self.targets["users/1"]})

    def test_unmatched_fingerprint_returns_empty(self):
        result = index_libraries._filter_targets(self.targets, "fp-does-not-exist")
        self.assertEqual(result, {})


class ClearLockFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock_file = Path(self.tmp.name) / "cron.lock"
        self.flock_file = Path(self.tmp.name) / "cron.lock.flock"
        self.log = logging.getLogger("test_index_libraries")

    def test_removes_both_lock_and_flock_files(self):
        self.lock_file.write_text("123")
        self.flock_file.write_text("")
        index_libraries._clear_lock_files(self.lock_file, self.log)
        self.assertFalse(self.lock_file.exists())
        self.assertFalse(self.flock_file.exists())

    def test_removes_flock_file_even_if_lock_file_absent(self):
        self.flock_file.write_text("")
        index_libraries._clear_lock_files(self.lock_file, self.log)
        self.assertFalse(self.flock_file.exists())

    def test_noop_when_neither_file_exists(self):
        index_libraries._clear_lock_files(self.lock_file, self.log)  # must not raise


if __name__ == "__main__":
    unittest.main()
