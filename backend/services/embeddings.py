"""
Embedding service for generating text embeddings.

Supports both local models (sentence-transformers) and remote APIs (OpenAI, Cohere).
Includes content-hash based caching to avoid recomputing embeddings.
"""

import hashlib
import logging
import os
from abc import ABC, abstractmethod
from typing import Optional

import numpy as np
from sentence_transformers import SentenceTransformer

from backend.config.presets import EmbeddingConfig


logger = logging.getLogger(__name__)


class EmbeddingService(ABC):
    """Abstract base class for embedding services."""

    @abstractmethod
    async def embed_text(self, text: str) -> list[float]:
        """
        Generate embedding for a single text.

        Args:
            text: Input text

        Returns:
            Embedding vector as list of floats
        """
        pass

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple texts.

        Args:
            texts: List of input texts

        Returns:
            List of embedding vectors
        """
        pass

    @abstractmethod
    def get_embedding_dim(self) -> int:
        """Get the dimensionality of embeddings."""
        pass

    @staticmethod
    def compute_content_hash(text: str) -> str:
        """
        Compute SHA256 hash of text content for caching.

        Args:
            text: Input text

        Returns:
            Hex string of hash
        """
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


class LocalEmbeddingService(EmbeddingService):
    """
    Embedding service using local sentence-transformer models.

    Uses HuggingFace sentence-transformers for local inference.
    """

    def __init__(
        self,
        config: EmbeddingConfig,
        cache_dir: Optional[str] = None,
        hf_token: Optional[str] = None,
    ):
        """
        Initialize local embedding service.

        Args:
            config: Embedding configuration
            cache_dir: Directory to cache model weights
            hf_token: HuggingFace token for model downloads
        """
        self.config = config
        self.cache_dir = cache_dir
        self.hf_token = hf_token
        self._model: Optional[SentenceTransformer] = None
        self._embedding_cache: dict[str, list[float]] = {}

        logger.info(f"Initialized LocalEmbeddingService with model: {config.model_name}")

    def _load_model(self):
        """Lazy load the embedding model."""
        if self._model is None:
            logger.info(f"Loading embedding model: {self.config.model_name}")

            model_kwargs = self.config.model_kwargs.copy()
            if self.cache_dir:
                model_kwargs["cache_folder"] = self.cache_dir

            # Set HuggingFace token as environment variable if provided
            # This ensures all HF libraries can access it
            if self.hf_token:
                os.environ["HF_TOKEN"] = self.hf_token
                model_kwargs["token"] = self.hf_token

            self._model = SentenceTransformer(
                self.config.model_name,
                **model_kwargs
            )

            logger.info(
                f"Model loaded. Embedding dimension: {self._model.get_sentence_embedding_dimension()}"
            )

    async def embed_text(self, text: str) -> list[float]:
        """Generate embedding for a single text."""
        # Check cache if enabled
        if self.config.cache_enabled:
            content_hash = self.compute_content_hash(text)
            if content_hash in self._embedding_cache:
                logger.debug(f"Cache hit for text hash: {content_hash[:8]}...")
                return self._embedding_cache[content_hash]

        # Generate embedding
        self._load_model()
        embedding = self._model.encode(text, convert_to_numpy=True)
        embedding_list = embedding.tolist()

        # Cache result if enabled
        if self.config.cache_enabled:
            content_hash = self.compute_content_hash(text)
            self._embedding_cache[content_hash] = embedding_list

        return embedding_list

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""
        if not texts:
            return []

        # Check cache for all texts
        uncached_texts = []
        uncached_indices = []
        results = [None] * len(texts)

        if self.config.cache_enabled:
            for i, text in enumerate(texts):
                content_hash = self.compute_content_hash(text)
                if content_hash in self._embedding_cache:
                    results[i] = self._embedding_cache[content_hash]
                else:
                    uncached_texts.append(text)
                    uncached_indices.append(i)
        else:
            uncached_texts = texts
            uncached_indices = list(range(len(texts)))

        # Generate embeddings for uncached texts
        if uncached_texts:
            self._load_model()
            logger.info(f"Generating embeddings for {len(uncached_texts)} texts")

            embeddings = self._model.encode(
                uncached_texts,
                batch_size=self.config.batch_size,
                convert_to_numpy=True,
                show_progress_bar=len(uncached_texts) > 10,
            )

            # Store results and cache
            for i, idx in enumerate(uncached_indices):
                embedding_list = embeddings[i].tolist()
                results[idx] = embedding_list

                if self.config.cache_enabled:
                    content_hash = self.compute_content_hash(texts[idx])
                    self._embedding_cache[content_hash] = embedding_list

        return results

    def get_embedding_dim(self) -> int:
        """Get the dimensionality of embeddings."""
        self._load_model()
        return self._model.get_sentence_embedding_dimension()

    def clear_cache(self):
        """Clear the embedding cache."""
        self._embedding_cache.clear()
        logger.info("Embedding cache cleared")


class RemoteEmbeddingService(EmbeddingService):
    """
    Embedding service using remote APIs (OpenAI, Cohere, etc.).

    This is a placeholder implementation - actual API integration would be added later.
    """

    def __init__(
        self,
        config: EmbeddingConfig,
        api_key: Optional[str] = None,
    ):
        """
        Initialize remote embedding service.

        Args:
            config: Embedding configuration
            api_key: API key for the service
        """
        self.config = config
        self.api_key = api_key
        self._embedding_cache: dict[str, list[float]] = {}

        logger.info(f"Initialized RemoteEmbeddingService with model: {config.model_name}")

    async def embed_text(self, text: str) -> list[float]:
        """Generate embedding for a single text."""
        # Check cache
        if self.config.cache_enabled:
            content_hash = self.compute_content_hash(text)
            if content_hash in self._embedding_cache:
                return self._embedding_cache[content_hash]

        # TODO: Implement actual API calls to OpenAI/Cohere
        logger.warning("Remote embedding not fully implemented - returning mock embedding")
        embedding = [0.0] * 1536  # OpenAI embedding dimension

        # Cache result
        if self.config.cache_enabled:
            content_hash = self.compute_content_hash(text)
            self._embedding_cache[content_hash] = embedding

        return embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""
        # TODO: Implement batched API calls
        logger.warning("Remote embedding not fully implemented - using sequential calls")
        return [await self.embed_text(text) for text in texts]

    def get_embedding_dim(self) -> int:
        """Get the dimensionality of embeddings."""
        # Return standard dimensions for known providers
        if "openai" in self.config.model_name.lower():
            return 1536
        elif "cohere" in self.config.model_name.lower():
            return 1024
        else:
            return 768  # Default


def create_embedding_service(
    config: EmbeddingConfig,
    cache_dir: Optional[str] = None,
    api_key: Optional[str] = None,
    hf_token: Optional[str] = None,
) -> EmbeddingService:
    """
    Factory function to create the appropriate embedding service.

    Args:
        config: Embedding configuration
        cache_dir: Directory to cache model weights (for local models)
        api_key: API key (for remote services)
        hf_token: HuggingFace token for model downloads (for local models)

    Returns:
        Configured embedding service

    Raises:
        ValueError: If model type is invalid
    """
    if config.model_type == "local":
        return LocalEmbeddingService(config, cache_dir=cache_dir, hf_token=hf_token)
    elif config.model_type == "remote":
        return RemoteEmbeddingService(config, api_key=api_key)
    else:
        raise ValueError(f"Invalid model_type: {config.model_type}")
