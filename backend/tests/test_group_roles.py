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


if __name__ == "__main__":
    unittest.main()
