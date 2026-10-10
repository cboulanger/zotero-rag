"""
Configuration API endpoints.
"""

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import asyncio
import logging
import os
import re

from backend.config.settings import get_settings
from backend.config.presets import get_preset, list_presets, current_platform, HardwarePreset
from backend.dependencies import get_zotero_identity, require_authorized_group_admin
from backend.services.access_gate import is_loopback
from backend.services.endpoint_cache import endpoint_cache
from backend.services.effective_preset import is_compatible
from backend.services.user_settings import get_preferred_preset, set_preferred_preset
from backend.services.admin_settings_store import (
    set_default_preset,
    update_remote_config,
    get_remote_config_value,
    resolve_shared_value,
)
from backend.services import provisioning
from backend.services.secret_store import (
    SecretsUnavailableError, is_secret_name, require_secrets_enabled,
)
from backend.services.secret_store import get_key_store
from backend.services.embeddings import RemoteEmbeddingService, env_var_to_header
from backend.services.usage_meters import recorder as usage_recorder
from backend.services.llm import RemoteLLMService
from backend.services.zotero_identity import ZoteroIdentity
from backend.providers import Credentials, Provider, ProviderConfigError, ProvisionContext, get_providers
from backend.providers.types import ModelInfo

router = APIRouter()
logger = logging.getLogger(__name__)


def _compatible_presets(current: HardwarePreset, data_path: Path, available: List[str]) -> List[str]:
    """Presets safe to switch to at runtime without a restart: both the
    embedding and LLM must be remote (no local model to load/unload), and
    the embedding model must match — same model means the same vector
    space, so the already-open VectorStore singleton stays valid. Matched
    by normalized identity (see effective_preset.embedding_model_identity), not literal
    string equality, since providers can serve the same model under
    different API model-name strings.

    `available` is the already-computed, platform-filtered preset name
    list (see list_presets(..., platform=...)) — passed in rather than
    re-derived here so a single request only lists the presets directory
    once.
    """
    if current.embedding.model_type != "remote" or current.llm.model_type != "remote":
        return [current.name]
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
        if is_compatible(current, preset):
            compatible.append(name)
    return compatible


def _stored_embedding_key_counts(settings) -> Dict[str, int]:
    """Count stored, non-invalid auto-index embedding keys by provider key name.

    Returns an empty dict when the key store is disabled or unreadable.
    """
    try:
        store = get_key_store()
        if not store.enabled:
            return {}
        return store.count_embedding_keys_by_name()
    except Exception as exc:
        logger.warning("Could not read auto-index key store for credential check: %s", exc)
        return {}


def _preset_credentials(
    preset: HardwarePreset,
    settings,
    request: Request,
    stored_key_counts: Optional[Dict[str, int]] = None,
) -> List[str]:
    """Return the names of credentials the given preset still lacks.

    An empty list means the preset is usable ("present and not known-invalid";
    no live validation, so no quota is spent). Only key *names* are returned,
    never values.

    - ``shared_base_url`` / ``shared_api_key``: admin-set remote config value
      or server environment variable. Not required for a side whose provider
      can provision: provisioning supplies them after the switch.
    - ``api_key`` (personal): the requesting admin's header, a server env var,
      or at least one stored non-invalid auto-index embedding key with that
      name (what the auto-index job needs; it also satisfies an LLM-side
      requirement of the same key name, since that is the same provider key).

    Args:
        preset: Preset to check (embedding and LLM sides).
        settings: Application settings (``data_path``, key-store config).
        request: Current request, for the personal-key header.
        stored_key_counts: Precomputed ``_stored_embedding_key_counts`` result.
    """
    if stored_key_counts is None:
        stored_key_counts = _stored_embedding_key_counts(settings)
    missing: List[str] = []

    try:
        providers = get_providers(preset)
    except ProviderConfigError:
        providers = {}

    def _check(fields: List[dict], side: str) -> None:
        provisionable = side in providers and providers[side].supports_provisioning
        for field in fields:
            name = field["key_name"]
            if field["kind"] in ("shared_base_url", "shared_api_key"):
                if provisionable:
                    continue
                ok = bool(get_remote_config_value(name))
            else:
                ok = bool(request.headers.get(field["header_name"]) or stored_key_counts.get(name, 0) > 0)
            if not ok and name not in missing:
                missing.append(name)

    _check(RemoteEmbeddingService.required_client_fields(preset.embedding), "embedding")
    _check(RemoteLLMService.required_client_fields_for_config(preset.llm), "llm")
    return missing


def _switchable_presets(
    current: HardwarePreset, compatible: List[str], settings, request: Request,
) -> List["SwitchablePreset"]:
    """Compatible presets that have usable credentials; the active one is always listed."""
    counts = _stored_embedding_key_counts(settings)
    result: List[SwitchablePreset] = []
    for name in compatible:
        is_active = name == current.name
        try:
            preset = current if is_active else get_preset(name, settings.data_path)
        except ValueError as exc:
            logger.warning("Skipping preset %r while computing switchable_presets: %s", name, exc)
            continue
        missing = _preset_credentials(preset, settings, request, counts)
        if missing and not is_active:
            continue
        result.append(SwitchablePreset(
            name=name, active=is_active, credentials="missing" if missing else "ok",
        ))
    return result


def _selectable_presets(
    effective: HardwarePreset, compatible: List[str], settings, request: Request,
) -> List["SwitchablePreset"]:
    """Presets this caller may choose: everything compatible with the default.

    Each entry says whether the caller has credentials for it (``credentials``) and, if
    not, which key names are missing (``missing_keys``), so the plugin can ask for them
    before choosing it. Only the caller's own request counts as a source of personal keys
    (never other users' stored keys). The caller's current preset is always listed.
    """
    names = list(compatible)
    if effective.name not in names:
        names.append(effective.name)
    result: List[SwitchablePreset] = []
    for name in names:
        try:
            preset = effective if name == effective.name else get_preset(name, settings.data_path)
        except ValueError as exc:
            logger.warning("Skipping preset %r while computing selectable_presets: %s", name, exc)
            continue
        missing = _preset_credentials(preset, settings, request, {})
        result.append(SwitchablePreset(
            name=name, active=name == effective.name, credentials="missing" if missing else "ok",
            missing_keys=sorted(missing), description=preset.description,
        ))
    return result


def _fell_back_choice(settings, identity: Optional[ZoteroIdentity], effective: HardwarePreset) -> Optional[str]:
    """The preset the caller saved but is not running on (removed, incompatible, ...), if any."""
    if identity is None:
        return None
    chosen = get_preferred_preset(settings.data_path, identity.user_id)
    return chosen if chosen and chosen != effective.name else None


class SwitchablePreset(BaseModel):
    """A preset the admin may switch the default to, or a user may choose for themselves."""
    name: str
    active: bool
    credentials: str  # "ok" | "missing"
    missing_keys: List[str] = []  # key names still needed (never values); only set for user choices
    description: str = ""


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
    switchable_presets: List[SwitchablePreset] = []  # compatible presets with usable credentials (admin switch)
    default_preset: str = ""  # the server default; ``preset_name`` is this caller's effective preset
    selectable_presets: List[SwitchablePreset] = []  # presets this caller may choose for themselves
    preset_fell_back: Optional[str] = None  # the caller's saved choice, when it is no longer honoured
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
    pattern: Optional[str] = None  # optional regex the value must fullmatch — see required_client_fields
    sides: List[str] = []  # which sides of the preset use this field ("embedding" and/or "llm")


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


def _llm_provider(preset: HardwarePreset) -> Optional[Provider]:
    """The LLM side's provider, or None when the preset's providers are invalid."""
    try:
        return get_providers(preset)["llm"]
    except ProviderConfigError as exc:
        logger.warning("Preset '%s' has an invalid provider configuration: %s", preset.name, exc)
        return None


def _live_llm_models(
    request: Request, preset: HardwarePreset, *, require_key: bool
) -> Optional[List[ModelInfo]]:
    """Live model list from the LLM provider, or None when it has none.

    The key is the one in the request header for the preset's personal-key
    field, else the process environment. ``require_key=False`` still queries
    without a key (the model-status route does, as it always has).
    """
    provider = _llm_provider(preset)
    if provider is None or not provider.has_live_models:
        return None
    api_key_env = preset.llm.model_kwargs.get("api_key_env", "")
    base_url = preset.llm.model_kwargs.get("base_url", "")
    if not base_url:
        return None
    header_name = env_var_to_header(api_key_env) if api_key_env else ""
    api_key = (request.headers.get(header_name) if header_name else None) or ""
    if require_key and not api_key:
        return None
    return provider.live_models(base_url, api_key)


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

    # Presets whose LLM provider has a live model endpoint (e.g. KISSKI) get a
    # dynamically fetched, availability-ordered list; any failure falls back
    # to the preset's static model list.
    llm_models = preset.llm.model_names
    live_models = _live_llm_models(request, preset, require_key=True)
    if live_models:
        llm_models = [m.id for m in live_models]

    available = list_presets(settings.data_path, platform=current_platform())
    default = settings.get_default_preset()
    compatible = _compatible_presets(default, settings.data_path, available)
    identity = get_zotero_identity(request)
    selectable = _selectable_presets(preset, compatible, settings, request)
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
        compatible_presets=compatible,
        switchable_presets=_switchable_presets(default, compatible, settings, request),
        default_preset=default.name,
        selectable_presets=selectable,
        preset_fell_back=_fell_back_choice(settings, identity, preset),
        # RAG configuration from preset
        default_top_k=preset.rag.top_k,
        default_min_score=preset.rag.score_threshold,
        max_chunk_size=preset.rag.max_chunk_size
    )


class MyPresetUpdate(BaseModel):
    """Body of PUT /api/config/my-preset; ``null`` returns the caller to the server default."""
    preset_name: Optional[str] = None


@router.get("/config/my-preset")
async def get_my_preset(request: Request) -> dict:
    """The caller's preset: the server default, the one they run on, their saved choice and
    what they may choose. A saved choice that is no longer honoured shows as ``fell_back``.
    ``loopback`` and ``is_admin`` tell the plugin which controls to offer (a loopback
    server has no per-user choice; only an admin may change the default)."""
    settings = get_settings()
    view = await asyncio.to_thread(_my_preset_view, request, settings.get_hardware_preset())
    view["loopback"] = is_loopback(settings)
    try:
        await require_authorized_group_admin(request)
        view["is_admin"] = True
    except HTTPException:
        view["is_admin"] = False
    return view


def _my_preset_view(request: Request, effective: HardwarePreset) -> dict:
    settings = get_settings()
    default = settings.get_default_preset()
    available = list_presets(settings.data_path, platform=current_platform())
    compatible = _compatible_presets(default, settings.data_path, available)
    identity = get_zotero_identity(request)
    return {
        "default": default.name,
        "effective": effective.name,
        "fell_back": _fell_back_choice(settings, identity, effective),
        "selectable": [p.model_dump() for p in _selectable_presets(effective, compatible, settings, request)],
    }


@router.put("/config/my-preset")
def put_my_preset(update: MyPresetUpdate, request: Request) -> dict:
    """Choose the preset this caller runs on (``null`` clears the choice).

    Needs a signed-in identity (a loopback server with no identity always runs the
    default). The choice must be compatible with the server default and the caller must
    have usable credentials for it (their key sent in its header, or a shared one set).
    """
    settings = get_settings()
    identity = get_zotero_identity(request)
    if identity is None:
        raise HTTPException(status_code=400, detail="Choosing a preset needs a signed-in Zotero identity.")
    if update.preset_name is None:
        set_preferred_preset(settings.data_path, identity.user_id, None)
        return _my_preset_view(request, settings.get_default_preset())

    default = settings.get_default_preset()
    available = list_presets(settings.data_path, platform=current_platform())
    if update.preset_name not in available:
        raise HTTPException(status_code=400, detail=f"Unknown preset: {update.preset_name}.")
    if update.preset_name not in _compatible_presets(default, settings.data_path, available):
        raise HTTPException(
            status_code=400,
            detail=f"Preset '{update.preset_name}' is not compatible with the server default '{default.name}' "
                   "(both sides must be remote and use the same embedding model).",
        )
    try:
        chosen = get_preset(update.preset_name, settings.data_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    missing = _preset_credentials(chosen, settings, request, {})
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Preset '{update.preset_name}' lacks credentials: {sorted(missing)}.",
        )
    set_preferred_preset(settings.data_path, identity.user_id, update.preset_name)
    return _my_preset_view(request, chosen)


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
            A preset without credentials can be selected; it is not ready
            until they are set.
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

    current = settings.get_default_preset()
    compatible = _compatible_presets(current, settings.data_path, available)
    if update.preset_name not in compatible:
        raise HTTPException(
            status_code=400,
            detail=f"Preset '{update.preset_name}' cannot be switched to at runtime from "
                   f"'{current.name}' (requires the same remote embedding model). "
                   f"Compatible presets: {compatible}. To use a different embedding model or a "
                   f"local-model preset, set MODEL_PRESET and restart the backend instead.",
        )

    # Credentials are deliberately not required here: the preset's own sections ask for
    # the keys and endpoints, and its health shows "not ready" until they are set.
    set_default_preset(settings.data_path, update.preset_name)
    # Rate-limit headers and rate-limit skips belong to the previous provider.
    usage_recorder.reset()
    try:
        store = get_key_store()
        if store.enabled:
            await asyncio.to_thread(store.clear_rate_limits)
    except Exception as exc:
        logger.warning("Could not clear stored rate-limit skips after preset switch: %s", exc)
    return await asyncio.to_thread(get_config, request)


class EndpointHealth(BaseModel):
    """Readiness of one remote endpoint."""
    status: str  # "ready" | "cold" | "throttled" | "paused" | "unreachable" — see backend.providers.types.Health
    detail: str


class EndpointHealthResponse(BaseModel):
    """Per-side health; ``None`` means the active preset declares no health check."""
    embedding: Optional[EndpointHealth] = None
    llm: Optional[EndpointHealth] = None


def _side_kwargs(preset: HardwarePreset, side: str) -> dict:
    return (preset.embedding if side == "embedding" else preset.llm).model_kwargs


def _caller_key(request: Optional[Request], key_env: Optional[str]) -> Optional[str]:
    """The caller's own key for ``key_env``, from its request header."""
    if request is None or not key_env:
        return None
    return request.headers.get(env_var_to_header(key_env)) or None


def _check_side(preset: HardwarePreset, side: str, request: Optional[Request] = None) -> Optional[EndpointHealth]:
    """Health of one side from its provider; None when the provider has no health concept.

    Whose key is used follows the credential scope: ``user`` uses the caller's
    own key (from the request header), ``managed``/``shared`` the admin-set key
    in the shared store. The URL is the shared admin-set value, the preset's
    fixed ``base_url``, or (for a provider that derives it) the one found from
    the key.
    """
    try:
        provider = get_providers(preset)[side]
    except ProviderConfigError:
        return None
    kwargs = _side_kwargs(preset, side)
    url_env = kwargs.get("shared_base_url_env")
    key_env = kwargs.get("shared_api_key_env")
    api_key = resolve_shared_value(key_env) if key_env else _caller_key(request, kwargs.get("api_key_env"))
    base_url = resolve_shared_value(url_env) if url_env else kwargs.get("base_url")
    if not base_url and api_key and provider.derives_endpoint_url:
        base_url = endpoint_cache.resolve(provider, side, api_key)  # may be None: the provider decides what that means
    health = provider.health(Credentials(api_key=api_key, base_url=base_url))
    if health is None:
        return None
    return EndpointHealth(status=health.status, detail=health.detail)


@router.get("/config/health", response_model=EndpointHealthResponse)
def get_endpoint_health(request: Request) -> EndpointHealthResponse:
    """
    Readiness (ready / cold / throttled / unreachable) of the active
    preset's remote embedding and LLM endpoints, as reported by each side's
    provider, using the caller's own key for ``user``-scope sides. A side whose
    provider has no health concept is ``null``. Plain ``def``: the provider
    checks do blocking HTTP, so FastAPI runs this in a thread pool.
    """
    settings = get_settings()
    preset = settings.get_hardware_preset()
    return EndpointHealthResponse(
        embedding=_check_side(preset, "embedding", request),
        llm=_check_side(preset, "llm", request),
    )


async def _caller_may_operate(request: Request, provider: Provider) -> bool:
    """Whether the caller may provision / pause / resume this side (by credential scope).

    ``user``: anyone signed in (or any caller of a loopback server). ``managed``: an
    admin of the authorizing group (or loopback). ``shared``: nobody, there is
    nothing to operate.
    """
    if not (provider.supports_provisioning or provider.supports_suspend):
        return False
    if provider.scope == "shared":
        return False
    try:
        if provider.scope == "managed":
            await require_authorized_group_admin(request)
            return True
        if is_loopback(get_settings()):
            return True
        return get_zotero_identity(request) is not None
    except HTTPException:
        return False


@router.get("/config/providers")
async def get_provider_descriptors(request: Request) -> dict:
    """Per-side provider descriptors of the caller's effective preset, for the plugin's
    per-side configuration sections.

    ``{"preset": name, "sides": {"embedding"|"llm": {"model_type", "provider"}}}``;
    ``provider`` is ``null`` for a local side. A descriptor carries the provider's id and
    label, the effective credential scope, capability flags (provisioning, pause), the
    credential the one-time provisioning key must match, an ``unavailable_hint`` and
    ``operable_by_caller``. It never contains secrets and no vendor knowledge lives in
    the plugin: everything it needs to render is here.
    """
    preset = get_settings().get_hardware_preset()
    try:
        providers = get_providers(preset)
    except ProviderConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    sides: Dict[str, dict] = {}
    for side in ("embedding", "llm"):
        cfg = preset.embedding if side == "embedding" else preset.llm
        provider = providers[side]
        if cfg.model_type != "remote":
            sides[side] = {"model_type": cfg.model_type, "provider": None}
            continue
        descriptor = provider.describe()
        descriptor.operable_by_caller = await _caller_may_operate(request, provider)
        sides[side] = {"model_type": cfg.model_type, "provider": descriptor.model_dump()}
    return {"preset": preset.name, "sides": sides}


class ProvisionRequest(BaseModel):
    """Optional body for POST /api/config/provision."""
    #: One-time credential values by key name (for example ``{"RUNPOD_API_KEY": "..."}``).
    #: Used for this run only and never stored.
    keys: Dict[str, str] = {}
    #: Sides to run; default every side whose provider can provision.
    sides: Optional[List[str]] = None


def _credential_env(kwargs: dict) -> Optional[str]:
    """Name of the key a side authenticates with (shared admin-set key, else personal)."""
    return kwargs.get("shared_api_key_env") or kwargs.get("api_key_env")


def _side_credential(
    preset: HardwarePreset, side: str, supplied: Dict[str, str], request: Optional[Request] = None
) -> Tuple[Optional[str], Optional[str]]:
    """The credential for provisioning one side, and the env name it belongs to.

    The one-time value supplied with the request (checked against the side's
    declared key format) wins; it is used for this run only. Otherwise a
    ``user``-scope side uses the caller's own key from the request header and a
    ``managed`` one the stored admin key.

    Raises:
        HTTPException: 400 if the supplied value does not match the declared format.
    """
    kwargs = _side_kwargs(preset, side)
    env = _credential_env(kwargs)
    if not env:
        return None, None
    value = supplied.get(env)
    if value:
        pattern = kwargs.get("shared_api_key_pattern") or kwargs.get("api_key_pattern")
        if pattern and not re.fullmatch(pattern, value):
            raise HTTPException(
                status_code=400,
                detail=f"The key for {env} does not match the expected format (expected to match: {pattern})",
            )
        return value, env
    if kwargs.get("shared_api_key_env"):
        return resolve_shared_value(env), env
    return _caller_key(request, env), env


def _slot_for(providers: Dict[str, Provider], sides: List[str], identity: Optional[ZoteroIdentity]) -> str:
    """Job slot: one global slot when any side is admin-operated, else the caller's own."""
    if any(providers[s].scope != "user" for s in sides):
        return provisioning.GLOBAL_SLOT
    return provisioning.user_slot(identity.user_id if identity else None)


async def _authorize_provisioning(request: Request, providers: Dict[str, Provider], sides: List[str]) -> Optional[ZoteroIdentity]:
    """Gate by credential scope: any signed-in user for ``user`` sides, an admin for ``managed`` ones."""
    if any(providers[s].scope != "user" for s in sides):
        return await require_authorized_group_admin(request)
    identity = get_zotero_identity(request)
    if identity is None and not is_loopback(get_settings()):
        raise HTTPException(status_code=401, detail="Missing or invalid Zotero API key.")
    return identity


async def _start_job(request: Request, body: ProvisionRequest, capability: str) -> dict:
    """Start a background job for ``capability`` ("provision" or "suspend") on the requested sides.

    Shared by provisioning and pausing: the same scope gating (any signed-in user for ``user``
    sides on their own key and slot, an admin for ``managed`` ones), credential resolution and
    job slots. Returns the caller's job state.
    """
    flag = "supports_provisioning" if capability == "provision" else "supports_suspend"
    verb = "provisioned" if capability == "provision" else "paused"
    settings = get_settings()
    preset = settings.get_hardware_preset()
    try:
        providers = get_providers(preset)
    except ProviderConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if body.sides is None:
        sides = [s for s in ("embedding", "llm") if getattr(providers[s], flag)]
    else:
        unknown = [s for s in body.sides if s not in providers]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown side(s): {', '.join(unknown)}")
        sides = [s for s in ("embedding", "llm") if s in body.sides]
        unsupported = [s for s in sides if not getattr(providers[s], flag)]
        if unsupported:
            raise HTTPException(
                status_code=400,
                detail=f"The {', '.join(unsupported)} side of preset '{preset.name}' cannot be {verb}.",
            )
    if not sides:
        raise HTTPException(status_code=400, detail=f"Preset '{preset.name}' has no side that can be {verb}.")

    identity = await _authorize_provisioning(request, providers, sides)
    slot = _slot_for(providers, sides, identity)

    jobs = []
    for side in sides:
        credential, env = _side_credential(preset, side, body.keys, request)
        if not credential:
            raise HTTPException(
                status_code=400,
                detail=f"No API key is available for the {side} side"
                       + (f" ({env}); enter your key in Preferences or one for this run." if env else "."),
            )
        ctx = ProvisionContext(side=side, preset=preset, credential=credential, data_path=settings.data_path)
        jobs.append(provisioning.SideJob(side=side, provider=providers[side], ctx=ctx))

    if provisioning.is_running(slot):
        raise HTTPException(status_code=409, detail="A provisioning job is already running.")
    provisioning.mark_running(sides, slot)

    action = None
    if capability == "suspend":
        def action(job, progress):  # noqa: F811 - pausing returns nothing to store
            job.provider.suspend(job.ctx, progress)
            return {}

    task = asyncio.create_task(provisioning.run_job(jobs, data_path=settings.data_path, slot=slot, action=action))
    _provision_tasks.add(task)  # keep a strong reference until done
    task.add_done_callback(_provision_tasks.discard)
    return provisioning.get_job_state(slot)


@router.post("/config/provision", status_code=202)
async def start_provisioning(
    request: Request,
    body: Optional[ProvisionRequest] = Body(default=None),
):
    """
    Provision (create, wake or resume) the active preset's remote endpoints as a
    background job. Each requested side's provider does the work; whatever
    URL it reports is applied via the shared remote-config store as soon as
    that side succeeds. Poll GET /api/config/provision/status.

    Who may run it follows the side's credential scope: any signed-in user for
    ``user`` sides (it runs on the caller's own key, in the caller's own job
    slot), an admin for ``managed`` ones (the admin's stored key, one global
    slot).

    Provisioning (creating/updating endpoints) may need a broader key than
    day-to-day inference, which can use one restricted to the endpoints. A key
    in ``keys`` is used for this run only. ``sides`` runs only those sides (for
    example to retry a failed one).

    Raises:
        HTTPException: 400 if no requested side can be provisioned, a side
            has no key available, or a supplied key has the wrong format;
            401 without a signed-in caller, 403 for a non-admin on a managed
            side; 409 if this caller's job is already running.
    """
    return await _start_job(request, body or ProvisionRequest(), "provision")


@router.post("/config/suspend", status_code=202)
async def start_suspend(
    request: Request,
    body: Optional[ProvisionRequest] = Body(default=None),
):
    """
    Pause the active preset's remote endpoints (stops their billing and their wake-ups; the
    URLs are kept) as a background job on the same slots and with the same gating as
    provisioning. ``sides`` pauses only those sides. Resuming is POST /api/config/provision.
    Poll GET /api/config/provision/status.

    Raises:
        HTTPException: 400 if no requested side supports pausing or has a key; 401/403 as for
            provisioning; 409 if this caller's job is already running.
    """
    return await _start_job(request, body or ProvisionRequest(), "suspend")


_provision_tasks: set = set()


@router.get("/config/provision/status")
def get_provisioning_status(request: Request) -> dict:
    """The caller's provisioning job state (the global one for admin-operated presets):
    status idle|running|succeeded|failed, per-side results and progress lines."""
    preset = get_settings().get_hardware_preset()
    try:
        providers = get_providers(preset)
    except ProviderConfigError:
        return provisioning.get_job_state()
    sides = [s for s in providers if providers[s].supports_provisioning]
    return provisioning.get_job_state(_slot_for(providers, sides, get_zotero_identity(request)))


def _shared_field_patterns(settings) -> Dict[str, Optional[str]]:
    """Admin-set (shared) fields declared by any available preset, with their format patterns.

    An admin sets these for the server, not for their own choice of preset, so
    every preset a user could run counts, not just the default.
    """
    patterns: Dict[str, Optional[str]] = {}
    for name in list_presets(settings.data_path, platform=current_platform()):
        try:
            preset = get_preset(name, settings.data_path)
        except ValueError:
            continue
        fields = RemoteEmbeddingService.required_client_fields(preset.embedding)
        fields += RemoteLLMService.required_client_fields_for_config(preset.llm)
        for key_info in fields:
            if key_info["kind"] in ("shared_base_url", "shared_api_key"):
                patterns[key_info["key_name"]] = key_info["pattern"]
    return patterns


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
            active preset's embedding/LLM config, or if a value doesn't
            match that key's declared `pattern` (see
            RemoteEmbeddingService.required_client_fields) — e.g. a RunPod
            base URL that isn't shaped like
            `https://api.runpod.ai/v2/<id>/openai/v1`. Caught here rather
            than left to surface as a confusing connection error on the
            next real query.
    """
    settings = get_settings()
    patterns = _shared_field_patterns(settings)
    allowed_keys = set(patterns.keys())

    unknown = set(update.values.keys()) - allowed_keys
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown remote-config key(s): {sorted(unknown)}. "
                   f"Allowed: {sorted(allowed_keys)}",
        )

    invalid = []
    for key_name, value in update.values.items():
        pattern = patterns.get(key_name)
        if pattern and not re.fullmatch(pattern, value):
            invalid.append(f"{key_name} (expected to match: {pattern})")
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Value does not match the expected format for: {'; '.join(invalid)}",
        )

    try:
        merged = update_remote_config(update.values)
    except SecretsUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
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

    A side whose provider can provision gets its base URL from
    POST /api/config/provision, so its `shared_base_url` entry is omitted.
    """
    settings = get_settings()
    preset = settings.get_hardware_preset()
    try:
        providers = get_providers(preset)
    except ProviderConfigError:
        providers = {}

    seen: Dict[str, ApiKeyRequirement] = {}

    def _merge(key_info: dict, side: str) -> None:
        provisionable = side in providers and providers[side].supports_provisioning
        if provisionable and key_info["kind"] == "shared_base_url":
            return
        key_name = key_info["key_name"]
        if key_name in seen:
            seen[key_name].required_for = list(
                set(seen[key_name].required_for) | set(key_info["required_for"])
            )
            if side not in seen[key_name].sides:
                seen[key_name].sides.append(side)
            return
        is_set = None
        if key_info["kind"] in ("shared_base_url", "shared_api_key"):
            is_set = bool(get_remote_config_value(key_name))
        seen[key_name] = ApiKeyRequirement(**key_info, is_set=is_set, sides=[side])

    for key_info in RemoteEmbeddingService.required_client_fields(preset.embedding):
        _merge(key_info, "embedding")
    for key_info in RemoteLLMService.required_client_fields(settings):
        _merge(key_info, "llm")

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

    Fetches the live model list from the LLM provider (``has_live_models``),
    filters to RAG-suitable models, and returns them ordered by demand
    (most available first).  Returns an empty list if the provider has no
    live model list or if the upstream call fails.

    The ``status`` field contains a human-readable availability label:
    ``"available"`` (demand 0), ``"busy"`` (1–5), or ``"very busy"`` (6+).
    """
    settings = get_settings()
    preset = settings.get_hardware_preset()

    live_models = _live_llm_models(request, preset, require_key=False)
    if not live_models:
        return ModelsStatusResponse(models=[])

    return ModelsStatusResponse(models=[
        ModelStatus(model=m.id, demand=m.demand or 0, status=m.availability or "available")
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
