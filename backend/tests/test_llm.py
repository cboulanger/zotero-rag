"""
Unit tests for LLM service.
"""

import unittest
from unittest.mock import AsyncMock, Mock, patch, MagicMock
import os

from backend.services.llm import (
    LLMService,
    LocalLLMService,
    RemoteLLMService,
    create_llm_service,
)
from backend.config.settings import Settings
from backend.config.presets import HardwarePreset, LLMConfig, EmbeddingConfig, RAGConfig


class TestLLMServiceFactory(unittest.IsolatedAsyncioTestCase):
    """Test LLM service factory function."""

    def setUp(self):
        """Set up test fixtures."""
        # Create mock settings with local preset
        self.local_preset = HardwarePreset(
            name="test-local",
            description="Test local preset",
            embedding=EmbeddingConfig(
                model_type="local",
                model_name="test-embedding",
            ),
            llm=LLMConfig(
                model_type="local",
                model_name="test-llm",
                quantization="4bit",
            ),
            rag=RAGConfig(),
            memory_budget_gb=4.0,
        )

        self.remote_preset = HardwarePreset(
            name="test-remote",
            description="Test remote preset",
            embedding=EmbeddingConfig(
                model_type="remote",
                model_name="openai",
            ),
            llm=LLMConfig(
                model_type="remote",
                model_name="gpt-4o-mini",
            ),
            rag=RAGConfig(),
            memory_budget_gb=1.0,
        )

    async def test_create_local_llm_service(self):
        """Test creating local LLM service."""
        mock_settings = Mock(spec=Settings)
        mock_settings.get_hardware_preset.return_value = self.local_preset

        service = create_llm_service(mock_settings)

        self.assertIsInstance(service, LocalLLMService)
        self.assertEqual(service.llm_config.model_type, "local")

    async def test_create_remote_llm_service(self):
        """Test creating remote LLM service."""
        mock_settings = Mock(spec=Settings)
        mock_settings.get_hardware_preset.return_value = self.remote_preset

        service = create_llm_service(mock_settings)

        self.assertIsInstance(service, RemoteLLMService)
        self.assertEqual(service.llm_config.model_type, "remote")


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
                model_name="test-model",
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


class TestRemoteLLMService(unittest.IsolatedAsyncioTestCase):
    """Test RemoteLLMService class."""

    def setUp(self):
        """Set up test fixtures."""
        self.openai_preset = HardwarePreset(
            name="test-openai",
            description="Test OpenAI preset",
            embedding=EmbeddingConfig(
                model_type="remote",
                model_name="openai",
            ),
            llm=LLMConfig(
                model_type="remote",
                model_name="gpt-4o-mini",
                temperature=0.7,
            ),
            rag=RAGConfig(),
            memory_budget_gb=1.0,
        )

        self.anthropic_preset = HardwarePreset(
            name="test-anthropic",
            description="Test Anthropic preset",
            embedding=EmbeddingConfig(
                model_type="remote",
                model_name="openai",
            ),
            llm=LLMConfig(
                model_type="remote",
                model_name="claude-3-5-sonnet-20241022",
                temperature=0.7,
            ),
            rag=RAGConfig(),
            memory_budget_gb=1.0,
        )

        self.mock_openai_settings = Mock(spec=Settings)
        self.mock_openai_settings.get_hardware_preset.return_value = self.openai_preset

        self.mock_anthropic_settings = Mock(spec=Settings)
        self.mock_anthropic_settings.get_hardware_preset.return_value = self.anthropic_preset

    async def test_init(self):
        """Test initialization."""
        service = RemoteLLMService(
            self.mock_openai_settings,
            api_key="test-key",
        )

        self.assertIsNone(service._openai_client)
        self.assertIsNone(service._anthropic_client)
        self.assertEqual(service.api_key, "test-key")

    async def test_generate_openai(self):
        """Test generation with OpenAI API."""
        service = RemoteLLMService(self.mock_openai_settings, api_key="test-key")

        # Mock OpenAI client
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message.content = "Generated answer from OpenAI"
        mock_client.chat.completions.create.return_value = mock_response

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await service.generate("Test prompt", max_tokens=100, temperature=0.5)

        self.assertEqual(result, "Generated answer from OpenAI")
        mock_client.chat.completions.create.assert_called_once()

    async def test_generate_anthropic(self):
        """Test generation with Anthropic API."""
        service = RemoteLLMService(self.mock_anthropic_settings, api_key="test-key")

        # Mock Anthropic client
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.content = [Mock()]
        mock_response.content[0].text = "Generated answer from Claude"
        mock_client.messages.create.return_value = mock_response

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            result = await service.generate("Test prompt", max_tokens=100, temperature=0.5)

        self.assertEqual(result, "Generated answer from Claude")
        mock_client.messages.create.assert_called_once()

    async def test_generate_unsupported_model(self):
        """Test error handling for unsupported model."""
        unsupported_preset = HardwarePreset(
            name="test-unsupported",
            description="Test unsupported preset",
            embedding=EmbeddingConfig(
                model_type="remote",
                model_name="openai",
            ),
            llm=LLMConfig(
                model_type="remote",
                model_name="unknown-model-xyz",
                temperature=0.7,
            ),
            rag=RAGConfig(),
            memory_budget_gb=1.0,
        )

        mock_settings = Mock(spec=Settings)
        mock_settings.get_hardware_preset.return_value = unsupported_preset

        service = RemoteLLMService(mock_settings, api_key="test-key")

        with self.assertRaises(RuntimeError) as context:
            await service.generate("Test prompt")

        self.assertIn("Unsupported remote model", str(context.exception))

    async def test_openai_missing_api_key(self):
        """Test error handling when OpenAI API key is missing."""
        service = RemoteLLMService(self.mock_openai_settings)

        # Ensure no API key in environment
        with patch.dict(os.environ, {}, clear=True):
            # The service will raise an error when trying to create the client
            with self.assertRaises(RuntimeError) as context:
                await service.generate("Test prompt")

            # Should fail because no API key
            self.assertTrue(
                "API key not provided" in str(context.exception) or
                "Missing openai package" in str(context.exception) or
                "generation failed" in str(context.exception).lower()
            )

    async def test_anthropic_missing_api_key(self):
        """Test error handling when Anthropic API key is missing."""
        service = RemoteLLMService(self.mock_anthropic_settings)

        # Ensure no API key in environment
        with patch.dict(os.environ, {}, clear=True):
            # The service will raise an error when trying to create the client
            with self.assertRaises(RuntimeError) as context:
                await service.generate("Test prompt")

            # Should fail because no API key
            self.assertTrue(
                "API key not provided" in str(context.exception) or
                "Missing anthropic package" in str(context.exception) or
                "generation failed" in str(context.exception).lower()
            )

    async def test_generate_with_defaults(self):
        """Test generation uses config defaults when parameters not specified."""
        service = RemoteLLMService(self.mock_openai_settings, api_key="test-key")

        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message.content = "Generated answer"
        mock_client.chat.completions.create.return_value = mock_response

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await service.generate("Test prompt")  # No max_tokens, temperature

        # Should use defaults from config
        call_args = mock_client.chat.completions.create.call_args
        self.assertEqual(call_args.kwargs["temperature"], 0.7)  # From preset
        self.assertEqual(call_args.kwargs["max_tokens"], 512)  # Default


if __name__ == "__main__":
    unittest.main()
