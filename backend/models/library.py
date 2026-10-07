"""
Data models for library metadata and indexing state.
"""

from typing import Literal
from datetime import datetime, UTC
from pydantic import BaseModel, Field, ConfigDict


class LibraryIndexMetadata(BaseModel):
    """Metadata tracking indexing state for a library."""

    model_config = ConfigDict(json_schema_extra={
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
    })

    library_id: str = Field(description="Library ID (e.g., '1' for user library)")
    library_type: Literal["user", "group"] = Field(description="Library type")
    library_name: str = Field(description="Human-readable library name")

    last_indexed_version: int = Field(
        default=0,
        description="Highest Zotero version number processed"
    )
    last_indexed_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
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

    last_full_scan_indexable: int = Field(
        default=0,
        description="Items with indexable content found in the last completed full scan"
    )

    last_full_scan_items_failed: int = Field(
        default=0,
        description=(
            "Items that failed per-item processing (e.g. dead attachment download "
            "link, extraction error) in the last completed full scan. Only a full "
            "scan re-examines every candidate, so incremental syncs never update "
            "this — it stays the authoritative floor of currently un-indexable "
            "items until the next full scan."
        )
    )

    last_scan_failed_downloads: list[dict] = Field(
        default_factory=list,
        description=(
            "Up to MAX_TRACKED_DOWNLOAD_FAILURES (see document_processor.py) "
            "{item_key, attachment_key} pairs whose attachment could not be "
            "downloaded from Zotero the last time this library was indexed, by "
            "any path — a full scan, an incremental sync, or the oversized-item "
            "reindex script. Unlike a generic parse error (which recurs regardless "
            "of how the bytes are obtained), these may be fixable client-side — "
            "e.g. the file exists on the user's Zotero desktop even though the "
            "server's cloud-storage fetch failed. Surfaced to the plugin's Fix "
            "Unavailable tool, which is meant to show ALL currently-known "
            "failures — the cap is a safety net against unbounded growth, not a "
            "realistic ceiling. A full scan replaces this list outright (it's the "
            "authoritative, complete view); an incremental sync or the oversized-"
            "item reindex script only sees a subset of the library, so each of "
            "those merges its own failures into whatever's already here instead, "
            "preferring to keep the newest-discovered entries if the cap is ever "
            "actually hit."
        )
    )

    last_scan_skipped_too_large: list[dict] = Field(
        default_factory=list,
        description=(
            "Up to MAX_TRACKED_DOWNLOAD_FAILURES (see document_processor.py) "
            "{item_key, attachment_key, detail} records refused outright for "
            "exceeding Settings.kreuzberg_max_content_bytes the last time this "
            "library was indexed, by any path — a full scan, an incremental "
            "sync, or the deferred pending-upload queue. `detail` is a "
            "human-readable message (e.g. '329 MB, which exceeds the 200 MB "
            "limit') for display in the plugin; older entries merged in before "
            "this field existed may lack it. Unlike last_scan_failed_downloads, "
            "there is nothing to automatically retry here: the file itself needs "
            "to be made smaller (a lower-resolution scan, splitting a combined "
            "PDF, etc.) by the user before it can be indexed. Surfaced to the "
            "plugin's Fix Unavailable tool as a non-actionable, explanatory "
            "status rather than something Search & Fix can resolve on its own. "
            "Same replace-vs-merge semantics as last_scan_failed_downloads: a "
            "full scan replaces this list outright, an incremental sync or a "
            "single deferred-upload drain merges into it."
        )
    )

    schema_version: int = Field(default=1)
