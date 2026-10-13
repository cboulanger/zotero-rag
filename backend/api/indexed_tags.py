"""Indexed-status tag endpoints.

GET  /api/indexed-tags/events          — new attachment indexed/un-indexed events (real-time path)
POST /api/indexed-tags/check           — fresh "is it indexed?" lookup before the plugin removes a tag
POST /api/indexed-tags/refresh         — spawn bin/sync_indexed_tags.py for the caller's key
GET  /api/indexed-tags/refresh/{id}    — tail that run's JSON-lines output by byte offset

The plugin polls ``events`` with its last-seen ``seq`` and applies the tag changes to
its local library; ``refresh`` reconciles everything the caller's stored auto-index key
can access. Both converge on VectorStore.get_indexed_attachment_keys. See
docs/indexed-status-tags.md.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api.document_upload import _update_item_cache
from backend.api.autoindex import _find_own_entry, _store
from backend.api.public_query import slug_to_backend_id
from backend.config.settings import get_settings
from backend.services import pending_upload_cache
from backend.dependencies import get_vector_store, get_zotero_identity
from backend.db.vector_store import VectorStore
from backend.services.autoindex_key_store import fingerprint
from backend.services.failed_attachments import get_failed_store
from backend.services.index_event_log import FAILED_TAG_NAME, INDEXED_TAG_NAME, IndexEventLog
from backend.services.indexed_tag_runs import is_valid_run_id, read_run, trigger_tag_sync
from backend.services.zotero_identity import ZoteroIdentity

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/indexed-tags/events", summary="Poll attachment indexed/un-indexed events")
async def get_events(
    since: Optional[int] = None,
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
) -> dict:
    """Return events newer than ``since``, limited to libraries the caller can read.

    Omit ``since`` on first contact to get just the log head (``last_seq``) and
    start following from "now" — the Refresh action covers history. ``gap`` is
    true when events were rotated out before the caller read them.
    """
    log = IndexEventLog(get_settings().index_events_path)
    if since is None:
        head = await asyncio.to_thread(log.last_seq)
        return {"tag": INDEXED_TAG_NAME, "failed_tag": FAILED_TAG_NAME, "events": [], "last_seq": head, "gap": False}
    result = await asyncio.to_thread(log.read_since, since)
    if identity is not None:
        visible = {slug_to_backend_id(t) for t in identity.targets}
        result["events"] = [e for e in result["events"] if e.get("library_id") in visible]
    return {"tag": INDEXED_TAG_NAME, "failed_tag": FAILED_TAG_NAME, **result}


class CheckRequest(BaseModel):
    library_id: str
    attachment_keys: list[str] = Field(..., max_length=500)


@router.post("/indexed-tags/check", summary="Which of these attachments are indexed right now?")
async def check_indexed(
    body: CheckRequest,
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
) -> dict:
    """Fresh ground-truth lookup used by the plugin just before it removes a tag,
    so a stale refresh operation or a transient re-index (chunks deleted, not yet
    re-added) cannot strip the tag from an attachment that is indexed again."""
    if identity is not None and body.library_id not in {slug_to_backend_id(t) for t in identity.targets}:
        raise HTTPException(status_code=403, detail="No access to this library.")
    indexed = await asyncio.to_thread(
        vector_store.get_indexed_attachment_keys, body.library_id, body.attachment_keys
    )
    return {"indexed": sorted(indexed)}


@router.get("/indexed-tags/failed", summary="List quarantined/refused attachments for a library, with reasons")
async def list_failed(
    library_id: str,
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
) -> dict:
    """Full ``rag-failed`` records for ``library_id`` (attachment_key, item_key,
    reason, detail, failed_at) — used by the Fix Unavailable Attachments dialog's
    "Include permanent failures" option, which needs the reason/detail text, not
    just the indexed/not-indexed boolean ``check`` gives."""
    if identity is not None and library_id not in {slug_to_backend_id(t) for t in identity.targets}:
        raise HTTPException(status_code=403, detail="No access to this library.")
    store = get_failed_store()
    items = await asyncio.to_thread(store.list_failed, library_id)
    return {"items": items}


@router.post("/indexed-tags/failed/clear", summary="Retry attachments whose rag-failed tag the user removed")
async def clear_failed(
    body: CheckRequest,
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
) -> dict:
    """Forget the failure record for each attachment so it is processed again.

    Called by the plugin when the user removes the ``rag-failed`` tag. Also
    releases a quarantined deferred upload (resetting its attempts) and drops the
    item from the check-indexed cache so the next scan sees it as not indexed.
    Idempotent: attachments without a record are ignored.
    """
    if identity is not None and body.library_id not in {slug_to_backend_id(t) for t in identity.targets}:
        raise HTTPException(status_code=403, detail="No access to this library.")
    settings = get_settings()
    store = get_failed_store()

    def _clear() -> list[str]:
        cleared = []
        for key in body.attachment_keys:
            record = store.clear(body.library_id, key)
            if record is None:
                continue
            pending_upload_cache.release_entry(settings.data_path, body.library_id, key)
            if record.get("item_key"):
                _update_item_cache(body.library_id, {record["item_key"]: None})
            cleared.append(key)
        return cleared

    return {"cleared": await asyncio.to_thread(_clear)}


@router.post("/indexed-tags/refresh", summary="Reconcile indexed-status tags across all accessible libraries")
async def start_refresh(request: Request) -> dict:
    """Spawn the tag-sync script for the caller's stored auto-index key.

    Uses the key already registered for automatic indexing (looked up by
    fingerprint inside the script, never passed on a command line). If the
    caller already has a run in progress, returns that run instead of starting
    a second one.
    """
    store = _store()
    api_key = request.headers.get("X-Zotero-API-Key")
    if not api_key:
        raise HTTPException(status_code=400, detail="Missing X-Zotero-API-Key header.")
    fp = fingerprint(api_key)
    if await asyncio.to_thread(_find_own_entry, store, fp) is None:
        raise HTTPException(
            status_code=400,
            detail="Enable automatic indexing in Preferences first; the tag refresh reuses that key.",
        )
    run_id, already_running = await trigger_tag_sync(get_settings(), fp)
    return {"run_id": run_id, "already_running": already_running, "tag": INDEXED_TAG_NAME, "failed_tag": FAILED_TAG_NAME}


@router.get("/indexed-tags/refresh/{run_id}", summary="Read new output of a tag-sync run")
async def get_refresh(request: Request, run_id: str, offset: int = 0) -> dict:
    api_key = request.headers.get("X-Zotero-API-Key")
    if not api_key or not is_valid_run_id(run_id) or offset < 0:
        raise HTTPException(status_code=400, detail="Invalid request.")
    result = await asyncio.to_thread(read_run, get_settings(), fingerprint(api_key), run_id, offset)
    if result is None:
        raise HTTPException(status_code=404, detail="Unknown tag-sync run.")
    return result
