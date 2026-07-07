"""
Unit tests for embedding service.
"""

import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, Mock, patch, MagicMock
import numpy as np

from openai import (
    AuthenticationError as OpenAIAuthenticationError,
    PermissionDeniedError as OpenAIPermissionDeniedError,
    RateLimitError as OpenAIRateLimitError,
)

from backend.config.presets import EmbeddingConfig
from backend.services.embeddings import (
    EmbeddingAuthenticationError,
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


if __name__ == "__main__":
    unittest.main()
