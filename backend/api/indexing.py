"""
Pull-model indexing endpoints — removed.

Indexing is now push-only: the Zotero plugin reads attachment files locally
and uploads them via POST /api/index/document. See api/document_upload.py.
"""

from fastapi import APIRouter, HTTPException
from typing import Literal, Optional
from fastapi import Query

router = APIRouter()


@router.post("/index/library/{library_id}")
async def start_library_indexing(library_id: str):
    """Removed: use the plugin's push-based indexing instead."""
    raise HTTPException(
        status_code=410,
        detail=(
            "Pull-based indexing has been removed. "
            "Indexing is now initiated from the Zotero plugin, which uploads "
            "attachment files directly via POST /api/index/document."
        )
    )


@router.get("/index/library/{library_id}/progress")
async def stream_indexing_progress(library_id: str):
    """Removed: progress is now reported inline by the plugin."""
    raise HTTPException(status_code=410, detail="Pull-based indexing has been removed.")


@router.post("/index/library/{library_id}/cancel")
async def cancel_library_indexing(library_id: str):
    """Removed: cancellation is now handled client-side by the plugin."""
    raise HTTPException(status_code=410, detail="Pull-based indexing has been removed.")
