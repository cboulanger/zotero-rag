"""Endpoint tests for POST /api/query's per-library authorization."""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.services.zotero_identity import ZoteroIdentity, reset_identity_cache
import backend.dependencies as dependencies


class QueryAuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        # `data_path` only *derives* other paths at Settings construction time
        # (see set_derived_paths); mutating it post-construction does not move
        # already-resolved paths. Without this, the real lifespan below would
        # open the real project's data/qdrant directory instead of the temp
        # sandbox — silently defeating isolation and risking a lock collision
        # with any other process (e.g. a locally running dev server) that has
        # that real directory open.
        s.vector_db_path = Path(self.tmp.name) / "qdrant"
        s.testing = True
        # Enter the TestClient as a context manager (not just construct it) so
        # that FastAPI's `lifespan` runs and populates app.state.vector_store —
        # otherwise get_vector_store() raises AttributeError before the
        # handler body (and its access-gate check) ever runs.
        self.client = TestClient(app).__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()
        app.dependency_overrides.clear()

    def _set_identity(self, identity):
        """Bypass the middleware/network round-trip: override the
        get_zotero_identity dependency directly, matching how FastAPI's own
        dependency_overrides mechanism is meant to be used in tests."""
        app.dependency_overrides[dependencies.get_zotero_identity] = lambda: identity

    def test_loopback_query_with_no_identity_is_unrestricted(self):
        self._set_identity(None)
        r = self.client.post("/api/query", json={"question": "q?", "library_ids": ["u1"]})
        self.assertNotEqual(r.status_code, 403)

    def test_query_outside_targets_is_403(self):
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        self._set_identity(identity)
        r = self.client.post("/api/query", json={"question": "q?", "library_ids": ["u2"]})
        self.assertEqual(r.status_code, 403)

    def test_query_within_targets_is_not_403(self):
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        self._set_identity(identity)
        r = self.client.post("/api/query", json={"question": "q?", "library_ids": ["u1"]})
        self.assertNotEqual(r.status_code, 403)

    def test_query_with_one_target_outside_is_403_even_if_another_is_inside(self):
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        self._set_identity(identity)
        r = self.client.post(
            "/api/query",
            json={"question": "q?", "library_ids": ["u1", "u2"]},
        )
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
