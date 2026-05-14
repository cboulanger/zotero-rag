"""
Configuration API endpoints.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Dict, List, Optional
import logging
import os
import httpx

from backend.config.settings import get_settings
from backend.config.presets import PRESETS
from backend.services.embeddings import RemoteEmbeddingService, env_var_to_header
from backend.services.llm import RemoteLLMService

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
async def get_config():
    """
    Get current configuration and available presets.

    Returns:
        Current configuration including active preset and model settings.
    """
    settings = get_settings()
    preset = PRESETS[settings.model_preset]

    return ConfigResponse(
        preset_name=settings.model_preset,
        api_version=settings.version,
        embedding_model=preset.embedding.model_name,
        embedding_model_type=preset.embedding.model_type,
        llm_model=preset.llm.model_name,
        llm_models=preset.llm.model_names,
        vector_db_path=str(settings.vector_db_path),
        model_cache_dir=str(settings.model_weights_path),
        available_presets=list(PRESETS.keys()),
        # RAG configuration from preset
        default_top_k=preset.rag.top_k,
        default_min_score=preset.rag.score_threshold,
        max_chunk_size=preset.rag.max_chunk_size
    )


@router.post("/config")
async def update_config(request: ConfigUpdateRequest):
    """
    Update configuration settings.

    Note: This endpoint updates runtime configuration but does not persist changes.
    To persist changes, update the .env file or environment variables.

    Args:
        request: Configuration update request with optional fields.

    Returns:
        Updated configuration.

    Raises:
        HTTPException: If preset name is invalid.
    """
    settings = get_settings()

    if request.preset_name and request.preset_name not in PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid preset: {request.preset_name}. "
                   f"Available presets: {list(PRESETS.keys())}"
        )

    # Note: Settings are immutable by default in pydantic-settings
    # For runtime updates, we'd need to implement a mutable config store
    # For now, just return current settings
    return {
        "message": "Configuration update received. "
                   "Note: Changes require server restart to take effect.",
        "current_config": await get_config()
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

    Calls the configured `models_status_url` (KISSKI format) and filters the
    result to models listed in the current preset.  Returns an empty list if
    the preset has no `models_status_url` or if the upstream call fails.
    """
    settings = get_settings()
    preset = settings.get_hardware_preset()
    status_url = preset.llm.models_status_url

    if not status_url:
        return ModelsStatusResponse(models=[])

    api_key_env = preset.llm.model_kwargs.get("api_key_env", "")
    header_name = env_var_to_header(api_key_env) if api_key_env else ""
    api_key = (
        (request.headers.get(header_name) if header_name else None)
        or (os.environ.get(api_key_env) if api_key_env else None)
        or ""
    )

    try:
        resp = httpx.post(
            status_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=5.0,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception as exc:
        logger.warning("Could not fetch model status from %s: %s", status_url, exc)
        return ModelsStatusResponse(models=[])

    allowed = set(preset.llm.model_names)
    models = [
        ModelStatus(model=entry["id"], demand=int(entry.get("demand", 0)), status=entry.get("status", "unknown"))
        for entry in data
        if isinstance(entry, dict) and entry.get("id") in allowed
    ]
    return ModelsStatusResponse(models=models)


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
