"""Unit tests for backend.services.access_gate (Part 2 gate, Part 4 loopback
exception, and the per-library enforcement helper used by route handlers)."""

import unittest

from fastapi import HTTPException

from backend.config.settings import Settings
from backend.services.access_gate import (
    assert_can_access,
    assert_safe_to_start,
    is_gate_configured,
    is_loopback,
    passes_gate,
)
from backend.services.zotero_identity import ZoteroIdentity


class LoopbackTest(unittest.TestCase):
    def test_localhost_is_loopback(self):
        self.assertTrue(is_loopback(Settings(api_host="localhost")))

    def test_127_0_0_1_is_loopback(self):
        self.assertTrue(is_loopback(Settings(api_host="127.0.0.1")))

    def test_fqdn_is_not_loopback(self):
        self.assertFalse(is_loopback(Settings(api_host="rag.example.com")))


class GateConfiguredTest(unittest.TestCase):
    def test_unconfigured(self):
        self.assertFalse(is_gate_configured(Settings()))

    def test_group_configured(self):
        self.assertTrue(is_gate_configured(Settings(authorized_group_id=1)))

    def test_allowlist_configured(self):
        self.assertTrue(is_gate_configured(Settings(authorized_user_ids=[1])))


class PassesGateTest(unittest.TestCase):
    def test_group_member_passes(self):
        settings = Settings(authorized_group_id=999)
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1", "groups/999"])
        self.assertTrue(passes_gate(identity, settings))

    def test_non_member_without_allowlist_fails(self):
        settings = Settings(authorized_group_id=999)
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        self.assertFalse(passes_gate(identity, settings))

    def test_allowlisted_user_passes_even_without_group_membership(self):
        settings = Settings(authorized_group_id=999, authorized_user_ids=[1])
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        self.assertTrue(passes_gate(identity, settings))

    def test_neither_configured_fails_closed(self):
        settings = Settings()
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1", "groups/999"])
        self.assertFalse(passes_gate(identity, settings))


class AssertSafeToStartTest(unittest.TestCase):
    def test_loopback_always_safe(self):
        assert_safe_to_start(Settings(api_host="localhost"))  # must not raise

    def test_remote_without_gate_raises(self):
        with self.assertRaises(RuntimeError):
            assert_safe_to_start(Settings(api_host="rag.example.com"))

    def test_remote_with_group_configured_is_safe(self):
        assert_safe_to_start(Settings(api_host="rag.example.com", authorized_group_id=999))

    def test_remote_with_allowlist_configured_is_safe(self):
        assert_safe_to_start(Settings(api_host="rag.example.com", authorized_user_ids=[1]))


class AssertCanAccessTest(unittest.TestCase):
    def test_none_identity_always_allowed(self):
        assert_can_access(None, "users/1")  # must not raise

    def test_identity_with_target_allowed(self):
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        assert_can_access(identity, "users/1")  # must not raise

    def test_identity_without_target_rejected(self):
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        with self.assertRaises(HTTPException) as ctx:
            assert_can_access(identity, "users/2")
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
