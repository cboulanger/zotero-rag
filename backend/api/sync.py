"""
Vector database synchronization API endpoints.
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Optional, Literal
import logging

from backend.services.vector_sync import VectorSyncService, SyncStatus
from backend.services.snapshot_manager import SnapshotManager
from backend.storage.factory import create_storage_backend
from backend.db.vector_store import VectorStore
from backend.zotero.local_api import ZoteroLocalAPI
from backend.config.settings import get_settings

router = APIRouter()
logger = logging.getLogger(__name__)


# Pydantic models for API

class SyncResponse(BaseModel):
    """Response from sync operation."""
    success: bool
    message: str
    operation: Optional[str] = None  # 'pull', 'push', 'none'
    library_id: str
    downloaded_bytes: Optional[int] = 0
    uploaded_bytes: Optional[int] = 0
    chunks_restored: Optional[int] = 0
    chunks_pushed: Optional[int] = 0
    library_version: Optional[int] = 0
    restore_time: Optional[float] = 0
    snapshot_time: Optional[float] = 0


class SyncStatusResponse(BaseModel):
    """Sync status for a library."""
    library_id: str
    local_exists: bool
    remote_exists: bool
    local_version: int
    remote_version: int
    sync_status: str
    local_chunks: int
    remote_chunks: int
    local_last_indexed: str
    remote_uploaded_at: str
    needs_pull: bool = False
    needs_push: bool = False


class RemoteLibrary(BaseModel):
    """Remote library information."""
    library_id: str
    library_version: int
    snapshot_file: str
    uploaded_at: str
    total_chunks: int
    total_items: int


class RemoteLibrariesResponse(BaseModel):
    """List of remote libraries."""
    libraries: list[RemoteLibrary]
    count: int


# Dependency injection helpers

def get_sync_service() -> Optional[VectorSyncService]:
    """
    Create VectorSyncService instance if sync is enabled.

    Returns:
        VectorSyncService instance or None if sync disabled
    """
    settings = get_settings()

    if not settings.sync_enabled:
        return None

    # Create storage backend
    storage = create_storage_backend(settings)
    if not storage:
        logger.warning("Sync enabled but no storage backend configured")
        return None

    # Create vector store
    vector_store = VectorStore(
        storage_path=settings.vector_db_path,
        embedding_dim=settings.embedding_dimension,
    )

    # Create snapshot manager
    snapshot_manager = SnapshotManager(vector_store=vector_store)

    # Create Zotero client
    zotero_client = ZoteroLocalAPI()

    # Create sync service
    sync_service = VectorSyncService(
        vector_store=vector_store,
        snapshot_manager=snapshot_manager,
        storage_backend=storage,
        zotero_client=zotero_client,
    )

    return sync_service


# API endpoints

@router.get("/vectors/sync/enabled")
async def check_sync_enabled():
    """
    Check if vector sync is enabled.

    Returns:
        Dict with enabled status and backend type
    """
    settings = get_settings()
    return {
        "enabled": settings.sync_enabled,
        "backend": settings.sync_backend if settings.sync_enabled else None,
        "auto_pull": settings.sync_auto_pull if settings.sync_enabled else False,
        "auto_push": settings.sync_auto_push if settings.sync_enabled else False,
    }


@router.get("/vectors/remote", response_model=RemoteLibrariesResponse)
async def list_remote_libraries():
    """
    List available libraries in remote storage.

    Returns:
        List of remote libraries with metadata

    Raises:
        HTTPException: If sync is not enabled or operation fails
    """
    sync_service = get_sync_service()
    if not sync_service:
        raise HTTPException(
            status_code=400,
            detail="Vector sync is not enabled. Configure SYNC_ENABLED=true in .env"
        )

    try:
        libraries = await sync_service.list_remote_libraries()
        return RemoteLibrariesResponse(
            libraries=[RemoteLibrary(**lib) for lib in libraries],
            count=len(libraries)
        )
    except Exception as e:
        logger.error(f"Error listing remote libraries: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list remote libraries: {str(e)}"
        )


@router.get("/vectors/{library_id}/sync-status", response_model=SyncStatusResponse)
async def get_sync_status(library_id: str):
    """
    Get sync status for a library.

    Args:
        library_id: Library ID to check

    Returns:
        Detailed sync status information

    Raises:
        HTTPException: If sync is not enabled or operation fails
    """
    sync_service = get_sync_service()
    if not sync_service:
        raise HTTPException(
            status_code=400,
            detail="Vector sync is not enabled"
        )

    try:
        status = await sync_service.get_sync_status(library_id)

        # Add convenience flags
        needs_pull = status["sync_status"] in [SyncStatus.REMOTE_NEWER, SyncStatus.NO_LOCAL]
        needs_push = status["sync_status"] == SyncStatus.LOCAL_NEWER

        return SyncStatusResponse(
            library_id=library_id,
            needs_pull=needs_pull,
            needs_push=needs_push,
            **status
        )
    except Exception as e:
        logger.error(f"Error getting sync status for library {library_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get sync status: {str(e)}"
        )


@router.post("/vectors/{library_id}/pull", response_model=SyncResponse)
async def pull_library(
    library_id: str,
    force: bool = False
):
    """
    Pull library vectors from remote storage.

    Downloads the latest snapshot from remote storage and restores it locally.

    Args:
        library_id: Library ID to pull
        force: Force pull even if not needed

    Returns:
        Sync response with operation details

    Raises:
        HTTPException: If sync is not enabled or operation fails
    """
    sync_service = get_sync_service()
    if not sync_service:
        raise HTTPException(
            status_code=400,
            detail="Vector sync is not enabled"
        )

    try:
        logger.info(f"Pull request for library {library_id} (force={force})")
        result = await sync_service.pull_library(library_id, force=force)

        return SyncResponse(
            library_id=library_id,
            operation="pull",
            **result
        )
    except Exception as e:
        logger.error(f"Error pulling library {library_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to pull library: {str(e)}"
        )


@router.post("/vectors/{library_id}/push", response_model=SyncResponse)
async def push_library(
    library_id: str,
    force: bool = False
):
    """
    Push library vectors to remote storage.

    Creates a snapshot of the local library and uploads it to remote storage.

    Args:
        library_id: Library ID to push
        force: Force push even if remote is newer

    Returns:
        Sync response with operation details

    Raises:
        HTTPException: If sync is not enabled or operation fails
    """
    sync_service = get_sync_service()
    if not sync_service:
        raise HTTPException(
            status_code=400,
            detail="Vector sync is not enabled"
        )

    try:
        logger.info(f"Push request for library {library_id} (force={force})")
        result = await sync_service.push_library(library_id, force=force)

        return SyncResponse(
            library_id=library_id,
            operation="push",
            **result
        )
    except Exception as e:
        logger.error(f"Error pushing library {library_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to push library: {str(e)}"
        )


@router.post("/vectors/{library_id}/sync", response_model=SyncResponse)
async def sync_library(
    library_id: str,
    direction: Literal["auto", "pull", "push"] = Query(
        default="auto",
        description="Sync direction: auto (intelligent), pull (download), or push (upload)"
    )
):
    """
    Bidirectional sync with automatic conflict detection.

    Auto mode intelligently chooses the sync direction based on version comparison:
    - Local newer: push to remote
    - Remote newer: pull from remote
    - Same version: no operation
    - No local: pull from remote
    - No remote: push to remote

    Args:
        library_id: Library ID to sync
        direction: Sync direction (auto, pull, push)

    Returns:
        Sync response with operation details

    Raises:
        HTTPException: If sync is not enabled, operation fails, or conflict detected
    """
    sync_service = get_sync_service()
    if not sync_service:
        raise HTTPException(
            status_code=400,
            detail="Vector sync is not enabled"
        )

    try:
        logger.info(f"Sync request for library {library_id} (direction={direction})")
        result = await sync_service.sync_library(library_id, direction=direction)

        return SyncResponse(
            library_id=library_id,
            **result
        )
    except Exception as e:
        logger.error(f"Error syncing library {library_id}: {e}")

        # Check if it's a conflict error
        if "diverged" in str(e).lower() or "conflict" in str(e).lower():
            raise HTTPException(
                status_code=409,  # Conflict
                detail=str(e)
            )

        raise HTTPException(
            status_code=500,
            detail=f"Failed to sync library: {str(e)}"
        )


@router.post("/vectors/sync-all")
async def sync_all_libraries(
    direction: Literal["auto", "pull", "push"] = Query(default="auto")
):
    """
    Sync all indexed libraries.

    Attempts to sync all libraries that exist locally. Failed syncs are logged
    but don't stop the operation.

    Args:
        direction: Sync direction for all libraries

    Returns:
        Summary of sync operations

    Raises:
        HTTPException: If sync is not enabled
    """
    sync_service = get_sync_service()
    if not sync_service:
        raise HTTPException(
            status_code=400,
            detail="Vector sync is not enabled"
        )

    try:
        # Get all local libraries
        settings = get_settings()
        vector_store = VectorStore(
            storage_path=settings.vector_db_path,
            embedding_dim=settings.embedding_dimension,
        )
        all_libraries = vector_store.get_all_library_metadata()

        logger.info(f"Syncing {len(all_libraries)} libraries (direction={direction})")

        results = []
        for lib_meta in all_libraries:
            try:
                result = await sync_service.sync_library(
                    lib_meta.library_id,
                    direction=direction
                )
                results.append({
                    "library_id": lib_meta.library_id,
                    "success": result.get("success", False),
                    "message": result.get("message", ""),
                    "operation": result.get("operation"),
                })
            except Exception as e:
                logger.error(f"Failed to sync library {lib_meta.library_id}: {e}")
                results.append({
                    "library_id": lib_meta.library_id,
                    "success": False,
                    "message": str(e),
                    "operation": None,
                })

        success_count = sum(1 for r in results if r["success"])

        return {
            "total_libraries": len(all_libraries),
            "successful": success_count,
            "failed": len(all_libraries) - success_count,
            "results": results,
        }

    except Exception as e:
        logger.error(f"Error syncing all libraries: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to sync libraries: {str(e)}"
        )
