"""Unit tests for bin/index_libraries.py's argument parsing and target scoping."""

import importlib.util
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


if __name__ == "__main__":
    unittest.main()
