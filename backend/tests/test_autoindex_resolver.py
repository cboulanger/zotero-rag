"""Unit tests for resolve_targets (re-validate + dedup)."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from backend.services.autoindex_key_store import AutoIndexKeyStore
from backend.services.autoindex_resolver import resolve_targets
from backend.zotero.key_validator import KeyValidation


class ResolveTargetsTest(unittest.IsolatedAsyncioTestCase):
    def _store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return AutoIndexKeyStore(Path(tmp.name) / "k.json", Fernet.generate_key().decode())

    async def test_dedup_shared_group(self):
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1", "groups/99"], read_only=True)
        v2 = KeyValidation(2, "b", ["users/2", "groups/99"], read_only=True)
        store.add("KA", v1)
        store.add("KB", v2)
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(side_effect=[v1, v2])):
            targets, issues = await resolve_targets(store)
        self.assertEqual(set(targets), {"users/1", "users/2", "groups/99"})
        self.assertEqual(issues, [])

    async def test_prunes_revoked_key(self):
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        store.add("KA", v1)
        revoked = KeyValidation(1, "a", read_only=False, reason="Key not found (revoked or expired).")
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=revoked)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(targets, {})
        self.assertEqual(len(issues), 1)
        self.assertIn("revoked", issues[0]["reason"].lower())
        self.assertEqual(store.list_metadata(), [])


if __name__ == "__main__":
    unittest.main()
