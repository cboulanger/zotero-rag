"""Local transformers text generation (skipped without the local-models extra)."""

import unittest
from unittest.mock import Mock, patch

from backend.config.presets import EmbeddingConfig, HardwarePreset, LLMConfig, RAGConfig
from backend.config.settings import Settings
from backend.plugins.local_torch.llm import LocalLLMService
from backend.services.llm import create_llm_service

try:
    import transformers  # noqa: F401
    HAS_TRANSFORMERS = True
except ImportError:
    HAS_TRANSFORMERS = False


class TestFactoryUsesTorch(unittest.TestCase):
    def test_a_local_llm_side_gets_the_torch_service(self):
        preset = HardwarePreset(
            name="t", description="t", rag=RAGConfig(), memory_budget_gb=1.0,
            embedding=EmbeddingConfig(model_type="local", model_name="e"),
            llm=LLMConfig(model_type="local", model_names="test-llm"),
        )
        settings = Mock(spec=Settings)
        settings.get_hardware_preset.return_value = preset
        service = create_llm_service(settings)
        self.assertIsInstance(service, LocalLLMService)
        self.assertEqual(service.llm_config.model_type, "local")


@unittest.skipUnless(HAS_TRANSFORMERS, "transformers not installed")
class TestLocalLLMService(unittest.IsolatedAsyncioTestCase):
    """Test LocalLLMService class."""

    def setUp(self):
        """Set up test fixtures."""
        self.preset = HardwarePreset(
            name="test-local",
            description="Test local preset",
            embedding=EmbeddingConfig(
                model_type="local",
                model_name="test-embedding",
            ),
            llm=LLMConfig(
                model_type="local",
                model_names="test-model",
                quantization="4bit",
                temperature=0.7,
            ),
            rag=RAGConfig(),
            memory_budget_gb=4.0,
        )

        self.mock_settings = Mock(spec=Settings)
        self.mock_settings.get_hardware_preset.return_value = self.preset

    async def test_init(self):
        """Test initialization."""
        service = LocalLLMService(
            self.mock_settings,
            cache_dir="/tmp/cache",
            hf_token="test-token",
        )

        self.assertIsNone(service._model)
        self.assertIsNone(service._tokenizer)
        self.assertEqual(service.cache_dir, "/tmp/cache")
        self.assertEqual(service.hf_token, "test-token")

    async def test_generate_with_mocked_model(self):
        """Test generation with mocked transformers."""
        # We need to patch the imports that happen inside _load_model
        with patch("transformers.AutoModelForCausalLM") as mock_model_class, \
             patch("transformers.AutoTokenizer") as mock_tokenizer_class, \
             patch("transformers.BitsAndBytesConfig") as mock_bnb_config, \
             patch("torch.float16", "float16"):

            # Mock input tensor with shape
            mock_input_ids = Mock()
            mock_input_ids.shape = (1, 5)  # 5 input tokens
            mock_input_ids.to = Mock(return_value=mock_input_ids)

            # Mock tokenizer
            mock_tokenizer = Mock()
            mock_tokenizer.return_value = {"input_ids": mock_input_ids}
            mock_tokenizer.eos_token_id = 2
            mock_tokenizer.decode.return_value = "This is the generated answer."
            mock_tokenizer_class.from_pretrained.return_value = mock_tokenizer

            # Mock output tensor (output has 10 total tokens, first 5 are input)
            mock_output_tensor = Mock()
            mock_output_tensor.__getitem__ = Mock(return_value=Mock())  # For slicing [5:]

            # Mock model
            mock_model = Mock()
            mock_model.device = "cpu"
            mock_model.generate.return_value = [mock_output_tensor]
            mock_model_class.from_pretrained.return_value = mock_model

            service = LocalLLMService(self.mock_settings)

            # Test generation
            result = await service.generate("Test prompt", max_tokens=100, temperature=0.5)

            self.assertEqual(result, "This is the generated answer.")
            mock_model.generate.assert_called_once()
            mock_tokenizer.decode.assert_called_once()

    async def test_generate_missing_dependencies(self):
        """Test error handling when dependencies are missing."""
        service = LocalLLMService(self.mock_settings)

        # Patch the import itself
        with patch.dict("sys.modules", {"transformers": None}):
            with self.assertRaises(RuntimeError) as context:
                await service.generate("Test prompt")

            self.assertIn("Missing dependencies", str(context.exception))


if __name__ == "__main__":
    unittest.main()
