"""Unit tests for backend.dependencies.make_llm_service's auto_select_model
support -- resolves candidate models without hardcoding any model name, and
wraps them in an AutoSelectLLMService."""

import unittest
from unittest.mock import MagicMock, Mock, patch

from backend.config.presets import EmbeddingConfig, HardwarePreset, LLMConfig, RAGConfig
from backend.dependencies import _resolve_auto_select_candidates, make_llm_service
from backend.services.llm import AutoSelectLLMService
from backend.utils.kisski import KISSKIModelInfo


def _make_preset(*, models_status_url=None, model_names="model-a,model-b") -> HardwarePreset:
    return HardwarePreset(
        name="test-preset",
        description="test",
        embedding=EmbeddingConfig(model_type="remote", model_name="test-embedding"),
        llm=LLMConfig(
            model_type="remote",
            model_names=model_names,
            model_kwargs={"api_key_env": "TEST_API_KEY", "base_url": "https://example.test/v1"},
            models_status_url=models_status_url,
        ),
        rag=RAGConfig(),
        memory_budget_gb=1.0,
    )


class TestResolveAutoSelectCandidates(unittest.TestCase):
    def test_falls_back_to_static_model_names_when_no_status_url(self):
        preset = _make_preset(models_status_url=None)
        candidates = _resolve_auto_select_candidates(preset, api_key="key")
        self.assertEqual(candidates, ["model-a", "model-b"])

    def test_falls_back_to_static_model_names_when_no_api_key(self):
        preset = _make_preset(models_status_url="https://example.test/v1/models")
        candidates = _resolve_auto_select_candidates(preset, api_key=None)
        self.assertEqual(candidates, ["model-a", "model-b"])

    @patch("backend.dependencies.fetch_kisski_rag_models")
    def test_uses_live_models_excluding_very_busy(self, mock_fetch):
        mock_fetch.return_value = [
            KISSKIModelInfo(id="live-1", name="live-1", demand=0, availability="available"),
            KISSKIModelInfo(id="live-2", name="live-2", demand=3, availability="busy"),
            KISSKIModelInfo(id="live-3", name="live-3", demand=9, availability="very busy"),
        ]
        preset = _make_preset(models_status_url="https://example.test/v1/models")
        candidates = _resolve_auto_select_candidates(preset, api_key="key")
        self.assertEqual(candidates, ["live-1", "live-2"])
        mock_fetch.assert_called_once_with("https://example.test/v1", "key")

    @patch("backend.dependencies.fetch_kisski_rag_models")
    def test_falls_back_to_static_list_when_live_fetch_fails(self, mock_fetch):
        mock_fetch.side_effect = RuntimeError("network error")
        preset = _make_preset(models_status_url="https://example.test/v1/models")
        candidates = _resolve_auto_select_candidates(preset, api_key="key")
        self.assertEqual(candidates, ["model-a", "model-b"])

    @patch("backend.dependencies.fetch_kisski_rag_models")
    def test_falls_back_to_static_list_when_live_fetch_returns_only_very_busy(self, mock_fetch):
        mock_fetch.return_value = [
            KISSKIModelInfo(id="live-1", name="live-1", demand=9, availability="very busy"),
        ]
        preset = _make_preset(models_status_url="https://example.test/v1/models")
        candidates = _resolve_auto_select_candidates(preset, api_key="key")
        self.assertEqual(candidates, ["model-a", "model-b"])


class TestMakeLlmServiceAutoSelect(unittest.TestCase):
    @patch("backend.dependencies.get_settings")
    def test_returns_auto_select_service_with_resolved_candidates(self, mock_get_settings):
        settings = MagicMock()
        settings.testing = False
        settings.get_hardware_preset.return_value = _make_preset(models_status_url=None)
        mock_get_settings.return_value = settings

        service = make_llm_service(auto_select_model=True)

        self.assertIsInstance(service, AutoSelectLLMService)
        self.assertEqual(service._candidates, ["model-a", "model-b"])

    @patch("backend.dependencies.get_settings")
    def test_ignores_model_name_override_in_auto_select_mode(self, mock_get_settings):
        settings = MagicMock()
        settings.testing = False
        settings.get_hardware_preset.return_value = _make_preset(models_status_url=None)
        mock_get_settings.return_value = settings

        service = make_llm_service(auto_select_model=True, model_name_override="ignored-model")

        self.assertIsInstance(service, AutoSelectLLMService)
        self.assertNotIn("ignored-model", service._candidates)

    @patch("backend.dependencies.get_settings")
    def test_default_still_returns_single_fixed_model_service(self, mock_get_settings):
        settings = MagicMock()
        settings.testing = False
        settings.get_hardware_preset.return_value = _make_preset(models_status_url=None)
        mock_get_settings.return_value = settings

        service = make_llm_service()

        self.assertNotIsInstance(service, AutoSelectLLMService)


if __name__ == "__main__":
    unittest.main()
