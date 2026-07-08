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


def _validation_with_labels():
    return KeyValidation(
        user_id=39226, username="cboulanger",
        targets=["users/39226", "groups/456"],
        target_names={"groups/456": "Alpha Group"},
        target_owners={"groups/456": 111},
        read_only=True,
    )


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

    def test_re_adding_zotero_key_preserves_embedding_key(self):
        """Re-registering the same Zotero key (e.g. to refresh validation)
        must not wipe an already-stored embedding key on that entry."""
        fp = self.store.add("ZOTKEY", _validation())
        self.store.set_embedding_key(fp, "EMBKEY", "KISSKI_API_KEY")
        fp2 = self.store.add("ZOTKEY", _validation())
        self.assertEqual(fp2, fp)
        key_name, key = self.store.get_decrypted_embedding_key(fp)
        self.assertEqual(key_name, "KISSKI_API_KEY")
        self.assertEqual(key, "EMBKEY")
        meta = self.store.list_metadata()[0]
        self.assertTrue(meta["has_embedding_key"])
        self.assertEqual(meta["embedding_key_status"], "ok")

    def test_rotating_zotero_key_replaces_old_entry_for_same_user(self):
        """A user submitting a NEW Zotero API key (e.g. after regenerating it —
        a different key value means a different fingerprint) must end up with
        exactly one registered entry, not two side by side."""
        v1 = _validation()
        fp1 = self.store.add("OLDKEY", v1)
        v2 = KeyValidation(user_id=v1.user_id, username=v1.username,
                            targets=["users/39226", "groups/999"], read_only=True)
        fp2 = self.store.add("NEWKEY", v2)

        self.assertNotEqual(fp1, fp2)
        metas = self.store.list_metadata()
        self.assertEqual(len(metas), 1)
        self.assertEqual(metas[0]["fingerprint"], fp2)
        self.assertEqual(metas[0]["targets"], ["users/39226", "groups/999"])
        self.assertIsNone(self.store.get_decrypted(fp1))
        self.assertEqual(self.store.get_decrypted(fp2), "NEWKEY")

    def test_rotating_zotero_key_preserves_embedding_key(self):
        """The embedding key isn't tied to the Zotero key — rotating the
        Zotero key must not force the user to re-enter their embedding key."""
        v1 = _validation()
        fp1 = self.store.add("OLDKEY", v1)
        self.store.set_embedding_key(fp1, "EMBKEY", "KISSKI_API_KEY")

        v2 = KeyValidation(user_id=v1.user_id, username=v1.username,
                            targets=["users/39226"], read_only=True)
        fp2 = self.store.add("NEWKEY", v2)

        key_name, key = self.store.get_decrypted_embedding_key(fp2)
        self.assertEqual(key_name, "KISSKI_API_KEY")
        self.assertEqual(key, "EMBKEY")

    def test_adding_key_for_different_user_does_not_remove_others(self):
        v1 = _validation()
        self.store.add("KEY1", v1)
        v2 = KeyValidation(user_id=999, username="other", targets=["users/999"], read_only=True)
        self.store.add("KEY2", v2)

        self.assertEqual(len(self.store.list_metadata()), 2)

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

    def test_list_metadata_exposes_rate_limit_until(self):
        fp = self.store.add("ZOTKEY", _validation())
        self.store.set_embedding_key(fp, "EMBKEY", "KISSKI_API_KEY")
        self.store.set_embedding_key_status(fp, "rate_limited", rate_limit_until="2026-01-01T00:00:00+00:00")
        meta = self.store.list_metadata()[0]
        self.assertEqual(meta["embedding_key_rate_limit_until"], "2026-01-01T00:00:00+00:00")

    def test_embedding_key_ciphertext_not_in_metadata(self):
        fp = self.store.add("ZOTKEY", _validation())
        self.store.set_embedding_key(fp, "EMBKEY", "KISSKI_API_KEY")
        meta = self.store.list_metadata()[0]
        self.assertNotIn("embedding_key_ciphertext", meta)
        self.assertNotIn("EMBKEY", str(meta))

    def test_get_target_labels_returns_names_and_owners_from_validation(self):
        self.store.add("SECRETKEY", _validation_with_labels())
        labels = self.store.get_target_labels()
        self.assertEqual(labels, {"groups/456": ("Alpha Group", 111)})

    def test_get_target_labels_empty_when_no_names_captured(self):
        self.store.add("SECRETKEY", _validation())
        self.assertEqual(self.store.get_target_labels(), {})

    def test_get_target_labels_merges_across_entries_first_wins(self):
        self.store.add("KEY1", _validation_with_labels())
        v2 = KeyValidation(
            user_id=999, username="other", targets=["groups/456"],
            target_names={"groups/456": "Duplicate Name"},
            target_owners={"groups/456": 222},
            read_only=True,
        )
        self.store.add("KEY2", v2)
        labels = self.store.get_target_labels()
        self.assertEqual(labels, {"groups/456": ("Alpha Group", 111)})

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
