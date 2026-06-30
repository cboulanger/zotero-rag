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
