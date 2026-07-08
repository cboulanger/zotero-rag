"""Unit tests for backend.zotero.key_validator."""

import unittest

import aiohttp
from aioresponses import aioresponses

from backend.zotero.key_validator import validate_key, ZOTERO_API_BASE


class KeyValidatorTest(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_user_library(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/RO", payload={
                "key": "RO", "userID": 39226, "username": "cboulanger",
                "access": {"user": {"library": True, "files": True, "notes": True}},
            })
            res = await validate_key("RO")
        self.assertTrue(res.read_only)
        self.assertEqual(res.user_id, 39226)
        self.assertEqual(res.targets, ["users/39226"])

    async def test_write_scope_rejected(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/RW", payload={
                "key": "RW", "userID": 39226, "username": "cboulanger",
                "access": {"user": {"library": True, "write": True}},
            })
            res = await validate_key("RW")
        self.assertFalse(res.read_only)
        self.assertIsNotNone(res.reason)
        self.assertEqual(res.targets, [])

    async def test_group_write_scope_rejected(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/GW", payload={
                "key": "GW", "userID": 1, "username": "u",
                "access": {"user": {"library": True},
                           "groups": {"all": {"library": True, "write": True}}},
            })
            res = await validate_key("GW")
        self.assertFalse(res.read_only)

    async def test_groups_all_enumerated(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/GA", payload={
                "key": "GA", "userID": 39226, "username": "cboulanger",
                "access": {"user": {"library": True},
                           "groups": {"all": {"library": True}}},
            })
            m.get(f"{ZOTERO_API_BASE}/users/39226/groups", payload=[
                {"id": 456}, {"id": 789},
            ])
            res = await validate_key("GA")
        self.assertTrue(res.read_only)
        self.assertEqual(set(res.targets), {"users/39226", "groups/456", "groups/789"})

    async def test_groups_all_enumerated_captures_names_and_owners(self):
        """The group-enumeration response carries each group's name/owner
        under `data` — capture them instead of discarding everything but
        `id`, so admin views can label auto-index-only group libraries."""
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/GA2", payload={
                "key": "GA2", "userID": 39226, "username": "cboulanger",
                "access": {"user": {"library": True},
                           "groups": {"all": {"library": True}}},
            })
            m.get(f"{ZOTERO_API_BASE}/users/39226/groups", payload=[
                {"id": 456, "data": {"id": 456, "name": "Alpha Group", "owner": 111}},
                {"id": 789, "data": {"id": 789, "name": "Beta Group", "owner": 222}},
            ])
            res = await validate_key("GA2")
        self.assertEqual(res.target_names, {"groups/456": "Alpha Group", "groups/789": "Beta Group"})
        self.assertEqual(res.target_owners, {"groups/456": 111, "groups/789": 222})

    async def test_specific_group(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/SG", payload={
                "key": "SG", "userID": 1, "username": "u",
                "access": {"groups": {"123": {"library": True}}},
            })
            m.get(f"{ZOTERO_API_BASE}/groups/123", payload={
                "id": 123, "data": {"id": 123, "name": "Specific Group", "owner": 55},
            })
            res = await validate_key("SG")
        self.assertTrue(res.read_only)
        self.assertEqual(res.targets, ["groups/123"])

    async def test_specific_group_captures_name_and_owner(self):
        """A key scoped to specific individual groups (not `groups.all`) is a
        common real-world case — it must still get labeled, not just keys
        granting blanket access to every group the user belongs to."""
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/SG2", payload={
                "key": "SG2", "userID": 1, "username": "u",
                "access": {"groups": {"123": {"library": True}, "456": {"library": True}}},
            })
            m.get(f"{ZOTERO_API_BASE}/groups/123", payload={
                "id": 123, "data": {"id": 123, "name": "Specific Group", "owner": 55},
            })
            m.get(f"{ZOTERO_API_BASE}/groups/456", payload={
                "id": 456, "data": {"id": 456, "name": "Another Specific Group", "owner": 66},
            })
            res = await validate_key("SG2")
        self.assertEqual(set(res.targets), {"groups/123", "groups/456"})
        self.assertEqual(res.target_names, {"groups/123": "Specific Group", "groups/456": "Another Specific Group"})
        self.assertEqual(res.target_owners, {"groups/123": 55, "groups/456": 66})

    async def test_specific_group_detail_fetch_failure_falls_back_to_slug(self):
        """A transient failure fetching one group's details must not crash
        validation or drop the group from targets — it just stays unlabeled."""
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/SG3", payload={
                "key": "SG3", "userID": 1, "username": "u",
                "access": {"groups": {"789": {"library": True}}},
            })
            m.get(f"{ZOTERO_API_BASE}/groups/789", status=500)
            res = await validate_key("SG3")
        self.assertEqual(res.targets, ["groups/789"])
        self.assertEqual(res.target_names, {})

    async def test_revoked_key_404(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/GONE", status=404)
            res = await validate_key("GONE")
        self.assertFalse(res.read_only)
        self.assertIn("revoked", res.reason.lower())
        self.assertFalse(res.transient)

    async def test_http_500_is_transient(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/X", status=500)
            res = await validate_key("X")
        self.assertFalse(res.read_only)
        self.assertTrue(res.transient)

    async def test_network_error(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/NET", exception=aiohttp.ClientError("boom"))
            res = await validate_key("NET")
        self.assertFalse(res.read_only)
        self.assertIn("zotero", res.reason.lower())
        self.assertEqual(res.targets, [])
        self.assertTrue(res.transient)

    async def test_network_error_is_transient(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/X", exception=aiohttp.ClientError("boom"))
            res = await validate_key("X")
        self.assertFalse(res.read_only)
        self.assertTrue(res.transient)

    async def test_no_readable_library(self):
        with aioresponses() as m:
            m.get(f"{ZOTERO_API_BASE}/keys/NONE", payload={
                "key": "NONE", "userID": 39226, "username": "cboulanger",
                "access": {"user": {}},
            })
            res = await validate_key("NONE")
        self.assertFalse(res.read_only)
        self.assertIn("no readable library", res.reason.lower())
        self.assertEqual(res.targets, [])


if __name__ == "__main__":
    unittest.main()
