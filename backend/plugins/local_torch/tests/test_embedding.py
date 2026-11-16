"""Local sentence-transformers embeddings (skipped without the local-models extra)."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np

from backend.config.presets import EmbeddingConfig
from backend.plugins.local_torch.embedding import LocalEmbeddingService
from backend.services.embeddings import create_embedding_service

try:
    import sentence_transformers  # noqa: F401
    HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    HAS_SENTENCE_TRANSFORMERS = False


class TestFactoryUsesTorch(unittest.TestCase):
    def test_a_local_embedding_side_gets_the_torch_service(self):
        service = create_embedding_service(EmbeddingConfig(model_type="local", model_name="test-model"))
        self.assertIsInstance(service, LocalEmbeddingService)


class TestQueryInstruction(unittest.IsolatedAsyncioTestCase):
    async def test_local_service_without_instruction_embeds_bare(self):
        name = "sentence-transformers/all-MiniLM-L6-v2"
        service = create_embedding_service(EmbeddingConfig(model_name=name))
        service.embed_text = AsyncMock(return_value=[0.5])
        await service.embed_query("Who uses Zotero?")
        service.embed_text.assert_awaited_once_with("Who uses Zotero?")


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


if __name__ == "__main__":
    unittest.main()
