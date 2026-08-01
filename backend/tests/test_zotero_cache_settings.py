"""Unit tests for the zotero_cache_path setting."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.config.settings import Settings


class ZoteroCachePathSettingTest(unittest.TestCase):
    def test_defaults_to_data_path_subdir(self):
        s = Settings(data_path=Path("/tmp/zotero-rag-test-data"))
        self.assertEqual(s.zotero_cache_path, Path("/tmp/zotero-rag-test-data/zotero_cache"))

    def test_explicit_value_from_env_is_respected(self):
        with patch.dict(os.environ, {"ZOTERO_CACHE_PATH": "/tmp/custom-zotero-cache"}):
            s = Settings()
        self.assertEqual(s.zotero_cache_path, Path("/tmp/custom-zotero-cache"))


if __name__ == "__main__":
    unittest.main()
