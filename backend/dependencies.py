"""
Shared FastAPI dependencies.

Provides the application-level VectorStore singleton and embedding service
factory, consumed via FastAPI's Depends() injection.
"""

import logging
from fastapi import Request

from backend.config.settings import get_settings
from backend.db.vector_store import VectorStore
from backend.services.embeddings import create_embedding_service

logger = logging.getLogger(__name__)


def make_embedding_service():
    """Create an EmbeddingService from current settings."""
    settings = get_settings()
    preset = settings.get_hardware_preset()
    return create_embedding_service(
        preset.embedding,
        cache_dir=str(settings.model_weights_path),
        hf_token=settings.get_api_key("HF_TOKEN"),
    )


def make_vector_store() -> VectorStore:
    """Open a VectorStore instance using the current settings."""
    settings = get_settings()
    embedding_service = make_embedding_service()
    return VectorStore(
        storage_path=settings.vector_db_path,
        embedding_dim=embedding_service.get_embedding_dim(),
        embedding_model_name=embedding_service.get_model_name(),
    )


def get_vector_store(request: Request) -> VectorStore:
    """
    FastAPI dependency that returns the application-wide VectorStore singleton.

    The singleton is opened once at startup (lifespan) and closed on shutdown,
    so all requests share a single Qdrant client and avoid the per-request
    lock-acquisition race that causes BlockingIOError / "already accessed by
    another instance" errors with the local Qdrant storage backend.
    """
    return request.app.state.vector_store
