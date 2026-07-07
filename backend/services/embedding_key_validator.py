"""Validate a candidate embedding API key by making a real embedding call.

Mirrors backend.zotero.key_validator's approach: a permanent rejection
(bad credentials) invalidates the key, while a transient failure (network,
timeout, rate limit) does not — the caller should still store the key and
let a later cron run re-discover any real problem.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from backend.config.presets import EmbeddingConfig
from backend.services.embeddings import EmbeddingAuthenticationError, create_embedding_service

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingKeyValidation:
    """Result of validating a candidate embedding API key."""

    status: str  # "ok" | "invalid" | "unverified"
    key_name: str
    reason: Optional[str] = None


async def validate_embedding_key(api_key: str, config: EmbeddingConfig) -> EmbeddingKeyValidation:
    """Validate `api_key` against the configured remote embedding provider."""
    key_name = config.model_kwargs.get("api_key_env", "OPENAI_API_KEY")
    if config.model_type != "remote":
        return EmbeddingKeyValidation(
            status="unverified",
            key_name=key_name,
            reason="Cannot validate an embedding key against a non-remote model config.",
        )
    service = create_embedding_service(config, api_key=api_key)
    try:
        await service.embed_text("test")
    except EmbeddingAuthenticationError as exc:
        return EmbeddingKeyValidation(status="invalid", key_name=key_name, reason=str(exc))
    except Exception as exc:
        logger.warning("Embedding key validation call failed transiently: %s", exc)
        return EmbeddingKeyValidation(status="unverified", key_name=key_name, reason=str(exc))
    return EmbeddingKeyValidation(status="ok", key_name=key_name)
