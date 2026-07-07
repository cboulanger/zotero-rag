"""Unit tests for resolve_targets (re-validate + dedup + embedding-key gating)."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet

from backend.services.autoindex_key_store import AutoIndexKeyStore
from backend.services.autoindex_resolver import resolve_targets
from backend.zotero.key_validator import KeyValidation


def _mock_settings(model_type: str = "remote") -> MagicMock:
    """A fake get_settings() return value with a controllable embedding model_type,
    so tests don't depend on whatever preset this machine's real .env configures."""
    settings = MagicMock()
    settings.get_hardware_preset.return_value.embedding.model_type = model_type
    return settings


class ResolveTargetsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # All existing tests assume a remote embedding provider (the only
        # config for which per-user embedding-key gating applies); override
        # per-test with another patch.object(...) call for local-preset cases.
        patcher = patch("backend.services.autoindex_resolver.get_settings",
                         return_value=_mock_settings("remote"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return AutoIndexKeyStore(Path(tmp.name) / "k.json", Fernet.generate_key().decode())

    async def test_dedup_shared_group(self):
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1", "groups/99"], read_only=True)
        v2 = KeyValidation(2, "b", ["users/2", "groups/99"], read_only=True)
        fp1 = store.add("KA", v1)
        fp2 = store.add("KB", v2)
        store.set_embedding_key(fp1, "EMB1", "KISSKI_API_KEY")
        store.set_embedding_key(fp2, "EMB2", "KISSKI_API_KEY")
        # Pre-set one fingerprint to a non-"ok" status so the assertion that
        # resolve refreshes it back to "ok" is meaningful.
        store.set_status(fp1, "stale")
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(side_effect=[v1, v2])):
            targets, issues = await resolve_targets(store)
        self.assertEqual(set(targets), {"users/1", "users/2", "groups/99"})
        self.assertEqual(issues, [])
        self.assertEqual(targets["users/1"]["fingerprint"], fp1)
        self.assertEqual(targets["users/1"]["embedding_key"], "EMB1")
        # Valid keys have their status refreshed/confirmed to "ok" after resolve.
        for meta in store.list_metadata():
            self.assertEqual(meta["last_status"], "ok")

    async def test_prunes_revoked_key(self):
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        fp1 = store.add("KA", v1)
        store.set_embedding_key(fp1, "EMB1", "KISSKI_API_KEY")
        revoked = KeyValidation(1, "a", read_only=False, reason="Key not found (revoked or expired).")
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=revoked)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(targets, {})
        self.assertEqual(len(issues), 1)
        self.assertIn("revoked", issues[0]["reason"].lower())
        self.assertTrue(issues[0]["pruned"])
        self.assertEqual(issues[0]["kind"], "zotero_key")
        self.assertEqual(store.list_metadata(), [])

    async def test_transient_error_keeps_key(self):
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1", "groups/5"], read_only=True)
        fp1 = store.add("KA", v1)
        store.set_embedding_key(fp1, "EMB1", "KISSKI_API_KEY")
        transient = KeyValidation(1, "a", read_only=False, reason="Could not reach Zotero API: boom", transient=True)
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=transient)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(set(targets), {"users/1", "groups/5"})
        self.assertEqual(len(issues), 1)
        self.assertFalse(issues[0]["pruned"])
        self.assertEqual(issues[0]["kind"], "zotero_key")
        self.assertEqual(len(store.list_metadata()), 1)
        self.assertEqual(store.list_metadata()[0]["last_status"], "transient_error")

    async def test_missing_embedding_key_skips_slug_with_issue(self):
        """A valid Zotero key with no embedding key configured is excluded from targets."""
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        store.add("KA", v1)
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=v1)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(targets, {})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["kind"], "embedding_key")
        self.assertFalse(issues[0]["pruned"])

    async def test_invalid_embedding_key_skips_slug_with_issue(self):
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        fp1 = store.add("KA", v1)
        store.set_embedding_key(fp1, "BADEMB", "KISSKI_API_KEY", status="invalid")
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=v1)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(targets, {})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["kind"], "embedding_key")

    async def test_rate_limited_embedding_key_skips_slug(self):
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        fp1 = store.add("KA", v1)
        store.set_embedding_key(fp1, "EMB1", "KISSKI_API_KEY")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        store.set_embedding_key_status(fp1, "rate_limited", rate_limit_until=future)
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=v1)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(targets, {})
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["kind"], "embedding_key")

    async def test_expired_rate_limit_allows_slug(self):
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        fp1 = store.add("KA", v1)
        store.set_embedding_key(fp1, "EMB1", "KISSKI_API_KEY")
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        store.set_embedding_key_status(fp1, "rate_limited", rate_limit_until=past)
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=v1)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(set(targets), {"users/1"})
        self.assertEqual(targets["users/1"]["embedding_key"], "EMB1")
        self.assertEqual(targets["users/1"]["fingerprint"], fp1)
        self.assertEqual(issues, [])

    async def test_unverified_embedding_key_is_allowed(self):
        """An embedding key that hasn't been actively verified yet is usable."""
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        fp1 = store.add("KA", v1)
        store.set_embedding_key(fp1, "EMB1", "KISSKI_API_KEY", status="unverified")
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=v1)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(set(targets), {"users/1"})
        self.assertEqual(targets["users/1"]["embedding_key"], "EMB1")
        self.assertEqual(issues, [])

    async def test_dedup_shared_group_survives_one_blocked_owner(self):
        """A shared slug still resolves via the owner with a valid embedding key,
        even when the other owner's embedding key is blocked."""
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1", "groups/99"], read_only=True)
        v2 = KeyValidation(2, "b", ["users/2", "groups/99"], read_only=True)
        fp1 = store.add("KA", v1)
        fp2 = store.add("KB", v2)
        store.set_embedding_key(fp1, "EMB1", "KISSKI_API_KEY")
        store.set_embedding_key(fp2, "BADEMB", "KISSKI_API_KEY", status="invalid")
        with patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(side_effect=[v1, v2])):
            targets, issues = await resolve_targets(store)
        self.assertEqual(set(targets), {"users/1", "groups/99"})
        self.assertEqual(targets["groups/99"]["fingerprint"], fp1)
        self.assertNotIn("users/2", targets)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["kind"], "embedding_key")
        self.assertEqual(issues[0]["fingerprint"], fp2)

    async def test_local_model_type_bypasses_embedding_key_gating(self):
        """A local (non-remote) embedding preset has no API key at all, so
        per-user gating must not exclude slugs for lacking one."""
        store = self._store()
        v1 = KeyValidation(1, "a", ["users/1"], read_only=True)
        store.add("KA", v1)  # no embedding key configured
        with patch("backend.services.autoindex_resolver.get_settings",
                   return_value=_mock_settings("local")), \
             patch("backend.services.autoindex_resolver.validate_key",
                   new=AsyncMock(return_value=v1)):
            targets, issues = await resolve_targets(store)
        self.assertEqual(set(targets), {"users/1"})
        self.assertIsNone(targets["users/1"]["embedding_key"])
        self.assertIsNone(targets["users/1"]["embedding_key_name"])
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
