"""Endpoint tests for GET /api/autoindex/keys: results are filtered to the
caller's own submitted key(s), not every user's."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.services.zotero_identity import ZoteroIdentity, reset_identity_cache
from backend.zotero.key_validator import KeyValidation
import backend.dependencies as dependencies


class AutoIndexKeysAuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        s.autoindex_secret = Fernet.generate_key().decode()
        s.autoindex_keys_path = Path(self.tmp.name) / "autoindex_keys.json"
        self.client = TestClient(app)

        v1 = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        v2 = KeyValidation(user_id=2, username="other", targets=["users/2"], read_only=True)
        with patch("backend.api.autoindex.validate_key", new=AsyncMock(side_effect=[v1, v2])):
            self.client.post("/api/autoindex/keys", json={"api_key": "KEY1"})
            self.client.post("/api/autoindex/keys", json={"api_key": "KEY2"})

    def tearDown(self):
        app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()

    def _set_identity(self, identity):
        app.dependency_overrides[dependencies.get_zotero_identity] = lambda: identity

    def test_list_keys_filters_to_own_user_id(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.get("/api/autoindex/keys")
        self.assertEqual(r.status_code, 200)
        user_ids = {k["user_id"] for k in r.json()["keys"]}
        self.assertEqual(user_ids, {1})

    def test_list_keys_unrestricted_when_no_identity(self):
        self._set_identity(None)
        r = self.client.get("/api/autoindex/keys")
        self.assertEqual(r.status_code, 200)
        user_ids = {k["user_id"] for k in r.json()["keys"]}
        self.assertEqual(user_ids, {1, 2})


if __name__ == "__main__":
    unittest.main()
