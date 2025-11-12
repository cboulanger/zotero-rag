"""
Library API endpoints.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from backend.zotero.local_api import ZoteroLocalAPI
from backend.models.library import LibraryIndexMetadata

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
async def get_library_status(library_id: str):
    """
    Get indexing status for a library.

    Args:
        library_id: Zotero library ID.

    Returns:
        Library indexing status including item counts.

    Raises:
        HTTPException: If library not found or status unavailable.
    """
    from backend.config.settings import get_settings
    from backend.db.vector_store import VectorStore
    from backend.services.embeddings import create_embedding_service

    try:
        settings = get_settings()
        preset = settings.get_hardware_preset()

        # Initialize embedding service to get dimension
        embedding_service = create_embedding_service(
            preset.embedding,
            cache_dir=str(settings.model_weights_path),
            hf_token=settings.get_api_key("HF_TOKEN")
        )

        # Check vector store for library data - use context manager to ensure cleanup
        with VectorStore(
            storage_path=settings.vector_db_path,
            embedding_dim=embedding_service.get_embedding_dim()
        ) as vector_store:

            # Count total chunks and unique items for this library using scroll
            from qdrant_client.models import Filter, FieldCondition, MatchValue

            query_filter = Filter(
                must=[
                    FieldCondition(
                        key="library_id",
                        match=MatchValue(value=library_id)
                    )
                ]
            )

            # Scroll through all chunks for this library
            total_chunks = 0
            unique_items: set[str] = set()
            offset = None

            # Get client reference
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
                    with_vectors=False
                )

                total_chunks += len(batch)

                # Collect unique item_ids
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
                total_items=indexed_items,  # Same as indexed_items for now
                indexed_items=indexed_items,
                last_indexed=None  # TODO: Track last indexed timestamp
            )
    except Exception as e:
        # If vector store doesn't exist or other error, return not indexed
        return LibraryStatusResponse(
            library_id=library_id,
            indexed=False,
            total_items=None,
            indexed_items=None,
            last_indexed=None
        )


@router.get("/libraries/{library_id}/index-status", response_model=LibraryIndexMetadata)
async def get_library_index_status(library_id: str):
    """
    Get detailed indexing status and metadata for a library.

    This endpoint returns comprehensive indexing metadata including:
    - Last indexed version
    - Last indexed timestamp
    - Total items and chunks indexed
    - Current indexing mode
    - Force reindex flag

    Args:
        library_id: Zotero library ID.

    Returns:
        Detailed library indexing metadata.

    Raises:
        HTTPException: 404 if library has never been indexed.
    """
    from backend.config.settings import get_settings
    from backend.db.vector_store import VectorStore
    from backend.services.embeddings import create_embedding_service

    try:
        settings = get_settings()
        preset = settings.get_hardware_preset()

        # Initialize embedding service to get dimension
        embedding_service = create_embedding_service(
            preset.embedding,
            cache_dir=str(settings.model_weights_path),
            hf_token=settings.get_api_key("HF_TOKEN")
        )

        # Check vector store for library metadata
        with VectorStore(
            storage_path=settings.vector_db_path,
            embedding_dim=embedding_service.get_embedding_dim()
        ) as vector_store:
            metadata = vector_store.get_library_metadata(library_id)

            if metadata is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Library {library_id} has not been indexed yet"
                )

            return metadata

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to retrieve library index status: {str(e)}"
        )


@router.post("/libraries/{library_id}/reset-index")
async def reset_library_index(library_id: str):
    """
    Mark a library for full reindex (hard reset).

    This sets the force_reindex flag to True. The next indexing
    operation will perform a full reindex regardless of mode parameter.

    Args:
        library_id: Zotero library ID.

    Returns:
        Success message with new status.

    Raises:
        HTTPException: If operation fails.
    """
    from backend.config.settings import get_settings
    from backend.db.vector_store import VectorStore
    from backend.services.embeddings import create_embedding_service

    try:
        settings = get_settings()
        preset = settings.get_hardware_preset()

        # Initialize embedding service to get dimension
        embedding_service = create_embedding_service(
            preset.embedding,
            cache_dir=str(settings.model_weights_path),
            hf_token=settings.get_api_key("HF_TOKEN")
        )

        # Mark library for reset
        with VectorStore(
            storage_path=settings.vector_db_path,
            embedding_dim=embedding_service.get_embedding_dim()
        ) as vector_store:
            vector_store.mark_library_for_reset(library_id)

            # Get updated metadata
            metadata = vector_store.get_library_metadata(library_id)

            return {
                "message": f"Library {library_id} marked for hard reset",
                "force_reindex": metadata.force_reindex if metadata else True,
                "next_index_mode": "full"
            }

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reset library index: {str(e)}"
        )


@router.get("/libraries/indexed", response_model=List[LibraryIndexMetadata])
async def list_indexed_libraries():
    """
    List all libraries that have been indexed.

    Returns:
        Array of library metadata objects for all indexed libraries.

    Raises:
        HTTPException: If operation fails.
    """
    from backend.config.settings import get_settings
    from backend.db.vector_store import VectorStore
    from backend.services.embeddings import create_embedding_service

    try:
        settings = get_settings()
        preset = settings.get_hardware_preset()

        # Initialize embedding service to get dimension
        embedding_service = create_embedding_service(
            preset.embedding,
            cache_dir=str(settings.model_weights_path),
            hf_token=settings.get_api_key("HF_TOKEN")
        )

        # Get all indexed libraries
        with VectorStore(
            storage_path=settings.vector_db_path,
            embedding_dim=embedding_service.get_embedding_dim()
        ) as vector_store:
            libraries = vector_store.get_all_library_metadata()
            return libraries

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list indexed libraries: {str(e)}"
        )
