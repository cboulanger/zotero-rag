"""Unit tests for backend.zotero.group_roles."""

import unittest

import aiohttp
from aioresponses import aioresponses

from backend.zotero.group_roles import AdminRoleCache, ZOTERO_API_BASE, is_group_admin


class IsGroupAdminTest(unittest.IsolatedAsyncioTestCase):
    async def test_true_when_meta_is_admin_true(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/groups/999", payload={"meta": {"isAdmin": True}})
            result = await is_group_admin(user_id=1, group_id=999, api_key="KEY")
        self.assertTrue(result)

    async def test_false_when_meta_is_admin_false_or_absent(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/groups/999", payload={"meta": {}})
            result = await is_group_admin(user_id=1, group_id=999, api_key="KEY")
        self.assertFalse(result)

    async def test_false_on_non_200(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/groups/999", status=403)
            result = await is_group_admin(user_id=1, group_id=999, api_key="KEY")
        self.assertFalse(result)

    async def test_false_on_client_error(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/groups/999", exception=aiohttp.ClientConnectionError("boom"))
            result = await is_group_admin(user_id=1, group_id=999, api_key="KEY")
        self.assertFalse(result)


class AdminRoleCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_serves_cached_result_within_ttl(self):
        """Only one response is registered — if the cache failed to hold and
        a second real HTTP call were made, aioresponses would raise for the
        unmatched request, failing this test."""
        cache = AdminRoleCache(ttl_seconds=60)
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/groups/999", payload={"meta": {"isAdmin": True}})
            first = await cache.is_admin(1, 999, "KEY")
            second = await cache.is_admin(1, 999, "KEY")
        self.assertTrue(first)
        self.assertTrue(second)

    async def test_expires_after_ttl(self):
        """ttl_seconds=0 means every call is a fresh lookup — both registered
        responses must be consumed."""
        cache = AdminRoleCache(ttl_seconds=0)
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/groups/999", payload={"meta": {"isAdmin": True}})
            m.get(f"{ZOTERO_API_BASE}/groups/999", payload={"meta": {"isAdmin": True}})
            first = await cache.is_admin(1, 999, "KEY")
            second = await cache.is_admin(1, 999, "KEY")
        self.assertTrue(first)
        self.assertTrue(second)

    async def test_different_keys_cached_independently(self):
        """Two different (user_id, group_id) pairs must not share a cache
        entry — a bug collapsing the cache key would otherwise let an
        admin-of-group-A's cached True leak into an unrelated group-B check."""
        cache = AdminRoleCache(ttl_seconds=60)
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/groups/111", payload={"meta": {"isAdmin": True}})
            m.get(f"{ZOTERO_API_BASE}/groups/222", payload={"meta": {}})
            admin_result = await cache.is_admin(1, 111, "KEY")
            non_admin_result = await cache.is_admin(1, 222, "KEY")
        self.assertTrue(admin_result)
        self.assertFalse(non_admin_result)


class ModuleSingletonTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        from backend.zotero.group_roles import reset_admin_role_cache
        reset_admin_role_cache()

    async def test_get_admin_role_cache_returns_same_instance(self):
        from backend.zotero.group_roles import get_admin_role_cache
        self.assertIs(get_admin_role_cache(), get_admin_role_cache())

    async def test_reset_admin_role_cache_clears_cached_entries(self):
        from backend.zotero.group_roles import get_admin_role_cache, reset_admin_role_cache
        cache = get_admin_role_cache()
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/groups/999", payload={"meta": {"isAdmin": True}})
            await cache.is_admin(1, 999, "KEY")  # populates the cache
        reset_admin_role_cache()
        # After reset, the same call must hit the network again (a second
        # registered response is required, or aioresponses raises for the
        # unmatched request — proving the cache was actually cleared).
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/groups/999", payload={"meta": {"isAdmin": True}})
            result = await cache.is_admin(1, 999, "KEY")
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
