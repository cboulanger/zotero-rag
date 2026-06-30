"""Endpoint tests for /api/autoindex/keys."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.zotero.key_validator import KeyValidation


class AutoIndexApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        s = get_settings()
        s.api_key = None  # disable auth middleware for the test
        s.autoindex_secret = Fernet.generate_key().decode()
        s.autoindex_keys_path = Path(self.tmp.name) / "autoindex_keys.json"
        self.client = TestClient(app)

    def tearDown(self):
        self.tmp.cleanup()
        reset_settings()

    def test_post_accepts_read_only_key(self):
        validation = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        with patch("backend.api.autoindex.validate_key", new=AsyncMock(return_value=validation)):
            r = self.client.post("/api/autoindex/keys", json={"api_key": "RO"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["targets"], ["users/1"])
        r2 = self.client.get("/api/autoindex/keys")
        self.assertNotIn("RO", r2.text)

    def test_post_rejects_write_key(self):
        validation = KeyValidation(user_id=1, username="u", read_only=False,
                                   reason="This key has write access.")
        with patch("backend.api.autoindex.validate_key", new=AsyncMock(return_value=validation)):
            r = self.client.post("/api/autoindex/keys", json={"api_key": "RW"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("write access", r.json()["detail"])

    def test_delete_removes_key(self):
        validation = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        with patch("backend.api.autoindex.validate_key", new=AsyncMock(return_value=validation)):
            self.client.post("/api/autoindex/keys", json={"api_key": "RO"})
            r = self.client.request("DELETE", "/api/autoindex/keys", json={"api_key": "RO"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["removed"])

    def test_disabled_without_secret(self):
        get_settings().autoindex_secret = None
        r = self.client.get("/api/autoindex/keys")
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
