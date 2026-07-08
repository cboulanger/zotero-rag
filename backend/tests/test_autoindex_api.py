"""Endpoint tests for /api/autoindex/keys and /api/autoindex/status."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.dependencies import require_authorized_group_admin
from backend.services.autoindex_scheduler import read_scheduler_state
from backend.services.zotero_identity import ZoteroIdentity, reset_identity_cache
from backend.zotero.group_roles import reset_admin_role_cache
from backend.zotero.key_validator import KeyValidation


class AutoIndexApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        s = get_settings()
        s.autoindex_secret = Fernet.generate_key().decode()
        s.autoindex_keys_path = Path(self.tmp.name) / "autoindex_keys.json"
        s.data_path = Path(self.tmp.name)
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

    def test_post_stores_valid_embedding_key(self):
        from backend.services.embedding_key_validator import EmbeddingKeyValidation
        validation = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        emb_validation = EmbeddingKeyValidation(status="ok", key_name="KISSKI_API_KEY")
        with patch("backend.api.autoindex.validate_key", new=AsyncMock(return_value=validation)), \
             patch("backend.api.autoindex.validate_embedding_key", new=AsyncMock(return_value=emb_validation)):
            r = self.client.post("/api/autoindex/keys", json={"api_key": "RO", "embedding_api_key": "EMB"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["embedding_key_status"], "ok")
        r2 = self.client.get("/api/autoindex/keys")
        self.assertTrue(r2.json()["keys"][0]["has_embedding_key"])
        self.assertNotIn("EMB", r2.text)

    def test_post_rejects_invalid_embedding_key_but_keeps_zotero_key(self):
        from backend.services.embedding_key_validator import EmbeddingKeyValidation
        validation = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        emb_validation = EmbeddingKeyValidation(status="invalid", key_name="KISSKI_API_KEY", reason="bad creds")
        with patch("backend.api.autoindex.validate_key", new=AsyncMock(return_value=validation)), \
             patch("backend.api.autoindex.validate_embedding_key", new=AsyncMock(return_value=emb_validation)):
            r = self.client.post("/api/autoindex/keys", json={"api_key": "RO", "embedding_api_key": "BAD"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["embedding_key_status"], "invalid")
        self.assertEqual(r.json()["embedding_key_error"], "bad creds")
        r2 = self.client.get("/api/autoindex/keys")
        self.assertFalse(r2.json()["keys"][0]["has_embedding_key"])

    def test_post_without_embedding_key_omits_status(self):
        validation = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        with patch("backend.api.autoindex.validate_key", new=AsyncMock(return_value=validation)):
            r = self.client.post("/api/autoindex/keys", json={"api_key": "RO"})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("embedding_key_status", r.json())

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

    def test_status_reports_registry_state_with_no_run(self):
        validation = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        with patch("backend.api.autoindex.validate_key", new=AsyncMock(return_value=validation)):
            self.client.post("/api/autoindex/keys", json={"api_key": "RO"})
        r = self.client.get("/api/autoindex/status")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["keys_registered"], 1)
        # No cron run yet -> no live-status fields merged in.
        self.assertNotIn("running", data)

    def test_status_merges_live_run(self):
        system_dir = Path(self.tmp.name) / "system"
        system_dir.mkdir()
        (system_dir / "cron_status.json").write_text(
            json.dumps({"running": False, "slugs": {"users/1": {"status": "done"}}}),
            encoding="utf-8",
        )
        r = self.client.get("/api/autoindex/status")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertFalse(data["running"])
        self.assertIn("users/1", data["slugs"])

    def test_status_available_when_disabled(self):
        get_settings().autoindex_secret = None
        r = self.client.get("/api/autoindex/status")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertFalse(data["enabled"])
        self.assertEqual(data["keys_registered"], 0)
        self.assertIn("disabled_reason", data)

    def _register_user(self, api_key="RO", targets=None):
        validation = KeyValidation(user_id=1, username="u", targets=targets or ["users/1"], read_only=True)
        with patch("backend.api.autoindex.validate_key", new=AsyncMock(return_value=validation)):
            self.client.post("/api/autoindex/keys", json={"api_key": api_key})

    def _set_model_type(self, model_type: str) -> None:
        mock_preset = MagicMock()
        mock_preset.embedding.model_type = model_type
        # Settings is a pydantic v2 BaseSettings model, which blocks plain
        # `settings.get_hardware_preset = ...` assignment (raises ValueError:
        # not a declared field). object.__setattr__ bypasses pydantic's
        # __setattr__ guard and writes directly into the instance __dict__,
        # which normal attribute lookup then prefers over the class method.
        object.__setattr__(get_settings(), "get_hardware_preset", MagicMock(return_value=mock_preset))

    def test_run_rejects_missing_header(self):
        self._register_user()
        r = self.client.post("/api/autoindex/run")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Missing X-Zotero-API-Key", r.json()["detail"])

    def test_run_rejects_unregistered_fingerprint(self):
        r = self.client.post("/api/autoindex/run", headers={"X-Zotero-API-Key": "NEVER-REGISTERED"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("not registered", r.json()["detail"])

    def test_run_rejects_missing_embedding_key_on_remote_preset(self):
        self._register_user()
        self._set_model_type("remote")
        r = self.client.post("/api/autoindex/run", headers={"X-Zotero-API-Key": "RO"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("No embedding API key", r.json()["detail"])

    def test_run_succeeds_on_local_preset_without_embedding_key(self):
        self._register_user()
        self._set_model_type("local")
        with patch("backend.services.autoindex_scheduler.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            r = self.client.post("/api/autoindex/run", headers={"X-Zotero-API-Key": "RO"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["started"])
        mock_spawn.assert_awaited_once()
        args = mock_spawn.await_args.args
        self.assertIn("--fingerprint", args)

    def test_run_succeeds_with_valid_embedding_key_on_remote_preset(self):
        from backend.services.embedding_key_validator import EmbeddingKeyValidation
        emb_validation = EmbeddingKeyValidation(status="ok", key_name="KISSKI_API_KEY")
        validation = KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True)
        with patch("backend.api.autoindex.validate_key", new=AsyncMock(return_value=validation)), \
             patch("backend.api.autoindex.validate_embedding_key", new=AsyncMock(return_value=emb_validation)):
            self.client.post("/api/autoindex/keys", json={"api_key": "RO", "embedding_api_key": "EMB"})
        self._set_model_type("remote")
        with patch("backend.services.autoindex_scheduler.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            r = self.client.post("/api/autoindex/run", headers={"X-Zotero-API-Key": "RO"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["started"])
        mock_spawn.assert_awaited_once()

    def test_run_rejects_when_already_running(self):
        self._register_user()
        self._set_model_type("local")
        system_dir = Path(self.tmp.name) / "system"
        system_dir.mkdir()
        (system_dir / "cron_status.json").write_text(json.dumps({"running": True, "pid": 1}), encoding="utf-8")
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True):
            r = self.client.post("/api/autoindex/run", headers={"X-Zotero-API-Key": "RO"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("already running", r.json()["detail"])


class AdminSchedulerControlsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        reset_admin_role_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        s.autoindex_secret = Fernet.generate_key().decode()
        s.autoindex_keys_path = Path(self.tmp.name) / "autoindex_keys.json"
        s.authorized_group_id = 999
        # api_host stays at its default ("localhost") here. require_authorized_
        # group_admin bypasses to a no-op on loopback deployments (same trust
        # boundary as the rest of the Zotero-key auth), which is exactly what
        # the admin-override tests below rely on: no X-Zotero-API-Key header is
        # sent, so the auth middleware (which also treats loopback specially)
        # lets the request through, and app.dependency_overrides substitutes a
        # fake admin identity for require_authorized_group_admin directly.
        # Tests that need to exercise the *real* 503/401/403 checks inside
        # require_authorized_group_admin set api_host to a non-loopback value
        # themselves (see below) — that dependency checks is_loopback() first,
        # so those checks are unreachable while api_host is "localhost".
        self.client = TestClient(app)

    def tearDown(self):
        from backend.main import app as main_app
        main_app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()
        reset_admin_role_cache()

    def _override_admin(self, identity):
        from backend.main import app as main_app
        main_app.dependency_overrides[require_authorized_group_admin] = lambda: identity

    def test_pause_requires_authorized_group_id(self):
        # Non-loopback so require_authorized_group_admin's is_loopback() guard
        # doesn't short-circuit before the authorized_group_id check we're
        # testing. The auth middleware runs resolve_zotero_identity() before
        # the route dependency is ever invoked, so a valid identity must be
        # supplied here too (mocked, not a real network round-trip) purely to
        # get past the middleware — the assertion targets require_authorized_
        # group_admin's own 503, not anything the middleware does.
        get_settings().authorized_group_id = None
        get_settings().api_host = "rag.example.com"
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=identity)):
            r = self.client.post("/api/autoindex/scheduler/pause", headers={"X-Zotero-API-Key": "K"})
        self.assertEqual(r.status_code, 503)

    def test_pause_rejects_non_admin(self):
        # Non-loopback so require_authorized_group_admin's real checks run.
        # zotero_identity.py imports validate_key by name
        # (`from backend.zotero.key_validator import ... validate_key`), so the
        # identity cache calls it as a module-level name in
        # backend.services.zotero_identity — patch it there, not at its
        # definition site, or the mock won't be observed. This keeps the whole
        # request hermetic (no live call to api.zotero.org), matching the
        # convention in backend/tests/test_resolve_zotero_identity.py.
        get_settings().api_host = "rag.example.com"
        # targets must include "groups/999" (the setUp's authorized_group_id)
        # so resolve_zotero_identity's own passes_gate() check succeeds and
        # the request reaches the route's is_group_admin() check below —
        # otherwise the middleware would 403 first for an unrelated reason
        # (not a member of the authorizing group at all, vs. member-but-not-
        # admin, which is what this test targets).
        validation = KeyValidation(user_id=1, username="u", targets=["users/1", "groups/999"], read_only=True)
        with patch("backend.services.zotero_identity.validate_key", new=AsyncMock(return_value=validation)), \
             patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=False)):
            r = self.client.post(
                "/api/autoindex/scheduler/pause",
                headers={"X-Zotero-API-Key": "K"},
            )
        # Identity resolution is now deterministic (resolved, non-admin), so
        # the outcome is precisely "not an admin of the authorizing group".
        self.assertEqual(r.status_code, 403)

    def test_pause_admin_writes_state(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        r = self.client.post("/api/autoindex/scheduler/pause")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"paused": True})
        self.assertTrue(read_scheduler_state(get_settings().data_path)["paused"])

    def test_resume_admin_writes_state(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        self.client.post("/api/autoindex/scheduler/pause")
        r = self.client.post("/api/autoindex/scheduler/resume")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"paused": False})
        self.assertFalse(read_scheduler_state(get_settings().data_path)["paused"])

    def test_run_now_admin_starts_unscoped_run(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        with patch("backend.services.autoindex_scheduler.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
            r = self.client.post("/api/autoindex/scheduler/run-now")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["started"])
        mock_spawn.assert_awaited_once()
        self.assertNotIn("--fingerprint", mock_spawn.await_args.args)  # unscoped: every registered library

    def test_run_now_admin_rejects_when_already_running(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        system_dir = Path(self.tmp.name) / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "cron_status.json").write_text(json.dumps({"running": True, "pid": 1}), encoding="utf-8")
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True):
            r = self.client.post("/api/autoindex/scheduler/run-now")
        self.assertEqual(r.status_code, 409)

    def test_run_now_admin_rejects_when_autoindex_disabled(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        get_settings().autoindex_secret = None
        r = self.client.post("/api/autoindex/scheduler/run-now")
        self.assertEqual(r.status_code, 503)

    def test_abort_rejects_when_nothing_running(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        r = self.client.post("/api/autoindex/abort")
        self.assertEqual(r.status_code, 409)

    def test_abort_calls_abort_process_with_pid(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        system_dir = Path(self.tmp.name) / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "cron_status.json").write_text(json.dumps({"running": True, "pid": 4242}), encoding="utf-8")
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True), \
             patch("backend.api.autoindex.abort_process", return_value=True) as mock_abort:
            r = self.client.post("/api/autoindex/abort")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"aborted": True, "pid": 4242})
        mock_abort.assert_called_once_with(4242)

    def test_abort_reports_false_when_process_already_gone(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        system_dir = Path(self.tmp.name) / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "cron_status.json").write_text(json.dumps({"running": True, "pid": 5555}), encoding="utf-8")
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True), \
             patch("backend.api.autoindex.abort_process", return_value=False) as mock_abort:
            r = self.client.post("/api/autoindex/abort")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"aborted": False, "pid": 5555})
        mock_abort.assert_called_once_with(5555)

    def _seed_running_status(self, slugs: dict) -> None:
        system_dir = Path(self.tmp.name) / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "cron_status.json").write_text(
            json.dumps({"running": True, "pid": 1, "slugs": slugs}), encoding="utf-8",
        )

    def test_skip_slug_rejects_when_nothing_running(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        r = self.client.post("/api/autoindex/scheduler/skip-slug", json={"slug": "users/1"})
        self.assertEqual(r.status_code, 409)

    def test_skip_slug_rejects_unknown_slug(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        self._seed_running_status({"users/1": {"status": "indexing"}})
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True):
            r = self.client.post("/api/autoindex/scheduler/skip-slug", json={"slug": "groups/999"})
        self.assertEqual(r.status_code, 404)

    def test_skip_slug_rejects_already_done_slug(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        self._seed_running_status({"users/1": {"status": "done"}})
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True):
            r = self.client.post("/api/autoindex/scheduler/skip-slug", json={"slug": "users/1"})
        self.assertEqual(r.status_code, 404)

    def test_skip_slug_writes_control_state_for_indexing_slug(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        self._seed_running_status({"users/1": {"status": "indexing"}})
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True):
            r = self.client.post("/api/autoindex/scheduler/skip-slug", json={"slug": "users/1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"skip_requested": True, "slug": "users/1"})
        from backend.services.cron_indexer import read_control_state
        self.assertEqual(read_control_state(get_settings().data_path)["skip_slug"], "users/1")

    def test_skip_slug_accepts_pending_slug(self):
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        self._seed_running_status({"users/1": {"status": "pending"}})
        with patch("backend.services.cron_indexer.is_process_alive", return_value=True):
            r = self.client.post("/api/autoindex/scheduler/skip-slug", json={"slug": "users/1"})
        self.assertEqual(r.status_code, 200)


class StatusAdminFieldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        reset_identity_cache()
        reset_admin_role_cache()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        s.autoindex_secret = Fernet.generate_key().decode()
        s.autoindex_keys_path = Path(self.tmp.name) / "autoindex_keys.json"
        self.client = TestClient(app)

    def tearDown(self):
        from backend.main import app as main_app
        main_app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()
        reset_admin_role_cache()

    def _set_identity(self, identity):
        import backend.dependencies as dependencies
        from backend.main import app as main_app
        main_app.dependency_overrides[dependencies.get_zotero_identity] = lambda: identity

    def test_is_admin_true_on_loopback(self):
        self._set_identity(None)
        r = self.client.get("/api/autoindex/status")
        self.assertTrue(r.json()["is_admin"])

    def test_is_admin_false_without_authorized_group_id(self):
        from backend.services.zotero_identity import ZoteroIdentity
        get_settings().authorized_group_id = None
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.get("/api/autoindex/status")
        self.assertFalse(r.json()["is_admin"])

    def test_is_admin_reflects_cache_result(self):
        from backend.services.zotero_identity import ZoteroIdentity
        get_settings().authorized_group_id = 999
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        with patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=True)):
            r = self.client.get("/api/autoindex/status")
        self.assertTrue(r.json()["is_admin"])

    def test_is_admin_false_when_cache_says_not_admin(self):
        from backend.services.zotero_identity import ZoteroIdentity
        get_settings().authorized_group_id = 999
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        with patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=False)):
            r = self.client.get("/api/autoindex/status")
        self.assertFalse(r.json()["is_admin"])

    def test_is_admin_false_on_admin_check_exception(self):
        """A transient Zotero API failure during the admin check must degrade
        to is_admin=False, not crash the whole status endpoint."""
        import asyncio as asyncio_module
        from backend.services.zotero_identity import ZoteroIdentity
        get_settings().authorized_group_id = 999
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        with patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(side_effect=asyncio_module.TimeoutError())):
            r = self.client.get("/api/autoindex/status")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["is_admin"])

    def _seed_status(self, slugs: dict) -> None:
        system_dir = Path(self.tmp.name) / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "cron_status.json").write_text(
            json.dumps({"running": False, "slugs": slugs}), encoding="utf-8",
        )

    def _seed_registrations(self, data: dict) -> None:
        system_dir = Path(self.tmp.name) / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "registrations.json").write_text(json.dumps(data), encoding="utf-8")
        get_settings().registrations_path = system_dir / "registrations.json"

    def test_scope_all_rejects_when_authorized_group_id_unset(self):
        """Without AUTHORIZED_GROUP_ID configured, is_admin is False even with
        a non-loopback identity override, so scope=all must still 403 —
        there's no admin to be."""
        from backend.services.zotero_identity import ZoteroIdentity
        self._seed_status({
            "users/1": {"status": "done"},
            "users/2": {"status": "indexing"},
        })
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        r = self.client.get("/api/autoindex/status?scope=all")
        self.assertEqual(r.status_code, 403)

    def test_scope_all_admin_sees_every_slug_with_labels(self):
        from backend.services.zotero_identity import ZoteroIdentity
        get_settings().authorized_group_id = 999
        self._seed_status({
            "users/1": {"status": "done"},
            "users/2": {"status": "indexing"},
        })
        self._seed_registrations({
            "u1": {"library_name": "Alice's Library", "users": [{"user_id": 1, "username": "alice"}]},
            "u2": {"library_name": "Bob's Library", "users": [{"user_id": 2, "username": "bob"}]},
        })
        self._set_identity(ZoteroIdentity(user_id=1, username="alice", targets=["users/1"]))
        with patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=True)):
            r = self.client.get("/api/autoindex/status?scope=all")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(set(data["slugs"].keys()), {"users/1", "users/2"})
        self.assertEqual(data["slugs"]["users/1"]["library_name"], "Alice's Library")
        self.assertEqual(data["slugs"]["users/1"]["owner_id"], 1)
        self.assertEqual(data["slugs"]["users/2"]["library_name"], "Bob's Library")
        self.assertEqual(data["slugs"]["users/2"]["owner_id"], 2)

    def test_scope_all_falls_back_to_raw_slug_when_no_registration(self):
        from backend.services.zotero_identity import ZoteroIdentity
        get_settings().authorized_group_id = 999
        self._seed_status({"groups/555": {"status": "pending"}})
        self._seed_registrations({})  # no matching registration for groups/555
        self._set_identity(ZoteroIdentity(user_id=1, username="alice", targets=["users/1"]))
        with patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=True)):
            r = self.client.get("/api/autoindex/status?scope=all")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["slugs"]["groups/555"]["library_name"], "groups/555")
        self.assertIsNone(data["slugs"]["groups/555"]["owner_id"])

    def test_scope_own_unaffected_by_registrations(self):
        """Regression check: the default (own) scope must not gain
        library_name/owner_id fields or change its filtering behavior."""
        from backend.services.zotero_identity import ZoteroIdentity
        self._seed_status({
            "users/1": {"status": "done"},
            "users/2": {"status": "done"},
        })
        self._set_identity(ZoteroIdentity(user_id=1, username="alice", targets=["users/1"]))
        r = self.client.get("/api/autoindex/status")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(set(data["slugs"].keys()), {"users/1"})
        self.assertNotIn("library_name", data["slugs"]["users/1"])

    def test_scope_all_loopback_sees_every_slug_with_labels(self):
        """Loopback deployments get is_admin=True unconditionally, so
        ?scope=all must work for them too, without needing an identity override."""
        self._seed_status({
            "users/1": {"status": "done"},
            "users/2": {"status": "indexing"},
        })
        self._seed_registrations({
            "u1": {"library_name": "Alice's Library", "users": [{"user_id": 1, "username": "alice"}]},
            "u2": {"library_name": "Bob's Library", "users": [{"user_id": 2, "username": "bob"}]},
        })
        self._set_identity(None)
        r = self.client.get("/api/autoindex/status?scope=all")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(set(data["slugs"].keys()), {"users/1", "users/2"})
        self.assertEqual(data["slugs"]["users/1"]["library_name"], "Alice's Library")

    def test_scope_all_admin_sees_all_key_issues_unfiltered(self):
        from backend.services.zotero_identity import ZoteroIdentity
        get_settings().authorized_group_id = 999
        system_dir = Path(self.tmp.name) / "system"
        system_dir.mkdir(parents=True, exist_ok=True)
        (system_dir / "cron_status.json").write_text(json.dumps({
            "running": False,
            "slugs": {"users/1": {"status": "done"}},
            "key_issues": [
                {"user": "alice", "reason": "revoked", "pruned": True},
                {"user": "bob", "reason": "revoked", "pruned": True},
            ],
        }), encoding="utf-8")
        self._set_identity(ZoteroIdentity(user_id=1, username="alice", targets=["users/1"]))
        with patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=True)):
            r = self.client.get("/api/autoindex/status?scope=all")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["key_issues"]), 2)  # both alice's and bob's, unfiltered


if __name__ == "__main__":
    unittest.main()
