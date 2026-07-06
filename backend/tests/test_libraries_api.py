"""Endpoint tests for backend.api.libraries authorization:
- GET /api/libraries is filtered to the caller's own targets
- DELETE .../index, POST .../sync-deletions, DELETE .../items/{key}/chunks
  all 403 for a library outside the caller's targets
"""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.db.vector_store import VectorStore
from backend.models.library import LibraryIndexMetadata
from backend.services.registration_service import RegistrationService
from backend.services.zotero_identity import ZoteroIdentity, reset_identity_cache
import backend.dependencies as dependencies


class LibrariesAuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        # `data_path` is only used to *derive* other paths at Settings construction
        # time (see set_derived_paths); mutating it post-construction does not
        # move already-resolved paths like registrations_path. Without this, the
        # RegistrationService calls below would write into the real project's
        # data/system/registrations.json instead of the temp sandbox.
        s.registrations_path = Path(self.tmp.name) / "system" / "registrations.json"
        s.testing = True
        self.client = TestClient(app)

        vector_store = VectorStore(
            storage_path=Path(self.tmp.name) / "qdrant",
            embedding_dim=8,
            embedding_model_name="test-model",
        )
        vector_store.update_library_metadata(
            LibraryIndexMetadata(library_id="users/1", library_type="user", library_name="Mine")
        )
        vector_store.update_library_metadata(
            LibraryIndexMetadata(library_id="users/2", library_type="user", library_name="Someone Else's")
        )
        app.state.vector_store = vector_store

        RegistrationService(s.registrations_path).register("users/1", "Mine", 1, "u")
        RegistrationService(s.registrations_path).register("users/2", "Someone Else's", 2, "other")

    def tearDown(self):
        app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()

    def _set_identity(self, identity):
        app.dependency_overrides[dependencies.get_zotero_identity] = lambda: identity

    def test_list_filters_to_own_targets(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.get("/api/libraries")
        self.assertEqual(r.status_code, 200)
        ids = [lib["library_id"] for lib in r.json()]
        self.assertEqual(ids, ["users/1"])

    def test_list_unrestricted_when_no_identity(self):
        self._set_identity(None)
        r = self.client.get("/api/libraries")
        self.assertEqual(r.status_code, 200)
        ids = {lib["library_id"] for lib in r.json()}
        self.assertEqual(ids, {"users/1", "users/2"})

    def test_delete_index_outside_targets_is_403(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.delete("/api/libraries/users/2/index")
        self.assertEqual(r.status_code, 403)

    def test_delete_index_within_targets_succeeds(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.delete("/api/libraries/users/1/index")
        self.assertEqual(r.status_code, 200)

    def test_sync_deletions_outside_targets_is_403(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.post("/api/libraries/users/2/sync-deletions", json={"current_item_keys": []})
        self.assertEqual(r.status_code, 403)

    def test_clear_item_chunks_outside_targets_is_403(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.delete("/api/libraries/users/2/items/ITEM1/chunks")
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
