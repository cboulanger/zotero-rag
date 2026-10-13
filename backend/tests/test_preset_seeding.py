"""Tests for preset schema versioning, provider config and bundled-preset re-seeding."""

import json
import logging
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from backend.config import presets as presets_module
from backend.config.presets import (
    DEFAULT_PRESETS_DIR,
    PRESET_SCHEMA_VERSION,
    EmbeddingConfig,
    HardwarePreset,
    LLMConfig,
    ProviderConfig,
    ensure_default_presets,
    get_preset,
    list_presets,
)


def _minimal_preset(**extra) -> dict:
    data = {
        "description": "custom",
        "embedding": {"model_type": "local", "model_name": "x"},
        "llm": {"model_type": "local", "model_names": ["y"]},
        "rag": {},
        "memory_budget_gb": 1.0,
    }
    data.update(extra)
    return data


class TestProviderSchema(unittest.TestCase):
    def test_schema_version_constant(self):
        self.assertEqual(PRESET_SCHEMA_VERSION, 2)

    def test_side_configs_default_to_generic_provider(self):
        emb = EmbeddingConfig(model_name="m")
        llm = LLMConfig(model_names=["m"])
        for side in (emb, llm):
            self.assertEqual(side.provider.id, "generic")
            self.assertIsNone(side.provider.scope)
            self.assertEqual(side.provider.options, {})

    def test_side_configs_accept_a_provider_block(self):
        emb = EmbeddingConfig.model_validate({
            "model_name": "m",
            "provider": {"id": "runpod", "scope": "managed", "options": {"gpu": "A"}},
        })
        self.assertEqual(emb.provider.id, "runpod")
        self.assertEqual(emb.provider.scope, "managed")
        self.assertEqual(emb.provider.options, {"gpu": "A"})

    def test_unknown_scope_is_rejected(self):
        with self.assertRaises(ValidationError):
            ProviderConfig(id="x", scope="everyone")

    def test_preset_version_defaults_to_one_when_absent(self):
        data = _minimal_preset()
        data["name"] = "custom"
        self.assertEqual(HardwarePreset.model_validate(data).version, 1)

    def test_every_bundled_preset_declares_the_current_version(self):
        for path in DEFAULT_PRESETS_DIR.glob("*.json"):
            with self.subTest(preset=path.stem):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data.get("version"), PRESET_SCHEMA_VERSION)


class _SeedingBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name).resolve()
        self.presets_dir = self.data_path / "presets"
        presets_module._seeded_paths.discard(self.data_path)
        presets_module._warned_custom_presets.clear()

    def tearDown(self):
        presets_module._seeded_paths.discard(self.data_path)
        presets_module._warned_custom_presets.clear()
        self._tmp.cleanup()

    def reseed(self):
        presets_module._seeded_paths.discard(self.data_path)
        ensure_default_presets(self.data_path)


class TestBundledReseeding(_SeedingBase):
    def test_modified_bundled_file_is_overwritten_and_warned_about(self):
        ensure_default_presets(self.data_path)
        target = self.presets_dir / "cpu-only.json"
        shipped = (DEFAULT_PRESETS_DIR / "cpu-only.json").read_bytes()
        target.write_text('{"description": "edited"}')

        with self.assertLogs(presets_module.logger, level=logging.WARNING) as logs:
            self.reseed()

        self.assertEqual(target.read_bytes(), shipped)
        self.assertTrue(any("cpu-only.json" in m and "modified locally" in m for m in logs.output))

    def test_identical_bundled_file_is_not_rewritten(self):
        ensure_default_presets(self.data_path)
        target = self.presets_dir / "cpu-only.json"
        before = target.stat().st_mtime_ns

        with self.assertNoLogs(presets_module.logger, level=logging.WARNING):
            self.reseed()

        self.assertEqual(target.stat().st_mtime_ns, before)

    def test_deleted_bundled_file_is_recreated(self):
        ensure_default_presets(self.data_path)
        (self.presets_dir / "cpu-only.json").unlink()
        self.reseed()
        self.assertTrue((self.presets_dir / "cpu-only.json").exists())

    def test_custom_file_is_never_touched(self):
        ensure_default_presets(self.data_path)
        custom = self.presets_dir / "my-preset.json"
        custom.write_text(json.dumps(_minimal_preset(version=PRESET_SCHEMA_VERSION)))
        before = custom.read_bytes()
        self.reseed()
        self.assertEqual(custom.read_bytes(), before)

    def test_no_temp_files_are_left_behind(self):
        ensure_default_presets(self.data_path)
        self.assertEqual(list(self.presets_dir.glob("*.tmp")), [])


class TestCustomPresetVersionWarnings(_SeedingBase):
    def _write_custom(self, name: str, data: dict) -> None:
        ensure_default_presets(self.data_path)
        (self.presets_dir / f"{name}.json").write_text(json.dumps(data))

    def test_missing_version_warns_once_per_process(self):
        self._write_custom("mine", _minimal_preset())
        with self.assertLogs(presets_module.logger, level=logging.WARNING) as logs:
            get_preset("mine", self.data_path)
            presets_module._preset_cache.clear()
            get_preset("mine", self.data_path)
        warnings = [m for m in logs.output if "Custom preset mine.json" in m]
        self.assertEqual(len(warnings), 1)
        self.assertIn("version", warnings[0])

    def test_old_version_warns_and_still_loads(self):
        self._write_custom("mine", _minimal_preset(version=1))
        with self.assertLogs(presets_module.logger, level=logging.WARNING) as logs:
            preset = get_preset("mine", self.data_path)
        self.assertEqual(preset.name, "mine")
        self.assertTrue(any("declares version 1" in m for m in logs.output))

    def test_current_version_does_not_warn(self):
        self._write_custom("mine", _minimal_preset(version=PRESET_SCHEMA_VERSION))
        with self.assertNoLogs(presets_module.logger, level=logging.WARNING):
            get_preset("mine", self.data_path)

    def test_bundled_files_are_not_treated_as_custom(self):
        ensure_default_presets(self.data_path)
        with self.assertNoLogs(presets_module.logger, level=logging.WARNING):
            get_preset("cpu-only", self.data_path)

    def test_removed_keys_are_named_in_the_warning(self):
        original = dict(presets_module.REMOVED_KEYS)
        presets_module.REMOVED_KEYS[PRESET_SCHEMA_VERSION] = [
            ("provisioning_script", "use the per-side provider block"),
            ("llm.models_status_url", "now a provider capability"),
        ]
        try:
            data = _minimal_preset(version=1, provisioning_script="bin/x.py")
            data["llm"]["models_status_url"] = "http://x"
            self._write_custom("old", data)
            with self.assertLogs(presets_module.logger, level=logging.WARNING) as logs:
                preset = get_preset("old", self.data_path)
        finally:
            presets_module.REMOVED_KEYS.clear()
            presets_module.REMOVED_KEYS.update(original)
        self.assertEqual(preset.name, "old")
        joined = " ".join(logs.output)
        self.assertIn("provisioning_script", joined)
        self.assertIn("llm.models_status_url", joined)

    def test_claude_model_without_the_anthropic_provider_is_warned_about(self):
        data = _minimal_preset(version=PRESET_SCHEMA_VERSION)
        data["llm"] = {"model_type": "remote", "model_names": ["claude-sonnet-4-5"]}
        self._write_custom("claude-old", data)
        with self.assertLogs(presets_module.logger, level=logging.WARNING) as logs:
            get_preset("claude-old", self.data_path)
        self.assertTrue(any("claude-sonnet-4-5" in m and "anthropic" in m for m in logs.output))

    def test_claude_model_with_the_anthropic_provider_is_not_warned_about(self):
        data = _minimal_preset(version=PRESET_SCHEMA_VERSION)
        data["llm"] = {"model_type": "remote", "model_names": ["claude-sonnet-4-5"],
                       "provider": {"id": "anthropic"}}
        self._write_custom("claude-new", data)
        with self.assertNoLogs(presets_module.logger, level=logging.WARNING):
            get_preset("claude-new", self.data_path)

    def test_list_presets_triggers_the_check_too(self):
        self._write_custom("mine", _minimal_preset())
        with self.assertLogs(presets_module.logger, level=logging.WARNING) as logs:
            self.assertIn("mine", list_presets(self.data_path))
        self.assertTrue(any("mine.json" in m for m in logs.output))


if __name__ == "__main__":
    unittest.main()
