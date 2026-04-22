"""
Library API endpoints.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional

from backend.models.library import LibraryIndexMetadata
from backend.db.vector_store import VectorStore
from backend.dependencies import get_vector_store
from backend.config.settings import get_settings
from backend.services.registration_service import RegistrationService

router = APIRouter()
logger = logging.getLogger(__name__)


class RegisteredUser(BaseModel):
    """A user who has registered a library."""
    user_id: int
    username: str
    registered_at: str


class LibraryDetailResponse(BaseModel):
    """Combined library status: index metadata + registrations + storage stats."""
    library_id: str
    library_name: str
    library_type: str
    # Index state (None if never indexed)
    last_indexed_at: Optional[str] = None
    last_indexed_version: Optional[int] = None
    total_items_indexed: int = 0
    total_chunks: int = 0
    indexing_mode: Optional[str] = None
    # Storage stats
    size_bytes: int = 0
    # Registration
    registered_at: Optional[str] = None
    users: list[RegisteredUser] = []


def _build_detail(
    library_id: str,
    metadata: Optional[LibraryIndexMetadata],
    reg_entry: Optional[dict],
    size_bytes: int,
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
        size_bytes=size_bytes,
        registered_at=registered_at,
        users=users,
    )


@router.get("/libraries", response_model=list[LibraryDetailResponse])
async def list_libraries(vector_store: VectorStore = Depends(get_vector_store)):
    """
    List all libraries known to the backend (indexed or registered).

    Returns combined index metadata, registration info, and storage stats for each.
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

    results = []
    for lid in sorted(all_ids):
        meta = metadata_by_id.get(lid)
        reg = registrations.get(lid)
        size = vector_store.get_library_size_bytes(lid) if meta else 0
        results.append(_build_detail(lid, meta, reg, size))

    return results


@router.get("/libraries/{library_id}/status", response_model=LibraryDetailResponse)
async def get_library_status(library_id: str, vector_store: VectorStore = Depends(get_vector_store)):
    """
    Get combined status for a single library: index metadata, registrations, and storage stats.
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

    size = vector_store.get_library_size_bytes(library_id) if metadata else 0
    return _build_detail(library_id, metadata, reg, size)


@router.get("/libraries/{library_id}/index-status", response_model=LibraryIndexMetadata)
async def get_library_index_status(library_id: str, vector_store: VectorStore = Depends(get_vector_store)):
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


@router.delete("/libraries/{library_id}/index")
async def clear_library_index(library_id: str, vector_store: VectorStore = Depends(get_vector_store)):
    """
    Remove all indexed data for a library (chunks, dedup records, metadata).
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

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


@router.delete("/libraries/{library_id}/items/{item_key}/chunks")
async def clear_item_chunks(library_id: str, item_key: str, vector_store: VectorStore = Depends(get_vector_store)):
    """
    Remove all indexed chunks for a specific item within a library.
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    try:
        chunks_deleted = vector_store.delete_item_chunks(library_id, item_key)
        return {
            "library_id": library_id,
            "item_key": item_key,
            "chunks_deleted": chunks_deleted,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear item chunks: {str(e)}")
