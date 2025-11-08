"""
Library API endpoints.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional

from backend.zotero.local_api import ZoteroLocalAPI

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
            libraries = await client.get_libraries()

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
    # TODO: Implement actual status checking from vector database
    # For now, return placeholder response
    return LibraryStatusResponse(
        library_id=library_id,
        indexed=False,
        total_items=None,
        indexed_items=None,
        last_indexed=None
    )
