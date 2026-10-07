"""Global admin-controlled runtime settings.

GET  /api/admin/settings                  — any authenticated identity (or loopback)
PUT  /api/admin/settings                  — admin only; body {"index_snapshots": bool}
POST /api/admin/settings/purge-snapshots  — admin only; no body

"Admin" here is the same concept used by the autoindex scheduler's
pause/resume/run-now controls: owner/admin of the server's
AUTHORIZED_GROUP_ID — see backend.dependencies.require_authorized_group_admin.

index_snapshots (default False) controls whether Zotero webpage-snapshot
attachments (title exactly "Snapshot") are indexed at all — see
backend.services.document_processor's _is_indexable_attachment (a later
task) and plugin/src/zotero-rag.js's getIndexSnapshotsEnabled() (a later
task). purge-snapshots deletes every already-indexed chunk whose source
attachment was a Snapshot, across every library on the server — see
VectorStore.delete_snapshot_chunks.
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.config.settings import get_settings
from backend.db.vector_store import VectorStore
from backend.dependencies import get_vector_store, require_authorized_group_admin
from backend.services.admin_settings_store import read_admin_settings, write_admin_settings
from backend.services.zotero_identity import ZoteroIdentity

router = APIRouter()


class AdminSettings(BaseModel):
    index_snapshots: bool = False


class PurgeSnapshotsResponse(BaseModel):
    deleted_chunks: int
    deleted_attachments: int


@router.get("/admin/settings", summary="Read global admin-controlled settings", response_model=AdminSettings)
async def get_admin_settings() -> AdminSettings:
    settings = get_settings()
    state = await asyncio.to_thread(read_admin_settings, settings.data_path)
    return AdminSettings(index_snapshots=state.get("index_snapshots", False))


@router.put("/admin/settings", summary="Update global admin-controlled settings (admin only)", response_model=AdminSettings)
async def put_admin_settings(
    body: AdminSettings,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
) -> AdminSettings:
    settings = get_settings()
    await asyncio.to_thread(write_admin_settings, settings.data_path, body.model_dump())
    return body


@router.post(
    "/admin/settings/purge-snapshots",
    summary="Delete every already-indexed Snapshot-attachment chunk, across all libraries (admin only)",
    response_model=PurgeSnapshotsResponse,
)
async def purge_snapshots(
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
    vector_store: VectorStore = Depends(get_vector_store),
) -> PurgeSnapshotsResponse:
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")
    deleted_chunks, deleted_attachments = await asyncio.to_thread(vector_store.delete_snapshot_chunks)
    return PurgeSnapshotsResponse(deleted_chunks=deleted_chunks, deleted_attachments=deleted_attachments)
