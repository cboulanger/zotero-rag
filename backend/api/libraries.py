"""
Library API endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from backend.zotero.local_api import ZoteroLocalAPI
from backend.models.library import LibraryIndexMetadata
from backend.db.vector_store import VectorStore
from backend.dependencies import get_vector_store

router = APIRouter()


class LibraryInfo(BaseModel):
    """Library information."""
    library_id: str
    name: str
    type: str  # 'user' or 'group'
    version: int


class LibraryStatusResponse(BaseModel):
    """Library indexing status."""
    library_id: str
    indexed: bool
    total_items: Optional[int] = None
    indexed_items: Optional[int] = None
    last_indexed: Optional[str] = None


@router.get("/libraries", response_model=List[LibraryInfo])
async def list_libraries():
    """
    List all available Zotero libraries.

    Returns:
        List of libraries accessible via Zotero local API.

    Raises:
        HTTPException: If Zotero is not running or local API is unavailable.
    """
    try:
        async with ZoteroLocalAPI() as client:
            libraries = await client.list_libraries()

            return [
                LibraryInfo(
                    library_id=lib["id"],
                    name=lib["name"],
                    type=lib["type"],
                    version=lib.get("version", 0)
                )
                for lib in libraries
            ]
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to Zotero local API: {str(e)}"
        )


@router.get("/libraries/{library_id}/status", response_model=LibraryStatusResponse)
async def get_library_status(library_id: str, vector_store: VectorStore = Depends(get_vector_store)):
    """
    Get indexing status for a library.

    Args:
        library_id: Zotero library ID.

    Returns:
        Library indexing status including item counts.

    Raises:
        HTTPException: If library not found or status unavailable.
    """
    if vector_store is None:
        return LibraryStatusResponse(library_id=library_id, indexed=False)

    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        query_filter = Filter(
            must=[FieldCondition(key="library_id", match=MatchValue(value=library_id))]
        )

        total_chunks = 0
        unique_items: set[str] = set()
        offset = None

        client = vector_store.client
        if not client:
            raise RuntimeError("Vector store client not initialized")

        while True:
            batch, offset = client.scroll(
                collection_name=vector_store.CHUNKS_COLLECTION,
                scroll_filter=query_filter,
                limit=100,
                offset=offset,
                with_payload=["item_id"],
                with_vectors=False,
            )

            total_chunks += len(batch)
            for point in batch:
                if point.payload and "item_id" in point.payload:
                    unique_items.add(point.payload["item_id"])

            if offset is None:
                break

        indexed = total_chunks > 0
        indexed_items = len(unique_items) if indexed else None

        return LibraryStatusResponse(
            library_id=library_id,
            indexed=indexed,
            total_items=indexed_items,
            indexed_items=indexed_items,
            last_indexed=None,
        )
    except Exception:
        return LibraryStatusResponse(library_id=library_id, indexed=False)


@router.get("/libraries/{library_id}/index-status", response_model=LibraryIndexMetadata)
async def get_library_index_status(library_id: str, vector_store: VectorStore = Depends(get_vector_store)):
    """
    Get detailed indexing status and metadata for a library.

    Args:
        library_id: Zotero library ID.

    Returns:
        Detailed library indexing metadata.

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


@router.post("/libraries/{library_id}/reset-index")
async def reset_library_index(library_id: str, vector_store: VectorStore = Depends(get_vector_store)):
    """
    Mark a library for full reindex (hard reset).

    Args:
        library_id: Zotero library ID.

    Returns:
        Success message with new status.

    Raises:
        HTTPException: If operation fails.
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    try:
        vector_store.mark_library_for_reset(library_id)
        metadata = vector_store.get_library_metadata(library_id)
        return {
            "message": f"Library {library_id} marked for hard reset",
            "force_reindex": metadata.force_reindex if metadata else True,
            "next_index_mode": "full",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reset library index: {str(e)}")


@router.delete("/libraries/{library_id}/index")
async def clear_library_index(library_id: str, vector_store: VectorStore = Depends(get_vector_store)):
    """
    Remove all indexed data for a library (chunks, dedup records, metadata).

    Args:
        library_id: Zotero library ID.

    Returns:
        Counts of deleted records.
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


@router.get("/libraries/indexed", response_model=List[LibraryIndexMetadata])
async def list_indexed_libraries(vector_store: VectorStore = Depends(get_vector_store)):
    """
    List all libraries that have been indexed.

    Returns:
        Array of library metadata objects for all indexed libraries.

    Raises:
        HTTPException: If operation fails.
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    try:
        return vector_store.get_all_library_metadata()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list indexed libraries: {str(e)}")
