"""
Unit tests for configuration system.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.config.presets import (
    DEFAULT_PRESETS_DIR,
    HardwarePreset,
    ensure_default_presets,
    get_preset,
    list_presets,
)
from backend.config.settings import Settings, get_settings, reset_settings
from backend.services.zotero_identity import reset_identity_cache
from backend.zotero.group_roles import reset_admin_role_cache

# The 9 preset names this project ships as bundled defaults. Hardcoded (not
# derived from DEFAULT_PRESETS_DIR.glob(...)) so an accidental deletion of
# one of these files is actually caught — a count/name comparison against
# the same glob the code under test reads would be circular and could never
# fail that way.
EXPECTED_BUNDLED_PRESET_NAMES = {
    "apple-silicon-32gb",
    "high-memory",
    "cpu-only",
    "remote-openai",
    "apple-silicon-kisski",
    "remote-kisski",
    "cloud-server-kisski",
    "windows-test",
    "remote-mpcdf",
    "runpod",
}


MPCDF_ENV = {
    "MPCDF_EMBEDDING_BASE_URL": "http://x", "MPCDF_EMBEDDING_API_KEY": "k",
    "MPCDF_LLM_BASE_URL": "http://x", "MPCDF_LLM_API_KEY": "k",
}


class TestBundledDefaultPresets(unittest.TestCase):
    """Validate the actual shipped files in backend/config/default_presets/
    directly against the schema — these are source code, not user data, so
    (unlike a user's own preset file) a broken one here should fail CI, not
    just log a warning at runtime (see list_presets()'s tolerant handling)."""

    def test_exactly_the_expected_bundled_preset_files_exist(self):
        names = {p.stem for p in DEFAULT_PRESETS_DIR.glob("*.json")}
        self.assertEqual(names, EXPECTED_BUNDLED_PRESET_NAMES)

    def test_every_bundled_default_validates_against_hardware_preset(self):
        for path in DEFAULT_PRESETS_DIR.glob("*.json"):
            with self.subTest(preset=path.stem):
                data = json.loads(path.read_text(encoding="utf-8"))
                data["name"] = path.stem
                HardwarePreset.model_validate(data)  # raises on a schema violation


class TestPresets(unittest.TestCase):
    """Test hardware presets, loaded from JSON files under data_path/presets/."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)
        ensure_default_presets(self.data_path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_get_preset_cpu_only(self):
        """Test getting cpu-only preset."""
        preset = get_preset("cpu-only", self.data_path)

        self.assertEqual(preset.name, "cpu-only")
        self.assertEqual(preset.embedding.model_name, "sentence-transformers/all-MiniLM-L6-v2")
        self.assertEqual(preset.llm.model_name, "TinyLlama/TinyLlama-1.1B-Chat-v1.0")

    def test_get_preset_remote_openai(self):
        """Test getting remote-openai preset."""
        preset = get_preset("remote-openai", self.data_path)

        self.assertEqual(preset.name, "remote-openai")
        self.assertEqual(preset.embedding.model_type, "remote")
        self.assertEqual(preset.llm.model_type, "remote")

    def test_get_preset_remote_kisski(self):
        """Test getting remote-kisski preset."""
        preset = get_preset("remote-kisski", self.data_path)

        self.assertEqual(preset.name, "remote-kisski")
        self.assertEqual(preset.embedding.model_type, "remote")  # Fully remote — no local torch
        self.assertEqual(preset.embedding.model_name, "multilingual-e5-large-instruct")
        self.assertEqual(preset.embedding.model_kwargs["base_url"], "https://chat-ai.academiccloud.de/v1")
        self.assertEqual(preset.embedding.model_kwargs["api_key_env"], "KISSKI_API_KEY")
        self.assertEqual(preset.llm.model_type, "remote")
        self.assertEqual(preset.llm.model_kwargs["base_url"], "https://chat-ai.academiccloud.de/v1")
        self.assertEqual(preset.llm.model_kwargs["api_key_env"], "KISSKI_API_KEY")

    def test_get_preset_remote_mpcdf_uses_shared_dynamic_fields(self):
        """remote-mpcdf has no static base_url — both base_url and API key are
        resolved at request time from the shared admin-set store (see
        backend.services.admin_settings_store), not baked into the preset."""
        preset = get_preset("remote-mpcdf", self.data_path)

        self.assertEqual(preset.name, "remote-mpcdf")
        self.assertEqual(preset.embedding.model_type, "remote")
        self.assertNotIn("base_url", preset.embedding.model_kwargs)
        self.assertEqual(preset.embedding.model_kwargs["shared_base_url_env"], "MPCDF_EMBEDDING_BASE_URL")
        self.assertEqual(preset.embedding.model_kwargs["shared_api_key_env"], "MPCDF_EMBEDDING_API_KEY")
        self.assertEqual(preset.llm.model_type, "remote")
        self.assertNotIn("base_url", preset.llm.model_kwargs)
        self.assertEqual(preset.llm.model_kwargs["shared_base_url_env"], "MPCDF_LLM_BASE_URL")
        self.assertEqual(preset.llm.model_kwargs["shared_api_key_env"], "MPCDF_LLM_API_KEY")
        # Same embedding model as remote-kisski — same vector space, so the two
        # presets are mutually hot-swappable at runtime.
        self.assertEqual(
            preset.embedding.model_name,
            get_preset("remote-kisski", self.data_path).embedding.model_name,
        )

    def test_get_preset_runpod_uses_shared_dynamic_fields(self):
        """runpod has no static base_url — both the embedding and LLM endpoint
        URLs are only known after scripts/provision_runpod_endpoints.py creates
        them, so (like remote-mpcdf) they're resolved at request time from the
        shared admin-set store rather than baked into the preset."""
        preset = get_preset("runpod", self.data_path)

        self.assertEqual(preset.name, "runpod")
        self.assertEqual(preset.embedding.model_type, "remote")
        self.assertEqual(preset.embedding.model_name, "intfloat/multilingual-e5-large-instruct")
        self.assertNotIn("base_url", preset.embedding.model_kwargs)
        self.assertEqual(preset.embedding.model_kwargs["shared_base_url_env"], "RUNPOD_EMBEDDING_BASE_URL")
        self.assertEqual(preset.embedding.model_kwargs["shared_api_key_env"], "RUNPOD_API_KEY")
        self.assertEqual(preset.llm.model_type, "remote")
        self.assertEqual(preset.llm.model_name, "Qwen/Qwen2.5-7B-Instruct")
        self.assertNotIn("base_url", preset.llm.model_kwargs)
        self.assertEqual(preset.llm.model_kwargs["shared_base_url_env"], "RUNPOD_LLM_BASE_URL")
        # Same env var for both — a RunPod account has one stable API key used
        # by every endpoint it owns (unlike MPCDF's two independent Slurm jobs,
        # each with its own distinct generated key).
        self.assertEqual(preset.llm.model_kwargs["shared_api_key_env"], "RUNPOD_API_KEY")

    def test_get_preset_invalid(self):
        """Test getting invalid preset raises error."""
        with self.assertRaises(ValueError) as ctx:
            get_preset("invalid-preset", self.data_path)

        self.assertIn("Unknown preset", str(ctx.exception))

    def test_list_presets(self):
        """Test listing all presets."""
        presets = list_presets(self.data_path)

        self.assertIn("cpu-only", presets)
        self.assertIn("remote-openai", presets)
        self.assertIn("remote-kisski", presets)
        expected_count = len(list(DEFAULT_PRESETS_DIR.glob("*.json")))
        self.assertEqual(len(presets), expected_count)

    def test_get_preset_auto_seeds_a_data_path_whose_presets_dir_was_never_created(self):
        """A caller that points Settings.data_path at a fresh directory without
        ever calling ensure_default_presets/ensure_directories (e.g. a test
        fixture) still gets a working preset lookup — get_preset/list_presets
        self-heal by seeding defaults on demand."""
        with tempfile.TemporaryDirectory() as fresh:
            fresh_path = Path(fresh)
            self.assertFalse((fresh_path / "presets").exists())

            preset = get_preset("cpu-only", fresh_path)

            self.assertEqual(preset.name, "cpu-only")
            self.assertIn("cpu-only", list_presets(fresh_path))

    def test_ensure_default_presets_copies_every_bundled_default(self):
        """Every bundled default file ends up in the (empty) target directory."""
        presets_dir = self.data_path / "presets"
        bundled_names = {p.name for p in DEFAULT_PRESETS_DIR.glob("*.json")}
        copied_names = {p.name for p in presets_dir.glob("*.json")}
        self.assertEqual(bundled_names, copied_names)

    def test_ensure_default_presets_does_not_overwrite_existing_file(self):
        """A user's edited preset file survives re-running the seeder (e.g. on
        every backend startup)."""
        presets_dir = self.data_path / "presets"
        custom_content = (
            '{"description": "edited by user", '
            '"embedding": {"model_type": "local", "model_name": "x"}, '
            '"llm": {"model_type": "local", "model_names": ["y"]}, '
            '"rag": {}, "memory_budget_gb": 1.0}'
        )
        (presets_dir / "cpu-only.json").write_text(custom_content)

        ensure_default_presets(self.data_path)

        self.assertEqual((presets_dir / "cpu-only.json").read_text(), custom_content)

    def test_get_preset_raises_for_malformed_json(self):
        """A file that isn't valid JSON raises ValueError naming the file."""
        presets_dir = self.data_path / "presets"
        broken_path = presets_dir / "broken.json"
        broken_path.write_text("{not valid json")

        with self.assertRaises(ValueError) as ctx:
            get_preset("broken", self.data_path)

        self.assertIn(str(broken_path), str(ctx.exception))

    def test_get_preset_raises_for_schema_invalid_file(self):
        """A file missing required HardwarePreset fields raises ValueError
        naming the file."""
        presets_dir = self.data_path / "presets"
        invalid_path = presets_dir / "invalid.json"
        invalid_path.write_text(json.dumps({"description": "missing required fields"}))

        with self.assertRaises(ValueError) as ctx:
            get_preset("invalid", self.data_path)

        self.assertIn(str(invalid_path), str(ctx.exception))

    def test_list_presets_skips_malformed_file(self):
        """One broken custom preset doesn't hide the rest."""
        presets_dir = self.data_path / "presets"
        (presets_dir / "broken.json").write_text("{not valid json")

        presets = list_presets(self.data_path)

        self.assertNotIn("broken", presets)
        self.assertIn("cpu-only", presets)

    def test_name_field_in_file_is_ignored_in_favor_of_filename(self):
        """The filename is authoritative; a stray "name" key in the file content
        can't make the preset's name drift from its filename."""
        presets_dir = self.data_path / "presets"
        data = json.loads((presets_dir / "cpu-only.json").read_text())
        data["name"] = "something-else"
        (presets_dir / "cpu-only.json").write_text(json.dumps(data))

        preset = get_preset("cpu-only", self.data_path)

        self.assertEqual(preset.name, "cpu-only")

    def test_get_preset_returns_independent_copies_across_calls(self):
        """get_preset() caches the loaded/validated object internally for
        performance (see module docstring), but each call must return an
        independent copy — mutating one caller's result (e.g.
        Settings.get_hardware_preset()'s EMBEDDING_BATCH_SIZE override)
        must never leak into another caller's."""
        first = get_preset("cpu-only", self.data_path)
        first.embedding.batch_size = 999999

        second = get_preset("cpu-only", self.data_path)

        self.assertEqual(second.embedding.batch_size, 16)

    def test_get_preset_rejects_path_traversal_in_name(self):
        with self.assertRaises(ValueError):
            get_preset("../../../../etc/passwd", self.data_path)
        with self.assertRaises(ValueError):
            get_preset("sub/dir", self.data_path)

    def test_list_presets_platform_filter_hides_other_platforms(self):
        linux_presets = list_presets(self.data_path, platform="linux")

        self.assertNotIn("windows-test", linux_presets)
        self.assertNotIn("apple-silicon-kisski", linux_presets)
        self.assertNotIn("apple-silicon-32gb", linux_presets)
        self.assertIn("cpu-only", linux_presets)  # platform: "any"
        self.assertIn("remote-kisski", linux_presets)  # platform: "any"

    def test_list_presets_platform_filter_keeps_matching_platform(self):
        windows_presets = list_presets(self.data_path, platform="windows")

        self.assertIn("windows-test", windows_presets)
        self.assertNotIn("apple-silicon-kisski", windows_presets)

    def test_list_presets_with_no_platform_filter_returns_everything(self):
        unfiltered = list_presets(self.data_path)

        self.assertIn("windows-test", unfiltered)
        self.assertIn("apple-silicon-kisski", unfiltered)
        self.assertIn("apple-silicon-32gb", unfiltered)

    def test_current_platform_matches_python_platform_module(self):
        import platform as platform_module
        from backend.config.presets import current_platform

        self.assertEqual(current_platform(), platform_module.system().lower())


class TestSettings(unittest.TestCase):
    """Test application settings."""

    def setUp(self):
        """Reset settings before each test."""
        reset_settings()

    def tearDown(self):
        """Reset settings after each test."""
        reset_settings()

    def test_default_settings(self):
        """Test default settings values."""
        # Clear MODEL_PRESET from environment to test actual defaults
        # Remove both MODEL_PRESET and LOG_LEVEL to test true defaults
        env_vars_to_remove = ["MODEL_PRESET", "LOG_LEVEL"]
        env_copy = {k: v for k, v in os.environ.items() if k not in env_vars_to_remove}

        with patch.dict(os.environ, env_copy, clear=True):
            # Also need to prevent pydantic from loading .env file
            # by temporarily patching the model_config
            from backend.config.settings import Settings as OrigSettings

            # Create a Settings class without env_file for this test
            class TestSettings(OrigSettings):
                model_config = OrigSettings.model_config.copy()

            # Remove env_file from config to prevent .env loading
            TestSettings.model_config['env_file'] = None

            settings = TestSettings()

            self.assertEqual(settings.api_host, "localhost")
            self.assertEqual(settings.api_port, 8119)
            self.assertEqual(settings.model_preset, "cpu-only")
            self.assertEqual(settings.log_level, "INFO")
            self.assertIsInstance(settings.version, str)
            self.assertTrue(len(settings.version) > 0)

    def test_path_expansion(self):
        """Test that paths are expanded correctly."""
        with patch.dict(os.environ, {
            "MODEL_WEIGHTS_PATH": "~/custom/models",
            "VECTOR_DB_PATH": "~/custom/qdrant",
        }):
            settings = Settings()

            self.assertFalse(str(settings.model_weights_path).startswith("~"))
            self.assertFalse(str(settings.vector_db_path).startswith("~"))
            # Use Path normalization to handle both Windows and Unix paths
            self.assertTrue("custom" in str(settings.model_weights_path))
            self.assertTrue("models" in str(settings.model_weights_path))
            self.assertTrue("custom" in str(settings.vector_db_path))
            self.assertTrue("qdrant" in str(settings.vector_db_path))

    def test_env_override(self):
        """Test that environment variables override defaults."""
        with patch.dict(os.environ, {
            "API_PORT": "9000",
            "MODEL_PRESET": "cpu-only",
            "LOG_LEVEL": "DEBUG",
        }):
            settings = Settings()

            self.assertEqual(settings.api_port, 9000)
            self.assertEqual(settings.model_preset, "cpu-only")
            self.assertEqual(settings.log_level, "DEBUG")

    def test_kreuzberg_max_content_bytes_default_and_size_string_parsing(self):
        """Default is 200MB; human-friendly size strings ('200MB', '1GB') parse
        to raw byte counts the same way pdf_split_threshold already does."""
        self.assertEqual(Settings().kreuzberg_max_content_bytes, 200 * 1024 ** 2)
        self.assertEqual(Settings(kreuzberg_max_content_bytes="200MB").kreuzberg_max_content_bytes, 200 * 1024 ** 2)
        self.assertEqual(Settings(kreuzberg_max_content_bytes="1GB").kreuzberg_max_content_bytes, 1024 ** 3)
        self.assertEqual(Settings(kreuzberg_max_content_bytes=12345).kreuzberg_max_content_bytes, 12345)

    def test_invalid_log_level(self):
        """Test that invalid log level raises error."""
        with self.assertRaises(ValueError):
            with patch.dict(os.environ, {"LOG_LEVEL": "INVALID"}):
                Settings()

    def test_get_hardware_preset(self):
        """Test getting hardware preset from settings."""
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(model_preset="cpu-only", data_path=tmp)
            settings.ensure_directories()
            preset = settings.get_hardware_preset()

            self.assertEqual(preset.name, "cpu-only")

    def test_get_hardware_preset_uses_active_preset_override_when_set(self):
        from backend.services.admin_settings_store import set_active_preset_override
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(model_preset="cpu-only", data_path=tmp)
            settings.ensure_directories()
            set_active_preset_override(settings.data_path, "remote-mpcdf")
            self.assertEqual(settings.get_hardware_preset().name, "remote-mpcdf")

    def test_get_hardware_preset_falls_back_to_model_preset_when_no_override_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(model_preset="cpu-only", data_path=tmp)
            settings.ensure_directories()
            self.assertEqual(settings.get_hardware_preset().name, "cpu-only")

    def test_get_hardware_preset_ignores_unknown_override(self):
        from backend.services.admin_settings_store import set_active_preset_override
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(model_preset="cpu-only", data_path=tmp)
            settings.ensure_directories()
            set_active_preset_override(settings.data_path, "no-such-preset")
            self.assertEqual(settings.get_hardware_preset().name, "cpu-only")

    def test_get_hardware_preset_applies_embedding_batch_size_env_override(self):
        """EMBEDDING_BATCH_SIZE (docs/cron-indexing.md's low-RAM tuning knob)
        overrides embedding.batch_size for whichever preset is active, not
        just remote-kisski."""
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(model_preset="cpu-only", data_path=tmp)
            settings.ensure_directories()
            with patch.dict(os.environ, {"EMBEDDING_BATCH_SIZE": "7"}):
                preset = settings.get_hardware_preset()
            self.assertEqual(preset.embedding.batch_size, 7)

    def test_get_api_key(self):
        """Test getting API keys from environment variables."""
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-openai-key",
            "ANTHROPIC_API_KEY": "test-anthropic-key",
            "KISSKI_API_KEY": "test-kisski-key",
        }):
            settings = Settings()

            self.assertEqual(settings.get_api_key("OPENAI_API_KEY"), "test-openai-key")
            self.assertEqual(settings.get_api_key("ANTHROPIC_API_KEY"), "test-anthropic-key")
            self.assertEqual(settings.get_api_key("KISSKI_API_KEY"), "test-kisski-key")
            self.assertIsNone(settings.get_api_key("NONEXISTENT_KEY"))

    def test_global_settings_singleton(self):
        """Test that get_settings returns singleton."""
        settings1 = get_settings()
        settings2 = get_settings()

        self.assertIs(settings1, settings2)

    def test_reset_settings(self):
        """Test resetting global settings."""
        settings1 = get_settings()
        reset_settings()
        settings2 = get_settings()

        self.assertIsNot(settings1, settings2)


def test_autoindex_settings_defaults(monkeypatch):
    monkeypatch.delenv("AUTOINDEX_SECRET", raising=False)
    from backend.config.settings import Settings
    s = Settings()
    # A local .env file (present in dev environments) may define AUTOINDEX_SECRET,
    # which pydantic reads regardless of the process environment. In that case the
    # "unset default" cannot be observed, so skip rather than fail.
    if s.autoindex_secret is not None:
        pytest.skip("AUTOINDEX_SECRET is provided via a .env file; cannot test unset default")
    assert s.autoindex_secret is None
    # Defaults under data_path/system
    assert str(s.autoindex_keys_path).endswith("system/autoindex_keys.json")


class TestConfigApi(unittest.TestCase):
    """Endpoint tests for GET/POST /api/config and POST /api/config/remote-fields."""

    def setUp(self):
        from backend.main import app
        reset_settings()
        reset_identity_cache()
        reset_admin_role_cache()
        self.tmp = tempfile.TemporaryDirectory()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        ensure_default_presets(s.data_path)
        s.model_preset = "remote-kisski"
        self.app = app
        from fastapi.testclient import TestClient
        self.client = TestClient(app)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()
        reset_admin_role_cache()

    def _override_admin(self, identity):
        from backend.dependencies import require_authorized_group_admin
        self.app.dependency_overrides[require_authorized_group_admin] = lambda: identity

    def test_get_config_reflects_current_preset_name(self):
        r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["preset_name"], "remote-kisski")

    def test_get_config_lists_compatible_presets_for_remote_kisski(self):
        r = self.client.get("/api/config")
        compatible = set(r.json()["compatible_presets"])
        self.assertIn("remote-kisski", compatible)
        self.assertIn("remote-mpcdf", compatible)
        self.assertNotIn("remote-openai", compatible)  # different embedding model
        self.assertNotIn("cpu-only", compatible)  # local preset
        # windows-test/apple-silicon-kisski share the embedding model too, but
        # are each platform-gated (see test_get_config_hides_other_platforms_presets
        # below) — only the one matching this host's actual platform shows up.
        from backend.config.presets import current_platform
        host = current_platform()
        self.assertEqual("windows-test" in compatible, host == "windows")
        self.assertEqual("apple-silicon-kisski" in compatible, host == "darwin")

    def test_get_config_lists_compatible_presets_includes_runpod_despite_hf_prefix(self):
        """runpod.json stores its embedding model as the full HuggingFace repo
        id ("intfloat/multilingual-e5-large-instruct", required by the RunPod
        worker image's API), while remote-kisski/remote-mpcdf store the short
        served-model alias ("multilingual-e5-large-instruct") — same
        underlying model and vector space, different literal string. The
        compatibility check must treat these as the same model (comparing by
        basename) rather than rejecting runpod via a literal string mismatch."""
        r = self.client.get("/api/config")
        self.assertIn("runpod", set(r.json()["compatible_presets"]))

    def test_get_config_hides_other_platforms_presets(self):
        """available_presets/compatible_presets never include a preset whose
        `platform` field names a different OS than current_platform()."""
        from unittest.mock import patch
        with patch("backend.api.config.current_platform", return_value="linux"):
            r = self.client.get("/api/config")
        available = set(r.json()["available_presets"])
        self.assertNotIn("windows-test", available)
        self.assertNotIn("apple-silicon-kisski", available)
        self.assertNotIn("apple-silicon-32gb", available)
        self.assertIn("remote-kisski", available)  # platform: "any"
        self.assertIn("cpu-only", available)  # platform: "any"

    def test_post_config_rejects_preset_hidden_on_this_platform(self):
        from backend.services.zotero_identity import ZoteroIdentity
        from unittest.mock import patch
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        with patch("backend.api.config.current_platform", return_value="linux"):
            r = self.client.post("/api/config", json={"preset_name": "windows-test"})
        self.assertEqual(r.status_code, 400)

    def test_post_config_requires_admin(self):
        from unittest.mock import AsyncMock
        from backend.services.zotero_identity import ZoteroIdentity
        get_settings().api_host = "rag.example.com"
        get_settings().authorized_group_id = 999
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=identity)), \
             patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=False)):
            r = self.client.post(
                "/api/config", json={"preset_name": "remote-mpcdf"},
                headers={"X-Zotero-API-Key": "K"},
            )
        self.assertEqual(r.status_code, 403)

    def test_post_config_switches_to_compatible_preset_as_admin(self):
        from backend.services.zotero_identity import ZoteroIdentity
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        with patch.dict(os.environ, MPCDF_ENV):
            r = self.client.post("/api/config", json={"preset_name": "remote-mpcdf"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["preset_name"], "remote-mpcdf")
        r2 = self.client.get("/api/config")
        self.assertEqual(r2.json()["preset_name"], "remote-mpcdf")

    def test_post_config_rejects_incompatible_preset_as_admin(self):
        from backend.services.zotero_identity import ZoteroIdentity
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        r = self.client.post("/api/config", json={"preset_name": "remote-openai"})
        self.assertEqual(r.status_code, 400)

    def test_post_config_rejects_unknown_preset_as_admin(self):
        from backend.services.zotero_identity import ZoteroIdentity
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        r = self.client.post("/api/config", json={"preset_name": "no-such-preset"})
        self.assertEqual(r.status_code, 400)

    def test_required_keys_reports_shared_kind_and_is_set_for_mpcdf(self):
        from backend.services.zotero_identity import ZoteroIdentity
        from backend.services.admin_settings_store import set_active_preset_override
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        set_active_preset_override(get_settings().data_path, "remote-mpcdf")
        r = self.client.get("/api/required-keys")
        self.assertEqual(r.status_code, 200)
        by_key = {k["key_name"]: k for k in r.json()["keys"]}
        self.assertEqual(by_key["MPCDF_EMBEDDING_BASE_URL"]["kind"], "shared_base_url")
        self.assertFalse(by_key["MPCDF_EMBEDDING_BASE_URL"]["is_set"])

    def test_remote_fields_requires_admin(self):
        from unittest.mock import AsyncMock
        from backend.services.zotero_identity import ZoteroIdentity
        get_settings().api_host = "rag.example.com"
        get_settings().authorized_group_id = 999
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=identity)), \
             patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=False)):
            r = self.client.post(
                "/api/config/remote-fields",
                json={"values": {"MPCDF_EMBEDDING_BASE_URL": "https://llm.mpcdf.mpg.de/abc/v1"}},
                headers={"X-Zotero-API-Key": "K"},
            )
        self.assertEqual(r.status_code, 403)

    def test_remote_fields_as_admin_sets_value_and_is_reflected_in_required_keys(self):
        from backend.services.zotero_identity import ZoteroIdentity
        from backend.services.admin_settings_store import set_active_preset_override
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        set_active_preset_override(get_settings().data_path, "remote-mpcdf")
        r = self.client.post(
            "/api/config/remote-fields",
            json={"values": {"MPCDF_EMBEDDING_BASE_URL": "https://llm.mpcdf.mpg.de/abc/v1"}},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["is_set"]["MPCDF_EMBEDDING_BASE_URL"])
        r2 = self.client.get("/api/required-keys")
        by_key = {k["key_name"]: k for k in r2.json()["keys"]}
        self.assertTrue(by_key["MPCDF_EMBEDDING_BASE_URL"]["is_set"])

    def test_remote_fields_rejects_unknown_key_as_admin(self):
        from backend.services.zotero_identity import ZoteroIdentity
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        self.client.post("/api/config", json={"preset_name": "remote-mpcdf"})
        r = self.client.post(
            "/api/config/remote-fields",
            json={"values": {"NOT_A_REAL_FIELD": "x"}},
        )
        self.assertEqual(r.status_code, 400)

    def test_remote_fields_rejects_value_not_matching_declared_pattern(self):
        """runpod.json declares shared_base_url_pattern/shared_api_key_pattern
        for its RunPod fields. A pasted-wrong-thing value (e.g. the dashboard
        URL instead of the API base URL) must be rejected immediately with a
        400, not silently stored and only surface as a connection error on
        the next real query."""
        from backend.services.zotero_identity import ZoteroIdentity
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        self.client.post("/api/config", json={"preset_name": "runpod"})
        r = self.client.post(
            "/api/config/remote-fields",
            json={"values": {"RUNPOD_EMBEDDING_BASE_URL": "https://www.runpod.io/console/serverless"}},
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("RUNPOD_EMBEDDING_BASE_URL", r.json()["detail"])

    def _switch_to_runpod(self):
        """Switch the active preset to runpod; devel's POST /api/config rejects a
        target preset that has no credentials, so provide them first."""
        from backend.services.admin_settings_store import update_remote_config
        update_remote_config(get_settings().data_path, {
            "RUNPOD_API_KEY": "rp_test",
            "RUNPOD_EMBEDDING_BASE_URL": "https://api.runpod.ai/v2/abc123/openai/v1",
            "RUNPOD_LLM_BASE_URL": "https://api.runpod.ai/v2/def456/openai/v1",
        })
        r = self.client.post("/api/config", json={"preset_name": "runpod"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_remote_fields_accepts_value_matching_declared_pattern(self):
        from backend.services.zotero_identity import ZoteroIdentity
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        self._switch_to_runpod()
        r = self.client.post(
            "/api/config/remote-fields",
            json={"values": {"RUNPOD_EMBEDDING_BASE_URL": "https://api.runpod.ai/v2/abc123/openai/v1"}},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["is_set"]["RUNPOD_EMBEDDING_BASE_URL"])

    def test_required_keys_omits_provisioned_base_urls_for_runpod(self):
        """runpod's endpoint URLs come from POST /api/config/provision, so the
        Preferences pane only asks for the shared API key."""
        from backend.services.zotero_identity import ZoteroIdentity
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        self._switch_to_runpod()
        r = self.client.get("/api/required-keys")
        key_names = [k["key_name"] for k in r.json()["keys"]]
        self.assertEqual(key_names, ["RUNPOD_API_KEY"])

    def test_provisionable_preset_is_switchable_without_shared_values(self):
        """A fresh runpod setup has no stored URLs/key yet; the switch must
        still be allowed, since provisioning (only offered once runpod is
        active) is what supplies them."""
        from backend.services.zotero_identity import ZoteroIdentity
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        with patch.dict(os.environ, {}, clear=False):
            for name in ("RUNPOD_API_KEY", "RUNPOD_EMBEDDING_BASE_URL", "RUNPOD_LLM_BASE_URL"):
                os.environ.pop(name, None)
            r = self.client.post("/api/config", json={"preset_name": "runpod"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_remote_fields_without_declared_pattern_accepts_any_value(self):
        """mpcdf fields declare no pattern — no regression in the unconstrained case."""
        from backend.services.zotero_identity import ZoteroIdentity
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        from backend.services.admin_settings_store import update_remote_config
        update_remote_config(get_settings().data_path, {
            "MPCDF_EMBEDDING_BASE_URL": "https://e/v1", "MPCDF_EMBEDDING_API_KEY": "k",
            "MPCDF_LLM_BASE_URL": "https://l/v1", "MPCDF_LLM_API_KEY": "k",
        })
        r = self.client.post("/api/config", json={"preset_name": "remote-mpcdf"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(
            "/api/config/remote-fields",
            json={"values": {"MPCDF_EMBEDDING_BASE_URL": "anything-goes"}},
        )
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()


class TestSwitchablePresets(unittest.TestCase):
    """switchable_presets / credential check / cache reset on GET+POST /api/config."""

    def setUp(self):
        from backend.main import app
        reset_settings()
        reset_identity_cache()
        reset_admin_role_cache()
        self.tmp = tempfile.TemporaryDirectory()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        ensure_default_presets(s.data_path)
        s.model_preset = "remote-kisski"
        s.autoindex_secret = None
        self.app = app
        from fastapi.testclient import TestClient
        self.client = TestClient(app)
        for k in list(MPCDF_ENV) + ["KISSKI_API_KEY"]:
            os.environ.pop(k, None)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()
        reset_identity_cache()
        reset_admin_role_cache()

    def _admin(self):
        from backend.dependencies import require_authorized_group_admin
        from backend.services.zotero_identity import ZoteroIdentity
        ident = ZoteroIdentity(user_id=1, username="admin", targets=["users/1"])
        self.app.dependency_overrides[require_authorized_group_admin] = lambda: ident

    def _names(self, body):
        return {p["name"]: p for p in body["switchable_presets"]}

    def test_active_always_listed_and_credentialless_excluded(self):
        body = self.client.get("/api/config").json()
        by = self._names(body)
        self.assertIn("remote-kisski", by)
        self.assertTrue(by["remote-kisski"]["active"])
        self.assertEqual(by["remote-kisski"]["credentials"], "missing")
        self.assertNotIn("remote-mpcdf", by)
        self.assertIn("remote-mpcdf", body["compatible_presets"])

    def test_shared_creds_via_env_make_preset_switchable(self):
        with patch.dict(os.environ, MPCDF_ENV):
            by = self._names(self.client.get("/api/config").json())
        self.assertEqual(by["remote-mpcdf"]["credentials"], "ok")
        self.assertFalse(by["remote-mpcdf"]["active"])

    def test_shared_creds_via_store(self):
        from backend.services.admin_settings_store import update_remote_config
        update_remote_config(get_settings().data_path, MPCDF_ENV)
        by = self._names(self.client.get("/api/config").json())
        self.assertIn("remote-mpcdf", by)

    def test_personal_key_via_header_env_and_llm_side(self):
        from backend.api.config import _preset_credentials
        s = get_settings()
        preset = get_preset("remote-kisski", s.data_path)

        class Req:
            def __init__(self, h): self.headers = h
        self.assertEqual(_preset_credentials(preset, s, Req({})), ["KISSKI_API_KEY"])
        self.assertEqual(_preset_credentials(preset, s, Req({"X-Kisski-Api-Key": "v"})), [])
        with patch.dict(os.environ, {"KISSKI_API_KEY": "v"}):
            self.assertEqual(_preset_credentials(preset, s, Req({})), [])
        mp = get_preset("remote-mpcdf", s.data_path)
        with patch.dict(os.environ, {"MPCDF_EMBEDDING_BASE_URL": "u", "MPCDF_EMBEDDING_API_KEY": "k"}):
            # LLM-side shared values still missing
            self.assertEqual(sorted(_preset_credentials(mp, s, Req({}))), ["MPCDF_LLM_API_KEY", "MPCDF_LLM_BASE_URL"])

    def test_personal_key_via_stored_key_but_not_invalid(self):
        from backend.api.config import _preset_credentials
        from backend.services.autoindex_key_store import AutoIndexKeyStore
        from backend.zotero.key_validator import KeyValidation
        from cryptography.fernet import Fernet
        s = get_settings()
        s.autoindex_secret = Fernet.generate_key().decode()
        store = AutoIndexKeyStore(s.autoindex_keys_path, s.autoindex_secret)
        fp = store.add("zkey", KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True))
        store.set_embedding_key(fp, "ekey", "KISSKI_API_KEY", status="invalid")
        preset = get_preset("remote-kisski", s.data_path)

        class Req:
            headers: dict = {}
        self.assertEqual(_preset_credentials(preset, s, Req()), ["KISSKI_API_KEY"])
        store.set_embedding_key_status(fp, "ok")
        self.assertEqual(_preset_credentials(preset, s, Req()), [])

    def test_post_rejects_missing_credentials_names_only(self):
        self._admin()
        r = self.client.post("/api/config", json={"preset_name": "remote-mpcdf"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("MPCDF_EMBEDDING_BASE_URL", r.json()["detail"])
        self.assertNotIn("http://x", r.json()["detail"])

    def test_post_success_resets_cache_and_clears_rate_limits(self):
        import backend.services.embeddings as emb
        from backend.services.autoindex_key_store import AutoIndexKeyStore
        from backend.zotero.key_validator import KeyValidation
        from cryptography.fernet import Fernet
        s = get_settings()
        s.autoindex_secret = Fernet.generate_key().decode()
        store = AutoIndexKeyStore(s.autoindex_keys_path, s.autoindex_secret)
        fp = store.add("zkey", KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True))
        store.set_embedding_key(fp, "ekey", "KISSKI_API_KEY")
        store.set_embedding_key_status(fp, "rate_limited", "2999-01-01T00:00:00+00:00")
        emb._last_rate_limit_headers = {"x-ratelimit-limit-hour": "1"}
        self._admin()
        with patch.dict(os.environ, MPCDF_ENV):
            r = self.client.post("/api/config", json={"preset_name": "remote-mpcdf"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(emb._last_rate_limit_headers)
        meta = store.list_metadata()[0]
        self.assertEqual(meta["embedding_key_status"], "ok")
        self.assertIsNone(meta["embedding_key_rate_limit_until"])

    def test_post_still_403_for_non_admin(self):
        from unittest.mock import AsyncMock
        from backend.services.zotero_identity import ZoteroIdentity
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=identity)), \
             patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=False)):
            r = self.client.post("/api/config", json={"preset_name": "remote-mpcdf"},
                                 headers={"X-Zotero-API-Key": "K"})
        self.assertEqual(r.status_code, 403)

    def test_key_store_clear_keeps_invalid(self):
        from backend.services.autoindex_key_store import AutoIndexKeyStore
        from backend.zotero.key_validator import KeyValidation
        from cryptography.fernet import Fernet
        store = AutoIndexKeyStore(Path(self.tmp.name) / "k.json", Fernet.generate_key().decode())
        fp = store.add("zkey", KeyValidation(user_id=1, username="u", targets=["users/1"], read_only=True))
        store.set_embedding_key(fp, "ekey", "KISSKI_API_KEY", status="invalid")
        self.assertEqual(store.clear_rate_limits(), 0)
        self.assertEqual(store.list_metadata()[0]["embedding_key_status"], "invalid")
        self.assertEqual(store.count_embedding_keys_by_name(), {})
