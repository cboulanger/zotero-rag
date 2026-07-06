"""
Library API endpoints.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional


from backend.models.library import LibraryIndexMetadata
from backend.db.vector_store import VectorStore
from backend.dependencies import get_vector_store, get_zotero_identity
from backend.config.settings import get_settings
from backend.services.access_gate import assert_can_access
from backend.services.registration_service import RegistrationService
from backend.services.zotero_identity import ZoteroIdentity

router = APIRouter()
logger = logging.getLogger(__name__)


class RegisteredUser(BaseModel):
    """A user who has registered a library."""
    user_id: int
    username: str
    registered_at: str


class LibraryDetailResponse(BaseModel):
    """Combined library status: index metadata + registrations."""
    library_id: str
    library_name: str
    library_type: str
    # Index state (None if never indexed)
    last_indexed_at: Optional[str] = None
    last_indexed_version: Optional[int] = None
    total_items_indexed: int = 0
    total_chunks: int = 0
    indexing_mode: Optional[str] = None
    # Registration
    registered_at: Optional[str] = None
    users: list[RegisteredUser] = []


def _build_detail(
    library_id: str,
    metadata: Optional[LibraryIndexMetadata],
    reg_entry: Optional[dict],
) -> LibraryDetailResponse:
    users: list[RegisteredUser] = []
    registered_at: Optional[str] = None
    library_name = metadata.library_name if metadata else library_id
    library_type = metadata.library_type if metadata else "user"

    if reg_entry:
        registered_at = reg_entry.get("registered_at")
        library_name = reg_entry.get("library_name", library_name)
        users = [
            RegisteredUser(
                user_id=u["user_id"],
                username=u["username"],
                registered_at=u["registered_at"],
            )
            for u in reg_entry.get("users", [])
        ]

    return LibraryDetailResponse(
        library_id=library_id,
        library_name=library_name,
        library_type=library_type,
        last_indexed_at=metadata.last_indexed_at if metadata else None,
        last_indexed_version=metadata.last_indexed_version if metadata else None,
        total_items_indexed=metadata.total_items_indexed if metadata else 0,
        total_chunks=metadata.total_chunks if metadata else 0,
        indexing_mode=metadata.indexing_mode if metadata else None,
        registered_at=registered_at,
        users=users,
    )


@router.get("/libraries", response_model=list[LibraryDetailResponse])
def list_libraries(
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    List libraries known to the backend (indexed or registered), filtered to
    the caller's own readable targets when authenticated via a Zotero key.
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    settings = get_settings()
    reg_service = RegistrationService(settings.registrations_path)
    registrations = reg_service.get_all()

    all_metadata = vector_store.get_all_library_metadata()
    metadata_by_id = {m.library_id: m for m in all_metadata}

    # Union of all known library IDs (indexed + registered)
    all_ids = set(metadata_by_id.keys()) | set(registrations.keys())
    if identity is not None:
        all_ids &= set(identity.targets)

    return [
        _build_detail(lid, metadata_by_id.get(lid), registrations.get(lid))
        for lid in sorted(all_ids)
    ]


@router.get("/libraries/{library_id}/status", response_model=LibraryDetailResponse)
def get_library_status(library_id: str, vector_store: VectorStore = Depends(get_vector_store)):
    """
    Get combined status for a single library: index metadata and registrations.
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    settings = get_settings()
    reg_service = RegistrationService(settings.registrations_path)
    registrations = reg_service.get_all()

    metadata = vector_store.get_library_metadata(library_id)
    reg = registrations.get(library_id)

    if metadata is None and reg is None:
        raise HTTPException(status_code=404, detail=f"Library {library_id} not found")

    return _build_detail(library_id, metadata, reg)


@router.get("/libraries/{library_id}/index-status", response_model=LibraryIndexMetadata)
def get_library_index_status(library_id: str, vector_store: VectorStore = Depends(get_vector_store)):
    """
    Get detailed indexing metadata for a library (used by the plugin to track sync state).

    Raises:
        HTTPException: 404 if library has never been indexed, 503 if store unavailable.
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    try:
        metadata = vector_store.get_library_metadata(library_id)
        if metadata is None:
            raise HTTPException(
                status_code=404,
                detail=f"Library {library_id} has not been indexed yet",
            )
        return metadata
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve library index status: {str(e)}")


@router.delete("/libraries/{library_id:path}/index")
def clear_library_index(
    library_id: str,
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    Remove all indexed data for a library (chunks, dedup records, metadata).
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")
    assert_can_access(identity, library_id)

    try:
        chunks_deleted = vector_store.delete_library_chunks(library_id)
        dedup_deleted = vector_store.delete_library_deduplication_records(library_id)
        metadata_deleted = vector_store.delete_library_metadata(library_id)

        return {
            "library_id": library_id,
            "chunks_deleted": chunks_deleted,
            "dedup_deleted": dedup_deleted,
            "metadata_deleted": metadata_deleted,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear library index: {str(e)}")


@router.post("/libraries/{library_id}/reconcile-count", response_model=LibraryIndexMetadata)
def reconcile_library_count(library_id: str, vector_store: VectorStore = Depends(get_vector_store)):
    """
    Recompute total_items_indexed from actual vector store data and persist it.

    Called by the plugin after a reindex pass to fix stale counters caused by
    interrupted indexing runs or other counting inconsistencies.
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    meta = vector_store.get_library_metadata(library_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Library not indexed")

    try:
        actual_count = vector_store.count_indexed_items(library_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to count indexed items: {e}")

    # Guard against a buggy/partial count wiping a well-indexed library's counter.
    # count_indexed_items scrolls the chunk collection and can undercount if called
    # while a scan is concurrently writing.  If the result collapses to under half of
    # the established scan floor, refuse the update — overwriting total_items_indexed
    # with a too-low value makes the cron under-indexed check force an unnecessary full
    # rescan (the June 2026 cascade that wiped 217k chunks).
    if meta.last_full_scan_indexable > 0 and actual_count < meta.last_full_scan_indexable * 0.5:
        logger.warning(
            "reconcile-count: computed count %d is < 50%% of scan_floor %d for "
            "library %s — refusing update",
            actual_count, meta.last_full_scan_indexable, library_id,
        )
        raise HTTPException(
            status_code=409,
            detail=(
                f"Computed count {actual_count} is implausibly low relative to the "
                f"scan floor {meta.last_full_scan_indexable}. Not updating "
                "total_items_indexed. If the library was intentionally reduced to "
                "fewer items, run a full scan first to reset the scan floor."
            ),
        )

    meta.total_items_indexed = actual_count
    vector_store.update_library_metadata(meta)
    logger.info(f"Reconciled total_items_indexed for library={library_id}: {actual_count}")
    return meta


class SyncDeletionsRequest(BaseModel):
    """List of item keys currently present in the Zotero library."""
    current_item_keys: list[str]


class SyncDeletionsResponse(BaseModel):
    """Result of a sync-deletions call."""
    deleted_items: int
    deleted_chunks: int


@router.post("/libraries/{library_id:path}/sync-deletions", response_model=SyncDeletionsResponse)
def sync_library_deletions(
    library_id: str,
    request: SyncDeletionsRequest,
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """Remove chunks for indexed items no longer present in the Zotero library.

    Called by the plugin after it has collected the complete set of current Zotero
    item keys (parent items only, not attachment keys).  Any indexed item whose key
    is absent from *current_item_keys* is treated as deleted and its chunks and
    deduplication records are purged.
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")
    assert_can_access(identity, library_id)

    try:
        indexed = vector_store.get_all_indexed_item_versions(library_id)
        current_keys = set(request.current_item_keys)
        orphaned = set(indexed) - current_keys

        deleted_chunks = 0
        for key in orphaned:
            deleted_chunks += vector_store.delete_item_chunks(library_id, key)
            vector_store.delete_item_deduplication_records(library_id, key)

        if orphaned:
            logger.info(
                "sync-deletions: removed %d orphaned item(s) (%d chunks) from library %s",
                len(orphaned),
                deleted_chunks,
                library_id,
            )

        return SyncDeletionsResponse(deleted_items=len(orphaned), deleted_chunks=deleted_chunks)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"sync-deletions failed: {str(e)}")


@router.delete("/libraries/{library_id:path}/items/{item_key}/chunks")
def clear_item_chunks(
    library_id: str,
    item_key: str,
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    Remove all indexed chunks for a specific item within a library.
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")
    assert_can_access(identity, library_id)

    try:
        chunks_deleted = vector_store.delete_item_chunks(library_id, item_key)
        # Also purge the dedup record so the item can be re-indexed.  A surviving
        # dedup record without chunks makes check_duplicate match an item that has
        # no chunks, leaving it permanently un-reindexable.
        vector_store.delete_item_deduplication_records(library_id, item_key)
        return {
            "library_id": library_id,
            "item_key": item_key,
            "chunks_deleted": chunks_deleted,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear item chunks: {str(e)}")
