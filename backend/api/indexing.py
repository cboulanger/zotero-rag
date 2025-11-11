"""
Indexing API endpoints.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional, AsyncGenerator
import asyncio
import json
import logging

from backend.zotero.local_api import ZoteroLocalAPI
from backend.services.document_processor import DocumentProcessor
from backend.services.embeddings import create_embedding_service
from backend.db.vector_store import VectorStore
from backend.config.settings import get_settings

router = APIRouter()
logger = logging.getLogger(__name__)


class IndexingRequest(BaseModel):
    """Request to start library indexing."""
    library_id: str
    force_reindex: bool = False


class IndexingResponse(BaseModel):
    """Response from indexing request."""
    library_id: str
    status: str
    message: str


class IndexingProgressEvent(BaseModel):
    """Progress event for SSE streaming."""
    event: str  # 'started', 'progress', 'completed', 'error'
    library_id: str
    message: str
    progress: Optional[float] = None  # Percentage (0-100)
    current_item: Optional[int] = None
    total_items: Optional[int] = None


# Store for tracking active indexing jobs
# In production, this should be a proper job queue (e.g., Celery, RQ)
active_jobs = {}


async def index_library_task(library_id: str, force_reindex: bool = False, max_items: Optional[int] = None):
    """
    Background task to index a library.

    Args:
        library_id: Zotero library ID to index.
        force_reindex: If True, reindex all items even if already indexed.
        max_items: Optional maximum number of items to process (for testing).
    """
    settings = get_settings()
    job_id = f"index_{library_id}"

    try:
        active_jobs[job_id] = {
            "status": "running",
            "progress": 0,
            "current_item": 0,
            "total_items": 0,
            "message": "Initializing..."
        }

        # Initialize services
        async with ZoteroLocalAPI() as zotero_client:
            # Get hardware preset and extract embedding config
            active_jobs[job_id]["message"] = "Loading configuration..."
            preset = settings.get_hardware_preset()

            # Determine library type by checking available libraries
            active_jobs[job_id]["message"] = "Detecting library type..."
            libraries = await zotero_client.list_libraries()
            library_type = "user"  # Default
            for lib in libraries:
                if lib["id"] == library_id:
                    library_type = lib["type"]
                    logger.info(f"Detected library {library_id} as type '{library_type}'")
                    break

            active_jobs[job_id]["message"] = f"Loading embedding model ({preset.embedding.model_name})..."
            embedding_service = create_embedding_service(
                preset.embedding,
                cache_dir=str(settings.model_weights_path),
                hf_token=settings.get_api_key("HF_TOKEN")
            )

            active_jobs[job_id]["message"] = "Initializing vector database..."
            # Use context manager to ensure VectorStore is closed after indexing
            with VectorStore(
                storage_path=settings.vector_db_path,
                embedding_dim=embedding_service.get_embedding_dim()
            ) as vector_store:

                active_jobs[job_id]["message"] = "Starting document indexing..."
                processor = DocumentProcessor(
                    zotero_client=zotero_client,
                    embedding_service=embedding_service,
                    vector_store=vector_store
                )

                # Index the library
                await processor.index_library(
                    library_id=library_id,
                    library_type=library_type,
                    force_reindex=force_reindex,
                    progress_callback=lambda current, total: active_jobs[job_id].update({
                        "progress": (current / total * 100) if total > 0 else 0,
                        "current_item": current,
                        "total_items": total
                    }),
                    max_items=max_items
                )

        active_jobs[job_id]["status"] = "completed"
        active_jobs[job_id]["progress"] = 100

    except Exception as e:
        logger.error(f"Error indexing library {library_id}: {e}", exc_info=True)
        active_jobs[job_id]["status"] = "error"
        active_jobs[job_id]["error"] = str(e)


@router.post("/index/library/{library_id}", response_model=IndexingResponse)
async def start_library_indexing(
    library_id: str,
    force_reindex: bool = False,
    max_items: Optional[int] = None
):
    """
    Start indexing a Zotero library.

    This endpoint starts the indexing process in the background.
    Use the SSE endpoint to monitor progress.

    Args:
        library_id: Zotero library ID to index.
        force_reindex: If True, reindex all items even if already indexed.
        max_items: Optional maximum number of items to process (for testing).

    Returns:
        Status message indicating indexing has started.

    Raises:
        HTTPException: If library not found or indexing already in progress.
    """
    job_id = f"index_{library_id}"

    # Check if already indexing
    if job_id in active_jobs and active_jobs[job_id]["status"] == "running":
        raise HTTPException(
            status_code=409,
            detail=f"Library {library_id} is already being indexed"
        )

    # Verify Zotero is accessible
    try:
        async with ZoteroLocalAPI() as client:
            # Just check connection - we'll use the library_id as passed from plugin
            # The plugin knows the correct Zotero internal library ID
            if not await client.check_connection():
                raise HTTPException(
                    status_code=503,
                    detail="Zotero local API is not accessible"
                )
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to Zotero: {str(e)}"
        )

    # Start background task
    asyncio.create_task(index_library_task(library_id, force_reindex, max_items))

    return IndexingResponse(
        library_id=library_id,
        status="started",
        message=f"Indexing started for library {library_id}"
    )


async def generate_progress_events(library_id: str) -> AsyncGenerator[str, None]:
    """
    Generate SSE events for indexing progress.

    Args:
        library_id: Zotero library ID being indexed.

    Yields:
        SSE-formatted event strings.
    """
    job_id = f"index_{library_id}"

    # Send started event
    event = IndexingProgressEvent(
        event="started",
        library_id=library_id,
        message=f"Started indexing library {library_id}"
    )
    yield f"data: {event.model_dump_json()}\n\n"

    # Poll for progress updates
    last_progress = 0
    last_message = ""
    while True:
        await asyncio.sleep(0.5)  # Poll every 500ms

        if job_id not in active_jobs:
            # Job not found or not started yet
            continue

        job = active_jobs[job_id]
        current_progress = job.get("progress", 0)
        current_message = job.get("message", "")

        # Send progress update if changed (progress or message)
        if current_progress != last_progress or current_message != last_message:
            # Use the custom message if available, otherwise default message
            if current_message:
                message = current_message
            else:
                message = f"Indexing progress: {current_progress:.1f}%"

            event = IndexingProgressEvent(
                event="progress",
                library_id=library_id,
                message=message,
                progress=current_progress,
                current_item=job.get("current_item"),
                total_items=job.get("total_items")
            )
            yield f"data: {event.model_dump_json()}\n\n"
            last_progress = current_progress
            last_message = current_message

        # Check for completion or error
        if job["status"] == "completed":
            event = IndexingProgressEvent(
                event="completed",
                library_id=library_id,
                message=f"Indexing completed for library {library_id}",
                progress=100,
                current_item=job.get("total_items"),
                total_items=job.get("total_items")
            )
            yield f"data: {event.model_dump_json()}\n\n"
            break

        elif job["status"] == "error":
            event = IndexingProgressEvent(
                event="error",
                library_id=library_id,
                message=f"Error: {job.get('error', 'Unknown error')}",
                progress=last_progress
            )
            yield f"data: {event.model_dump_json()}\n\n"
            break


@router.get("/index/library/{library_id}/progress")
async def stream_indexing_progress(library_id: str):
    """
    Stream indexing progress via Server-Sent Events (SSE).

    Args:
        library_id: Zotero library ID being indexed.

    Returns:
        SSE stream with progress updates.
    """
    return StreamingResponse(
        generate_progress_events(library_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"  # Disable buffering in nginx
        }
    )
