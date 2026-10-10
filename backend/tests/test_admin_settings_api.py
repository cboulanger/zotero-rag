"""Endpoint tests for GET/PUT /api/admin/settings and POST /api/admin/settings/purge-snapshots."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.dependencies import require_authorized_group_admin, get_vector_store
from backend.services.zotero_identity import ZoteroIdentity, reset_identity_cache
from backend.zotero.group_roles import reset_admin_role_cache


class AdminSettingsApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        reset_admin_role_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()
        reset_admin_role_cache()

    def _override_admin(self, identity):
        app.dependency_overrides[require_authorized_group_admin] = lambda: identity

    def test_get_returns_default_false_with_no_identity_required(self):
        r = self.client.get("/api/admin/settings")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"index_snapshots": False})

    def test_put_requires_admin(self):
        get_settings().api_host = "rag.example.com"
        get_settings().authorized_group_id = 999
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=identity)), \
             patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=False)):
            r = self.client.put(
                "/api/admin/settings", json={"index_snapshots": True},
                headers={"X-Zotero-API-Key": "K"},
            )
        self.assertEqual(r.status_code, 403)

    def test_put_as_admin_persists_and_get_reflects_it(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        r = self.client.put("/api/admin/settings", json={"index_snapshots": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"index_snapshots": True})
        r2 = self.client.get("/api/admin/settings")
        self.assertEqual(r2.json(), {"index_snapshots": True})

    def test_purge_snapshots_requires_admin(self):
        get_settings().api_host = "rag.example.com"
        get_settings().authorized_group_id = 999
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=identity)), \
             patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=False)):
            r = self.client.post(
                "/api/admin/settings/purge-snapshots",
                headers={"X-Zotero-API-Key": "K"},
            )
        self.assertEqual(r.status_code, 403)

    def test_purge_snapshots_as_admin_calls_vector_store_and_returns_counts(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        mock_store = Mock()
        mock_store.delete_snapshot_chunks.return_value = (5, 2)
        app.dependency_overrides[get_vector_store] = lambda: mock_store
        r = self.client.post("/api/admin/settings/purge-snapshots")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"deleted_chunks": 5, "deleted_attachments": 2})
        mock_store.delete_snapshot_chunks.assert_called_once()

    def test_put_preserves_default_preset_and_remote_config(self):
        """PUT /api/admin/settings only has an `index_snapshots` field in its
        request body, but write_admin_settings is a full-replace — so a naive
        `write_admin_settings(data_path, body.model_dump())` silently wipes
        default_preset/remote_config every time someone flips the
        unrelated index-snapshots checkbox."""
        from backend.services.admin_settings_store import (
            set_default_preset, update_remote_config, read_admin_settings,
        )
        set_default_preset(get_settings().data_path, "remote-mpcdf")
        update_remote_config({"MPCDF_LLM_BASE_URL": "https://x/v1"}, data_path=get_settings().data_path)
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))

        r = self.client.put("/api/admin/settings", json={"index_snapshots": True})
        self.assertEqual(r.status_code, 200)

        state = read_admin_settings(get_settings().data_path)
        self.assertTrue(state["index_snapshots"])
        self.assertEqual(state["default_preset"], "remote-mpcdf")
        self.assertEqual(state["remote_config"], {"MPCDF_LLM_BASE_URL": "https://x/v1"})


if __name__ == "__main__":
    unittest.main()
