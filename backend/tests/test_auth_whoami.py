"""Tests for GET /api/auth/whoami — used by the plugin's Preferences pane and
setup wizard to validate a Zotero API key and show the caller's identity."""

import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.services.zotero_identity import reset_identity_cache
from backend.zotero.key_validator import KeyValidation


class AuthWhoamiTest(unittest.TestCase):
    def setUp(self):
        reset_settings()
        reset_identity_cache()
        self.client = TestClient(app)

    def tearDown(self):
        reset_settings()
        reset_identity_cache()

    def test_loopback_reports_loopback_true(self):
        r = self.client.get("/api/auth/whoami")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"authorized": True, "loopback": True})

    def test_remote_valid_gated_key_returns_identity(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        validation = KeyValidation(user_id=1, username="alice", targets=["users/1", "groups/999"], read_only=True)
        with patch("backend.services.zotero_identity.validate_key", new=AsyncMock(return_value=validation)):
            r = self.client.get("/api/auth/whoami", headers={"X-Zotero-API-Key": "KEY"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {
            "authorized": True,
            "loopback": False,
            "user_id": 1,
            "username": "alice",
            "targets": ["users/1", "groups/999"],
        })

    def test_remote_missing_key_is_401(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        r = self.client.get("/api/auth/whoami")
        self.assertEqual(r.status_code, 401)

    def test_remote_ungated_key_is_403(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        validation = KeyValidation(user_id=1, username="alice", targets=["users/1"], read_only=True)
        with patch("backend.services.zotero_identity.validate_key", new=AsyncMock(return_value=validation)):
            r = self.client.get("/api/auth/whoami", headers={"X-Zotero-API-Key": "KEY"})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
