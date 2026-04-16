"""
Unit tests for embedding service.
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
import numpy as np

from backend.config.presets import EmbeddingConfig
from backend.services.embeddings import (
    EmbeddingService,
    LocalEmbeddingService,
    RemoteEmbeddingService,
    create_embedding_service,
)


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

        mock_client = MagicMock()
        mock_client.embeddings.create = MagicMock(
            return_value=mock_response.__class__()  # placeholder
        )
        # Make embeddings.create an awaitable
        async def fake_create(**kwargs):
            return mock_response
        mock_client.embeddings.create = fake_create
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


if __name__ == "__main__":
    unittest.main()
