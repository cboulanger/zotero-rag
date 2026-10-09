"""
Configuration API endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from pathlib import Path
from typing import Dict, List, Optional
import asyncio
import logging
import os

from backend.config.settings import get_settings
from backend.config.presets import get_preset, list_presets, current_platform, HardwarePreset
from backend.dependencies import require_authorized_group_admin
from backend.services.admin_settings_store import (
    set_active_preset_override,
    update_remote_config,
    get_remote_config_value,
    resolve_shared_value,
)
from backend.services import provisioning
from backend.utils.endpoint_health import HEALTH_CHECKS
from backend.services.embeddings import RemoteEmbeddingService, env_var_to_header
from backend.services.llm import RemoteLLMService
from backend.services.zotero_identity import ZoteroIdentity
from backend.utils.kisski import fetch_kisski_rag_models

router = APIRouter()
logger = logging.getLogger(__name__)


def _embedding_model_identity(model_name: str) -> str:
    """Normalize an embedding model name for cross-preset compatibility
    comparison: the basename after any "org/" prefix.

    Different providers serve the same underlying model under different
    literal API model-name strings — e.g. KISSKI/MPCDF serve
    "multilingual-e5-large-instruct" while RunPod's worker-infinity-embedding
    requires the full HuggingFace repo id "intfloat/multilingual-e5-large-instruct"
    (it's what the worker was launched with, and what must be sent as the
    "model" field in every embeddings API call — see
    scripts/provision_runpod_endpoints.py's MODEL_NAMES env var). Comparing
    basenames treats these as the same model without changing either
    preset's actual on-the-wire model_name.
    """
    return model_name.rsplit("/", 1)[-1]


def _compatible_presets(current: HardwarePreset, data_path: Path, available: List[str]) -> List[str]:
    """Presets safe to switch to at runtime without a restart: both the
    embedding and LLM must be remote (no local model to load/unload), and
    the embedding model must match — same model means the same vector
    space, so the already-open VectorStore singleton stays valid. Matched
    by normalized identity (see _embedding_model_identity), not literal
    string equality, since providers can serve the same model under
    different API model-name strings.

    `available` is the already-computed, platform-filtered preset name
    list (see list_presets(..., platform=...)) — passed in rather than
    re-derived here so a single request only lists the presets directory
    once.
    """
    if current.embedding.model_type != "remote" or current.llm.model_type != "remote":
        return [current.name]
    current_identity = _embedding_model_identity(current.embedding.model_name)
    compatible = []
    for name in available:
        try:
            preset = get_preset(name, data_path)
        except ValueError as exc:
            # A preset file can be deleted/corrupted between list_presets()'s
            # read and this one (the feature's whole point is that these
            # files are live-editable) — skip it like list_presets() itself
            # does, rather than letting one bad file 500 the whole request.
            logger.warning("Skipping preset %r while computing compatible_presets: %s", name, exc)
            continue
        if (
            preset.embedding.model_type == "remote"
            and preset.llm.model_type == "remote"
            and _embedding_model_identity(preset.embedding.model_name) == current_identity
        ):
            compatible.append(name)
    return compatible


class ConfigResponse(BaseModel):
    """Current configuration response."""
    preset_name: str
    preset_description: str
    api_version: str
    embedding_model: str
    embedding_model_type: str  # "local" | "remote"
    llm_model: str  # default (first) model name — kept for backward compatibility
    llm_models: List[str]  # all model names for the active preset
    vector_db_path: str
    model_cache_dir: str
    available_presets: List[str]  # filtered to this host's platform — see current_platform()
    compatible_presets: List[str]
    provisionable: bool = False  # active preset declares a provisioning_script
    # RAG configuration
    default_top_k: int
    default_min_score: float
    max_chunk_size: int


class ApiKeyRequirement(BaseModel):
    """A single client-configurable field required by the active preset."""
    key_name: str
    header_name: str
    kind: str = "api_key"  # "api_key" (personal, per-request header) | "shared_base_url" | "shared_api_key" (admin-set, global)
    description: str
    docs_url: Optional[str] = None
    required_for: List[str]
    is_set: Optional[bool] = None  # only meaningful for shared_* kinds — never exposes the value itself


class RequiredKeysResponse(BaseModel):
    """List of client-configurable fields required by the active preset."""
    keys: List[ApiKeyRequirement]


class ConfigUpdateRequest(BaseModel):
    """Configuration update request."""
    preset_name: Optional[str] = None
    vector_db_path: Optional[str] = None
    model_cache_dir: Optional[str] = None
    embedding_api_key: Optional[str] = None
    llm_api_key: Optional[str] = None


class RemoteFieldsUpdateRequest(BaseModel):
    """Body for POST /api/config/remote-fields."""
    values: Dict[str, str]


class RemoteFieldsResponse(BaseModel):
    """Presence map after an update — never echoes the values themselves."""
    is_set: Dict[str, bool]


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
    preset = settings.get_hardware_preset()

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

    available = list_presets(settings.data_path, platform=current_platform())
    return ConfigResponse(
        preset_name=preset.name,
        preset_description=preset.description,
        api_version=settings.version,
        embedding_model=preset.embedding.model_name,
        embedding_model_type=preset.embedding.model_type,
        llm_model=llm_models[0] if llm_models else preset.llm.model_name,
        llm_models=llm_models,
        vector_db_path=str(settings.vector_db_path),
        model_cache_dir=str(settings.model_weights_path),
        available_presets=available,
        compatible_presets=_compatible_presets(preset, settings.data_path, available),
        provisionable=bool(preset.provisioning_script),
        # RAG configuration from preset
        default_top_k=preset.rag.top_k,
        default_min_score=preset.rag.score_threshold,
        max_chunk_size=preset.rag.max_chunk_size
    )


@router.post("/config", response_model=ConfigResponse)
async def update_config(
    update: ConfigUpdateRequest,
    request: Request,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
):
    """
    Switch the active hardware preset at runtime (admin only).

    Takes effect immediately for every subsequent request, including the
    cron auto-indexer — no backend restart needed. Only presets compatible
    with the currently active one (see `compatible_presets` on GET /api/config)
    can be switched to; anything else (a different embedding model, or a
    local-model preset) still requires MODEL_PRESET + a restart, since it
    would invalidate the already-open vector store or require loading a
    local model into memory.

    Args:
        update: Must set `preset_name`. Other fields are accepted but
            ignored — this backend has no other runtime-mutable config.

    Raises:
        HTTPException: 400 if `preset_name` is missing, unknown, not in the
            current `compatible_presets` list, or hidden on this host's
            platform (a preset whose own `platform` field names a different
            OS than `current_platform()` — see backend.config.presets).
    """
    settings = get_settings()

    if not update.preset_name:
        raise HTTPException(status_code=400, detail="preset_name is required.")
    available = list_presets(settings.data_path, platform=current_platform())
    if update.preset_name not in available:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid preset: {update.preset_name}. Available: {available}",
        )

    current = settings.get_hardware_preset()
    compatible = _compatible_presets(current, settings.data_path, available)
    if update.preset_name not in compatible:
        raise HTTPException(
            status_code=400,
            detail=f"Preset '{update.preset_name}' cannot be switched to at runtime from "
                   f"'{current.name}' (requires the same remote embedding model). "
                   f"Compatible presets: {compatible}. To use a different embedding model or a "
                   f"local-model preset, set MODEL_PRESET and restart the backend instead.",
        )

    set_active_preset_override(settings.data_path, update.preset_name)
    return await asyncio.to_thread(get_config, request)


class EndpointHealth(BaseModel):
    """Readiness of one remote endpoint."""
    status: str  # "ready" | "cold" | "unreachable"
    detail: str


class EndpointHealthResponse(BaseModel):
    """Per-side health; ``None`` means the active preset declares no health check."""
    embedding: Optional[EndpointHealth] = None
    llm: Optional[EndpointHealth] = None


def _check_side(provider: Optional[str], model_kwargs: dict, data_path: Path) -> Optional[EndpointHealth]:
    if not provider:
        return None
    check = HEALTH_CHECKS.get(provider)
    if check is None:
        return EndpointHealth(status="unreachable", detail=f"Unknown health check provider: {provider}")
    url_env = model_kwargs.get("shared_base_url_env")
    key_env = model_kwargs.get("shared_api_key_env")
    base_url = resolve_shared_value(data_path, url_env) if url_env else None
    api_key = resolve_shared_value(data_path, key_env) if key_env else None
    if not base_url or not api_key:
        return EndpointHealth(status="unreachable", detail="not configured")
    return EndpointHealth(**check(base_url, api_key))


@router.get("/config/health", response_model=EndpointHealthResponse)
def get_endpoint_health() -> EndpointHealthResponse:
    """
    Readiness (ready / cold / unreachable) of the active preset's remote
    embedding and LLM endpoints. A side whose config declares no
    ``health_check_provider`` is ``null``. Plain ``def``: the provider checks
    do blocking HTTP, so FastAPI runs this in a thread pool.
    """
    settings = get_settings()
    preset = settings.get_hardware_preset()
    return EndpointHealthResponse(
        embedding=_check_side(
            preset.embedding.health_check_provider, preset.embedding.model_kwargs, settings.data_path
        ),
        llm=_check_side(preset.llm.health_check_provider, preset.llm.model_kwargs, settings.data_path),
    )


@router.post("/config/provision", status_code=202)
async def start_provisioning(
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
):
    """
    Run the active preset's ``provisioning_script`` as a background job
    (admin only). On success the script's reported base URLs are applied via
    the shared remote-config store. Poll GET /api/config/provision/status.

    Raises:
        HTTPException: 400 if the active preset has no provisioning_script;
            409 if a job is already running.
    """
    settings = get_settings()
    preset = settings.get_hardware_preset()
    if not preset.provisioning_script:
        raise HTTPException(
            status_code=400,
            detail=f"Preset '{preset.name}' does not declare a provisioning_script.",
        )
    if provisioning.is_running():
        raise HTTPException(status_code=409, detail="A provisioning job is already running.")
    provisioning.mark_running()
    try:
        proc = await provisioning.start_job(preset.provisioning_script)
    except Exception as exc:
        provisioning._finish("failed", f"Could not start provisioning script: {exc}")
        return provisioning.get_job_state()
    task = asyncio.create_task(provisioning.await_job(proc, settings.data_path))
    _provision_tasks.add(task)  # keep a strong reference until done
    task.add_done_callback(_provision_tasks.discard)
    return provisioning.get_job_state()


_provision_tasks: set = set()


@router.get("/config/provision/status")
def get_provisioning_status() -> dict:
    """Current provisioning job state: status idle|running|succeeded|failed."""
    return provisioning.get_job_state()


@router.post("/config/remote-fields", response_model=RemoteFieldsResponse)
async def set_remote_fields(
    update: RemoteFieldsUpdateRequest,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
) -> RemoteFieldsResponse:
    """
    Set one or more shared, admin-controlled remote-config values (a
    preset's `shared_base_url_env`/`shared_api_key_env` fields — see
    backend.services.admin_settings_store) for the currently active
    preset. Admin-gated: this is global state, visible to every caller
    and to the cron auto-indexer, not a per-user setting.

    Raises:
        HTTPException: 400 if any key in `values` isn't declared by the
            active preset's embedding/LLM config.
    """
    settings = get_settings()
    preset = settings.get_hardware_preset()

    allowed_keys: set = set()
    for key_info in RemoteEmbeddingService.required_client_fields(preset.embedding):
        if key_info["kind"] in ("shared_base_url", "shared_api_key"):
            allowed_keys.add(key_info["key_name"])
    for key_info in RemoteLLMService.required_client_fields(settings):
        if key_info["kind"] in ("shared_base_url", "shared_api_key"):
            allowed_keys.add(key_info["key_name"])

    unknown = set(update.values.keys()) - allowed_keys
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown remote-config key(s) for preset '{preset.name}': {sorted(unknown)}. "
                   f"Allowed: {sorted(allowed_keys)}",
        )

    merged = update_remote_config(settings.data_path, update.values)
    return RemoteFieldsResponse(is_set={k: bool(v) for k, v in merged.items()})


@router.get("/required-keys", response_model=RequiredKeysResponse)
async def get_required_api_keys():
    """
    List the client-configurable fields required by the current backend preset.

    `kind="api_key"` entries are personal: the client should send each in the
    specified HTTP header on indexing/querying requests, overriding any
    server-side environment variable with the same name.

    `kind="shared_base_url"`/`"shared_api_key"` entries are global, admin-set
    values (e.g. the MPCDF preset's rotating job endpoint/key) — set them via
    POST /api/config/remote-fields, not a per-request header. `is_set`
    reports whether a value is already available (from the shared store or
    an environment variable), without ever exposing the value itself.
    """
    settings = get_settings()
    preset = settings.get_hardware_preset()

    seen: Dict[str, ApiKeyRequirement] = {}

    def _merge(key_info: dict) -> None:
        key_name = key_info["key_name"]
        if key_name in seen:
            seen[key_name].required_for = list(
                set(seen[key_name].required_for) | set(key_info["required_for"])
            )
            return
        is_set = None
        if key_info["kind"] in ("shared_base_url", "shared_api_key"):
            is_set = bool(
                get_remote_config_value(settings.data_path, key_name) or os.environ.get(key_name)
            )
        seen[key_name] = ApiKeyRequirement(**key_info, is_set=is_set)

    for key_info in RemoteEmbeddingService.required_client_fields(preset.embedding):
        _merge(key_info)
    for key_info in RemoteLLMService.required_client_fields(settings):
        _merge(key_info)

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
