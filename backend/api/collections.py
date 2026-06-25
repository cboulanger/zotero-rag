"""



Provides:
- GET  /api/collections/vectors/status  - counts of item and collection vectors
- POST /api/collections/vectors/sync    - full-library sync of item/collection vectors
- GET  /api/collections/suggest         - suggest collections for an item via vector search
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query as QueryParam, Request

from backend.db.vector_store import VectorStore
from backend.dependencies import get_client_api_keys, get_vector_store, make_embedding_service
from backend.models.collection import (
    CollectionSuggestion,
    CollectionVectorsStatus,
    CollectionVectorSyncRequest,
    CollectionVectorSyncStats,
)
from backend.services.collection_vector_service import CollectionVectorService

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/collections/vectors/status", response_model=CollectionVectorsStatus)
def get_collection_vectors_status(
    library_id: str,
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    Return counts of pre-computed item and collection vectors for a library.

    Both VectorStore calls are synchronous, so this handler is declared as
    ``def`` (not ``async def``) so FastAPI runs it in a thread pool.

    Args:
        library_id: Zotero library ID to query.
        vector_store: Injected VectorStore singleton.

    Returns:
        CollectionVectorsStatus with item_vectors_count, collection_vectors_count,
        and a computed ``computed`` flag (True when collection_vectors_count > 0).
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    try:
        item_count = vector_store.count_item_vectors(library_id)
        coll_count = vector_store.count_collection_vectors(library_id)
        return CollectionVectorsStatus(
            library_id=library_id,
            item_vectors_count=item_count,
            collection_vectors_count=coll_count,
            computed=coll_count > 0,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to retrieve collection vectors status: {str(e)}")


@router.post("/collections/vectors/sync", response_model=CollectionVectorSyncStats)
async def sync_collection_vectors(
    request: CollectionVectorSyncRequest,
    http_request: Request,
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    Full-library sync: compute item vectors then collection centroids.

    Awaits ``CollectionVectorService.sync_library()``, so the handler is
    ``async def``.

    Args:
        request: Sync request containing library_id, collection_map, and
            optional collection_names.
        http_request: Raw FastAPI request (used to extract client API keys).
        vector_store: Injected VectorStore singleton.

    Returns:
        CollectionVectorSyncStats with keys: items_computed, items_skipped,
        collections_computed, collections_skipped.

    Raises:
        HTTPException 503: If the vector store or embedding service is unavailable.
        HTTPException 500: On unexpected errors during sync.
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    try:
        # DEBUG
        logger.debug(
            "sync_collection_vectors: library_id=%s items_in_map=%d unique_collections=%d",
            request.library_id,
            len(request.collection_map),
            len({c for cols in request.collection_map.values() for c in cols}),
        )
        # END DEBUG
        client_keys = get_client_api_keys(http_request)
        try:
            embedding_service = make_embedding_service(client_keys)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Embedding service unavailable: {str(e)}")
        svc = CollectionVectorService(vector_store, embedding_service)
        return await svc.sync_library(
            library_id=request.library_id,
            collection_map=request.collection_map,
            collection_names=request.collection_names,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to sync collection vectors: {str(e)}")


@router.get("/collections/suggest", response_model=list[CollectionSuggestion])
def suggest_collections(
    library_id: str,
    item_key: str,
    limit: int = QueryParam(default=5, ge=1, le=20),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """
    Suggest collections for an item using pre-computed vectors.

    Retrieves the item's pre-computed vector from ``item_vectors`` and
    searches ``collection_vectors`` for the nearest neighbours.  All
    VectorStore calls are synchronous, so this handler is declared as
    ``def`` (not ``async def``).

    Args:
        library_id: Zotero library ID.
        item_key: Zotero item key.
        limit: Maximum number of suggestions to return (1–20, defaults to 5).
        vector_store: Injected VectorStore singleton.

    Returns:
        List of CollectionSuggestion objects sorted by descending similarity
        score.  Returns an empty list (not 404) when no item vector exists.

    Raises:
        HTTPException 503: If the vector store is unavailable.
        HTTPException 422: If limit is outside 1–20.
        HTTPException 500: On unexpected errors.
    """
    if vector_store is None:
        raise HTTPException(status_code=503, detail="Vector store is unavailable")

    try:
        item_result = vector_store.get_item_vector(library_id, item_key)
        # DEBUG
        logger.debug("suggest_collections: library_id=%s item_key=%s item_vector_found=%s", library_id, item_key, item_result is not None)  # DEBUG
        if item_result is None:
            logger.debug("suggest_collections: no item vector for %s/%s - returning []", library_id, item_key)  # DEBUG
            return []

        item_vector, _ = item_result
        results = vector_store.search_collection_vectors(item_vector, limit=limit)
        # DEBUG
        logger.debug("suggest_collections: collection_vectors search returned %d result(s)", len(results))  # DEBUG
        return [
            CollectionSuggestion(
                collection_id=coll_id,
                collection_name=payload.get("collection_name", ""),
                library_id=payload.get("library_id", library_id),
                score=score,
            )
            for coll_id, score, payload in results
        ]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to suggest collections: {str(e)}")
