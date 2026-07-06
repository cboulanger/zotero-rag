"""Unit tests for backend.dependencies.resolve_zotero_identity, tested in
isolation via a throwaway FastAPI app (not backend.main.app)."""

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from backend.config.settings import get_settings, reset_settings
from backend.dependencies import resolve_zotero_identity
from backend.services.zotero_identity import reset_identity_cache
from backend.zotero.key_validator import KeyValidation


def _make_probe_app() -> FastAPI:
    app = FastAPI()

    @app.get("/probe")
    async def probe(identity=Depends(resolve_zotero_identity)):
        if identity is None:
            return {"identity": None}
        return {"user_id": identity.user_id, "targets": identity.targets}

    return app


class ResolveZoteroIdentityTest(unittest.TestCase):
    def setUp(self):
        reset_settings()
        reset_identity_cache()
        self.client = TestClient(_make_probe_app())

    def tearDown(self):
        reset_settings()
        reset_identity_cache()

    def test_loopback_skips_auth_entirely(self):
        get_settings().api_host = "localhost"
        r = self.client.get("/probe")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["identity"])

    def test_remote_missing_key_rejected(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        r = self.client.get("/probe")
        self.assertEqual(r.status_code, 401)

    def test_legacy_shared_key_no_longer_accepted(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        r = self.client.get("/probe", headers={"X-API-Key": "SHARED"})
        self.assertEqual(r.status_code, 401)

    def test_remote_valid_gated_key_returns_identity(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        validation = KeyValidation(user_id=1, username="u", targets=["users/1", "groups/999"], read_only=True)
        with patch("backend.services.zotero_identity.validate_key", new=AsyncMock(return_value=validation)):
            r = self.client.get("/probe", headers={"X-Zotero-API-Key": "KEY"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["user_id"], 1)

    def test_remote_valid_but_ungated_key_rejected(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        validation = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        with patch("backend.services.zotero_identity.validate_key", new=AsyncMock(return_value=validation)):
            r = self.client.get("/probe", headers={"X-Zotero-API-Key": "KEY"})
        self.assertEqual(r.status_code, 403)

    def test_remote_revoked_key_rejected_with_401(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        validation = KeyValidation(None, None, read_only=False, reason="revoked", transient=False)
        with patch("backend.services.zotero_identity.validate_key", new=AsyncMock(return_value=validation)):
            r = self.client.get("/probe", headers={"X-Zotero-API-Key": "KEY"})
        self.assertEqual(r.status_code, 401)

    def test_remote_zotero_unreachable_no_cache_returns_503(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        validation = KeyValidation(None, None, read_only=False, reason="down", transient=True)
        with patch("backend.services.zotero_identity.validate_key", new=AsyncMock(return_value=validation)):
            r = self.client.get("/probe", headers={"X-Zotero-API-Key": "KEY"})
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
