"""A Claude model on the anthropic provider is a valid preset (core warns otherwise)."""

import json
import logging
import tempfile
import unittest
from pathlib import Path

from backend.config import presets as presets_module
from backend.config.presets import PRESET_SCHEMA_VERSION, ensure_default_presets, get_preset


class ClaudePresetTest(unittest.TestCase):
    def test_claude_model_with_the_anthropic_provider_is_not_warned_about(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp).resolve()
            presets_module._warned_custom_presets.clear()
            ensure_default_presets(data_path)
            data = {
                "version": PRESET_SCHEMA_VERSION, "description": "custom",
                "embedding": {"model_type": "local", "model_name": "x"},
                "llm": {"model_type": "remote", "model_names": ["claude-sonnet-4-5"], "provider": {"id": "anthropic"}},
                "rag": {}, "memory_budget_gb": 1.0, "platform": "any",
            }
            (data_path / "presets" / "claude-new.json").write_text(json.dumps(data))
            with self.assertNoLogs(presets_module.logger, level=logging.WARNING):
                get_preset("claude-new", data_path)


if __name__ == "__main__":
    unittest.main()
