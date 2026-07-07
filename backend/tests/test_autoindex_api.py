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
        with patch("backend.api.autoindex.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
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
        with patch("backend.api.autoindex.asyncio.create_subprocess_exec", new=AsyncMock()) as mock_spawn:
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


if __name__ == "__main__":
    unittest.main()
