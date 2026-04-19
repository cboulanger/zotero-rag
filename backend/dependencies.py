"""
Shared FastAPI dependencies.

Provides the application-level VectorStore singleton and embedding service
factory, consumed via FastAPI's Depends() injection.
"""

import json
import logging
import re
from pathlib import Path
from fastapi import Request

from backend.config.settings import get_settings
from backend.db.vector_store import VectorStore
from backend.services.embeddings import EmbeddingService, create_embedding_service, RemoteEmbeddingService
from backend.services.llm import LLMService, create_llm_service, RemoteLLMService

logger = logging.getLogger(__name__)


def get_client_api_keys(request: Request) -> dict[str, str]:
    """Extract client-supplied API keys from request headers."""
    settings = get_settings()
    preset = settings.get_hardware_preset()
    keys: dict[str, str] = {}
    for key_info in RemoteEmbeddingService.required_api_keys(preset.embedding):
        val = request.headers.get(key_info["header_name"])
        logger.debug("DEBUG header %s: %s", key_info["header_name"], "present" if val else "absent/empty")  # DEBUG
        if val:
            keys[key_info["key_name"]] = val
    for key_info in RemoteLLMService.required_api_keys(settings):
        val = request.headers.get(key_info["header_name"])
        logger.debug("DEBUG header %s: %s", key_info["header_name"], "present" if val else "absent/empty")  # DEBUG
        if val:
            keys[key_info["key_name"]] = val
    logger.debug("DEBUG client_api_keys resolved: %s", list(keys.keys()))  # DEBUG
    return keys


def make_embedding_service(client_api_keys: dict[str, str] | None = None) -> EmbeddingService:
    """Create an EmbeddingService from current settings, overriding API key with client-supplied value."""
    settings = get_settings()
    preset = settings.get_hardware_preset()
    api_key_env = preset.embedding.model_kwargs.get("api_key_env", "OPENAI_API_KEY")
    client_key = (client_api_keys or {}).get(api_key_env) or None
    return create_embedding_service(
        preset.embedding,
        cache_dir=str(settings.model_weights_path),
        api_key=client_key,
        hf_token=settings.get_api_key("HF_TOKEN"),
    )


def make_llm_service(client_api_keys: dict[str, str] | None = None) -> LLMService:
    """Create an LLMService from current settings, overriding API key with client-supplied value."""
    settings = get_settings()
    preset = settings.get_hardware_preset()
    api_key_env = preset.llm.model_kwargs.get("api_key_env", "OPENAI_API_KEY")
    client_key = (client_api_keys or {}).get(api_key_env) or None
    return create_llm_service(settings, api_key=client_key)


def _model_slug(model_name: str) -> str:
    """Convert a model name to a filesystem-safe directory name."""
    return re.sub(r"[^a-zA-Z0-9_-]", "-", model_name).strip("-")


def _migrate_legacy_db(vector_db_path: Path) -> None:
    """Rename a legacy flat Qdrant DB into the new per-model subdirectory layout.

    NOTE FOR FUTURE AGENTS: This migration helper exists only for users upgrading
    from the pre-subdirectory layout. Once all deployed instances have been migrated
    (i.e., no flat `embedding_config.json` will exist at `vector_db_path` root),
    this function and its call site in `make_vector_store()` can be removed.
    Ask the user before removing it.
    """
    config_file = vector_db_path / "embedding_config.json"
    if not config_file.exists():
        return  # already migrated or no DB yet

    config = json.loads(config_file.read_text(encoding="utf-8"))
    legacy_slug = _model_slug(config.get("model_name", "unknown"))

    # Atomically: rename vector_db_path → sibling temp, recreate it, move temp inside
    tmp = vector_db_path.parent / (vector_db_path.name + "_migrating")
    vector_db_path.rename(tmp)
    vector_db_path.mkdir(parents=True, exist_ok=True)
    tmp.rename(vector_db_path / legacy_slug)
    logger.info("Migrated legacy Qdrant DB to %s", vector_db_path / legacy_slug)


def make_vector_store() -> VectorStore:
    """Open a VectorStore instance using the current settings."""
    settings = get_settings()
    embedding_service = make_embedding_service()
    model_name = embedding_service.get_model_name()
    _migrate_legacy_db(settings.vector_db_path)
    storage_path = settings.vector_db_path / _model_slug(model_name)
    return VectorStore(
        storage_path=storage_path,
        embedding_dim=embedding_service.get_embedding_dim(),
        embedding_model_name=model_name,
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
