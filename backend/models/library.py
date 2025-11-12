"""
Data models for library metadata and indexing state.
"""

from typing import Literal
from datetime import datetime
from pydantic import BaseModel, Field


class LibraryIndexMetadata(BaseModel):
    """Metadata tracking indexing state for a library."""

    library_id: str = Field(description="Library ID (e.g., '1' for user library)")
    library_type: Literal["user", "group"] = Field(description="Library type")
    library_name: str = Field(description="Human-readable library name")

    last_indexed_version: int = Field(
        default=0,
        description="Highest Zotero version number processed"
    )
    last_indexed_at: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat(),
        description="ISO timestamp of last indexing operation"
    )

    total_items_indexed: int = Field(
        default=0,
        description="Total number of items successfully indexed"
    )
    total_chunks: int = Field(
        default=0,
        description="Total number of chunks in vector store"
    )

    indexing_mode: Literal["full", "incremental"] = Field(
        default="incremental",
        description="Last indexing mode used"
    )

    force_reindex: bool = Field(
        default=False,
        description="If True, next index will be full reindex (hard reset)"
    )

    schema_version: int = Field(default=1)

    class Config:
        json_schema_extra = {
            "example": {
                "library_id": "1",
                "library_type": "user",
                "library_name": "My Library",
                "last_indexed_version": 12345,
                "last_indexed_at": "2025-01-12T10:30:00Z",
                "total_items_indexed": 250,
                "total_chunks": 12500,
                "indexing_mode": "incremental",
                "force_reindex": False
            }
        }
