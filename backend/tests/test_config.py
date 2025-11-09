"""
Unit tests for configuration system.
"""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.config.presets import get_preset, list_presets, PRESETS
from backend.config.settings import Settings, get_settings, reset_settings


class TestPresets(unittest.TestCase):
    """Test hardware presets."""

    def test_get_preset_mac_mini(self):
        """Test getting mac-mini-m4-16gb preset."""
        preset = get_preset("mac-mini-m4-16gb")

        self.assertEqual(preset.name, "mac-mini-m4-16gb")
        self.assertEqual(preset.embedding.model_type, "local")
        self.assertEqual(preset.embedding.model_name, "nomic-ai/nomic-embed-text-v1.5")
        self.assertEqual(preset.llm.model_type, "local")
        self.assertEqual(preset.llm.model_name, "Qwen/Qwen2.5-3B-Instruct")
        self.assertEqual(preset.llm.quantization, "4bit")
        self.assertLessEqual(preset.memory_budget_gb, 8.0)

    def test_get_preset_gpu_high_memory(self):
        """Test getting gpu-high-memory preset."""
        preset = get_preset("gpu-high-memory")

        self.assertEqual(preset.name, "gpu-high-memory")
        self.assertEqual(preset.llm.model_name, "mistralai/Mistral-7B-Instruct-v0.3")
        self.assertEqual(preset.llm.quantization, "8bit")

    def test_get_preset_cpu_only(self):
        """Test getting cpu-only preset."""
        preset = get_preset("cpu-only")

        self.assertEqual(preset.name, "cpu-only")
        self.assertEqual(preset.embedding.model_name, "sentence-transformers/all-MiniLM-L6-v2")
        self.assertEqual(preset.llm.model_name, "TinyLlama/TinyLlama-1.1B-Chat-v1.0")

    def test_get_preset_remote_openai(self):
        """Test getting remote-openai preset."""
        preset = get_preset("remote-openai")

        self.assertEqual(preset.name, "remote-openai")
        self.assertEqual(preset.embedding.model_type, "remote")
        self.assertEqual(preset.llm.model_type, "remote")

    def test_get_preset_remote_kisski(self):
        """Test getting remote-kisski preset."""
        preset = get_preset("remote-kisski")

        self.assertEqual(preset.name, "remote-kisski")
        self.assertEqual(preset.embedding.model_type, "local")  # Use local embeddings
        self.assertEqual(preset.llm.model_type, "remote")
        self.assertEqual(preset.llm.model_name, "meta-llama/Llama-3.3-70B-Instruct")
        self.assertEqual(preset.llm.model_kwargs["base_url"], "https://chat-ai.academiccloud.de/v1")
        self.assertEqual(preset.llm.model_kwargs["api_key_env"], "KISSKI_API_KEY")

    def test_get_preset_invalid(self):
        """Test getting invalid preset raises error."""
        with self.assertRaises(ValueError) as ctx:
            get_preset("invalid-preset")

        self.assertIn("Unknown preset", str(ctx.exception))

    def test_list_presets(self):
        """Test listing all presets."""
        presets = list_presets()

        self.assertIn("mac-mini-m4-16gb", presets)
        self.assertIn("gpu-high-memory", presets)
        self.assertIn("cpu-only", presets)
        self.assertIn("remote-openai", presets)
        self.assertIn("remote-kisski", presets)
        self.assertEqual(len(presets), len(PRESETS))


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
        settings = Settings()

        self.assertEqual(settings.api_host, "localhost")
        self.assertEqual(settings.api_port, 8119)
        self.assertEqual(settings.model_preset, "cpu-only")
        self.assertEqual(settings.log_level, "INFO")
        self.assertEqual(settings.version, "0.1.0")

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

    def test_invalid_log_level(self):
        """Test that invalid log level raises error."""
        with self.assertRaises(ValueError):
            with patch.dict(os.environ, {"LOG_LEVEL": "INVALID"}):
                Settings()

    def test_get_hardware_preset(self):
        """Test getting hardware preset from settings."""
        settings = Settings(model_preset="gpu-high-memory")
        preset = settings.get_hardware_preset()

        self.assertEqual(preset.name, "gpu-high-memory")

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


if __name__ == "__main__":
    unittest.main()
