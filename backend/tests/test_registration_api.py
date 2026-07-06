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


if __name__ == "__main__":
    unittest.main()
