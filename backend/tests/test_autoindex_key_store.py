"""Unit tests for backend.services.autoindex_key_store."""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from backend.services.autoindex_key_store import AutoIndexKeyStore
from backend.zotero.key_validator import KeyValidation


def _validation():
    return KeyValidation(user_id=39226, username="cboulanger",
                         targets=["users/39226", "groups/456"], read_only=True)


class AutoIndexKeyStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "autoindex_keys.json"
        self.secret = Fernet.generate_key().decode()
        self.store = AutoIndexKeyStore(self.path, self.secret)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_decrypt_roundtrip(self):
        fp = self.store.add("SECRETKEY", _validation())
        self.assertEqual(self.store.get_decrypted(fp), "SECRETKEY")

    def test_list_metadata_has_no_secrets(self):
        self.store.add("SECRETKEY", _validation())
        meta = self.store.list_metadata()
        self.assertEqual(len(meta), 1)
        entry = meta[0]
        self.assertNotIn("ciphertext", entry)
        self.assertEqual(entry["user_id"], 39226)
        self.assertEqual(entry["targets"], ["users/39226", "groups/456"])
        self.assertNotIn("SECRETKEY", str(meta))

    def test_remove_by_key(self):
        self.store.add("SECRETKEY", _validation())
        self.assertTrue(self.store.remove_by_key("SECRETKEY"))
        self.assertEqual(self.store.list_metadata(), [])

    def test_iter_decrypted(self):
        self.store.add("K1", _validation())
        items = list(self.store.iter_decrypted())
        self.assertEqual(len(items), 1)
        fp, key, entry = items[0]
        self.assertEqual(key, "K1")
        self.assertEqual(entry["targets"], ["users/39226", "groups/456"])

    def test_set_and_get_embedding_key(self):
        fp = self.store.add("ZOTKEY", _validation())
        self.store.set_embedding_key(fp, "EMBKEY", "KISSKI_API_KEY")
        key_name, key = self.store.get_decrypted_embedding_key(fp)
        self.assertEqual(key_name, "KISSKI_API_KEY")
        self.assertEqual(key, "EMBKEY")

    def test_get_decrypted_embedding_key_absent_returns_none(self):
        fp = self.store.add("ZOTKEY", _validation())
        self.assertIsNone(self.store.get_decrypted_embedding_key(fp))

    def test_list_metadata_reports_embedding_key_presence(self):
        fp = self.store.add("ZOTKEY", _validation())
        meta = self.store.list_metadata()[0]
        self.assertFalse(meta["has_embedding_key"])
        self.store.set_embedding_key(fp, "EMBKEY", "KISSKI_API_KEY")
        meta = self.store.list_metadata()[0]
        self.assertTrue(meta["has_embedding_key"])
        self.assertEqual(meta["embedding_key_status"], "ok")

    def test_set_embedding_key_status_updates_rate_limit(self):
        fp = self.store.add("ZOTKEY", _validation())
        self.store.set_embedding_key(fp, "EMBKEY", "KISSKI_API_KEY")
        self.store.set_embedding_key_status(fp, "rate_limited", rate_limit_until="2026-01-01T00:00:00+00:00")
        meta = self.store.list_metadata()[0]
        self.assertEqual(meta["embedding_key_status"], "rate_limited")

    def test_embedding_key_ciphertext_not_in_metadata(self):
        fp = self.store.add("ZOTKEY", _validation())
        self.store.set_embedding_key(fp, "EMBKEY", "KISSKI_API_KEY")
        meta = self.store.list_metadata()[0]
        self.assertNotIn("embedding_key_ciphertext", meta)
        self.assertNotIn("EMBKEY", str(meta))

    def test_disabled_when_no_secret(self):
        store = AutoIndexKeyStore(self.path, secret=None)
        self.assertFalse(store.enabled)
        with self.assertRaises(RuntimeError):
            store.add("K", _validation())

    @unittest.skipIf(sys.platform == "win32", "POSIX permissions not enforced on Windows")
    def test_keys_file_is_0600(self):
        self.store.add("SECRETKEY", _validation())
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
