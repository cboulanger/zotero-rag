"""Endpoint tests for backend.api.registration authorization:
- GET /api/registrations is filtered to the caller's own accessible libraries
- POST /api/register 403s when the caller can't access the target library_id
"""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.services.registration_service import RegistrationService
from backend.services.zotero_identity import ZoteroIdentity, reset_identity_cache
import backend.dependencies as dependencies


class RegistrationAuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        s.registrations_path = Path(self.tmp.name) / "system" / "registrations.json"
        self.client = TestClient(app)
        RegistrationService(s.registrations_path).register("u1", "Mine", 1, "u")
        RegistrationService(s.registrations_path).register("u2", "Someone Else's", 2, "other")

    def tearDown(self):
        app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()

    def _set_identity(self, identity):
        app.dependency_overrides[dependencies.get_zotero_identity] = lambda: identity

    def test_get_registrations_filters_to_own_targets(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.get("/api/registrations")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.json().keys()), {"u1"})

    def test_get_registrations_unrestricted_when_no_identity(self):
        self._set_identity(None)
        r = self.client.get("/api/registrations")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.json().keys()), {"u1", "u2"})

    def test_register_outside_targets_is_403(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.post(
            "/api/register",
            json={"library_id": "u2", "library_name": "Attempt", "user_id": 1, "username": "u"},
        )
        self.assertEqual(r.status_code, 403)

    def test_register_within_targets_succeeds(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.post(
            "/api/register",
            json={"library_id": "u1", "library_name": "Mine Updated", "user_id": 1, "username": "u"},
        )
        self.assertEqual(r.status_code, 200)

    def test_register_ignores_body_user_id_uses_identity(self):
        # setUp already registered user_id=1/"u" for library "u1"; RegistrationService.register()
        # dedups new entries by user_id, so this identity must use a user_id not already present
        # on "u1" for the new-entry path (and thus the body-vs-identity divergence) to be exercised.
        self._set_identity(ZoteroIdentity(user_id=7, username="real_owner", targets=["users/1"]))
        r = self.client.post(
            "/api/register",
            json={"library_id": "u1", "library_name": "Mine", "user_id": 999999, "username": "spoofed_victim"},
        )
        self.assertEqual(r.status_code, 200)
        # Fetch back via the (already-fixed, identity-filtered) GET /api/registrations
        r2 = self.client.get("/api/registrations")
        entry = r2.json()["u1"]
        usernames = {u["username"] for u in entry["users"]}
        user_ids = {u["user_id"] for u in entry["users"]}
        self.assertIn("real_owner", usernames)
        self.assertNotIn("spoofed_victim", usernames)
        self.assertIn(7, user_ids)
        self.assertNotIn(999999, user_ids)


if __name__ == "__main__":
    unittest.main()
