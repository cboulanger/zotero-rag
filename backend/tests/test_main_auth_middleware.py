"""End-to-end tests for the api_key_middleware wiring in backend.main.

Uses the real app (TestClient(app)) and an already-existing, unmodified
route (GET /api/libraries) purely to exercise the middleware's status-code
behavior. Response *content* filtering is covered later once
backend/api/libraries.py itself is wired (see test_libraries_api.py, a
later task not yours)."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.services.zotero_identity import reset_identity_cache
from backend.zotero.key_validator import KeyValidation


class AuthMiddlewareTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        # GET /api/libraries (the unmodified route used below purely to probe
        # middleware status codes) depends on app.state.vector_store, which is
        # only populated by the app's lifespan — and TestClient(app) without a
        # `with` block never runs lifespan. Stub it directly so requests that
        # are meant to reach the handler don't 500 for this unrelated reason.
        app.state.vector_store = MagicMock()
        app.state.vector_store.get_all_library_metadata.return_value = []
        self.client = TestClient(app)

    def tearDown(self):
        del app.state.vector_store
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()

    def test_loopback_reaches_handler_without_any_key(self):
        r = self.client.get("/api/libraries")
        self.assertEqual(r.status_code, 200)

    def test_remote_without_any_key_is_401(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        r = self.client.get("/api/libraries")
        self.assertEqual(r.status_code, 401)

    def test_remote_with_gated_zotero_key_reaches_handler(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        validation = KeyValidation(user_id=1, username="u", targets=["users/1", "groups/999"], read_only=True)
        with patch("backend.services.zotero_identity.validate_key", new=AsyncMock(return_value=validation)):
            r = self.client.get("/api/libraries", headers={"X-Zotero-API-Key": "KEY"})
        self.assertEqual(r.status_code, 200)

    def test_health_and_version_exempt_even_on_remote_host(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/api/version").status_code, 200)


if __name__ == "__main__":
    unittest.main()
