"""Tests for the central secret store and encrypted remote-config keys."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from backend.config.settings import get_settings, reset_settings
from backend.services import secret_store
from backend.services.admin_settings_store import (
    get_remote_config_value,
    migrate_plaintext_secrets,
    resolve_shared_value,
    update_remote_config,
)
from backend.services.secret_store import SecretsUnavailableError


class _IsolatedSettings(unittest.TestCase):
    secret: "str | None" = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self.tmp.name)
        self.secret = Fernet.generate_key().decode()
        self._env = patch.dict(os.environ, {"DATA_PATH": self.tmp.name, "AUTOINDEX_SECRET": self.secret})
        self._env.start()
        reset_settings()

    def tearDown(self):
        self._env.stop()
        reset_settings()
        self.tmp.cleanup()

    def _disable_secret(self):
        get_settings().autoindex_secret = None

    def _file(self) -> dict:
        return json.loads((self.data_path / "system" / "admin_settings.json").read_text())


class SecretStoreCryptoTest(_IsolatedSettings):
    def test_encrypt_decrypt_round_trip(self):
        token = secret_store.encrypt("hunter2")
        self.assertNotIn("hunter2", token)
        self.assertEqual(secret_store.decrypt(token), "hunter2")

    def test_seal_unseal_round_trip_and_legacy_plaintext(self):
        envelope = secret_store.seal("k")
        self.assertTrue(secret_store.is_sealed(envelope))
        self.assertEqual(secret_store.unseal(envelope), "k")
        self.assertEqual(secret_store.unseal("legacy-plaintext"), "legacy-plaintext")
        self.assertIsNone(secret_store.unseal(None))

    def test_encrypt_without_secret_raises(self):
        self._disable_secret()
        self.assertFalse(secret_store.secrets_enabled())
        with self.assertRaises(SecretsUnavailableError):
            secret_store.encrypt("x")
        with self.assertRaises(SecretsUnavailableError):
            secret_store.require_secrets_enabled()

    def test_wrong_secret_decrypts_to_none(self):
        token = secret_store.encrypt("x")
        self.assertIsNone(secret_store.decrypt(token, secret=Fernet.generate_key().decode()))

    def test_is_secret_name(self):
        self.assertTrue(secret_store.is_secret_name("RUNPOD_API_KEY"))
        self.assertTrue(secret_store.is_secret_name("MPCDF_LLM_API_KEY"))
        self.assertFalse(secret_store.is_secret_name("RUNPOD_LLM_BASE_URL"))

    def test_get_key_store_uses_configured_paths_and_secret(self):
        store = secret_store.get_key_store()
        self.assertTrue(store.enabled)
        self.assertEqual(store._path, get_settings().autoindex_keys_path)
        self.assertEqual(store._path.parent, self.data_path / "system")


class EncryptedRemoteConfigTest(_IsolatedSettings):
    def test_api_key_is_encrypted_on_disk_and_decrypted_on_read(self):
        update_remote_config({
            "RUNPOD_API_KEY": "rpa_SECRET",
            "RUNPOD_LLM_BASE_URL": "https://api.runpod.ai/v2/x/openai/v1",
        })
        raw = (self.data_path / "system" / "admin_settings.json").read_text()
        self.assertNotIn("rpa_SECRET", raw)
        self.assertIn("https://api.runpod.ai/v2/x/openai/v1", raw)
        self.assertTrue(secret_store.is_sealed(self._file()["remote_config"]["RUNPOD_API_KEY"]))
        self.assertEqual(get_remote_config_value("RUNPOD_API_KEY"), "rpa_SECRET")
        self.assertEqual(resolve_shared_value("RUNPOD_API_KEY"), "rpa_SECRET")
        self.assertEqual(get_remote_config_value("RUNPOD_LLM_BASE_URL"), "https://api.runpod.ai/v2/x/openai/v1")

    def test_saving_a_key_without_secret_is_refused_and_writes_nothing(self):
        self._disable_secret()
        with self.assertRaises(SecretsUnavailableError):
            update_remote_config({"RUNPOD_API_KEY": "rpa_SECRET"})
        self.assertFalse((self.data_path / "system" / "admin_settings.json").exists())

    def test_urls_can_be_saved_without_secret(self):
        self._disable_secret()
        update_remote_config({"RUNPOD_LLM_BASE_URL": "https://u/v1"})
        self.assertEqual(get_remote_config_value("RUNPOD_LLM_BASE_URL"), "https://u/v1")

    def _write_legacy(self):
        path = self.data_path / "system" / "admin_settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"remote_config": {
            "RUNPOD_API_KEY": "legacy_key", "RUNPOD_LLM_BASE_URL": "https://u/v1",
        }}))

    def test_legacy_plaintext_key_still_readable(self):
        self._write_legacy()
        self.assertEqual(get_remote_config_value("RUNPOD_API_KEY"), "legacy_key")

    def test_legacy_plaintext_key_is_encrypted_on_next_write(self):
        self._write_legacy()
        update_remote_config({"RUNPOD_LLM_BASE_URL": "https://new/v1"})
        self.assertNotIn("legacy_key", json.dumps(self._file()))
        self.assertEqual(get_remote_config_value("RUNPOD_API_KEY"), "legacy_key")

    def test_startup_migration_encrypts_plaintext_keys(self):
        self._write_legacy()
        self.assertEqual(migrate_plaintext_secrets(), 1)
        self.assertNotIn("legacy_key", json.dumps(self._file()))
        self.assertEqual(get_remote_config_value("RUNPOD_API_KEY"), "legacy_key")
        self.assertEqual(migrate_plaintext_secrets(), 0)

    def test_startup_migration_without_secret_leaves_file_alone(self):
        self._write_legacy()
        self._disable_secret()
        self.assertEqual(migrate_plaintext_secrets(), 0)
        self.assertIn("legacy_key", json.dumps(self._file()))
        self.assertEqual(get_remote_config_value("RUNPOD_API_KEY"), "legacy_key")

    def test_undecryptable_key_falls_back_to_environment(self):
        update_remote_config({"RUNPOD_API_KEY": "rpa_SECRET"})
        get_settings().autoindex_secret = Fernet.generate_key().decode()
        with patch.dict(os.environ, {"RUNPOD_API_KEY": "from_env"}):
            self.assertEqual(resolve_shared_value("RUNPOD_API_KEY"), "from_env")


if __name__ == "__main__":
    unittest.main()
