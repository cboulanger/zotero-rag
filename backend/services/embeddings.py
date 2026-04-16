"""
Embedding service for generating text embeddings.

Supports both local models (sentence-transformers) and remote APIs (OpenAI-compatible).
Includes content-hash based caching to avoid recomputing embeddings.
"""

import hashlib
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Optional

import numpy as np

from backend.config.presets import EmbeddingConfig


logger = logging.getLogger(__name__)

# Known embedding dimensions for common remote models.
# Used by get_embedding_dim() to avoid an extra API round-trip.
_KNOWN_DIMS: dict[str, int] = {
    # OpenAI
    "text-embedding-ada-002": 1536,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "openai": 1536,  # generic "openai" sentinel used in remote-openai preset
    # KISSKI / SAIA (https://docs.hpc.gwdg.de/services/ai-services/saia/)
    "multilingual-e5-large-instruct": 1024,
    "e5-mistral-7b-instruct": 4096,
    "qwen3-embedding-4b": 2048,
    # Cohere
    "embed-english-v3.0": 1024,
    "embed-multilingual-v3.0": 1024,
}


class EmbeddingService(ABC):
    """Abstract base class for embedding services."""

    @abstractmethod
    async def embed_text(self, text: str) -> list[float]:
        """Generate embedding for a single text."""

    @abstractmethod
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""

    @abstractmethod
    def get_embedding_dim(self) -> int:
        """Get the dimensionality of embeddings."""

    @staticmethod
    def compute_content_hash(text: str) -> str:
        """Compute SHA256 hash of text content for caching."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


class LocalEmbeddingService(EmbeddingService):
    """
    Embedding service using local sentence-transformer models.

    sentence-transformers (and torch) are imported lazily so that the module
    can be loaded without them when a remote preset is used.
    """

    def __init__(
        self,
        config: EmbeddingConfig,
        cache_dir: Optional[str] = None,
        hf_token: Optional[str] = None,
    ):
        self.config = config
        self.cache_dir = cache_dir
        self.hf_token = hf_token
        self._model: Optional[Any] = None  # SentenceTransformer, imported lazily
        self._embedding_cache: dict[str, list[float]] = {}
        logger.info(f"Initialized LocalEmbeddingService with model: {config.model_name}")

    def _load_model(self):
        """Lazy-load the embedding model (imports sentence-transformers on first call)."""
        if self._model is None:
            logger.info(f"Loading embedding model: {self.config.model_name}")
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                raise RuntimeError(
                    "sentence-transformers is required for local embeddings. "
                    "Install it with: uv add sentence-transformers"
                )

            model_kwargs = self.config.model_kwargs.copy()
            if self.cache_dir:
                model_kwargs["cache_folder"] = self.cache_dir
            if self.hf_token:
                os.environ["HF_TOKEN"] = self.hf_token
                model_kwargs["token"] = self.hf_token

            self._model = SentenceTransformer(self.config.model_name, **model_kwargs)
            logger.info(
                f"Model loaded. Embedding dimension: {self._model.get_sentence_embedding_dimension()}"
            )

    async def embed_text(self, text: str) -> list[float]:
        """Generate embedding for a single text."""
        if self.config.cache_enabled:
            content_hash = self.compute_content_hash(text)
            if content_hash in self._embedding_cache:
                logger.debug(f"Cache hit for text hash: {content_hash[:8]}...")
                return self._embedding_cache[content_hash]

        self._load_model()
        embedding = self._model.encode(text, convert_to_numpy=True)
        embedding_list = embedding.tolist()

        if self.config.cache_enabled:
            content_hash = self.compute_content_hash(text)
            self._embedding_cache[content_hash] = embedding_list

        return embedding_list

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""
        if not texts:
            return []

        uncached_texts: list[str] = []
        uncached_indices: list[int] = []
        results: list[Optional[list[float]]] = [None] * len(texts)

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

        if uncached_texts:
            self._load_model()
            logger.info(f"Generating embeddings for {len(uncached_texts)} texts")
            embeddings = self._model.encode(
                uncached_texts,
                batch_size=self.config.batch_size,
                convert_to_numpy=True,
                show_progress_bar=len(uncached_texts) > 10,
            )
            for i, idx in enumerate(uncached_indices):
                embedding_list = embeddings[i].tolist()
                results[idx] = embedding_list
                if self.config.cache_enabled:
                    content_hash = self.compute_content_hash(texts[idx])
                    self._embedding_cache[content_hash] = embedding_list

        return results  # type: ignore[return-value]

    def get_embedding_dim(self) -> int:
        """Get the dimensionality of embeddings."""
        self._load_model()
        return self._model.get_sentence_embedding_dimension()

    def clear_cache(self):
        """Clear the embedding cache."""
        self._embedding_cache.clear()
        logger.info("Embedding cache cleared")

    def close(self):
        """Release model from memory and clear CUDA cache if available."""
        try:
            if self._model is not None:
                del self._model
                self._model = None
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except (ImportError, RuntimeError):
                    pass
                logger.debug("LocalEmbeddingService model cleaned up")
        except Exception as e:
            logger.debug(f"Error during embedding service cleanup: {e}")


class RemoteEmbeddingService(EmbeddingService):
    """
    Embedding service using any OpenAI-compatible embeddings API.

    Supports OpenAI, KISSKI/SAIA (https://chat-ai.academiccloud.de/v1), and
    any other provider that implements the /v1/embeddings endpoint.

    Configuration via EmbeddingConfig.model_kwargs:
      - ``base_url``:    API base URL (default: OpenAI)
      - ``api_key_env``: env-var name that holds the API key (default: OPENAI_API_KEY)
    """

    def __init__(self, config: EmbeddingConfig, api_key: Optional[str] = None):
        self.config = config
        self._api_key = api_key  # explicit override; falls back to env-var lookup
        self._client: Optional[Any] = None  # AsyncOpenAI, imported lazily
        self._embedding_cache: dict[str, list[float]] = {}
        self._dim: Optional[int] = None
        logger.info(
            f"Initialized RemoteEmbeddingService: model={config.model_name} "
            f"base_url={config.model_kwargs.get('base_url', 'openai-default')}"
        )

    def _get_client(self):
        """Lazy-initialize the AsyncOpenAI client."""
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError:
                raise RuntimeError(
                    "openai package is required for remote embeddings. "
                    "Install it with: uv add openai"
                )
            api_key_env = self.config.model_kwargs.get("api_key_env", "OPENAI_API_KEY")
            api_key = self._api_key or os.getenv(api_key_env)
            if not api_key:
                raise ValueError(
                    f"API key not found. Set the {api_key_env} environment variable."
                )
            base_url = self.config.model_kwargs.get("base_url")
            if base_url:
                self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
                logger.info(f"OpenAI-compatible client initialised with base_url={base_url}")
            else:
                self._client = AsyncOpenAI(api_key=api_key)
                logger.info("OpenAI embeddings client initialised")
        return self._client

    def _resolve_model_name(self) -> str:
        """Return the actual API model name (maps 'openai' sentinel to a real name)."""
        name = self.config.model_name
        if name == "openai":
            return "text-embedding-3-small"
        return name

    async def embed_text(self, text: str) -> list[float]:
        """Generate embedding for a single text."""
        if self.config.cache_enabled:
            content_hash = self.compute_content_hash(text)
            if content_hash in self._embedding_cache:
                return self._embedding_cache[content_hash]

        client = self._get_client()
        response = await client.embeddings.create(
            model=self._resolve_model_name(),
            input=text,
            encoding_format="float",
        )
        embedding = response.data[0].embedding

        if self._dim is None:
            self._dim = len(embedding)

        if self.config.cache_enabled:
            content_hash = self.compute_content_hash(text)
            self._embedding_cache[content_hash] = embedding

        return embedding

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts in a single API call."""
        if not texts:
            return []

        uncached_texts: list[str] = []
        uncached_indices: list[int] = []
        results: list[Optional[list[float]]] = [None] * len(texts)

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

        if uncached_texts:
            client = self._get_client()
            logger.info(
                f"Requesting embeddings for {len(uncached_texts)} texts "
                f"(model={self._resolve_model_name()})"
            )
            # Send in batches respecting config.batch_size
            batch_size = self.config.batch_size
            for batch_start in range(0, len(uncached_texts), batch_size):
                batch = uncached_texts[batch_start:batch_start + batch_size]
                response = await client.embeddings.create(
                    model=self._resolve_model_name(),
                    input=batch,
                    encoding_format="float",
                )
                for j, item in enumerate(response.data):
                    embedding = item.embedding
                    global_idx = uncached_indices[batch_start + j]
                    results[global_idx] = embedding
                    if self.config.cache_enabled:
                        content_hash = self.compute_content_hash(texts[global_idx])
                        self._embedding_cache[content_hash] = embedding
                    if self._dim is None:
                        self._dim = len(embedding)

        return results  # type: ignore[return-value]

    def get_embedding_dim(self) -> int:
        """
        Return embedding dimensionality.

        Uses the cached value from a previous call, then the _KNOWN_DIMS table,
        then a sensible default. The true dimension is confirmed on the first
        actual embedding call.
        """
        if self._dim is not None:
            return self._dim
        model = self._resolve_model_name()
        for key, dim in _KNOWN_DIMS.items():
            if key in model:
                return dim
        logger.warning(
            f"Unknown embedding dimension for model '{model}', defaulting to 1536. "
            "Will be confirmed on first embedding call."
        )
        return 1536


def create_embedding_service(
    config: EmbeddingConfig,
    cache_dir: Optional[str] = None,
    api_key: Optional[str] = None,
    hf_token: Optional[str] = None,
) -> EmbeddingService:
    """
    Factory: create the appropriate EmbeddingService for the given config.

    Args:
        config:    Embedding configuration (from a HardwarePreset).
        cache_dir: Model weights cache directory (local only).
        api_key:   Unused — remote services read the key from the env var
                   named in config.model_kwargs['api_key_env']. Kept for
                   backwards-compatibility.
        hf_token:  HuggingFace token for gated models (local only).
    """
    if config.model_type == "local":
        return LocalEmbeddingService(config, cache_dir=cache_dir, hf_token=hf_token)
    elif config.model_type == "remote":
        return RemoteEmbeddingService(config, api_key=api_key)
    else:
        raise ValueError(f"Invalid model_type: {config.model_type!r}")
