"""Unit tests for backend.services.zotero_identity (the Part 1.3 validation cache)."""

import unittest
from unittest.mock import AsyncMock, patch

from backend.services.zotero_identity import ZoteroIdentity, ZoteroIdentityCache
from backend.zotero.key_validator import KeyValidation


class ZoteroIdentityCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_caches_successful_validation(self):
        validation = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        mock_validate = AsyncMock(return_value=validation)
        cache = ZoteroIdentityCache(ttl_seconds=60)
        with patch("backend.services.zotero_identity.validate_key", new=mock_validate):
            first = await cache.resolve("KEY")
            second = await cache.resolve("KEY")
        self.assertIs(first, validation)
        self.assertIs(second, validation)
        mock_validate.assert_awaited_once()

    async def test_expired_entry_revalidates(self):
        old = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        new = KeyValidation(user_id=1, username="u", targets=["users/1", "groups/2"], read_only=True)
        mock_validate = AsyncMock(side_effect=[old, new])
        cache = ZoteroIdentityCache(ttl_seconds=0)
        with patch("backend.services.zotero_identity.validate_key", new=mock_validate):
            first = await cache.resolve("KEY")
            second = await cache.resolve("KEY")
        self.assertIs(first, old)
        self.assertIs(second, new)
        self.assertEqual(mock_validate.await_count, 2)

    async def test_transient_failure_serves_stale_cache(self):
        good = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        transient_failure = KeyValidation(None, None, read_only=False, reason="down", transient=True)
        mock_validate = AsyncMock(side_effect=[good, transient_failure])
        cache = ZoteroIdentityCache(ttl_seconds=0)
        with patch("backend.services.zotero_identity.validate_key", new=mock_validate):
            first = await cache.resolve("KEY")
            second = await cache.resolve("KEY")
        self.assertIs(first, good)
        self.assertIs(second, good)  # stale cache served instead of the transient failure

    async def test_hard_failure_not_masked_by_stale_cache(self):
        good = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        revoked = KeyValidation(None, None, read_only=False, reason="revoked", transient=False)
        mock_validate = AsyncMock(side_effect=[good, revoked])
        cache = ZoteroIdentityCache(ttl_seconds=0)
        with patch("backend.services.zotero_identity.validate_key", new=mock_validate):
            first = await cache.resolve("KEY")
            second = await cache.resolve("KEY")
        self.assertIs(first, good)
        self.assertIs(second, revoked)  # a hard failure always overwrites the cache

    async def test_no_cache_entry_and_transient_failure_propagates(self):
        transient_failure = KeyValidation(None, None, read_only=False, reason="down", transient=True)
        mock_validate = AsyncMock(return_value=transient_failure)
        cache = ZoteroIdentityCache(ttl_seconds=60)
        with patch("backend.services.zotero_identity.validate_key", new=mock_validate):
            result = await cache.resolve("KEY")
        self.assertIs(result, transient_failure)

    def test_zotero_identity_is_a_plain_value_object(self):
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        self.assertEqual(identity.user_id, 1)
        self.assertEqual(identity.username, "u")
        self.assertEqual(identity.targets, ["users/1"])


if __name__ == "__main__":
    unittest.main()
