"""
Unit tests for LLM service.
"""

import unittest
from unittest.mock import AsyncMock, Mock, patch, MagicMock
import os

import httpx
from openai import APIConnectionError as OpenAIAPIConnectionError

from backend.services.llm import (
    LLMConfigurationError,
    LLMEndpointUnavailableError,
    LLMService,
    LocalLLMService,
    RemoteLLMService,
    create_llm_service,
)
from backend.config.settings import Settings
from backend.config.presets import HardwarePreset, LLMConfig, EmbeddingConfig, ProviderConfig, RAGConfig

try:
    import transformers  # noqa: F401
    HAS_TRANSFORMERS = True
except ImportError:
    HAS_TRANSFORMERS = False


def _raw(parsed):
    """A with_raw_response result: parsed body plus (empty) headers."""
    raw = Mock()
    raw.parse.return_value = parsed
    raw.headers = {}
    return raw


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
                model_names="test-llm",
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
                model_names="gpt-4o-mini",
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
                model_names="gpt-4o-mini",
                temperature=0.7,
                provider=ProviderConfig(id="openai"),
                model_kwargs={"api_key_env": "OPENAI_API_KEY"},
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
                model_names="claude-3-5-sonnet-20241022",
                temperature=0.7,
                provider=ProviderConfig(id="anthropic"),
                model_kwargs={"api_key_env": "ANTHROPIC_API_KEY"},
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

    async def test_required_client_fields_reports_shared_kinds_for_mpcdf_style_preset(self):
        mpcdf_preset = HardwarePreset(
            name="test-mpcdf",
            description="Test MPCDF preset",
            embedding=EmbeddingConfig(model_type="remote", model_name="multilingual-e5-large-instruct"),
            llm=LLMConfig(
                model_type="remote",
                model_names="openai/gpt-oss-120b",
                model_kwargs={
                    "shared_base_url_env": "MPCDF_LLM_BASE_URL",
                    "shared_api_key_env": "MPCDF_LLM_API_KEY",
                },
            ),
            rag=RAGConfig(),
            memory_budget_gb=0.5,
        )
        mock_settings = Mock(spec=Settings)
        mock_settings.get_hardware_preset.return_value = mpcdf_preset

        fields = RemoteLLMService.required_client_fields(mock_settings)
        by_key = {f["key_name"]: f for f in fields}
        self.assertEqual(len(fields), 2)
        self.assertEqual(by_key["MPCDF_LLM_BASE_URL"]["kind"], "shared_base_url")
        self.assertEqual(by_key["MPCDF_LLM_API_KEY"]["kind"], "shared_api_key")

    async def test_get_openai_client_resolves_shared_fields_from_store_over_env(self):
        import os
        import tempfile
        from pathlib import Path
        from backend.services.admin_settings_store import update_remote_config

        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp)
            update_remote_config({
                "MPCDF_LLM_BASE_URL": "https://llm.mpcdf.mpg.de/abc123/v1",
                "MPCDF_LLM_API_KEY": "store-key",
            }, data_path=data_path)
            mpcdf_preset = HardwarePreset(
                name="test-mpcdf",
                description="Test MPCDF preset",
                embedding=EmbeddingConfig(model_type="remote", model_name="multilingual-e5-large-instruct"),
                llm=LLMConfig(
                    model_type="remote",
                    model_names="openai/gpt-oss-120b",
                    model_kwargs={
                        "shared_base_url_env": "MPCDF_LLM_BASE_URL",
                        "shared_api_key_env": "MPCDF_LLM_API_KEY",
                    },
                ),
                rag=RAGConfig(),
                memory_budget_gb=0.5,
            )
            mock_settings = Mock(spec=Settings)
            mock_settings.get_hardware_preset.return_value = mpcdf_preset
            mock_settings.log_file = None
            mock_settings.data_path = data_path

            with patch.dict(os.environ, {"MPCDF_LLM_BASE_URL": "https://should-not-be-used/v1"}):
                service = RemoteLLMService(mock_settings)
                with patch("openai.AsyncOpenAI") as mock_openai_cls:
                    service._get_openai_client()
                    _, kwargs = mock_openai_cls.call_args

        self.assertEqual(kwargs["base_url"], "https://llm.mpcdf.mpg.de/abc123/v1")
        self.assertEqual(kwargs["api_key"], "store-key")

    async def test_get_openai_client_appends_v1_to_shared_base_url_missing_it(self):
        import tempfile
        from pathlib import Path
        from backend.services.admin_settings_store import update_remote_config

        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp)
            update_remote_config({
                "MPCDF_LLM_BASE_URL": "https://llm.mpcdf.mpg.de/abc123",
                "MPCDF_LLM_API_KEY": "store-key",
            }, data_path=data_path)
            mpcdf_preset = HardwarePreset(
                name="test-mpcdf",
                description="Test MPCDF preset",
                embedding=EmbeddingConfig(model_type="remote", model_name="multilingual-e5-large-instruct"),
                llm=LLMConfig(
                    model_type="remote",
                    model_names="openai/gpt-oss-120b",
                    model_kwargs={
                        "shared_base_url_env": "MPCDF_LLM_BASE_URL",
                        "shared_api_key_env": "MPCDF_LLM_API_KEY",
                    },
                ),
                rag=RAGConfig(),
                memory_budget_gb=0.5,
            )
            mock_settings = Mock(spec=Settings)
            mock_settings.get_hardware_preset.return_value = mpcdf_preset
            mock_settings.log_file = None
            mock_settings.data_path = data_path

            service = RemoteLLMService(mock_settings)
            with patch("openai.AsyncOpenAI") as mock_openai_cls:
                service._get_openai_client()
                _, kwargs = mock_openai_cls.call_args

        self.assertEqual(kwargs["base_url"], "https://llm.mpcdf.mpg.de/abc123/v1")

    async def test_get_openai_client_raises_clear_error_when_shared_api_key_unset(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            mpcdf_preset = HardwarePreset(
                name="test-mpcdf",
                description="Test MPCDF preset",
                embedding=EmbeddingConfig(model_type="remote", model_name="multilingual-e5-large-instruct"),
                llm=LLMConfig(
                    model_type="remote",
                    model_names="openai/gpt-oss-120b",
                    model_kwargs={
                        "shared_base_url_env": "MPCDF_LLM_BASE_URL",
                        "shared_api_key_env": "MPCDF_LLM_API_KEY",
                    },
                ),
                rag=RAGConfig(),
                memory_budget_gb=0.5,
            )
            mock_settings = Mock(spec=Settings)
            mock_settings.get_hardware_preset.return_value = mpcdf_preset
            mock_settings.log_file = None
            mock_settings.data_path = Path(tmp)

            service = RemoteLLMService(mock_settings)
            with self.assertRaises(LLMConfigurationError) as ctx:
                service._get_openai_client()
        self.assertIn("MPCDF_LLM_API_KEY", str(ctx.exception))

    async def test_generate_openai(self):
        """Test generation with OpenAI API."""
        service = RemoteLLMService(self.mock_openai_settings, api_key="test-key")

        # Mock OpenAI client
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message.content = "Generated answer from OpenAI"
        mock_client.chat.completions.with_raw_response.create.return_value = _raw(mock_response)

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await service.generate("Test prompt", max_tokens=100, temperature=0.5)

        self.assertEqual(result, "Generated answer from OpenAI")
        mock_client.chat.completions.with_raw_response.create.assert_called_once()

    async def test_generate_anthropic(self):
        """Test generation with Anthropic API."""
        service = RemoteLLMService(self.mock_anthropic_settings, api_key="test-key")

        # Mock Anthropic client
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.content = [Mock()]
        mock_response.content[0].text = "Generated answer from Claude"
        mock_client.messages.with_raw_response.create.return_value = _raw(mock_response)

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            result = await service.generate("Test prompt", max_tokens=100, temperature=0.5)

        self.assertEqual(result, "Generated answer from Claude")
        mock_client.messages.with_raw_response.create.assert_called_once()

    async def test_generate_openai_raises_endpoint_unavailable_on_connection_error(self):
        """A transport-level failure (e.g. a cold/unreachable RunPod serverless
        LLM endpoint) raises openai.APIConnectionError — must become a clear,
        typed LLMEndpointUnavailableError, not the generic
        "Remote LLM generation failed: Connection error." RuntimeError that
        gives the caller (backend.api.query) no way to distinguish this from
        an actual code bug."""
        service = RemoteLLMService(self.mock_openai_settings, api_key="test-key")

        mock_client = AsyncMock()
        mock_client.chat.completions.with_raw_response.create.side_effect = OpenAIAPIConnectionError(
            request=httpx.Request("POST", "https://example.com/v1/chat/completions")
        )

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            with self.assertRaises(LLMEndpointUnavailableError) as ctx:
                await service.generate("Test prompt")

        self.assertIn("Connection error", str(ctx.exception))

    async def test_generate_openai_strips_html_from_a_gateway_error_page(self):
        """Observed live: RunPod's edge gateway (openresty) returned a 405
        with an HTML body, not JSON — the openai SDK uses that raw HTML text
        verbatim as both exc.body and str(exc) (see
        backend.services.embeddings._extract_error_detail's docstring for
        why). The raw markup must not reach the end user as the error
        message raised from generate()."""
        import httpx
        from openai import APIStatusError

        service = RemoteLLMService(self.mock_openai_settings, api_key="test-key")
        html = (
            "<html>\n<head><title>405 Not Allowed</title></head>\n<body>\n"
            "<center><h1>405 Not Allowed</h1></center>\n<hr><center>openresty</center>\n"
            "</body>\n</html>"
        )
        response = httpx.Response(
            status_code=405, request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
        )
        exc = APIStatusError(html, response=response, body=html)

        mock_client = AsyncMock()
        mock_client.chat.completions.with_raw_response.create.side_effect = exc

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            with self.assertRaises(RuntimeError) as ctx:
                await service.generate("Test prompt")

        self.assertIn("405 Not Allowed", str(ctx.exception))
        self.assertNotIn("<html>", str(ctx.exception))

    async def test_protocol_comes_from_the_provider_not_the_model_name(self):
        """A Claude-named model on an OpenAI-compatible provider uses the OpenAI
        protocol; an unfamiliar model name on the anthropic provider uses Anthropic's."""
        def settings_for(provider_id, model_name):
            preset = HardwarePreset(
                name="t", description="t",
                embedding=EmbeddingConfig(model_type="remote", model_name="openai"),
                llm=LLMConfig(model_type="remote", model_names=model_name, temperature=0.7,
                              provider=ProviderConfig(id=provider_id),
                              model_kwargs={"api_key_env": "SOME_KEY"}),
                rag=RAGConfig(), memory_budget_gb=1.0,
            )
            settings = Mock(spec=Settings)
            settings.get_hardware_preset.return_value = preset
            return settings

        openai_service = RemoteLLMService(settings_for("openai", "claude-3-opus"), api_key="k")
        self.assertEqual(openai_service._llm_api(), "openai")
        anthropic_service = RemoteLLMService(settings_for("anthropic", "totally-unknown-model"), api_key="k")
        self.assertEqual(anthropic_service._llm_api(), "anthropic")

        mock_openai = AsyncMock()
        response = Mock(); response.choices = [Mock()]
        response.choices[0].message.content = "via openai protocol"
        mock_openai.chat.completions.with_raw_response.create.return_value = _raw(response)
        with patch("openai.AsyncOpenAI", return_value=mock_openai):
            self.assertEqual(await openai_service.generate("hi"), "via openai protocol")
        mock_openai.chat.completions.with_raw_response.create.assert_called_once()

        mock_anthropic = AsyncMock()
        reply = Mock(); reply.content = [Mock()]; reply.content[0].text = "via anthropic protocol"
        mock_anthropic.messages.with_raw_response.create.return_value = _raw(reply)
        with patch("anthropic.AsyncAnthropic", return_value=mock_anthropic):
            self.assertEqual(await anthropic_service.generate("hi"), "via anthropic protocol")

    async def test_keyless_generic_endpoint_is_called_with_a_placeholder_key(self):
        """A generic OpenAI-compatible server that declares no key needs none."""
        preset = HardwarePreset(
            name="t", description="t",
            embedding=EmbeddingConfig(model_type="remote", model_name="openai"),
            llm=LLMConfig(model_type="remote", model_names="local-model", temperature=0.7,
                          model_kwargs={"base_url": "http://localhost:8000/v1"}),
            rag=RAGConfig(), memory_budget_gb=1.0,
        )
        settings = Mock(spec=Settings)
        settings.get_hardware_preset.return_value = preset
        service = RemoteLLMService(settings)
        mock_client = AsyncMock()
        response = Mock(); response.choices = [Mock()]; response.choices[0].message.content = "ok"
        mock_client.chat.completions.with_raw_response.create.return_value = _raw(response)
        with patch("openai.AsyncOpenAI", return_value=mock_client) as ctor:
            await service.generate("hi")
        self.assertEqual(ctor.call_args.kwargs["api_key"], "not-needed")

    async def test_openai_missing_api_key(self):
        """A missing API key is a classified configuration problem
        (LLMConfigurationError, handled as a 503 by backend.api.query), not
        a generic RuntimeError indistinguishable from an actual code bug."""
        service = RemoteLLMService(self.mock_openai_settings)

        # Ensure no API key in environment
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(LLMConfigurationError) as context:
                await service.generate("Test prompt")

            self.assertIn("No API key for", str(context.exception))

    async def test_anthropic_missing_api_key(self):
        """Symmetrical case for Anthropic (see test_openai_missing_api_key)."""
        service = RemoteLLMService(self.mock_anthropic_settings)

        # Ensure no API key in environment
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(LLMConfigurationError) as context:
                await service.generate("Test prompt")

            self.assertIn("Anthropic API key not provided", str(context.exception))

    async def test_generate_with_defaults(self):
        """Test generation uses config defaults when parameters not specified."""
        service = RemoteLLMService(self.mock_openai_settings, api_key="test-key")

        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message.content = "Generated answer"
        mock_client.chat.completions.with_raw_response.create.return_value = _raw(mock_response)

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await service.generate("Test prompt")  # No max_tokens, temperature

        # Should use defaults from config
        call_args = mock_client.chat.completions.with_raw_response.create.call_args
        self.assertEqual(call_args.kwargs["temperature"], 0.7)  # From preset
        self.assertEqual(call_args.kwargs["max_tokens"], 512)  # Default

    async def test_generate_openai_passes_configured_extra_body(self):
        """model_kwargs['extra_body'] (e.g. to disable KISSKI 'thinking' mode) is forwarded."""
        preset = HardwarePreset(
            name="test-openai-extra-body",
            description="Test OpenAI preset with extra_body",
            embedding=EmbeddingConfig(model_type="remote", model_name="openai"),
            llm=LLMConfig(
                model_type="remote",
                model_names="qwen3.5-397b-a17b",
                temperature=0.7,
                model_kwargs={
                    "base_url": "https://chat-ai.academiccloud.de/v1",
                    "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
                },
            ),
            rag=RAGConfig(),
            memory_budget_gb=1.0,
        )
        mock_settings = Mock(spec=Settings)
        mock_settings.get_hardware_preset.return_value = preset
        service = RemoteLLMService(mock_settings, api_key="test-key")

        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message.content = "Generated answer"
        mock_client.chat.completions.with_raw_response.create.return_value = _raw(mock_response)

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            await service.generate("Test prompt", max_tokens=100, temperature=0.5)

        call_args = mock_client.chat.completions.with_raw_response.create.call_args
        self.assertEqual(
            call_args.kwargs["extra_body"],
            {"chat_template_kwargs": {"enable_thinking": False}},
        )

    async def test_generate_openai_extra_body_defaults_to_none(self):
        """Presets with no 'extra_body' in model_kwargs (e.g. real OpenAI) send extra_body=None."""
        service = RemoteLLMService(self.mock_openai_settings, api_key="test-key")

        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message.content = "Generated answer"
        mock_client.chat.completions.with_raw_response.create.return_value = _raw(mock_response)

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            await service.generate("Test prompt", max_tokens=100, temperature=0.5)

        call_args = mock_client.chat.completions.with_raw_response.create.call_args
        self.assertIsNone(call_args.kwargs["extra_body"])

    async def test_generate_openai_raises_clear_error_on_none_content(self):
        """A None message.content (e.g. finish_reason='length' mid-reasoning) raises a
        descriptive RuntimeError instead of crashing with TypeError on len(None)."""
        service = RemoteLLMService(self.mock_openai_settings, api_key="test-key")

        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.choices = [Mock()]
        mock_response.choices[0].message.content = None
        mock_response.choices[0].finish_reason = "length"
        mock_client.chat.completions.with_raw_response.create.return_value = _raw(mock_response)

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            with self.assertRaises(RuntimeError) as context:
                await service.generate("Test prompt", max_tokens=100, temperature=0.5)

        self.assertIn("length", str(context.exception))
        self.assertIn("gpt-4o-mini", str(context.exception))

    async def test_generate_anthropic_raises_clear_error_on_none_text(self):
        """Symmetrical guard: a None content[0].text raises a descriptive RuntimeError."""
        service = RemoteLLMService(self.mock_anthropic_settings, api_key="test-key")

        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.content = [Mock()]
        mock_response.content[0].text = None
        mock_response.stop_reason = "max_tokens"
        mock_client.messages.with_raw_response.create.return_value = _raw(mock_response)

        with patch("anthropic.AsyncAnthropic", return_value=mock_client):
            with self.assertRaises(RuntimeError) as context:
                await service.generate("Test prompt", max_tokens=100, temperature=0.5)

        self.assertIn("max_tokens", str(context.exception))


if __name__ == "__main__":
    unittest.main()
