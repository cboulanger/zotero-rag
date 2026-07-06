"""
Shared FastAPI dependencies.

Provides the application-level VectorStore singleton and embedding service
factory, consumed via FastAPI's Depends() injection.
"""

import json
import logging
import re
from pathlib import Path
from typing import Optional
from fastapi import HTTPException, Request

from backend.config.settings import get_settings
from backend.db.vector_store import VectorStore
from backend.services.access_gate import is_loopback, passes_gate
from backend.services.embeddings import EmbeddingService, create_embedding_service, RemoteEmbeddingService
from backend.services.llm import LLMService, create_llm_service, RemoteLLMService
from backend.services.zotero_identity import ZoteroIdentity, get_identity_cache

logger = logging.getLogger(__name__)


async def resolve_zotero_identity(request: Request) -> Optional[ZoteroIdentity]:
    """Resolve and gate the caller's Zotero identity for the current request.

    Returns None for the loopback no-auth path (Part 4) and for the
    transitional legacy-shared-key path (Part 5) — both skip per-library
    enforcement in access_gate.assert_can_access(). Raises HTTPException:
    401 for a missing/invalid/revoked key, 403 if the Part 2 gate rejects
    an otherwise-valid identity, 503 if zotero.org is unreachable with no
    cached validation to fall back on.

    Called once per request by the auth middleware (added in a later task),
    which stashes the result on request.state.zotero_identity. Route
    handlers should depend on get_zotero_identity instead, not this
    function directly, to avoid re-validating the key a second time.
    """
    settings = get_settings()
    if is_loopback(settings):
        return None

    zotero_key = request.headers.get("X-Zotero-API-Key")
    if not zotero_key:
        legacy_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
        if settings.api_key and legacy_key == settings.api_key:
            logger.warning("Request authenticated via legacy shared API_KEY (transitional path)")
            return None
        raise HTTPException(status_code=401, detail="Missing X-Zotero-API-Key header.")

    validation = await get_identity_cache().resolve(zotero_key)
    if not validation.read_only:
        status_code = 503 if validation.transient else 401
        raise HTTPException(status_code=status_code, detail=validation.reason or "Invalid Zotero API key.")

    identity = ZoteroIdentity(user_id=validation.user_id, username=validation.username, targets=validation.targets)
    if not passes_gate(identity, settings):
        raise HTTPException(status_code=403, detail="This Zotero account is not authorized to use this server.")
    return identity


def get_zotero_identity(request: Request) -> Optional[ZoteroIdentity]:
    """FastAPI dependency: read back the identity the auth middleware resolved.

    Route handlers should depend on this — not resolve_zotero_identity
    directly — to avoid re-validating the key a second time per request.

    NOTE: no middleware currently sets request.state.zotero_identity (that
    wiring lands in a later task); until then this always returns None,
    which access_gate.assert_can_access() correctly treats as "skip
    per-library enforcement" — the same as today's pre-this-plan behavior.
    """
    return getattr(request.state, "zotero_identity", None)


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
    if settings.testing:
        from backend.services.embeddings import MockEmbeddingService, _KNOWN_DIMS
        model_name = settings.get_hardware_preset().embedding.model_name
        dim = next((d for k, d in _KNOWN_DIMS.items() if k in model_name), 1024)
        return MockEmbeddingService(embedding_dim=dim)
    preset = settings.get_hardware_preset()
    api_key_env = preset.embedding.model_kwargs.get("api_key_env", "OPENAI_API_KEY")
    client_key = (client_api_keys or {}).get(api_key_env) or None
    return create_embedding_service(
        preset.embedding,
        cache_dir=str(settings.model_weights_path),
        api_key=client_key,
        hf_token=settings.get_api_key("HF_TOKEN"),
    )


def make_llm_service(client_api_keys: dict[str, str] | None = None, model_name_override: str | None = None) -> LLMService:
    """Create an LLMService from current settings, overriding API key with client-supplied value."""
    settings = get_settings()
    if settings.testing:
        from backend.services.llm import MockLLMService
        return MockLLMService()
    preset = settings.get_hardware_preset()
    api_key_env = preset.llm.model_kwargs.get("api_key_env", "OPENAI_API_KEY")
    client_key = (client_api_keys or {}).get(api_key_env) or None
    return create_llm_service(settings, api_key=client_key, model_name_override=model_name_override)


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
    storage_path = settings.vector_db_path / _model_slug(model_name)
    qdrant_url = settings.qdrant_url or None
    if not qdrant_url:
        _migrate_legacy_db(settings.vector_db_path)
    return VectorStore(
        storage_path=storage_path,
        embedding_dim=embedding_service.get_embedding_dim(),
        embedding_model_name=model_name,
        url=qdrant_url,
        timeout=settings.qdrant_timeout,
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
