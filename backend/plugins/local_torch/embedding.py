"""Local embeddings with sentence-transformers on torch (CPU, CUDA or Apple MPS).

sentence-transformers (and torch) are imported lazily so that the plugin loads
without them; the capability then reports the missing package.
"""

import asyncio
import logging
import os
from typing import Any, Callable, Optional

from backend.config.presets import EmbeddingConfig
from backend.services.embeddings import EmbeddingService

logger = logging.getLogger(__name__)


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
        logger.debug(f"Initialized LocalEmbeddingService with model: {config.model_name}")

    def _load_model(self):
        """Lazy-load the embedding model (imports sentence-transformers on first call)."""
        if self._model is None:
            logger.debug(f"Loading embedding model: {self.config.model_name}")
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

            # Auto-select best available device when the preset doesn't specify one.
            if "device" not in model_kwargs:
                try:
                    import torch
                    if torch.backends.mps.is_available():
                        model_kwargs["device"] = "mps"
                        logger.debug("MPS (Apple Silicon) detected — using GPU acceleration.")
                    elif torch.cuda.is_available():
                        model_kwargs["device"] = "cuda"
                        logger.debug("CUDA detected — using GPU acceleration.")
                except ImportError:
                    pass

            self._model = SentenceTransformer(self.config.model_name, **model_kwargs)
            logger.debug(
                f"Model loaded on {model_kwargs.get('device', 'cpu')}. "
                f"Embedding dimension: {self._model.get_sentence_embedding_dimension()}"
            )

    async def embed_text(self, text: str) -> list[float]:
        """Generate embedding for a single text."""
        if self.config.cache_enabled:
            content_hash = self.compute_content_hash(text)
            if content_hash in self._embedding_cache:
                logger.debug(f"Cache hit for text hash: {content_hash[:8]}...")
                return self._embedding_cache[content_hash]

        self._load_model()
        embedding = await asyncio.to_thread(self._model.encode, text, convert_to_numpy=True)
        embedding_list = embedding.tolist()

        if self.config.cache_enabled:
            content_hash = self.compute_content_hash(text)
            self._embedding_cache[content_hash] = embedding_list

        return embedding_list

    async def embed_batch(
        self,
        texts: list[str],
        on_batch: Optional[Callable[[int, int], None]] = None,
    ) -> list[list[float]]:
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
            logger.debug(f"Generating embeddings for {len(uncached_texts)} texts")
            embeddings = await asyncio.to_thread(
                self._model.encode,
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

        if on_batch:
            on_batch(len(texts), len(texts))
        return results  # type: ignore[return-value]

    def get_embedding_dim(self) -> int:
        """Get the dimensionality of embeddings."""
        self._load_model()
        return self._model.get_sentence_embedding_dimension()

    def get_model_name(self) -> str:
        """Return the model identifier."""
        return self.config.model_name

    async def get_rate_limit_info(self) -> dict[str, str] | None:
        return None

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
