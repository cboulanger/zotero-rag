"""Auto-index key endpoints.

POST   /api/autoindex/keys                — submit a read-only key (validated, stored encrypted)
DELETE /api/autoindex/keys                — remove a key
GET    /api/autoindex/keys                — list the caller's own key metadata (no plaintext)
GET    /api/autoindex/status              — live cron-run progress; admins may pass ?scope=all
POST   /api/autoindex/run                 — on-demand run scoped to the caller's own libraries
POST   /api/autoindex/scheduler/pause     — pause the built-in scheduler (admin only)
POST   /api/autoindex/scheduler/resume    — resume the built-in scheduler (admin only)
POST   /api/autoindex/scheduler/run-now   — immediate unscoped run of every library (admin only)
POST   /api/autoindex/scheduler/skip-slug — cooperatively skip one job in the active run (admin only)
POST   /api/autoindex/abort               — kill the entire running indexing process (admin only)

All endpoints are protected by the global Zotero-key auth middleware (X-Zotero-API-Key). When
AUTOINDEX_SECRET is unset the feature is disabled and the key endpoints return 503. The
scheduler/abort/skip-slug endpoints additionally require the caller to be an owner/admin of
AUTHORIZED_GROUP_ID — see backend.dependencies.require_authorized_group_admin.
"""

import asyncio
import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.api.public_query import slug_to_backend_id
from backend.config.settings import get_settings
from backend.dependencies import get_zotero_identity, require_authorized_group_admin
from backend.services.autoindex_key_store import AutoIndexKeyStore, fingerprint
from backend.services.autoindex_resolver import is_embedding_key_usable
from backend.services.autoindex_scheduler import trigger_index_run, write_scheduler_state
from backend.services.cron_indexer import abort_process, read_live_status, write_control_state
from backend.services.embedding_key_validator import validate_embedding_key
from backend.services.registration_service import RegistrationService
from backend.services.zotero_identity import ZoteroIdentity
from backend.zotero.group_roles import get_admin_role_cache
from backend.zotero.key_validator import validate_key

router = APIRouter()
logger = logging.getLogger(__name__)


class KeyRequest(BaseModel):
    api_key: str
    embedding_api_key: Optional[str] = None


class SkipSlugRequest(BaseModel):
    slug: str


def _store() -> AutoIndexKeyStore:
    settings = get_settings()
    store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
    if not store.enabled:
        raise HTTPException(
            status_code=503,
            detail="Auto-indexing is not configured on this server (AUTOINDEX_SECRET unset).",
        )
    return store


@router.post("/autoindex/keys", summary="Submit a read-only auto-index key")
async def add_key(request: KeyRequest) -> dict:
    store = _store()
    validation = await validate_key(request.api_key)
    if not validation.read_only:
        raise HTTPException(status_code=400, detail=validation.reason or "Key is not read-only.")
    fp = await asyncio.to_thread(store.add, request.api_key, validation)

    response: dict = {
        "user_id": validation.user_id,
        "username": validation.username,
        "targets": validation.targets,
    }

    if request.embedding_api_key:
        settings = get_settings()
        preset = settings.get_hardware_preset()
        emb_validation = await validate_embedding_key(request.embedding_api_key, preset.embedding)
        if emb_validation.status == "invalid":
            response["embedding_key_status"] = "invalid"
            response["embedding_key_error"] = emb_validation.reason
        else:
            await asyncio.to_thread(
                store.set_embedding_key, fp, request.embedding_api_key,
                emb_validation.key_name, emb_validation.status,
            )
            response["embedding_key_status"] = emb_validation.status

    return response


@router.delete("/autoindex/keys", summary="Remove an auto-index key")
def delete_key(request: KeyRequest) -> dict:
    store = _store()
    removed = store.remove_by_key(request.api_key)
    return {"removed": removed}


@router.get("/autoindex/keys", summary="List the caller's own auto-index key metadata")
def list_keys(identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity)) -> dict:
    """List auto-index key metadata.

    On loopback deployments (identity=None) returns every submitted key's
    metadata unfiltered, matching the single-trusted-local-user model used
    throughout this backend's Zotero-key auth. On a gated remote deployment,
    each caller sees only their own submitted key's metadata, not other
    users' — this endpoint previously leaked every user's real username and
    user_id to any caller who merely passed the instance-wide access gate.
    """
    store = _store()
    all_keys = store.list_metadata()
    if identity is None:
        return {"keys": all_keys}
    return {"keys": [k for k in all_keys if k.get("user_id") == identity.user_id]}


@router.get("/autoindex/status", summary="Live auto-index cron-run progress")
async def status(
    request: Request,
    scope: Literal["own", "all"] = "own",
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
) -> dict:
    """Return the live status of the auto-index cron run.

    Unlike the ``/`` root endpoint (which exposes only ``enabled``), this
    authenticated endpoint surfaces the number of registered keys and the last
    run's full progress: whether a run is currently ``running`` (or ``crashed``),
    per-slug counts, timestamps and any ``key_issues`` recorded during the run.
    When the feature is disabled (``AUTOINDEX_SECRET`` unset) ``keys_registered``
    is ``0`` and ``disabled_reason`` explains why. Run-specific fields are absent
    until the first cron run writes a status file.

    On loopback deployments (identity=None) returns full detail unfiltered,
    matching the single-trusted-local-user model used throughout this
    backend's Zotero-key auth. On a gated remote deployment, ``slugs`` is
    filtered to the caller's own readable targets and ``key_issues`` to
    entries matching the caller's own username — this endpoint previously
    leaked every user's real username and every library's slug/stats to any
    caller who merely passed the instance-wide access gate.

    Also reports ``is_admin``: True on loopback, True/False (via the cached
    Zotero group-admin check) when AUTHORIZED_GROUP_ID is configured, False
    when it isn't — the plugin uses this to decide whether to show admin
    controls, without a separate round trip.

    Admins (see require_authorized_group_admin) may pass ?scope=all to see
    every job in the run, not just their own, with each job labeled with its
    library name and owner id (joined from registrations.json). Non-admins
    passing scope=all get a 403.
    """
    settings = get_settings()
    result: dict = {}
    try:
        store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
        result["enabled"] = store.enabled
        if store.enabled:
            result["keys_registered"] = len(await asyncio.to_thread(store.list_metadata))
        else:
            result["keys_registered"] = 0
            result["disabled_reason"] = "AUTOINDEX_SECRET is not set"
    except Exception as exc:
        logger.warning("Failed to read auto-index key store: %s", exc)
        result["enabled"] = False
        result["keys_registered"] = 0
        result["disabled_reason"] = f"key store error: {exc}"

    try:
        result.update(await asyncio.to_thread(read_live_status, settings.data_path))
    except Exception as exc:
        logger.warning("Failed to read cron status file: %s", exc)

    if identity is not None:
        if settings.authorized_group_id:
            api_key = request.headers.get("X-Zotero-API-Key", "")
            try:
                result["is_admin"] = await get_admin_role_cache().is_admin(
                    identity.user_id, settings.authorized_group_id, api_key,
                )
            except Exception as exc:
                logger.warning("Failed to check admin role: %s", exc)
                result["is_admin"] = False
        else:
            result["is_admin"] = False
    else:
        result["is_admin"] = True  # loopback: same trust-boundary bypass as require_authorized_group_admin

    if scope == "all":
        if not result["is_admin"]:
            raise HTTPException(status_code=403, detail="This Zotero account is not an admin of the authorizing group.")
        if "slugs" in result:
            registrations = await asyncio.to_thread(RegistrationService(settings.registrations_path).get_all)
            for slug, info in result["slugs"].items():
                library_name, owner_id = _job_label(slug, registrations)
                info["library_name"] = library_name
                info["owner_id"] = owner_id
    elif identity is not None:
        if "slugs" in result:
            result["slugs"] = {
                slug: info for slug, info in result["slugs"].items()
                if slug in identity.targets
            }
        if "key_issues" in result:
            result["key_issues"] = [
                issue for issue in result["key_issues"]
                if issue.get("user") == identity.username
            ]

    return result


def _job_label(slug: str, registrations: dict) -> tuple[str, Optional[int]]:
    """Join a slug to its human-readable library name and owner id via registrations.json.

    Falls back to the raw slug / owner_id=None when there's no matching
    registration (e.g. a library registered for auto-indexing but never
    separately registered for RAG querying) — a real, expected case, not
    an error. registrations.json entries carry a `users` list, not a single
    owner; users[0] (first-registered) is used as a pragmatic stand-in —
    exact for personal libraries (users/{id}, which have exactly one
    registered user by construction), an arbitrary but deterministic
    tie-break for shared group libraries.
    """
    try:
        backend_id = slug_to_backend_id(slug)
    except ValueError:
        return slug, None
    entry = registrations.get(backend_id)
    if not entry:
        return slug, None
    users = entry.get("users") or []
    owner_id = users[0]["user_id"] if users else None
    return entry.get("library_name", slug), owner_id


@router.post("/autoindex/run", summary="Trigger an on-demand indexing run for the caller's own libraries")
async def run_now(request: Request) -> dict:
    """Start a server-side indexing run scoped to the caller's own libraries.

    Spawns bin/index_libraries.py --fingerprint <fp> as a detached subprocess —
    the same script the hourly cron runs — so the caller's own registered
    entry is indexed without waiting for the next cron tick. Refuses to start
    if the caller isn't registered, is missing a usable embedding key (when
    the configured preset requires one), or a run is already in progress.
    """
    store = _store()
    api_key = request.headers.get("X-Zotero-API-Key")
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing X-Zotero-API-Key header.")
    fp = fingerprint(api_key)

    own = await asyncio.to_thread(_find_own_entry, store, fp)
    if own is None:
        raise HTTPException(
            status_code=400,
            detail="You have not registered for automatic indexing yet. Set it up in Preferences first.",
        )

    settings = get_settings()
    if settings.get_hardware_preset().embedding.model_type == "remote":
        reason = _embedding_key_block_reason(own)
        if reason:
            raise HTTPException(status_code=400, detail=reason)

    result = await trigger_index_run(settings, fingerprint=fp)
    if result == "already_running":
        raise HTTPException(status_code=409, detail="Indexing is already running on the server.")
    return {"started": True}


@router.post("/autoindex/scheduler/pause", summary="Pause the built-in scheduler (admin only)")
async def pause_scheduler(identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin)) -> dict:
    settings = get_settings()
    await asyncio.to_thread(write_scheduler_state, settings.data_path, {"paused": True})
    return {"paused": True}


@router.post("/autoindex/scheduler/resume", summary="Resume the built-in scheduler (admin only)")
async def resume_scheduler(identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin)) -> dict:
    settings = get_settings()
    await asyncio.to_thread(write_scheduler_state, settings.data_path, {"paused": False})
    return {"paused": False}


@router.post(
    "/autoindex/scheduler/run-now",
    summary="Trigger an immediate full indexing run for every registered library (admin only)",
)
async def run_now_admin(identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin)) -> dict:
    settings = get_settings()
    result = await trigger_index_run(settings)
    if result == "already_running":
        raise HTTPException(status_code=409, detail="Indexing is already running on the server.")
    if result == "disabled":
        raise HTTPException(
            status_code=503,
            detail="Auto-indexing is not configured on this server (AUTOINDEX_SECRET unset).",
        )
    return {"started": True}


@router.post("/autoindex/abort", summary="Abort the entire running indexing process (admin only)")
async def abort_run(identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin)) -> dict:
    settings = get_settings()
    live_status = await asyncio.to_thread(read_live_status, settings.data_path)
    if not live_status.get("running"):
        raise HTTPException(status_code=409, detail="No indexing run is currently active.")
    pid = live_status.get("pid")
    if pid is None:
        raise HTTPException(status_code=500, detail="Indexing is reported as running but no PID was recorded.")
    aborted = await asyncio.to_thread(abort_process, pid)
    return {"aborted": aborted, "pid": pid}


@router.post(
    "/autoindex/scheduler/skip-slug",
    summary="Cooperatively skip one job in the active run without killing the process (admin only)",
)
async def skip_slug(
    body: SkipSlugRequest,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
) -> dict:
    settings = get_settings()
    live_status = await asyncio.to_thread(read_live_status, settings.data_path)
    if not live_status.get("running"):
        raise HTTPException(status_code=409, detail="No indexing run is currently active.")
    slug_state = live_status.get("slugs", {}).get(body.slug)
    if slug_state is None or slug_state.get("status") not in ("pending", "indexing"):
        raise HTTPException(
            status_code=404,
            detail=f"{body.slug!r} is not a pending or in-progress job in the active run.",
        )
    from datetime import datetime, timezone
    await asyncio.to_thread(
        write_control_state, settings.data_path,
        {"skip_slug": body.slug, "requested_at": datetime.now(timezone.utc).isoformat()},
    )
    return {"skip_requested": True, "slug": body.slug}


def _find_own_entry(store: AutoIndexKeyStore, fp: str) -> Optional[dict]:
    return next((k for k in store.list_metadata() if k["fingerprint"] == fp), None)


def _embedding_key_block_reason(own: dict) -> Optional[str]:
    status = own.get("embedding_key_status")
    rate_limit_until = own.get("embedding_key_rate_limit_until")
    if is_embedding_key_usable(status, rate_limit_until):
        return None
    if not own.get("has_embedding_key"):
        return "No embedding API key configured; set one up in Preferences before running indexing."
    if status == "invalid":
        return "Embedding API key was rejected; update it in Preferences."
    if status == "rate_limited":
        return f"Embedding API key is rate-limited until {rate_limit_until}; try again later."
    return f"Embedding API key has unrecognized status {status!r}."
