"""Auto-index key endpoints.

POST   /api/autoindex/keys    — submit a read-only key (validated, stored encrypted)
DELETE /api/autoindex/keys    — remove a key
GET    /api/autoindex/keys    — list the caller's own key metadata (no plaintext)
GET    /api/autoindex/status  — live cron-run progress (running/crashed, counts)

All endpoints are protected by the global X-API-Key middleware. When
AUTOINDEX_SECRET is unset the feature is disabled and the key endpoints return 503.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.config.settings import get_settings
from backend.dependencies import get_zotero_identity
from backend.services.autoindex_key_store import AutoIndexKeyStore
from backend.services.cron_indexer import read_live_status
from backend.services.zotero_identity import ZoteroIdentity
from backend.zotero.key_validator import validate_key

router = APIRouter()
logger = logging.getLogger(__name__)


class KeyRequest(BaseModel):
    api_key: str


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
    await asyncio.to_thread(store.add, request.api_key, validation)
    return {
        "user_id": validation.user_id,
        "username": validation.username,
        "targets": validation.targets,
    }


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
def status(identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity)) -> dict:
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
    """
    settings = get_settings()
    result: dict = {}
    try:
        store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
        result["enabled"] = store.enabled
        if store.enabled:
            result["keys_registered"] = len(store.list_metadata())
        else:
            result["keys_registered"] = 0
            result["disabled_reason"] = "AUTOINDEX_SECRET is not set"
    except Exception as exc:
        logger.warning("Failed to read auto-index key store: %s", exc)
        result["enabled"] = False
        result["keys_registered"] = 0
        result["disabled_reason"] = f"key store error: {exc}"

    try:
        result.update(read_live_status(settings.data_path))
    except Exception as exc:
        logger.warning("Failed to read cron status file: %s", exc)

    if identity is not None:
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
