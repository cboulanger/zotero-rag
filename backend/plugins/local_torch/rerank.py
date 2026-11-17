"""Local re-ranking with a sentence-transformers ``CrossEncoder`` in the backend process."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Optional

from backend.services.rerank import Reranker, RerankerUnavailable, _sigmoid

logger = logging.getLogger(__name__)


_model_cache: dict[tuple, Any] = {}
_model_lock = threading.Lock()


def _load_cross_encoder(model_name: str, max_length: int, kwargs: dict) -> Any:
    """Load (once per process) a sentence-transformers CrossEncoder."""
    key = (model_name, max_length, tuple(sorted((k, repr(v)) for k, v in kwargs.items())))
    with _model_lock:
        model = _model_cache.get(key)
        if model is None:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:  # pragma: no cover - depends on the install
                raise RerankerUnavailable(
                    "sentence-transformers is not installed (install the 'local-models' extra)"
                ) from exc
            started = time.monotonic()
            model = CrossEncoder(model_name, max_length=max_length, **kwargs)
            logger.info(f"Loaded re-ranker {model_name} in {time.monotonic() - started:.1f}s")
            _model_cache[key] = model
        return model


class LocalCrossEncoderReranker(Reranker):
    """A cross-encoder in the backend process; scores in a worker thread."""

    provider_id = None

    def __init__(self, model_name: str, max_length: int = 512, batch_size: int = 16, model_kwargs: Optional[dict] = None):
        self.model_name = model_name
        self.max_length = max_length
        self.batch_size = batch_size
        self.model_kwargs = dict(model_kwargs or {})

    def _score_sync(self, question: str, texts: list[str]) -> list[float]:
        model = _load_cross_encoder(self.model_name, self.max_length, self.model_kwargs)
        raw = model.predict(
            [(question, t) for t in texts],
            batch_size=self.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        values = [float(v) for v in list(raw)]
        # Models differ in whether predict() applies a sigmoid; map logits into [0, 1].
        if any(v < 0.0 or v > 1.0 for v in values):
            values = [_sigmoid(v) for v in values]
        return values

    async def score(self, question: str, texts: list[str]) -> list[float]:
        return await asyncio.to_thread(self._score_sync, question, texts)
