"""
Data models for collection-based smart filing features.
"""

from pydantic import BaseModel, Field


class CollectionSuggestion(BaseModel):
    """A suggested Zotero collection for a given item, with a similarity score."""

    collection_id: str
    collection_name: str
    library_id: str
    score: float  # cosine similarity 0..1


class CollectionVectorsStatus(BaseModel):
    """Status of pre-computed item and collection vectors for a library."""

    library_id: str
    item_vectors_count: int
    collection_vectors_count: int
    computed: bool  # True if collection_vectors_count > 0


class CollectionVectorSyncRequest(BaseModel):
    """Request payload for syncing item/collection vectors from the Zotero plugin."""

    library_id: str
    # Maps item_key -> list of collection_ids the item belongs to
    collection_map: dict[str, list[str]]
    # Maps collection_id -> human-readable collection name
    collection_names: dict[str, str] = Field(default_factory=dict)


class CollectionVectorSyncStats(BaseModel):
    """Stats returned after syncing item/collection vectors."""

    items_computed: int
    items_skipped: int
    collections_computed: int
    collections_skipped: int
