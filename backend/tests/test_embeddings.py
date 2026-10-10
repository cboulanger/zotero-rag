"""
Unit tests for embedding service.
"""

import os
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, Mock, patch, MagicMock
import httpx
import numpy as np

from openai import (
    APIConnectionError as OpenAIAPIConnectionError,
    APIStatusError as OpenAIAPIStatusError,
    AuthenticationError as OpenAIAuthenticationError,
    BadRequestError as OpenAIBadRequestError,
    InternalServerError as OpenAIInternalServerError,
    PermissionDeniedError as OpenAIPermissionDeniedError,
    RateLimitError as OpenAIRateLimitError,
)

from backend.config.presets import EmbeddingConfig
from backend.services.embeddings import (
    EmbeddingAuthenticationError,
    EmbeddingConfigurationError,
    EmbeddingEndpointUnavailableError,
    EmbeddingRateLimitExhaustedError,
    EmbeddingService,
    LocalEmbeddingService,
    MockEmbeddingService,
    RemoteEmbeddingService,
    _extract_error_detail,
    create_embedding_service,
)

try:
    import sentence_transformers  # noqa: F401
    HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    HAS_SENTENCE_TRANSFORMERS = False


class TestEmbeddingService(unittest.TestCase):
    """Test base embedding service functionality."""

    def test_compute_content_hash(self):
        """Test content hash computation."""
        text = "This is a test"
        hash1 = EmbeddingService.compute_content_hash(text)
        hash2 = EmbeddingService.compute_content_hash(text)

        self.assertEqual(hash1, hash2)
        self.assertEqual(len(hash1), 64)  # SHA256 hex is 64 chars

    def test_different_texts_different_hashes(self):
        """Test that different texts produce different hashes."""
        hash1 = EmbeddingService.compute_content_hash("text1")
        hash2 = EmbeddingService.compute_content_hash("text2")

        self.assertNotEqual(hash1, hash2)

    def test_mock_service_has_rate_limit_retries_default(self):
        """Every concrete subclass must have rate_limit_retries even if it never
        sets it itself — backend/api/document_upload.py reads it unconditionally
        regardless of which embedding service is active (e.g. MockEmbeddingService
        under TESTING=true, or LocalEmbeddingService), so a missing default here
        crashes every document upload for those configurations."""
        service = MockEmbeddingService()
        self.assertEqual(service.rate_limit_retries, 0)
        self.assertEqual(service.rate_limit_wait_seconds, 0.0)


class TestExtractErrorDetail(unittest.TestCase):
    def test_flat_message_body(self):
        exc = Exception("Error code: 401 - {'message': 'Unauthorized', 'request_id': 'x'}")
        exc.body = {"message": "Unauthorized", "request_id": "x"}
        self.assertEqual(_extract_error_detail(exc), "Unauthorized")

    def test_nested_error_message_body(self):
        exc = Exception("Error code: 401 - {'error': {'message': 'Invalid API key', 'type': 'invalid_request_error'}}")
        exc.body = {"error": {"message": "Invalid API key", "type": "invalid_request_error"}}
        self.assertEqual(_extract_error_detail(exc), "Invalid API key")

    def test_falls_back_to_str_when_body_missing(self):
        exc = Exception("plain message, no body attribute")
        self.assertEqual(_extract_error_detail(exc), "plain message, no body attribute")

    def test_falls_back_to_str_when_body_not_a_dict(self):
        exc = Exception("raw text body")
        exc.body = "not a dict"
        self.assertEqual(_extract_error_detail(exc), "raw text body")

    def test_falls_back_to_str_when_dict_has_no_message_key(self):
        exc = Exception("Error code: 500 - {'code': 'boom'}")
        exc.body = {"code": "boom"}
        self.assertEqual(_extract_error_detail(exc), str(exc))

    def test_extracts_title_from_an_html_gateway_error_page(self):
        """Observed live: RunPod's edge gateway (openresty) returned a 405
        with an HTML body, not JSON. The openai SDK then uses the raw HTML
        text itself as both exc.body and str(exc) (see
        _make_status_error_from_response in openai/_base_client.py — it only
        tries json.loads, falling back to the raw response text verbatim).
        Regression: this raw HTML page was shown to the end user as the
        query's error detail. Pull just the <title> instead."""
        html = (
            "<html>\n<head><title>405 Not Allowed</title></head>\n<body>\n"
            "<center><h1>405 Not Allowed</h1></center>\n<hr><center>openresty</center>\n"
            "</body>\n</html>"
        )
        exc = Exception(html)
        exc.body = html
        self.assertEqual(_extract_error_detail(exc), "405 Not Allowed")

    def test_strips_tags_from_html_body_with_no_title(self):
        html = "<html><body><h1>502 Bad Gateway</h1></body></html>"
        exc = Exception(html)
        exc.body = html
        self.assertEqual(_extract_error_detail(exc), "502 Bad Gateway")

    def test_truncates_a_very_long_html_body_with_no_title_or_recognizable_text(self):
        html = "<html><body>" + ("x" * 500) + "</body></html>"
        exc = Exception(html)
        exc.body = html
        result = _extract_error_detail(exc)
        self.assertLessEqual(len(result), 220)
        self.assertTrue(result.startswith("xxx"))


@unittest.skipUnless(HAS_SENTENCE_TRANSFORMERS, "sentence_transformers not installed")
class TestLocalEmbeddingService(unittest.IsolatedAsyncioTestCase):
    """Test local embedding service."""

    def setUp(self):
        """Set up test fixtures."""
        self.config = EmbeddingConfig(
            model_type="local",
            model_name="sentence-transformers/all-MiniLM-L6-v2",
            batch_size=32,
            cache_enabled=True,
        )

    @patch("sentence_transformers.SentenceTransformer")
    async def test_init(self, mock_st):
        """Test service initialization."""
        service = LocalEmbeddingService(self.config, cache_dir="/tmp/cache")

        self.assertEqual(service.config, self.config)
        self.assertEqual(service.cache_dir, "/tmp/cache")
        self.assertIsNone(service._model)  # Lazy loading

    @patch("sentence_transformers.SentenceTransformer")
    async def test_embed_text(self, mock_st):
        """Test single text embedding."""
        # Mock the model
        mock_model = MagicMock()
        mock_embedding = np.array([0.1, 0.2, 0.3, 0.4])
        mock_model.encode.return_value = mock_embedding
        mock_model.get_sentence_embedding_dimension.return_value = 4
        mock_st.return_value = mock_model

        service = LocalEmbeddingService(self.config)
        embedding = await service.embed_text("test text")

        self.assertEqual(embedding, [0.1, 0.2, 0.3, 0.4])
        mock_model.encode.assert_called_once()

    @patch("sentence_transformers.SentenceTransformer")
    async def test_embed_text_caching(self, mock_st):
        """Test that embeddings are cached."""
        mock_model = MagicMock()
        mock_embedding = np.array([0.1, 0.2, 0.3, 0.4])
        mock_model.encode.return_value = mock_embedding
        mock_model.get_sentence_embedding_dimension.return_value = 4
        mock_st.return_value = mock_model

        service = LocalEmbeddingService(self.config)

        # First call
        embedding1 = await service.embed_text("test text")

        # Second call with same text
        embedding2 = await service.embed_text("test text")

        # Should only call encode once due to caching
        self.assertEqual(embedding1, embedding2)
        self.assertEqual(mock_model.encode.call_count, 1)

    @patch("sentence_transformers.SentenceTransformer")
    async def test_embed_batch(self, mock_st):
        """Test batch embedding."""
        mock_model = MagicMock()
        mock_embeddings = np.array([
            [0.1, 0.2, 0.3, 0.4],
            [0.5, 0.6, 0.7, 0.8],
        ])
        mock_model.encode.return_value = mock_embeddings
        mock_model.get_sentence_embedding_dimension.return_value = 4
        mock_st.return_value = mock_model

        service = LocalEmbeddingService(self.config)
        texts = ["text 1", "text 2"]
        embeddings = await service.embed_batch(texts)

        self.assertEqual(len(embeddings), 2)
        self.assertEqual(embeddings[0], [0.1, 0.2, 0.3, 0.4])
        self.assertEqual(embeddings[1], [0.5, 0.6, 0.7, 0.8])

    @patch("sentence_transformers.SentenceTransformer")
    async def test_embed_batch_with_cache(self, mock_st):
        """Test batch embedding with partial cache hits."""
        mock_model = MagicMock()

        # First call will cache both
        mock_embeddings1 = np.array([
            [0.1, 0.2, 0.3, 0.4],
            [0.5, 0.6, 0.7, 0.8],
        ])
        mock_model.encode.return_value = mock_embeddings1
        mock_model.get_sentence_embedding_dimension.return_value = 4
        mock_st.return_value = mock_model

        service = LocalEmbeddingService(self.config)
        texts1 = ["text 1", "text 2"]
        await service.embed_batch(texts1)

        # Second call with one cached, one new
        mock_embeddings2 = np.array([[0.9, 0.8, 0.7, 0.6]])
        mock_model.encode.return_value = mock_embeddings2

        texts2 = ["text 1", "text 3"]  # text 1 is cached
        embeddings = await service.embed_batch(texts2)

        # Should only compute embedding for "text 3"
        self.assertEqual(len(embeddings), 2)
        self.assertEqual(embeddings[0], [0.1, 0.2, 0.3, 0.4])  # From cache
        self.assertEqual(embeddings[1], [0.9, 0.8, 0.7, 0.6])  # Newly computed

    @patch("sentence_transformers.SentenceTransformer")
    async def test_get_embedding_dim(self, mock_st):
        """Test getting embedding dimension."""
        mock_model = MagicMock()
        mock_model.get_sentence_embedding_dimension.return_value = 384
        mock_st.return_value = mock_model

        service = LocalEmbeddingService(self.config)
        dim = service.get_embedding_dim()

        self.assertEqual(dim, 384)

    @patch("sentence_transformers.SentenceTransformer")
    async def test_clear_cache(self, mock_st):
        """Test clearing the cache."""
        mock_model = MagicMock()
        mock_embedding = np.array([0.1, 0.2, 0.3, 0.4])
        mock_model.encode.return_value = mock_embedding
        mock_model.get_sentence_embedding_dimension.return_value = 4
        mock_st.return_value = mock_model

        service = LocalEmbeddingService(self.config)

        # Add to cache
        await service.embed_text("test")
        self.assertEqual(len(service._embedding_cache), 1)

        # Clear cache
        service.clear_cache()
        self.assertEqual(len(service._embedding_cache), 0)


class TestRemoteEmbeddingService(unittest.IsolatedAsyncioTestCase):
    """Test remote embedding service."""

    def setUp(self):
        """Set up test fixtures."""
        self.config = EmbeddingConfig(
            model_type="remote",
            model_name="openai",
            cache_enabled=True,
        )

    async def test_init(self):
        """Test service initialization."""
        service = RemoteEmbeddingService(self.config, api_key="test-key")

        self.assertEqual(service.config, self.config)
        self.assertEqual(service._api_key, "test-key")

    def test_required_client_fields_reports_kind_api_key_for_kisski_style_config(self):
        config = EmbeddingConfig(
            model_type="remote",
            model_name="multilingual-e5-large-instruct",
            model_kwargs={"base_url": "https://chat-ai.academiccloud.de/v1", "api_key_env": "KISSKI_API_KEY"},
        )
        fields = RemoteEmbeddingService.required_client_fields(config)
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0]["key_name"], "KISSKI_API_KEY")
        self.assertEqual(fields[0]["kind"], "api_key")

    def test_required_client_fields_reports_shared_kinds_for_mpcdf_style_config(self):
        config = EmbeddingConfig(
            model_type="remote",
            model_name="multilingual-e5-large-instruct",
            model_kwargs={
                "shared_base_url_env": "MPCDF_EMBEDDING_BASE_URL",
                "shared_api_key_env": "MPCDF_EMBEDDING_API_KEY",
            },
        )
        fields = RemoteEmbeddingService.required_client_fields(config)
        by_key = {f["key_name"]: f for f in fields}
        self.assertEqual(len(fields), 2)
        self.assertEqual(by_key["MPCDF_EMBEDDING_BASE_URL"]["kind"], "shared_base_url")
        self.assertEqual(by_key["MPCDF_EMBEDDING_API_KEY"]["kind"], "shared_api_key")

    def test_required_client_fields_reports_declared_pattern_when_present(self):
        """A preset may declare shared_base_url_pattern/shared_api_key_pattern
        (and api_key_pattern for the personal-key case) in model_kwargs so
        POST /api/config/remote-fields can reject a malformed value outright
        instead of accepting a typo that only surfaces as a connection error
        on the next real query."""
        config = EmbeddingConfig(
            model_type="remote",
            model_name="intfloat/multilingual-e5-large-instruct",
            model_kwargs={
                "shared_base_url_env": "RUNPOD_EMBEDDING_BASE_URL",
                "shared_base_url_pattern": r"^https://api\.runpod\.ai/v2/[A-Za-z0-9]+/openai/v1$",
                "shared_api_key_env": "RUNPOD_API_KEY",
                "shared_api_key_pattern": r"^rpa_[A-Za-z0-9]+$",
            },
        )
        fields = RemoteEmbeddingService.required_client_fields(config)
        by_key = {f["key_name"]: f for f in fields}
        self.assertEqual(
            by_key["RUNPOD_EMBEDDING_BASE_URL"]["pattern"],
            r"^https://api\.runpod\.ai/v2/[A-Za-z0-9]+/openai/v1$",
        )
        self.assertEqual(by_key["RUNPOD_API_KEY"]["pattern"], r"^rpa_[A-Za-z0-9]+$")

    def test_required_client_fields_lists_shared_api_key_before_shared_base_url(self):
        """The API key is the one value every setup needs regardless of which
        endpoint it's paired with, so it belongs first in the Preferences
        pane — entering it once before tabbing through the URL field(s) is
        easier than the reverse order."""
        config = EmbeddingConfig(
            model_type="remote",
            model_name="intfloat/multilingual-e5-large-instruct",
            model_kwargs={
                "shared_base_url_env": "RUNPOD_EMBEDDING_BASE_URL",
                "shared_api_key_env": "RUNPOD_API_KEY",
            },
        )
        fields = RemoteEmbeddingService.required_client_fields(config)
        key_names = [f["key_name"] for f in fields]
        self.assertEqual(key_names, ["RUNPOD_API_KEY", "RUNPOD_EMBEDDING_BASE_URL"])

    def test_required_client_fields_pattern_defaults_to_none(self):
        config = EmbeddingConfig(
            model_type="remote",
            model_name="multilingual-e5-large-instruct",
            model_kwargs={
                "shared_base_url_env": "MPCDF_EMBEDDING_BASE_URL",
                "shared_api_key_env": "MPCDF_EMBEDDING_API_KEY",
            },
        )
        fields = RemoteEmbeddingService.required_client_fields(config)
        for field in fields:
            self.assertIsNone(field["pattern"])

    @patch("openai.AsyncOpenAI")
    async def test_get_client_resolves_shared_fields_from_store_over_env(self, mock_openai_cls):
        import os
        import tempfile
        from pathlib import Path
        from backend.services.admin_settings_store import update_remote_config

        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp)
            update_remote_config({
                "MPCDF_EMBEDDING_BASE_URL": "https://llm.mpcdf.mpg.de/abc123/v1",
                "MPCDF_EMBEDDING_API_KEY": "store-key",
            }, data_path=data_path)
            with patch.dict(os.environ, {"MPCDF_EMBEDDING_BASE_URL": "https://should-not-be-used/v1"}):
                config = EmbeddingConfig(
                    model_type="remote",
                    model_name="multilingual-e5-large-instruct",
                    model_kwargs={
                        "shared_base_url_env": "MPCDF_EMBEDDING_BASE_URL",
                        "shared_api_key_env": "MPCDF_EMBEDDING_API_KEY",
                    },
                )
                service = RemoteEmbeddingService(config, data_path=data_path)
                service._get_client()

        _, kwargs = mock_openai_cls.call_args
        self.assertEqual(kwargs["base_url"], "https://llm.mpcdf.mpg.de/abc123/v1")
        self.assertEqual(kwargs["api_key"], "store-key")

    @patch("openai.AsyncOpenAI")
    async def test_get_client_appends_v1_to_shared_base_url_missing_it(self, mock_openai_cls):
        import tempfile
        from pathlib import Path
        from backend.services.admin_settings_store import update_remote_config

        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp)
            update_remote_config({
                "MPCDF_EMBEDDING_BASE_URL": "https://llm.mpcdf.mpg.de/abc123",
                "MPCDF_EMBEDDING_API_KEY": "store-key",
            }, data_path=data_path)
            config = EmbeddingConfig(
                model_type="remote",
                model_name="multilingual-e5-large-instruct",
                model_kwargs={
                    "shared_base_url_env": "MPCDF_EMBEDDING_BASE_URL",
                    "shared_api_key_env": "MPCDF_EMBEDDING_API_KEY",
                },
            )
            service = RemoteEmbeddingService(config, data_path=data_path)
            service._get_client()

        _, kwargs = mock_openai_cls.call_args
        self.assertEqual(kwargs["base_url"], "https://llm.mpcdf.mpg.de/abc123/v1")

    async def test_get_client_ignores_shared_values_set_only_in_the_environment(self):
        import os
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {
                "MPCDF_EMBEDDING_BASE_URL": "https://llm.mpcdf.mpg.de/from-env/v1",
                "MPCDF_EMBEDDING_API_KEY": "env-key",
            }):
                config = EmbeddingConfig(
                    model_type="remote",
                    model_name="multilingual-e5-large-instruct",
                    model_kwargs={
                        "shared_base_url_env": "MPCDF_EMBEDDING_BASE_URL",
                        "shared_api_key_env": "MPCDF_EMBEDDING_API_KEY",
                    },
                )
                service = RemoteEmbeddingService(config, data_path=Path(tmp))
                with self.assertRaises(EmbeddingConfigurationError):
                    service._get_client()

    async def test_get_client_ignores_a_personal_key_set_only_in_the_environment(self):
        import os
        config = EmbeddingConfig(
            model_type="remote", model_name="openai", model_kwargs={"api_key_env": "OPENAI_API_KEY"},
        )
        with patch.dict(os.environ, {"OPENAI_API_KEY": "from-env"}):
            with self.assertRaises(EmbeddingConfigurationError):
                RemoteEmbeddingService(config)._get_client()

    async def test_get_client_raises_clear_error_when_shared_base_url_unset(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            data_path = Path(tmp)
            config = EmbeddingConfig(
                model_type="remote",
                model_name="multilingual-e5-large-instruct",
                model_kwargs={
                    "shared_base_url_env": "MPCDF_EMBEDDING_BASE_URL",
                    "shared_api_key_env": "MPCDF_EMBEDDING_API_KEY",
                },
            )
            service = RemoteEmbeddingService(config, api_key="explicit-key", data_path=data_path)
            with self.assertRaises(EmbeddingConfigurationError) as ctx:
                service._get_client()
        self.assertIn("MPCDF_EMBEDDING_BASE_URL", str(ctx.exception))

    @patch("openai.AsyncOpenAI")
    async def test_get_client_passes_a_bounded_timeout(self, mock_openai_cls):
        """Without an explicit timeout the openai SDK defaults to a 600s read
        timeout — observed live: a query against a cold/stuck RunPod
        embedding endpoint hung with zero feedback for minutes because of
        this. Mirrors RemoteLLMService._get_openai_client's same default."""
        config = EmbeddingConfig(
            model_type="remote", model_name="openai", model_kwargs={"api_key_env": "OPENAI_API_KEY"},
        )
        service = RemoteEmbeddingService(config, api_key="k")
        service._get_client()
        self.assertEqual(mock_openai_cls.call_args.kwargs["timeout"], 120.0)

    @patch("openai.AsyncOpenAI")
    async def test_get_client_timeout_is_configurable_via_model_kwargs(self, mock_openai_cls):
        config = EmbeddingConfig(
            model_type="remote", model_name="openai",
            model_kwargs={"api_key_env": "OPENAI_API_KEY", "timeout": 30},
        )
        service = RemoteEmbeddingService(config, api_key="k")
        service._get_client()
        self.assertEqual(mock_openai_cls.call_args.kwargs["timeout"], 30.0)

    async def test_get_client_raises_configuration_error_when_shared_api_key_unset(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            config = EmbeddingConfig(
                model_type="remote",
                model_name="multilingual-e5-large-instruct",
                model_kwargs={
                    "shared_base_url_env": "MPCDF_EMBEDDING_BASE_URL",
                    "shared_api_key_env": "MPCDF_EMBEDDING_API_KEY",
                },
            )
            service = RemoteEmbeddingService(config, data_path=Path(tmp))
            with self.assertRaises(EmbeddingConfigurationError) as ctx:
                service._get_client()
        self.assertIn("MPCDF_EMBEDDING_API_KEY", str(ctx.exception))

    async def test_get_client_raises_configuration_error_when_the_named_api_key_is_unset(self):
        config = EmbeddingConfig(
            model_type="remote", model_name="openai", model_kwargs={"api_key_env": "OPENAI_API_KEY"},
        )
        with patch.dict(os.environ, {}, clear=True):
            service = RemoteEmbeddingService(config)
            with self.assertRaises(EmbeddingConfigurationError) as ctx:
                service._get_client()
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))

    @patch("openai.AsyncOpenAI")
    async def test_get_client_without_a_declared_key_uses_a_placeholder(self, mock_openai_cls):
        """A generic OpenAI-compatible server that declares no key needs none."""
        config = EmbeddingConfig(
            model_type="remote", model_name="local-embedder", model_kwargs={"base_url": "http://localhost:8000/v1"},
        )
        with patch.dict(os.environ, {}, clear=True):
            RemoteEmbeddingService(config)._get_client()
        self.assertEqual(mock_openai_cls.call_args.kwargs["api_key"], "not-needed")

    @patch("openai.AsyncOpenAI")
    async def test_embed_text_returns_correct_dimension(self, mock_openai_cls):
        """Test that remote service calls the API and returns the right dimension."""
        # Build a fake response matching openai's EmbeddingObject structure
        fake_embedding = [0.1] * 1536
        mock_item = MagicMock()
        mock_item.embedding = fake_embedding
        mock_response = MagicMock()
        mock_response.data = [mock_item]

        mock_raw = MagicMock()
        mock_raw.headers = {}
        mock_raw.parse.return_value = mock_response

        mock_client = MagicMock()
        async def fake_raw_create(**kwargs):
            return mock_raw
        mock_client.embeddings.with_raw_response.create = fake_raw_create
        mock_openai_cls.return_value = mock_client

        service = RemoteEmbeddingService(self.config, api_key="test-key")
        embedding = await service.embed_text("test")

        # Should return what the API returned
        self.assertEqual(len(embedding), 1536)
        self.assertEqual(embedding, fake_embedding)

    async def test_get_embedding_dim(self):
        """Test getting embedding dimension."""
        service = RemoteEmbeddingService(self.config)
        dim = service.get_embedding_dim()

        self.assertEqual(dim, 1536)  # OpenAI dimension


class TestCreateEmbeddingService(unittest.TestCase):
    """Test embedding service factory."""

    def test_create_local_service(self):
        """Test creating local service."""
        config = EmbeddingConfig(
            model_type="local",
            model_name="test-model",
        )

        service = create_embedding_service(config)
        self.assertIsInstance(service, LocalEmbeddingService)

    def test_create_remote_service(self):
        """Test creating remote service."""
        config = EmbeddingConfig(
            model_type="remote",
            model_name="openai",
        )

        service = create_embedding_service(config, api_key="test-key")
        self.assertIsInstance(service, RemoteEmbeddingService)

    def test_create_invalid_type(self):
        """Test that invalid model type raises error during config validation."""
        # Pydantic validates model_type at config creation, so we test that
        with self.assertRaises(Exception):  # ValidationError from Pydantic
            config = EmbeddingConfig(
                model_type="invalid",  # type: ignore
                model_name="test",
            )


class TestEmbeddingRateLimitExhaustedError(unittest.IsolatedAsyncioTestCase):
    """Verify that long retry-after values raise EmbeddingRateLimitExhaustedError."""

    def _make_service(self) -> RemoteEmbeddingService:
        config = EmbeddingConfig(
            model_type="remote",
            model_name="text-embedding-3-small",
            batch_size=10,
            cache_enabled=False,
        )
        return RemoteEmbeddingService(config, api_key="test-key")

    async def test_long_retry_after_header_raises_exhausted(self):
        """retry-after > 60 s must raise EmbeddingRateLimitExhaustedError immediately."""
        service = self._make_service()

        mock_response = MagicMock()
        mock_response.headers = {"retry-after": "3600"}  # 1 hour
        exc = OpenAIRateLimitError("rate limit", response=mock_response, body=None)

        with patch.object(service, "_get_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.embeddings.with_raw_response.create = AsyncMock(side_effect=exc)

            before = datetime.now(timezone.utc)
            with self.assertRaises(EmbeddingRateLimitExhaustedError) as ctx:
                await service._create_embeddings_with_backoff(["hello"])
            after = datetime.now(timezone.utc)

        err = ctx.exception
        self.assertIsInstance(err.available_at, datetime)
        # available_at should be roughly now + 3600 s (allow 5 s tolerance)
        expected_min = before + timedelta(seconds=3595)
        expected_max = after + timedelta(seconds=3605)
        self.assertGreater(err.available_at, expected_min)
        self.assertLess(err.available_at, expected_max)

    async def test_no_retry_after_header_raises_exhausted(self):
        """Missing retry-after on a RateLimitError must raise EmbeddingRateLimitExhaustedError."""
        service = self._make_service()

        mock_response = MagicMock()
        mock_response.headers = {}  # no header
        exc = OpenAIRateLimitError("rate limit", response=mock_response, body=None)

        with patch.object(service, "_get_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.embeddings.with_raw_response.create = AsyncMock(side_effect=exc)

            with self.assertRaises(EmbeddingRateLimitExhaustedError):
                await service._create_embeddings_with_backoff(["hello"])

    async def test_short_retry_after_retries_then_succeeds(self):
        """retry-after <= 60 s should sleep and retry (not raise EmbeddingRateLimitExhaustedError)."""
        service = self._make_service()

        mock_response = MagicMock()
        mock_response.headers = {"retry-after": "1"}  # 1 second — short
        rate_exc = OpenAIRateLimitError("rate limit", response=mock_response, body=None)

        success_raw = MagicMock()
        success_raw.headers = {}
        success_raw.parse.return_value = MagicMock(data=[MagicMock(embedding=[0.1, 0.2])])

        with patch.object(service, "_get_client") as mock_client_fn, \
             patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.embeddings.with_raw_response.create = AsyncMock(
                side_effect=[rate_exc, success_raw]
            )
            result = await service._create_embeddings_with_backoff(["hello"])

        mock_sleep.assert_awaited_once()
        sleep_duration = mock_sleep.call_args[0][0]
        self.assertGreater(sleep_duration, 0.9)  # at least the server-supplied 1 s
        self.assertLess(sleep_duration, 3.0)     # plus small jitter only
        self.assertIsNotNone(result)


class TestEmbeddingInternalServerErrorRetry(unittest.IsolatedAsyncioTestCase):
    """A 500 from the embedding API must be retried regardless of its message.

    Regression: the retry filter previously only retried InternalServerError
    when "try again" appeared in the message, on the assumption that's the only
    transient phrasing KISSKI returns. Production showed a bare generic
    "Error code: 500" (no "try again" wording) failing roughly 1 in 3 calls and
    succeeding immediately on the very next identical call — i.e. genuinely
    transient, but previously raised on the first attempt instead of retrying.
    """

    def _make_service(self) -> RemoteEmbeddingService:
        config = EmbeddingConfig(
            model_type="remote",
            model_name="text-embedding-3-small",
            batch_size=10,
            cache_enabled=False,
        )
        return RemoteEmbeddingService(config, api_key="test-key")

    async def test_generic_500_without_try_again_wording_is_retried(self):
        service = self._make_service()
        mock_response = MagicMock()
        mock_response.headers = {}
        exc = OpenAIInternalServerError("Error code: 500", response=mock_response, body=None)

        success_raw = MagicMock()
        success_raw.headers = {}
        success_raw.parse.return_value = MagicMock(data=[MagicMock(embedding=[0.1, 0.2])])

        with patch.object(service, "_get_client") as mock_client_fn, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.embeddings.with_raw_response.create = AsyncMock(
                side_effect=[exc, success_raw]
            )
            result = await service._create_embeddings_with_backoff(["hello"])

        self.assertIsNotNone(result)

    async def test_generic_500_exhausts_retries_then_raises_endpoint_unavailable(self):
        """A 5xx that never recovers is a systemic endpoint problem (e.g. a
        provider's upstream gateway failing, not this request's content), so
        it must be wrapped the same way APIConnectionError/APIStatusError
        already are — not left as the raw SDK exception.

        Regression: CronIndexer._drain_pending_uploads exempts
        EmbeddingEndpointUnavailableError (and other systemic types) from
        counting toward a pending-upload entry's quarantine threshold. A raw
        OpenAIInternalServerError falls outside that exemption set, so a
        perfectly good document could get permanently quarantined and tagged
        rag-failed purely because the embedding provider was flaky — observed
        in production with a RunPod/Cloudflare 502 recurring across several
        cron cycles on an attachment that extracted identically every time.
        """
        service = self._make_service()
        mock_response = MagicMock()
        mock_response.headers = {}
        exc = OpenAIInternalServerError("Error code: 502 - Bad gateway", response=mock_response, body=None)

        with patch.object(service, "_get_client") as mock_client_fn, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.embeddings.with_raw_response.create = AsyncMock(side_effect=exc)

            with self.assertRaises(EmbeddingEndpointUnavailableError):
                await service._create_embeddings_with_backoff(["hello"])


class TestEmbeddingContextLengthRetry(unittest.IsolatedAsyncioTestCase):
    """A "context length exceeded" BadRequestError must be retried with the
    input truncated, same as the existing per-text truncation path — and if
    every attempt still exceeds the limit, the call must raise, not silently
    return None.

    Regression: _create_embeddings_with_backoff's BadRequestError handler
    truncated `input` and looped again on every attempt, including the last
    one, with no check for loop exhaustion (unlike the InternalServerError
    handler just above it, which explicitly re-raises on the last attempt).
    When every attempt still exceeded the context length, the for loop simply
    ran out of iterations and the function fell off its end, implicitly
    returning None. The caller (embed_batch) then crashed with
    "AttributeError: 'NoneType' object has no attribute 'data'" — a confusing
    crash that hid the real, actionable "still exceeds context length" error.
    """

    def _make_service(self) -> RemoteEmbeddingService:
        config = EmbeddingConfig(
            model_type="remote",
            model_name="multilingual-e5-large-instruct",
            batch_size=10,
            cache_enabled=False,
        )
        return RemoteEmbeddingService(config, api_key="test-key")

    def _context_length_error(self) -> OpenAIBadRequestError:
        mock_response = MagicMock()
        mock_response.headers = {}
        return OpenAIBadRequestError(
            "Error code: 400 - maximum context length is 512 tokens",
            response=mock_response,
            body=None,
        )

    async def test_truncates_and_retries_until_a_short_enough_batch_succeeds(self):
        service = self._make_service()
        success_raw = MagicMock()
        success_raw.headers = {}
        success_raw.parse.return_value = MagicMock(data=[MagicMock(embedding=[0.1, 0.2])])

        with patch.object(service, "_get_client") as mock_client_fn, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.embeddings.with_raw_response.create = AsyncMock(
                side_effect=[self._context_length_error(), self._context_length_error(), success_raw]
            )
            result = await service._create_embeddings_with_backoff(["a long text " * 100])

        self.assertIsNotNone(result)

    async def test_exhausting_all_truncation_attempts_raises_instead_of_returning_none(self):
        service = self._make_service()

        with patch.object(service, "_get_client") as mock_client_fn, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.embeddings.with_raw_response.create = AsyncMock(
                side_effect=self._context_length_error()
            )

            with self.assertRaises(OpenAIBadRequestError):
                await service._create_embeddings_with_backoff(["a long text " * 100])

    async def test_truncation_shrinks_text_with_no_word_boundaries(self):
        """Regression: a chunk dominated by one giant whitespace-free token
        (e.g. unsegmented CJK text, a long URL/hash) made the old word-based
        truncation a no-op — `text.split()` returns a single "word" for such
        text, and `max(1, int(1 * fraction))` always keeps that one word
        whole regardless of `fraction`. In production this meant 7-8 retry
        rounds each logging a shrinking `keep_fraction` while the actual
        request size — and the API's reported token count — never changed,
        exhausting the retry budget without ever reducing the real request.
        Character-based truncation always shrinks the text every round,
        regardless of script or whitespace."""
        service = self._make_service()
        success_raw = MagicMock()
        success_raw.headers = {}
        success_raw.parse.return_value = MagicMock(data=[MagicMock(embedding=[0.1, 0.2])])
        no_space_text = "x" * 2000  # one unbreakable "word" by whitespace splitting

        with patch.object(service, "_get_client") as mock_client_fn, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            create_mock = AsyncMock(side_effect=[self._context_length_error(), success_raw])
            mock_client.embeddings.with_raw_response.create = create_mock
            result = await service._create_embeddings_with_backoff([no_space_text])

        self.assertIsNotNone(result)
        retried_input = create_mock.await_args_list[1].kwargs["input"]
        self.assertLess(len(retried_input[0]), 2000)

    def _real_overflow_error(self, actual_tokens: int, limit_tokens: int = 512) -> OpenAIBadRequestError:
        # The exact message format the KISSKI/OpenAI-compatible API returns —
        # see _context_length_truncation_fraction, which parses these numbers.
        mock_response = MagicMock()
        mock_response.headers = {}
        return OpenAIBadRequestError(
            f"Error code: 400 - {{'error': {{'message': \"This model's maximum context "
            f"length is {limit_tokens} tokens. However, your request has {actual_tokens} "
            f"input tokens. Please reduce the length of the input messages.\", "
            f"'type': 'BadRequestError', 'param': None, 'code': 400}}}}",
            response=mock_response,
            body=None,
        )

    async def test_an_input_far_over_the_limit_converges_in_one_truncation_round(self):
        # Regression: a blind 15%-per-round cut needs many rounds to shrink an
        # input that's 2-3x over the limit (e.g. a merged chunk far bigger
        # than the model's context window), and can exhaust the shared retry
        # budget before converging — see the module's root-cause analysis.
        # Parsing the real overflow ratio from the error message should land
        # under the limit in a single extra attempt instead.
        service = self._make_service()
        success_raw = MagicMock()
        success_raw.headers = {}
        success_raw.parse.return_value = MagicMock(data=[MagicMock(embedding=[0.1, 0.2])])

        with patch.object(service, "_get_client") as mock_client_fn, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.embeddings.with_raw_response.create = AsyncMock(
                side_effect=[self._real_overflow_error(actual_tokens=1500, limit_tokens=512), success_raw]
            )
            result = await service._create_embeddings_with_backoff("word " * 1000)

        self.assertIsNotNone(result)
        self.assertEqual(mock_client.embeddings.with_raw_response.create.await_count, 2)


class TestContextLengthTruncationFraction(unittest.TestCase):
    """Unit tests for the overflow-ratio parser used by the retry loop above."""

    def test_computes_a_precise_fraction_from_the_reported_token_counts(self):
        from backend.services.embeddings import _context_length_truncation_fraction

        # limit/actual = 512/656 ≈ 0.78, times a 0.9 safety margin ≈ 0.70.
        fraction = _context_length_truncation_fraction(
            "maximum context length is 512 tokens. However, your request has 656 input tokens."
        )
        self.assertAlmostEqual(fraction, (512 / 656) * 0.9, places=4)

    def test_falls_back_to_the_default_when_the_message_has_no_parseable_numbers(self):
        from backend.services.embeddings import _context_length_truncation_fraction

        self.assertEqual(_context_length_truncation_fraction("something went wrong"), 0.85)

    def test_falls_back_to_the_default_when_actual_is_not_actually_over_the_limit(self):
        from backend.services.embeddings import _context_length_truncation_fraction

        # Degenerate/contradictory input — never trust it blindly.
        fraction = _context_length_truncation_fraction(
            "maximum context length is 512 tokens. However, your request has 100 input tokens."
        )
        self.assertEqual(fraction, 0.85)

    def test_never_returns_more_than_the_default_even_for_a_barely_over_input(self):
        from backend.services.embeddings import _context_length_truncation_fraction

        # 511/512 is barely over — the computed fraction would be ~0.998*0.9,
        # still capped at `default` so a single round never keeps MORE than
        # the blind heuristic would, avoiding a near-no-op "truncation".
        fraction = _context_length_truncation_fraction(
            "maximum context length is 511 tokens. However, your request has 512 input tokens."
        )
        self.assertLessEqual(fraction, 0.85)


class TestEmbeddingAuthenticationError(unittest.IsolatedAsyncioTestCase):
    """An invalid API key (HTTP 401/403) must raise a fatal EmbeddingAuthenticationError.

    Without this, a single expired key turns every embedding call into a swallowed
    per-item error and the indexing run silently completes with zero chunks.
    """

    def _make_service(self) -> RemoteEmbeddingService:
        config = EmbeddingConfig(
            model_type="remote",
            model_name="text-embedding-3-small",
            batch_size=10,
            cache_enabled=False,
        )
        return RemoteEmbeddingService(config, api_key="bad-key")

    async def test_401_raises_authentication_error(self):
        service = self._make_service()
        mock_response = MagicMock()
        mock_response.headers = {}
        exc = OpenAIAuthenticationError(
            "invalid api key", response=mock_response, body=None
        )

        with patch.object(service, "_get_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.embeddings.with_raw_response.create = AsyncMock(side_effect=exc)

            with self.assertRaises(EmbeddingAuthenticationError):
                await service._create_embeddings_with_backoff(["hello"])

    async def test_401_message_omits_raw_dict_repr(self):
        """Regression: the SDK's default str(exc) is literally
        "Error code: 401 - {'message': 'Unauthorized', 'request_id': '...'}" —
        the raised EmbeddingAuthenticationError must surface the clean inner
        message instead of that raw dict repr."""
        service = self._make_service()
        mock_response = MagicMock()
        mock_response.headers = {}
        exc = OpenAIAuthenticationError(
            "Error code: 401 - {'message': 'Unauthorized', 'request_id': 'req-1'}",
            response=mock_response,
            body={"message": "Unauthorized", "request_id": "req-1"},
        )

        with patch.object(service, "_get_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.embeddings.with_raw_response.create = AsyncMock(side_effect=exc)

            with self.assertRaises(EmbeddingAuthenticationError) as ctx:
                await service._create_embeddings_with_backoff(["hello"])

        message = str(ctx.exception)
        self.assertIn("Unauthorized", message)
        self.assertNotIn("{'message'", message)

    async def test_403_raises_authentication_error(self):
        service = self._make_service()
        mock_response = MagicMock()
        mock_response.headers = {}
        exc = OpenAIPermissionDeniedError(
            "forbidden", response=mock_response, body=None
        )

        with patch.object(service, "_get_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.embeddings.with_raw_response.create = AsyncMock(side_effect=exc)

            with self.assertRaises(EmbeddingAuthenticationError):
                await service._create_embeddings_with_backoff(["hello"])


class TestEmbeddingEndpointUnavailableError(unittest.IsolatedAsyncioTestCase):
    """A status code the SDK doesn't give a specific exception for (e.g. 404/405)
    must raise a fatal EmbeddingEndpointUnavailableError, not propagate as a raw
    openai.APIStatusError that only fails the one item it was raised for.

    Regression: an ephemeral embedding job's endpoint (e.g. MPCDF's <=8h Slurm
    jobs) expiring mid-run returned HTTP 405, which no except clause in
    _create_embeddings_with_backoff caught — the run then churned through every
    remaining item in the library, each failing identically, instead of
    aborting once like an authentication failure already does.
    """

    def _make_service(self) -> RemoteEmbeddingService:
        config = EmbeddingConfig(
            model_type="remote",
            model_name="multilingual-e5-large-instruct",
            batch_size=10,
            cache_enabled=False,
        )
        return RemoteEmbeddingService(config, api_key="test-key")

    def _status_error(self, status_code: int, message: str) -> OpenAIAPIStatusError:
        response = httpx.Response(
            status_code=status_code,
            request=httpx.Request("POST", "https://example.com/v1/embeddings"),
        )
        return OpenAIAPIStatusError(message, response=response, body=None)

    async def test_405_raises_endpoint_unavailable_error(self):
        service = self._make_service()
        exc = self._status_error(405, "Error code: 405 - Method Not Allowed")

        with patch.object(service, "_get_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.embeddings.with_raw_response.create = AsyncMock(side_effect=exc)

            with self.assertRaises(EmbeddingEndpointUnavailableError) as ctx:
                await service._create_embeddings_with_backoff(["hello"])

        self.assertIn("405", str(ctx.exception))

    async def test_404_raises_endpoint_unavailable_error(self):
        service = self._make_service()
        exc = self._status_error(404, "Error code: 404 - Not Found")

        with patch.object(service, "_get_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.embeddings.with_raw_response.create = AsyncMock(side_effect=exc)

            with self.assertRaises(EmbeddingEndpointUnavailableError):
                await service._create_embeddings_with_backoff(["hello"])

    async def test_connection_error_raises_endpoint_unavailable_error(self):
        """A transport-level failure (e.g. a cold/unreachable RunPod serverless
        endpoint) raises openai.APIConnectionError, not an APIStatusError — a
        distinct exception type with no status_code, uncaught by any existing
        except clause. Regression: this previously propagated all the way to
        a live /api/query request as a raw "Connection error." traceback
        instead of the same fatal, clearly-worded error the 404/405 case
        already gets."""
        service = self._make_service()
        exc = OpenAIAPIConnectionError(request=httpx.Request("POST", "https://example.com/v1/embeddings"))

        with patch.object(service, "_get_client") as mock_client_fn:
            mock_client = MagicMock()
            mock_client_fn.return_value = mock_client
            mock_client.embeddings.with_raw_response.create = AsyncMock(side_effect=exc)

            with self.assertRaises(EmbeddingEndpointUnavailableError) as ctx:
                await service._create_embeddings_with_backoff(["hello"])

        self.assertIn("Connection error", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
