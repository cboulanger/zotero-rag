"""
Configuration API endpoints.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Dict, List, Optional
import logging
import os

from backend.config.settings import get_settings
from backend.config.presets import PRESETS
from backend.services.embeddings import RemoteEmbeddingService, env_var_to_header
from backend.services.llm import RemoteLLMService
from backend.utils.kisski import fetch_kisski_rag_models

router = APIRouter()
logger = logging.getLogger(__name__)


class ConfigResponse(BaseModel):
    """Current configuration response."""
    preset_name: str
    api_version: str
    embedding_model: str
    embedding_model_type: str  # "local" | "remote"
    llm_model: str  # default (first) model name — kept for backward compatibility
    llm_models: List[str]  # all model names for the active preset
    vector_db_path: str
    model_cache_dir: str
    available_presets: List[str]
    # RAG configuration
    default_top_k: int
    default_min_score: float
    max_chunk_size: int


class ApiKeyRequirement(BaseModel):
    """A single API key required by the active preset."""
    key_name: str
    header_name: str
    description: str
    required_for: List[str]


class RequiredKeysResponse(BaseModel):
    """List of API keys required by the active preset."""
    keys: List[ApiKeyRequirement]


class ConfigUpdateRequest(BaseModel):
    """Configuration update request."""
    preset_name: Optional[str] = None
    vector_db_path: Optional[str] = None
    model_cache_dir: Optional[str] = None
    embedding_api_key: Optional[str] = None
    llm_api_key: Optional[str] = None


@router.get("/config", response_model=ConfigResponse)
def get_config(request: Request):
    """
    Get current configuration and available presets.

    For presets with a live models endpoint (e.g. KISSKI), the LLM model list is
    fetched dynamically and ordered by availability. Falls back to the preset's
    static model list if the live fetch fails.

    Returns:
        Current configuration including active preset and model settings.
    """
    settings = get_settings()
    preset = PRESETS[settings.model_preset]

    # Try to fetch a live, ordered model list for presets that support it
    llm_models = preset.llm.model_names
    if preset.llm.models_status_url:
        api_key_env = preset.llm.model_kwargs.get("api_key_env", "")
        base_url = preset.llm.model_kwargs.get("base_url", "")
        if api_key_env and base_url:
            header_name = env_var_to_header(api_key_env)
            api_key = (
                request.headers.get(header_name)
                or os.environ.get(api_key_env)
                or ""
            )
            if api_key:
                try:
                    live_models = fetch_kisski_rag_models(base_url, api_key)
                    if live_models:
                        llm_models = [m.id for m in live_models]
                except Exception as exc:
                    logger.warning(
                        "Could not fetch live models from %s: %s — using preset fallback",
                        base_url, exc,
                    )

    return ConfigResponse(
        preset_name=settings.model_preset,
        api_version=settings.version,
        embedding_model=preset.embedding.model_name,
        embedding_model_type=preset.embedding.model_type,
        llm_model=llm_models[0] if llm_models else preset.llm.model_name,
        llm_models=llm_models,
        vector_db_path=str(settings.vector_db_path),
        model_cache_dir=str(settings.model_weights_path),
        available_presets=list(PRESETS.keys()),
        # RAG configuration from preset
        default_top_k=preset.rag.top_k,
        default_min_score=preset.rag.score_threshold,
        max_chunk_size=preset.rag.max_chunk_size
    )


@router.post("/config")
def update_config(update: ConfigUpdateRequest, request: Request):
    """
    Update configuration settings.

    Note: This endpoint updates runtime configuration but does not persist changes.
    To persist changes, update the .env file or environment variables.

    Args:
        update: Configuration update request with optional fields.
        request: FastAPI request (forwarded to get_config for auth header resolution).

    Returns:
        Updated configuration.

    Raises:
        HTTPException: If preset name is invalid.
    """
    settings = get_settings()

    if update.preset_name and update.preset_name not in PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid preset: {update.preset_name}. "
                   f"Available presets: {list(PRESETS.keys())}"
        )

    # Note: Settings are immutable by default in pydantic-settings
    # For runtime updates, we'd need to implement a mutable config store
    # For now, just return current settings
    return {
        "message": "Configuration update received. "
                   "Note: Changes require server restart to take effect.",
        "current_config": get_config(request)
    }


@router.get("/required-keys", response_model=RequiredKeysResponse)
async def get_required_api_keys():
    """
    List the API keys required by the current backend preset.

    The client should send each key in the specified HTTP header when calling
    indexing or querying endpoints.  Keys in the header always override any
    server-side environment variable with the same name.
    """
    settings = get_settings()
    preset = settings.get_hardware_preset()

    seen: Dict[str, ApiKeyRequirement] = {}

    for key_info in RemoteEmbeddingService.required_api_keys(preset.embedding):
        key_name = key_info["key_name"]
        if key_name in seen:
            seen[key_name].required_for = list(
                set(seen[key_name].required_for) | set(key_info["required_for"])
            )
        else:
            seen[key_name] = ApiKeyRequirement(**key_info)

    for key_info in RemoteLLMService.required_api_keys(settings):
        key_name = key_info["key_name"]
        if key_name in seen:
            seen[key_name].required_for = list(
                set(seen[key_name].required_for) | set(key_info["required_for"])
            )
        else:
            seen[key_name] = ApiKeyRequirement(**key_info)

    return RequiredKeysResponse(keys=list(seen.values()))


class ModelStatus(BaseModel):
    """Availability status for a single remote model."""
    model: str
    demand: int
    status: str


class ModelsStatusResponse(BaseModel):
    """Per-model availability metrics for the active preset."""
    models: List[ModelStatus]


@router.get("/models/status", response_model=ModelsStatusResponse)
def get_models_status(request: Request):
    """
    Return per-model demand/availability metrics for the active preset.

    Fetches the live model list from the configured ``models_status_url``,
    filters to RAG-suitable models, and returns them ordered by demand
    (most available first).  Returns an empty list if the preset has no
    ``models_status_url`` or if the upstream call fails.

    The ``status`` field contains a human-readable availability label:
    ``"available"`` (demand 0), ``"busy"`` (1–5), or ``"very busy"`` (6+).
    """
    settings = get_settings()
    preset = settings.get_hardware_preset()

    if not preset.llm.models_status_url:
        return ModelsStatusResponse(models=[])

    api_key_env = preset.llm.model_kwargs.get("api_key_env", "")
    base_url = preset.llm.model_kwargs.get("base_url", "")
    if not base_url:
        return ModelsStatusResponse(models=[])

    header_name = env_var_to_header(api_key_env) if api_key_env else ""
    api_key = (
        (request.headers.get(header_name) if header_name else None)
        or (os.environ.get(api_key_env) if api_key_env else None)
        or ""
    )

    try:
        live_models = fetch_kisski_rag_models(base_url, api_key)
    except Exception as exc:
        logger.warning("Could not fetch model status from %s: %s", preset.llm.models_status_url, exc)
        return ModelsStatusResponse(models=[])

    return ModelsStatusResponse(models=[
        ModelStatus(model=m.id, demand=m.demand, status=m.availability)
        for m in live_models
    ])


@router.get("/version")
async def get_version():
    """
    Get backend API version.

    Used by Zotero plugin to check compatibility.

    Returns:
        API version information.
    """
    settings = get_settings()
    return {
        "api_version": settings.version,
        "service": "Zotero RAG API"
    }
