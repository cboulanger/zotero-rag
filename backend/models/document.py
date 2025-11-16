"""
Data models for documents and chunks.
"""

from typing import Optional
from datetime import datetime
from pydantic import BaseModel, Field


class DocumentMetadata(BaseModel):
    """Metadata for a source document (Zotero item)."""

    library_id: str = Field(..., description="Zotero library ID")
    item_key: str = Field(..., description="Zotero item key")
    title: Optional[str] = Field(None, description="Document title")
    authors: list[str] = Field(default_factory=list, description="Document authors")
    year: Optional[int] = Field(None, description="Publication year")
    item_type: Optional[str] = Field(None, description="Zotero item type")
    attachment_key: Optional[str] = Field(None, description="PDF attachment key")


class ChunkMetadata(BaseModel):
    """Metadata for a document chunk with version tracking."""

    chunk_id: str = Field(..., description="Unique chunk identifier")
    document_metadata: DocumentMetadata = Field(..., description="Parent document metadata")
    page_number: Optional[int] = Field(None, description="Page number in source document")
    text_preview: str = Field(..., description="First 5 words as citation anchor")
    chunk_index: int = Field(..., description="Index of chunk within document")
    content_hash: str = Field(..., description="SHA256 hash of chunk content")

    # Version tracking fields (new in schema v2)
    item_version: int = Field(
        default=0,
        description="Zotero item version at time of indexing"
    )
    attachment_version: int = Field(
        default=0,
        description="Zotero attachment version at time of indexing"
    )
    indexed_at: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat(),
        description="ISO 8601 timestamp when chunk was indexed"
    )
    zotero_modified: str = Field(
        default="",
        description="Item's dateModified field from Zotero"
    )

    # Schema version for future migrations
    schema_version: int = Field(default=2)


class DocumentChunk(BaseModel):
    """A chunk of text from a document with metadata."""

    text: str = Field(..., description="Chunk text content")
    metadata: ChunkMetadata = Field(..., description="Chunk metadata")
    embedding: Optional[list[float]] = Field(None, description="Vector embedding")


class SearchResult(BaseModel):
    """Result from a vector similarity search."""

    chunk: DocumentChunk = Field(..., description="Matched chunk")
    score: float = Field(..., description="Similarity score")


class DeduplicationRecord(BaseModel):
    """Record for tracking duplicate documents across libraries."""

    content_hash: str = Field(..., description="Hash of document content")
    library_id: str = Field(..., description="Library ID")
    item_key: str = Field(..., description="Item key")
    relation_uri: Optional[str] = Field(None, description="owl:sameAs relation URI if present")
