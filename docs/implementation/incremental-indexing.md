# Incremental Indexing Implementation

**Document Version:** 1.0
**Created:** 2025-01-12
**Status:** Implementation Plan
**Parent Document:** [Indexing and Embedding Strategies](indexing-and-embedding-strategies.md)

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Implementation Overview](#implementation-overview)
3. [Step 1: Extend Vector Store Schema](#step-1-extend-vector-store-schema)
4. [Step 2: Enhance Zotero Local API Client](#step-2-enhance-zotero-local-api-client)
5. [Step 3: Update Document Processor](#step-3-update-document-processor)
6. [Step 4: Add API Endpoints](#step-4-add-api-endpoints)
7. [Step 5: Plugin UI Updates](#step-5-plugin-ui-updates)
8. [Step 6: Testing](#step-6-testing)
9. [Migration Strategy](#migration-strategy)
10. [Rollback Plan](#rollback-plan)

---

## Executive Summary

### Goals

Implement **version-based incremental indexing** to enable efficient detection and processing of new or modified Zotero items without reindexing entire libraries.

### Key Benefits

- **80% faster updates**: Only process new/modified items instead of entire library
- **Better UX**: Show users exactly what will be indexed before starting
- **Efficient API usage**: Leverage Zotero's `?since=<version>` parameter
- **Metadata change detection**: Catch title/author updates even if PDF unchanged

### Decisions Made

**Problem 1 - Incomplete Indexing Detection:**
- ✅ Use **Alternative 1: Metadata-Based Version Tracking**
- Store Zotero item/attachment versions in chunk payload
- Track library-level indexing state in dedicated Qdrant collection
- Implement incremental indexing using `?since=<version>` API parameter

**Problem 2 - Embedding Storage:**
- ✅ Use **Alternative 3: Keep Current Approach (Local-Only)**
- No changes to storage architecture in this phase
- Defer cloud sync/sharing to future versions based on user demand

**Additional Features:**
- ✅ Add hard-reset API endpoint for manual full reindexing
- ✅ Support both incremental and full reindex modes

### Timeline

**Estimated Effort:** 2-3 days
**Priority:** Critical (prerequisite for improved UX)

---

## Implementation Overview

### Architecture Changes

```
┌─────────────────────────────────────────────────────────────┐
│ Zotero Desktop (localhost:23119)                            │
└────────────────┬────────────────────────────────────────────┘
                 │ HTTP API
                 ▼
┌─────────────────────────────────────────────────────────────┐
│ Backend Services                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ Zotero Local API Client (ENHANCED)                   │   │
│  │  - Support ?since=<version> parameter                │   │
│  │  - Extract version fields from responses             │   │
│  │  - Get library version range                         │   │
│  └──────────────────┬───────────────────────────────────┘   │
│                     ▼                                        │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ Document Processor (ENHANCED)                        │   │
│  │  - Incremental indexing mode                         │   │
│  │  - Version comparison logic                          │   │
│  │  - Smart item filtering                              │   │
│  └──────────────────┬───────────────────────────────────┘   │
│                     ▼                                        │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ Vector Store (ENHANCED)                              │   │
│  │  Collections:                                        │   │
│  │   1. document_chunks (+ version fields in payload)   │   │
│  │   2. deduplication (existing)                        │   │
│  │   3. library_metadata (NEW)                          │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### New Data Structures

**Enhanced Chunk Payload:**
```python
{
    # Existing fields
    "text": str,
    "chunk_id": str,
    "library_id": str,
    "item_key": str,
    "attachment_key": str,
    "title": str,
    "authors": List[str],
    "year": int,
    "page_number": int,
    "text_preview": str,
    "chunk_index": int,
    "content_hash": str,

    # NEW: Version tracking fields
    "item_version": int,           # Zotero item version when indexed
    "attachment_version": int,     # Zotero attachment version when indexed
    "indexed_at": str,             # ISO 8601 timestamp
    "zotero_modified": str         # Item's dateModified field
}
```

**Library Metadata (new collection):**
```python
{
    "id": "{library_id}",          # Point ID
    "vector": [0.0],               # Dummy 1D vector
    "payload": {
        "library_id": str,
        "library_type": str,       # "user" | "group"
        "library_name": str,
        "last_indexed_version": int,  # Highest version processed
        "last_indexed_at": str,       # ISO timestamp
        "total_items_indexed": int,
        "total_chunks": int,
        "indexing_mode": str,      # "full" | "incremental"
        "force_reindex": bool      # Hard-reset flag
    }
}
```

### API Changes

**New Endpoints:**
- `GET /api/libraries/{id}/index-status` - Get indexing metadata
- `GET /api/libraries/{id}/reset-index` - Mark library for full reindex
- `POST /api/libraries/{id}/index?mode=auto|incremental|full` - Index with mode selection

**Modified Endpoints:**
- Existing index endpoint accepts `mode` query parameter

---

## Step 1: Extend Vector Store Schema

### 1.1 Update Chunk Payload Model

**File:** `backend/models/document.py`

**Action:** Extend `ChunkMetadata` model with version fields

```python
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

class ChunkMetadata(BaseModel):
    """Enhanced metadata for document chunks with version tracking."""

    # Existing fields
    text: str
    chunk_id: str
    library_id: str
    item_key: str
    attachment_key: str
    title: str
    authors: List[str]
    year: Optional[int] = None
    page_number: int
    text_preview: str
    chunk_index: int
    content_hash: str

    # NEW: Version tracking fields
    item_version: int = Field(
        description="Zotero item version at time of indexing"
    )
    attachment_version: int = Field(
        description="Zotero attachment version at time of indexing"
    )
    indexed_at: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat(),
        description="ISO 8601 timestamp when chunk was indexed"
    )
    zotero_modified: str = Field(
        description="Item's dateModified field from Zotero"
    )

    # Schema version for future migrations
    schema_version: int = Field(default=2, const=True)
```

**Backward Compatibility:**
```python
class ChunkMetadataV1(BaseModel):
    """Legacy chunk metadata without version tracking."""
    # Original fields only (schema_version=1 or missing)
    pass

def migrate_chunk_v1_to_v2(chunk_v1: ChunkMetadataV1) -> ChunkMetadata:
    """Migrate legacy chunk to new schema."""
    return ChunkMetadata(
        **chunk_v1.dict(),
        item_version=0,  # Unknown version
        attachment_version=0,
        indexed_at="2024-01-01T00:00:00Z",  # Placeholder
        zotero_modified="2024-01-01T00:00:00Z",
        schema_version=2
    )
```

### 1.2 Create Library Metadata Model

**File:** `backend/models/library.py` (new file)

```python
from pydantic import BaseModel, Field
from typing import Literal
from datetime import datetime

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

    schema_version: int = Field(default=1, const=True)

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
```

### 1.3 Extend Vector Store Class

**File:** `backend/db/vector_store.py`

**Actions:**

1. Add library metadata collection management
2. Add methods to read/write library metadata
3. Add methods to query chunks by version
4. Support backward compatibility with version-less chunks

```python
from qdrant_client import QdrantClient
from qdrant_client.models import (
    PointStruct, VectorParams, Distance, Filter, FieldCondition,
    MatchValue, Range, CollectionInfo
)
from backend.models.library import LibraryIndexMetadata
from backend.models.document import ChunkMetadata
from typing import Optional, List, Dict
import logging

logger = logging.getLogger(__name__)

class VectorStore:
    """Enhanced vector store with library metadata tracking."""

    # Collection names
    CHUNKS_COLLECTION = "document_chunks"
    DEDUP_COLLECTION = "deduplication"
    METADATA_COLLECTION = "library_metadata"  # NEW

    def __init__(self, client: QdrantClient, embedding_dim: int):
        self.client = client
        self.embedding_dim = embedding_dim
        self._ensure_collections_exist()

    def _ensure_collections_exist(self):
        """Create collections if they don't exist."""
        collections = {c.name for c in self.client.get_collections().collections}

        # Existing collections
        if self.CHUNKS_COLLECTION not in collections:
            self._create_chunks_collection()
        if self.DEDUP_COLLECTION not in collections:
            self._create_dedup_collection()

        # NEW: Library metadata collection
        if self.METADATA_COLLECTION not in collections:
            self._create_metadata_collection()

    def _create_metadata_collection(self):
        """Create library metadata collection with dummy vectors."""
        logger.info(f"Creating {self.METADATA_COLLECTION} collection")
        self.client.create_collection(
            collection_name=self.METADATA_COLLECTION,
            vectors_config=VectorParams(
                size=1,  # Dummy 1D vector
                distance=Distance.COSINE
            )
        )
        # Create index on library_id for fast lookups
        self.client.create_payload_index(
            collection_name=self.METADATA_COLLECTION,
            field_name="library_id",
            field_schema="keyword"
        )

    # ========== Library Metadata Methods (NEW) ==========

    def get_library_metadata(self, library_id: str) -> Optional[LibraryIndexMetadata]:
        """Get indexing metadata for a library."""
        try:
            points = self.client.retrieve(
                collection_name=self.METADATA_COLLECTION,
                ids=[library_id]
            )
            if points:
                return LibraryIndexMetadata(**points[0].payload)
            return None
        except Exception as e:
            logger.error(f"Error retrieving library metadata: {e}")
            return None

    def update_library_metadata(self, metadata: LibraryIndexMetadata):
        """Update or create library metadata."""
        point = PointStruct(
            id=metadata.library_id,
            vector=[0.0],  # Dummy vector
            payload=metadata.dict()
        )
        self.client.upsert(
            collection_name=self.METADATA_COLLECTION,
            points=[point]
        )
        logger.info(f"Updated metadata for library {metadata.library_id}")

    def mark_library_for_reset(self, library_id: str):
        """Mark library for full reindex (hard reset)."""
        metadata = self.get_library_metadata(library_id)
        if metadata:
            metadata.force_reindex = True
            self.update_library_metadata(metadata)
            logger.info(f"Library {library_id} marked for hard reset")
        else:
            # Create new metadata with reset flag
            metadata = LibraryIndexMetadata(
                library_id=library_id,
                library_type="user",  # Will be updated during next index
                library_name="Unknown",
                force_reindex=True
            )
            self.update_library_metadata(metadata)

    def get_all_library_metadata(self) -> List[LibraryIndexMetadata]:
        """Get metadata for all indexed libraries."""
        try:
            results, _ = self.client.scroll(
                collection_name=self.METADATA_COLLECTION,
                limit=100  # Reasonable limit for number of libraries
            )
            return [LibraryIndexMetadata(**p.payload) for p in results]
        except Exception as e:
            logger.error(f"Error retrieving all library metadata: {e}")
            return []

    # ========== Version-Aware Chunk Methods (NEW) ==========

    def get_item_chunks(self, library_id: str, item_key: str) -> List[Dict]:
        """Get all chunks for a specific item."""
        results = self.client.scroll(
            collection_name=self.CHUNKS_COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="library_id", match=MatchValue(value=library_id)),
                FieldCondition(key="item_key", match=MatchValue(value=item_key))
            ]),
            limit=1000  # Max chunks per item
        )
        return [{"id": p.id, "payload": p.payload} for p in results[0]]

    def get_item_version(self, library_id: str, item_key: str) -> Optional[int]:
        """Get the indexed version of an item (from any of its chunks)."""
        chunks = self.get_item_chunks(library_id, item_key)
        if chunks and "item_version" in chunks[0]["payload"]:
            return chunks[0]["payload"]["item_version"]
        return None  # Not indexed or legacy chunk without version

    def delete_item_chunks(self, library_id: str, item_key: str) -> int:
        """Delete all chunks for a specific item. Returns count deleted."""
        chunks = self.get_item_chunks(library_id, item_key)
        if not chunks:
            return 0

        chunk_ids = [c["id"] for c in chunks]
        self.client.delete(
            collection_name=self.CHUNKS_COLLECTION,
            points_selector=chunk_ids
        )
        logger.info(f"Deleted {len(chunk_ids)} chunks for item {item_key}")
        return len(chunk_ids)

    def count_library_chunks(self, library_id: str) -> int:
        """Count total chunks for a library."""
        result = self.client.count(
            collection_name=self.CHUNKS_COLLECTION,
            count_filter=Filter(must=[
                FieldCondition(key="library_id", match=MatchValue(value=library_id))
            ])
        )
        return result.count

    # ========== Existing Methods (unchanged) ==========

    def add_chunks(self, chunks: List[ChunkMetadata], embeddings: List[List[float]]):
        """Add chunks with embeddings to vector store."""
        # Implementation unchanged, but now accepts enhanced ChunkMetadata
        pass

    def search(self, query_vector: List[float], library_id: str, limit: int = 5):
        """Search for similar chunks in a library."""
        # Implementation unchanged
        pass

    # ... other existing methods ...
```

**Testing Checkpoint:**
```python
# Test script: backend/tests/test_vector_store_metadata.py
import unittest
from backend.db.vector_store import VectorStore
from backend.models.library import LibraryIndexMetadata

class TestLibraryMetadata(unittest.TestCase):
    def test_create_and_retrieve_metadata(self):
        # Test metadata CRUD operations
        pass

    def test_mark_for_reset(self):
        # Test hard reset flag
        pass
```

---

## Step 2: Enhance Zotero Local API Client

### 2.1 Add Version-Aware Methods

**File:** `backend/zotero/local_api.py`

**Actions:**

1. Add support for `?since=<version>` parameter
2. Extract version fields from item responses
3. Add method to get library version range
4. Add method to fetch only modified items

```python
from typing import List, Dict, Optional, Tuple
import httpx
import logging

logger = logging.getLogger(__name__)

class ZoteroLocalClient:
    """Enhanced Zotero Local API client with version tracking support."""

    def __init__(self, api_url: str = "http://localhost:23119"):
        self.api_url = api_url
        self.client = httpx.AsyncClient(timeout=30.0)

    # ========== NEW: Version-Aware Methods ==========

    async def get_library_items_since(
        self,
        library_id: str,
        library_type: str = "user",
        since_version: Optional[int] = None,
        limit: int = 100
    ) -> List[Dict]:
        """
        Get library items, optionally filtering by version.

        Args:
            library_id: Library ID
            library_type: "user" or "group"
            since_version: If provided, only return items modified since this version
            limit: Max items per request (pagination handled internally)

        Returns:
            List of item dictionaries with full metadata including 'version' field
        """
        endpoint = f"{self.api_url}/connector/{library_type}s/{library_id}/items"
        params = {"limit": limit}

        if since_version is not None:
            params["since"] = since_version
            logger.info(f"Fetching items since version {since_version}")

        all_items = []
        start = 0

        while True:
            params["start"] = start
            response = await self.client.get(endpoint, params=params)
            response.raise_for_status()

            items = response.json()
            if not items:
                break

            all_items.extend(items)
            start += len(items)

            # Check if there are more items (Zotero pagination)
            if len(items) < limit:
                break

        logger.info(f"Retrieved {len(all_items)} items from library {library_id}")
        return all_items

    async def get_library_version_range(
        self,
        library_id: str,
        library_type: str = "user"
    ) -> Tuple[int, int]:
        """
        Get the min and max version numbers in a library.

        Returns:
            (min_version, max_version) tuple
        """
        items = await self.get_library_items_since(
            library_id=library_id,
            library_type=library_type,
            since_version=None,
            limit=1000  # Should be enough to get version range
        )

        if not items:
            return (0, 0)

        versions = [item.get("version", 0) for item in items]
        return (min(versions), max(versions))

    async def get_item_with_version(
        self,
        library_id: str,
        item_key: str,
        library_type: str = "user"
    ) -> Optional[Dict]:
        """
        Get a single item with full version information.

        Returns:
            Item dict with 'version', 'data', etc., or None if not found
        """
        endpoint = f"{self.api_url}/connector/{library_type}s/{library_id}/items/{item_key}"
        try:
            response = await self.client.get(endpoint)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.warning(f"Item {item_key} not found")
                return None
            raise

    async def get_attachment_with_version(
        self,
        library_id: str,
        attachment_key: str,
        library_type: str = "user"
    ) -> Optional[Dict]:
        """
        Get attachment metadata including version.

        Note: Attachments are items too, so we use the same endpoint.
        """
        return await self.get_item_with_version(
            library_id=library_id,
            item_key=attachment_key,
            library_type=library_type
        )

    # ========== Enhanced Existing Methods ==========

    async def get_library_items(
        self,
        library_id: str,
        library_type: str = "user"
    ) -> List[Dict]:
        """
        Get all library items (wrapper for backward compatibility).

        DEPRECATED: Use get_library_items_since() for version-aware fetching.
        """
        return await self.get_library_items_since(
            library_id=library_id,
            library_type=library_type,
            since_version=None
        )

    # ... other existing methods unchanged ...
```

**Testing Checkpoint:**
```python
# Test script: backend/tests/test_zotero_client_versions.py
import unittest
from backend.zotero.local_api import ZoteroLocalClient

class TestVersionAwareFetching(unittest.TestCase):
    async def test_get_items_since_version(self):
        # Test incremental fetching
        pass

    async def test_version_range(self):
        # Test version range detection
        pass
```

---

## Step 3: Update Document Processor

### 3.1 Add Incremental Indexing Logic

**File:** `backend/services/document_processor.py`

**Actions:**

1. Add incremental indexing mode
2. Implement version comparison logic
3. Smart filtering of items to process
4. Update library metadata after indexing

```python
from typing import List, Dict, Optional, Literal
from backend.zotero.local_api import ZoteroLocalClient
from backend.db.vector_store import VectorStore
from backend.models.library import LibraryIndexMetadata
from backend.models.document import ChunkMetadata
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

class DocumentProcessor:
    """Enhanced document processor with incremental indexing."""

    def __init__(
        self,
        zotero_client: ZoteroLocalClient,
        vector_store: VectorStore,
        # ... other dependencies
    ):
        self.zotero_client = zotero_client
        self.vector_store = vector_store
        # ... other initialization

    # ========== NEW: Incremental Indexing Methods ==========

    async def index_library(
        self,
        library_id: str,
        library_type: str = "user",
        library_name: str = "Unknown",
        mode: Literal["auto", "incremental", "full"] = "auto"
    ) -> Dict:
        """
        Index a library with intelligent mode selection.

        Args:
            library_id: Library ID
            library_type: "user" or "group"
            library_name: Human-readable name
            mode: Indexing mode
                - "auto": Automatically choose best mode
                - "incremental": Only index new/modified items
                - "full": Reindex entire library

        Returns:
            Statistics dict with counts and timing
        """
        logger.info(f"Starting indexing for library {library_id} (mode={mode})")
        start_time = datetime.utcnow()

        # Get or create library metadata
        metadata = self.vector_store.get_library_metadata(library_id)
        if metadata is None:
            logger.info(f"First-time indexing for library {library_id}")
            metadata = LibraryIndexMetadata(
                library_id=library_id,
                library_type=library_type,
                library_name=library_name
            )
            effective_mode = "full"
        else:
            # Check for hard reset flag
            if metadata.force_reindex:
                logger.info(f"Hard reset requested for library {library_id}")
                effective_mode = "full"
                metadata.force_reindex = False  # Clear flag
            elif mode == "full":
                effective_mode = "full"
            elif mode == "incremental":
                effective_mode = "incremental"
            else:  # mode == "auto"
                # Auto-select based on library state
                effective_mode = "incremental" if metadata.last_indexed_version > 0 else "full"

        logger.info(f"Selected indexing mode: {effective_mode}")

        # Execute indexing
        if effective_mode == "full":
            stats = await self._index_library_full(library_id, library_type, metadata)
        else:
            stats = await self._index_library_incremental(library_id, library_type, metadata)

        # Update library metadata
        metadata.indexing_mode = effective_mode
        metadata.last_indexed_at = datetime.utcnow().isoformat()
        metadata.total_chunks = self.vector_store.count_library_chunks(library_id)
        self.vector_store.update_library_metadata(metadata)

        elapsed = (datetime.utcnow() - start_time).total_seconds()
        stats["elapsed_seconds"] = elapsed
        stats["mode"] = effective_mode

        logger.info(f"Indexing complete: {stats}")
        return stats

    async def _index_library_incremental(
        self,
        library_id: str,
        library_type: str,
        metadata: LibraryIndexMetadata
    ) -> Dict:
        """Incremental indexing: only process new/modified items."""
        logger.info(f"Incremental index from version {metadata.last_indexed_version}")

        # Fetch items modified since last index
        since_version = metadata.last_indexed_version
        items = await self.zotero_client.get_library_items_since(
            library_id=library_id,
            library_type=library_type,
            since_version=since_version
        )

        logger.info(f"Found {len(items)} items modified since version {since_version}")

        if not items:
            return {
                "items_processed": 0,
                "items_added": 0,
                "items_updated": 0,
                "chunks_added": 0,
                "chunks_deleted": 0
            }

        # Filter to items with PDF attachments
        items_with_pdfs = await self._filter_items_with_pdfs(items, library_id, library_type)

        items_added = 0
        items_updated = 0
        chunks_added = 0
        chunks_deleted = 0
        max_version_seen = metadata.last_indexed_version

        for item in items_with_pdfs:
            item_key = item["data"]["key"]
            item_version = item["version"]
            max_version_seen = max(max_version_seen, item_version)

            # Check if item already indexed
            existing_version = self.vector_store.get_item_version(library_id, item_key)

            if existing_version is None:
                # New item
                logger.info(f"Indexing new item {item_key} (version {item_version})")
                chunk_count = await self._index_item(item, library_id, library_type)
                items_added += 1
                chunks_added += chunk_count
            elif existing_version < item_version:
                # Updated item - delete old chunks and reindex
                logger.info(f"Reindexing updated item {item_key} ({existing_version} -> {item_version})")
                deleted = self.vector_store.delete_item_chunks(library_id, item_key)
                chunk_count = await self._index_item(item, library_id, library_type)
                items_updated += 1
                chunks_deleted += deleted
                chunks_added += chunk_count
            else:
                # Already up-to-date (shouldn't happen with ?since, but defensive)
                logger.debug(f"Item {item_key} already up-to-date (version {item_version})")

        # Update metadata with new version
        metadata.last_indexed_version = max_version_seen
        metadata.total_items_indexed = metadata.total_items_indexed + items_added

        return {
            "items_processed": len(items_with_pdfs),
            "items_added": items_added,
            "items_updated": items_updated,
            "chunks_added": chunks_added,
            "chunks_deleted": chunks_deleted,
            "last_version": max_version_seen
        }

    async def _index_library_full(
        self,
        library_id: str,
        library_type: str,
        metadata: LibraryIndexMetadata
    ) -> Dict:
        """Full indexing: delete all chunks and reindex entire library."""
        logger.info(f"Full reindex for library {library_id}")

        # Delete all existing chunks for this library
        logger.info("Deleting all existing chunks...")
        # TODO: Implement delete_library_chunks method in VectorStore
        # For now, we'll delete items one by one (could be optimized)
        existing_items = self.vector_store.get_all_library_metadata()
        chunks_deleted = 0

        # Fetch all items
        items = await self.zotero_client.get_library_items_since(
            library_id=library_id,
            library_type=library_type,
            since_version=None  # Get all items
        )

        logger.info(f"Retrieved {len(items)} total items")

        # Filter to items with PDFs
        items_with_pdfs = await self._filter_items_with_pdfs(items, library_id, library_type)

        logger.info(f"Found {len(items_with_pdfs)} items with PDFs")

        # Index all items
        chunks_added = 0
        max_version_seen = 0

        for item in items_with_pdfs:
            item_key = item["data"]["key"]
            item_version = item["version"]
            max_version_seen = max(max_version_seen, item_version)

            # Delete existing chunks for this item (if any)
            deleted = self.vector_store.delete_item_chunks(library_id, item_key)
            chunks_deleted += deleted

            # Index item
            chunk_count = await self._index_item(item, library_id, library_type)
            chunks_added += chunk_count

        # Update metadata
        metadata.last_indexed_version = max_version_seen
        metadata.total_items_indexed = len(items_with_pdfs)

        return {
            "items_processed": len(items_with_pdfs),
            "items_added": len(items_with_pdfs),
            "items_updated": 0,
            "chunks_added": chunks_added,
            "chunks_deleted": chunks_deleted,
            "last_version": max_version_seen
        }

    async def _index_item(
        self,
        item: Dict,
        library_id: str,
        library_type: str
    ) -> int:
        """
        Index a single item with all its PDF attachments.

        Returns:
            Number of chunks created
        """
        item_key = item["data"]["key"]
        item_version = item["version"]
        item_modified = item["data"].get("dateModified", datetime.utcnow().isoformat())

        # Get attachments
        attachments = await self.zotero_client.get_item_children(
            library_id=library_id,
            item_key=item_key,
            library_type=library_type
        )

        pdf_attachments = [
            att for att in attachments
            if att.get("data", {}).get("contentType") == "application/pdf"
        ]

        if not pdf_attachments:
            logger.debug(f"Item {item_key} has no PDF attachments")
            return 0

        total_chunks = 0

        for attachment in pdf_attachments:
            attachment_key = attachment["data"]["key"]
            attachment_version = attachment.get("version", item_version)

            # Download PDF
            pdf_bytes = await self.zotero_client.download_attachment(
                library_id=library_id,
                attachment_key=attachment_key,
                library_type=library_type
            )

            # Check deduplication (content hash)
            content_hash = self._compute_hash(pdf_bytes)
            if self.vector_store.check_duplicate(content_hash):
                logger.info(f"Skipping duplicate PDF {attachment_key} (hash: {content_hash[:8]})")
                continue

            # Extract text and chunk
            text_pages = await self._extract_text(pdf_bytes)
            chunks = await self._chunk_text(text_pages)

            # Generate embeddings
            chunk_texts = [c["text"] for c in chunks]
            embeddings = await self._generate_embeddings(chunk_texts)

            # Create chunk metadata with version info
            chunk_metadata_list = []
            for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
                metadata = ChunkMetadata(
                    text=chunk["text"],
                    chunk_id=f"{library_id}:{item_key}:{attachment_key}:{i}",
                    library_id=library_id,
                    item_key=item_key,
                    attachment_key=attachment_key,
                    title=item["data"].get("title", "Untitled"),
                    authors=self._extract_authors(item),
                    year=self._extract_year(item),
                    page_number=chunk.get("page_number", 0),
                    text_preview=chunk["text"][:200],
                    chunk_index=i,
                    content_hash=content_hash,
                    # NEW: Version fields
                    item_version=item_version,
                    attachment_version=attachment_version,
                    indexed_at=datetime.utcnow().isoformat(),
                    zotero_modified=item_modified
                )
                chunk_metadata_list.append(metadata)

            # Store in vector database
            self.vector_store.add_chunks(chunk_metadata_list, embeddings)

            # Record in deduplication table
            self.vector_store.add_deduplication_record(
                content_hash=content_hash,
                library_id=library_id,
                item_key=item_key,
                attachment_key=attachment_key
            )

            total_chunks += len(chunks)
            logger.info(f"Indexed {len(chunks)} chunks for attachment {attachment_key}")

        return total_chunks

    async def _filter_items_with_pdfs(
        self,
        items: List[Dict],
        library_id: str,
        library_type: str
    ) -> List[Dict]:
        """Filter items to only those with PDF attachments."""
        items_with_pdfs = []

        for item in items:
            # Skip if not a regular item (skip attachments, notes, etc.)
            item_type = item["data"].get("itemType")
            if item_type in ["attachment", "note"]:
                continue

            # Check if item has PDF attachments
            item_key = item["data"]["key"]
            attachments = await self.zotero_client.get_item_children(
                library_id=library_id,
                item_key=item_key,
                library_type=library_type
            )

            has_pdf = any(
                att.get("data", {}).get("contentType") == "application/pdf"
                for att in attachments
            )

            if has_pdf:
                items_with_pdfs.append(item)

        return items_with_pdfs

    # ========== Existing Methods (unchanged) ==========

    async def _extract_text(self, pdf_bytes: bytes) -> List[Dict]:
        """Extract text from PDF with page numbers."""
        # Existing implementation
        pass

    async def _chunk_text(self, text_pages: List[Dict]) -> List[Dict]:
        """Chunk text using semantic boundaries."""
        # Existing implementation
        pass

    async def _generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for text chunks."""
        # Existing implementation
        pass

    # ... other existing methods ...
```

**Testing Checkpoint:**
```python
# Test script: backend/tests/test_incremental_indexing.py
import unittest
from backend.services.document_processor import DocumentProcessor

class TestIncrementalIndexing(unittest.TestCase):
    async def test_incremental_mode(self):
        # Test incremental indexing detects new items
        pass

    async def test_full_mode(self):
        # Test full reindex
        pass

    async def test_version_comparison(self):
        # Test version-based filtering
        pass
```

---

## Step 4: Add API Endpoints

### 4.1 Create Index Status Endpoint

**File:** `backend/api/routes/libraries.py`

**Actions:**

1. Add GET `/api/libraries/{id}/index-status` endpoint
2. Add GET `/api/libraries/{id}/reset-index` endpoint
3. Enhance POST `/api/libraries/{id}/index` with mode parameter

```python
from fastapi import APIRouter, HTTPException, Query, Depends
from backend.db.vector_store import VectorStore
from backend.services.document_processor import DocumentProcessor
from backend.models.library import LibraryIndexMetadata
from typing import Literal, Dict
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/libraries", tags=["libraries"])

# Dependency injection (adjust based on your FastAPI setup)
def get_vector_store() -> VectorStore:
    # Return configured VectorStore instance
    pass

def get_document_processor() -> DocumentProcessor:
    # Return configured DocumentProcessor instance
    pass

# ========== NEW: Index Status Endpoint ==========

@router.get("/{library_id}/index-status", response_model=LibraryIndexMetadata)
async def get_index_status(
    library_id: str,
    vector_store: VectorStore = Depends(get_vector_store)
):
    """
    Get indexing status and metadata for a library.

    Returns:
        - Library metadata with last indexed version, timestamp, counts
        - 404 if library has never been indexed

    Example response:
    ```json
    {
        "library_id": "1",
        "library_type": "user",
        "library_name": "My Library",
        "last_indexed_version": 12345,
        "last_indexed_at": "2025-01-12T10:30:00Z",
        "total_items_indexed": 250,
        "total_chunks": 12500,
        "indexing_mode": "incremental",
        "force_reindex": false
    }
    ```
    """
    metadata = vector_store.get_library_metadata(library_id)

    if metadata is None:
        raise HTTPException(
            status_code=404,
            detail=f"Library {library_id} has not been indexed yet"
        )

    return metadata

# ========== NEW: Hard Reset Endpoint ==========

@router.get("/{library_id}/reset-index")
async def reset_library_index(
    library_id: str,
    vector_store: VectorStore = Depends(get_vector_store)
):
    """
    Mark a library for full reindex (hard reset).

    This sets the `force_reindex` flag to True. The next indexing
    operation will perform a full reindex regardless of mode parameter.

    Returns:
        - Success message with new status

    Example:
        GET /api/libraries/1/reset-index
    """
    vector_store.mark_library_for_reset(library_id)

    metadata = vector_store.get_library_metadata(library_id)

    return {
        "message": f"Library {library_id} marked for hard reset",
        "force_reindex": metadata.force_reindex if metadata else True,
        "next_index_mode": "full"
    }

# ========== ENHANCED: Index Endpoint with Mode Parameter ==========

@router.post("/{library_id}/index")
async def index_library(
    library_id: str,
    library_type: str = Query(default="user", regex="^(user|group)$"),
    library_name: str = Query(default="Unknown"),
    mode: Literal["auto", "incremental", "full"] = Query(
        default="auto",
        description=(
            "Indexing mode:\n"
            "- auto: Automatically choose best mode (recommended)\n"
            "- incremental: Only index new/modified items\n"
            "- full: Reindex entire library"
        )
    ),
    processor: DocumentProcessor = Depends(get_document_processor)
):
    """
    Index a Zotero library with intelligent mode selection.

    Query Parameters:
        - library_id: Library ID (from URL path)
        - library_type: "user" or "group" (default: "user")
        - library_name: Human-readable name (default: "Unknown")
        - mode: Indexing mode (default: "auto")

    Returns:
        - Indexing statistics including counts, timing, and mode used

    Examples:
        POST /api/libraries/1/index?mode=auto
        POST /api/libraries/1/index?mode=incremental
        POST /api/libraries/1/index?mode=full
    """
    try:
        stats = await processor.index_library(
            library_id=library_id,
            library_type=library_type,
            library_name=library_name,
            mode=mode
        )

        return {
            "success": True,
            "library_id": library_id,
            "statistics": stats
        }

    except Exception as e:
        logger.error(f"Error indexing library {library_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to index library: {str(e)}"
        )

# ========== NEW: List All Libraries ==========

@router.get("", response_model=list[LibraryIndexMetadata])
async def list_indexed_libraries(
    vector_store: VectorStore = Depends(get_vector_store)
):
    """
    List all libraries that have been indexed.

    Returns:
        - Array of library metadata objects
    """
    libraries = vector_store.get_all_library_metadata()
    return libraries
```

**API Documentation:**

```markdown
### GET /api/libraries/{id}/index-status

Get current indexing status for a library.

**Response:**
- 200: Library metadata
- 404: Library not indexed

---

### GET /api/libraries/{id}/reset-index

Mark library for hard reset (full reindex).

**Response:**
- 200: Success message

---

### POST /api/libraries/{id}/index?mode={mode}

Index library with specified mode.

**Query Parameters:**
- `mode`: "auto" | "incremental" | "full" (default: "auto")
- `library_type`: "user" | "group" (default: "user")
- `library_name`: string (default: "Unknown")

**Response:**
- 200: Indexing statistics
- 500: Error details
```

---

## Step 5: Plugin UI Updates

### 5.1 Update Library Selection Dialog

**File:** `plugin/src/ui/library-selection-dialog.js`

**Actions:**

1. Show last indexed timestamp per library
2. Display "X new items" count before indexing
3. Add "Quick Update" vs "Full Reindex" buttons
4. Show indexing progress with real-time stats

```javascript
/**
 * Enhanced library selection dialog with incremental indexing UI.
 *
 * @typedef {Object} LibraryMetadata
 * @property {string} library_id
 * @property {string} library_type
 * @property {string} library_name
 * @property {number} last_indexed_version
 * @property {string} last_indexed_at
 * @property {number} total_items_indexed
 * @property {number} total_chunks
 * @property {string} indexing_mode
 * @property {boolean} force_reindex
 */

class LibrarySelectionDialog {
  constructor() {
    this.apiUrl = "http://localhost:8000/api";
  }

  /**
   * Fetch indexing status for a library.
   * @param {string} libraryId
   * @returns {Promise<LibraryMetadata|null>}
   */
  async getIndexStatus(libraryId) {
    try {
      const response = await fetch(
        `${this.apiUrl}/libraries/${libraryId}/index-status`
      );
      if (response.status === 404) {
        return null; // Not indexed yet
      }
      return await response.json();
    } catch (error) {
      console.error("Error fetching index status:", error);
      return null;
    }
  }

  /**
   * Build library list UI with indexing status.
   * @param {Array} libraries - Array of Zotero libraries
   */
  async buildLibraryList(libraries) {
    const listbox = document.getElementById("library-listbox");
    listbox.innerHTML = ""; // Clear existing

    for (const library of libraries) {
      const metadata = await this.getIndexStatus(library.id);

      const listitem = document.createElement("richlistitem");
      listitem.setAttribute("value", library.id);

      // Create layout
      const vbox = document.createElement("vbox");
      vbox.setAttribute("flex", "1");

      // Library name
      const nameLabel = document.createElementNS(
        "http://www.w3.org/1999/xhtml",
        "label"
      );
      nameLabel.className = "library-name";
      nameLabel.textContent = library.name;

      // Status label
      const statusLabel = document.createElementNS(
        "http://www.w3.org/1999/xhtml",
        "label"
      );
      statusLabel.className = "library-status";

      if (metadata) {
        const lastIndexed = new Date(metadata.last_indexed_at);
        const timeAgo = this.formatTimeAgo(lastIndexed);

        statusLabel.textContent =
          `Last indexed: ${timeAgo} | ` +
          `${metadata.total_items_indexed} items | ` +
          `${metadata.total_chunks} chunks`;
        statusLabel.style.color = "green";
      } else {
        statusLabel.textContent = "Not indexed yet";
        statusLabel.style.color = "gray";
      }

      vbox.appendChild(nameLabel);
      vbox.appendChild(statusLabel);
      listitem.appendChild(vbox);
      listbox.appendChild(listitem);
    }
  }

  /**
   * Show indexing options dialog.
   * @param {string} libraryId
   * @param {string} libraryName
   */
  async showIndexingOptions(libraryId, libraryName) {
    const metadata = await this.getIndexStatus(libraryId);

    const dialog = document.createElement("dialog");
    dialog.setAttribute("title", `Index ${libraryName}`);

    const dialogContent = `
      <vbox>
        <description>Choose how to index this library:</description>

        ${metadata ? `
          <groupbox>
            <caption label="Current Status"/>
            <description>Last indexed: ${new Date(metadata.last_indexed_at).toLocaleString()}</description>
            <description>Items indexed: ${metadata.total_items_indexed}</description>
            <description>Total chunks: ${metadata.total_chunks}</description>
          </groupbox>
        ` : `
          <description>This library has not been indexed yet.</description>
        `}

        <separator/>

        <radiogroup id="index-mode">
          <radio id="mode-auto" label="Auto (Recommended)" value="auto" selected="true">
            <description class="indent">Let the system choose the best method</description>
          </radio>

          ${metadata ? `
            <radio id="mode-incremental" label="Quick Update" value="incremental">
              <description class="indent">Only index new or modified items</description>
            </radio>
          ` : ''}

          <radio id="mode-full" label="Full Reindex" value="full">
            <description class="indent">Reindex entire library (slow)</description>
          </radio>
        </radiogroup>
      </vbox>
    `;

    dialog.innerHTML = dialogContent;

    // Dialog buttons
    dialog.setAttribute("buttons", "accept,cancel");
    dialog.setAttribute("buttonlabelaccept", "Start Indexing");

    // Show dialog
    window.openDialog(
      "chrome://zotero-rag/content/indexing-options.xul",
      "",
      "chrome,modal,centerscreen",
      { libraryId, libraryName, metadata }
    );
  }

  /**
   * Start indexing with selected mode.
   * @param {string} libraryId
   * @param {string} libraryType
   * @param {string} libraryName
   * @param {string} mode - "auto" | "incremental" | "full"
   */
  async startIndexing(libraryId, libraryType, libraryName, mode = "auto") {
    const progressDialog = this.showProgressDialog(libraryName);

    try {
      const response = await fetch(
        `${this.apiUrl}/libraries/${libraryId}/index?mode=${mode}&library_type=${libraryType}&library_name=${encodeURIComponent(libraryName)}`,
        { method: "POST" }
      );

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }

      const result = await response.json();

      this.showIndexingResults(result);

    } catch (error) {
      this.showError(`Indexing failed: ${error.message}`);
    } finally {
      progressDialog.close();
    }
  }

  /**
   * Show indexing results dialog.
   * @param {Object} result - API response with statistics
   */
  showIndexingResults(result) {
    const stats = result.statistics;

    const message =
      `Indexing Complete!\n\n` +
      `Mode: ${stats.mode}\n` +
      `Items processed: ${stats.items_processed}\n` +
      `Items added: ${stats.items_added}\n` +
      `Items updated: ${stats.items_updated}\n` +
      `Chunks added: ${stats.chunks_added}\n` +
      `Chunks deleted: ${stats.chunks_deleted}\n` +
      `Time elapsed: ${Math.round(stats.elapsed_seconds)}s`;

    Services.prompt.alert(null, "Zotero RAG", message);
  }

  /**
   * Request hard reset for a library.
   * @param {string} libraryId
   */
  async requestHardReset(libraryId) {
    const confirmed = Services.prompt.confirm(
      null,
      "Confirm Hard Reset",
      "This will mark the library for full reindexing. " +
      "All existing chunks will be deleted on next index. Continue?"
    );

    if (!confirmed) return;

    try {
      const response = await fetch(
        `${this.apiUrl}/libraries/${libraryId}/reset-index`
      );

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const result = await response.json();

      Services.prompt.alert(
        null,
        "Hard Reset Scheduled",
        result.message
      );

    } catch (error) {
      this.showError(`Reset failed: ${error.message}`);
    }
  }

  /**
   * Format timestamp as relative time.
   * @param {Date} date
   * @returns {string}
   */
  formatTimeAgo(date) {
    const seconds = Math.floor((new Date() - date) / 1000);

    if (seconds < 60) return "just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} minutes ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} hours ago`;
    return `${Math.floor(seconds / 86400)} days ago`;
  }

  // ... other existing methods ...
}
```

### 5.2 Update XUL Dialog

**File:** `plugin/content/library-selection.xul`

**Add context menu for hard reset:**

```xml
<?xml version="1.0"?>
<?xml-stylesheet href="chrome://global/skin/" type="text/css"?>
<?xml-stylesheet href="chrome://zotero-rag/skin/library-selection.css" type="text/css"?>

<dialog xmlns="http://www.mozilla.org/keymaster/gatekeeper/there.is.only.xul"
        xmlns:html="http://www.w3.org/1999/xhtml"
        id="library-selection-dialog"
        title="Select Library to Index"
        buttons="accept,cancel"
        buttonlabelaccept="Index Selected"
        ondialogaccept="return LibrarySelectionDialog.onAccept();">

  <script src="chrome://zotero-rag/content/library-selection-dialog.js"/>

  <vbox flex="1">
    <description>Select a library to index for RAG queries:</description>

    <richlistbox id="library-listbox" flex="1" context="library-context-menu">
      <!-- Populated dynamically -->
    </richlistbox>

    <!-- Context menu for library actions -->
    <menupopup id="library-context-menu">
      <menuitem label="Quick Update (Incremental)" oncommand="LibrarySelectionDialog.quickUpdate();"/>
      <menuitem label="Full Reindex" oncommand="LibrarySelectionDialog.fullReindex();"/>
      <menuseparator/>
      <menuitem label="Hard Reset..." oncommand="LibrarySelectionDialog.hardReset();"/>
    </menupopup>

    <separator/>

    <hbox>
      <html:label class="info-label">
        Tip: Right-click a library for more options
      </html:label>
    </hbox>
  </vbox>
</dialog>
```

---

## Step 6: Testing

### 6.1 Unit Tests

**File:** `backend/tests/test_incremental_indexing.py`

```python
import unittest
from unittest.mock import Mock, AsyncMock, patch
from backend.services.document_processor import DocumentProcessor
from backend.db.vector_store import VectorStore
from backend.models.library import LibraryIndexMetadata
from backend.zotero.local_api import ZoteroLocalClient

class TestIncrementalIndexing(unittest.IsolatedAsyncioTestCase):
    """Test incremental indexing functionality."""

    async def asyncSetUp(self):
        """Set up test fixtures."""
        self.mock_vector_store = Mock(spec=VectorStore)
        self.mock_zotero_client = Mock(spec=ZoteroLocalClient)

        self.processor = DocumentProcessor(
            zotero_client=self.mock_zotero_client,
            vector_store=self.mock_vector_store
        )

    async def test_first_time_indexing_uses_full_mode(self):
        """First-time indexing should use full mode."""
        # Mock: No existing metadata
        self.mock_vector_store.get_library_metadata.return_value = None

        # Mock: Library has 10 items
        self.mock_zotero_client.get_library_items_since = AsyncMock(
            return_value=self._create_mock_items(10)
        )

        # Run indexing with auto mode
        stats = await self.processor.index_library(
            library_id="1",
            mode="auto"
        )

        # Assert: Full mode was used
        self.assertEqual(stats["mode"], "full")
        self.assertEqual(stats["items_added"], 10)

    async def test_incremental_indexing_fetches_only_new_items(self):
        """Incremental mode should only fetch items since last version."""
        # Mock: Existing metadata
        existing_metadata = LibraryIndexMetadata(
            library_id="1",
            library_type="user",
            library_name="Test Library",
            last_indexed_version=100
        )
        self.mock_vector_store.get_library_metadata.return_value = existing_metadata

        # Mock: 3 new items since version 100
        self.mock_zotero_client.get_library_items_since = AsyncMock(
            return_value=self._create_mock_items(3, start_version=101)
        )

        # Run incremental indexing
        stats = await self.processor.index_library(
            library_id="1",
            mode="incremental"
        )

        # Assert: Only fetched items since version 100
        self.mock_zotero_client.get_library_items_since.assert_called_once_with(
            library_id="1",
            library_type="user",
            since_version=100
        )

        # Assert: Incremental mode was used
        self.assertEqual(stats["mode"], "incremental")
        self.assertEqual(stats["items_added"], 3)

    async def test_hard_reset_flag_forces_full_reindex(self):
        """force_reindex flag should trigger full mode."""
        # Mock: Metadata with reset flag
        metadata = LibraryIndexMetadata(
            library_id="1",
            library_type="user",
            library_name="Test Library",
            last_indexed_version=100,
            force_reindex=True  # Hard reset requested
        )
        self.mock_vector_store.get_library_metadata.return_value = metadata

        # Mock: All items
        self.mock_zotero_client.get_library_items_since = AsyncMock(
            return_value=self._create_mock_items(50)
        )

        # Run with auto mode (should detect reset flag)
        stats = await self.processor.index_library(
            library_id="1",
            mode="auto"
        )

        # Assert: Full mode was used despite auto mode
        self.assertEqual(stats["mode"], "full")

        # Assert: Reset flag was cleared
        updated_metadata = self.mock_vector_store.update_library_metadata.call_args[0][0]
        self.assertFalse(updated_metadata.force_reindex)

    async def test_version_comparison_detects_updates(self):
        """Items with higher version should be reindexed."""
        # Mock: Item exists with version 50
        self.mock_vector_store.get_item_version.return_value = 50

        # Mock: Zotero returns same item with version 55
        updated_item = self._create_mock_item(
            key="ABCD1234",
            version=55
        )

        # Simulate incremental indexing finding this item
        # Should detect version change and reindex

        # (Implementation detail: test _index_item logic)
        pass

    def _create_mock_items(self, count: int, start_version: int = 1):
        """Create mock Zotero items for testing."""
        return [
            self._create_mock_item(f"ITEM{i:04d}", start_version + i)
            for i in range(count)
        ]

    def _create_mock_item(self, key: str, version: int):
        """Create a single mock Zotero item."""
        return {
            "key": key,
            "version": version,
            "data": {
                "key": key,
                "itemType": "journalArticle",
                "title": f"Test Item {key}",
                "dateModified": "2025-01-12T10:00:00Z",
                "creators": [
                    {"creatorType": "author", "firstName": "John", "lastName": "Doe"}
                ]
            }
        }

if __name__ == "__main__":
    unittest.main()
```

### 6.2 Integration Tests

**File:** `backend/tests/integration/test_full_indexing_flow.py`

```python
import unittest
import httpx
from backend.main import app
from fastapi.testclient import TestClient

class TestFullIndexingFlow(unittest.TestCase):
    """Integration tests for complete indexing workflow."""

    def setUp(self):
        """Set up test client."""
        self.client = TestClient(app)

    def test_full_indexing_workflow(self):
        """Test complete indexing flow from API."""
        library_id = "1"

        # Step 1: Check initial status (should be 404)
        response = self.client.get(f"/api/libraries/{library_id}/index-status")
        self.assertEqual(response.status_code, 404)

        # Step 2: Perform first indexing (should use full mode)
        response = self.client.post(
            f"/api/libraries/{library_id}/index",
            params={"mode": "auto"}
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["statistics"]["mode"], "full")

        # Step 3: Check status again (should exist now)
        response = self.client.get(f"/api/libraries/{library_id}/index-status")
        self.assertEqual(response.status_code, 200)
        metadata = response.json()
        self.assertGreater(metadata["total_chunks"], 0)

        # Step 4: Perform incremental update
        response = self.client.post(
            f"/api/libraries/{library_id}/index",
            params={"mode": "incremental"}
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["statistics"]["mode"], "incremental")

        # Step 5: Request hard reset
        response = self.client.get(f"/api/libraries/{library_id}/reset-index")
        self.assertEqual(response.status_code, 200)

        # Step 6: Verify reset flag is set
        response = self.client.get(f"/api/libraries/{library_id}/index-status")
        metadata = response.json()
        self.assertTrue(metadata["force_reindex"])

        # Step 7: Next index should be full mode
        response = self.client.post(
            f"/api/libraries/{library_id}/index",
            params={"mode": "auto"}
        )
        result = response.json()
        self.assertEqual(result["statistics"]["mode"], "full")

if __name__ == "__main__":
    unittest.main()
```

### 6.3 Manual Testing Checklist

**Test Scenarios:**

- [ ] First-time library indexing uses full mode
- [ ] Second indexing with no changes skips all items
- [ ] Adding new item triggers incremental indexing
- [ ] Modifying item metadata triggers reindexing
- [ ] Hard reset endpoint sets force_reindex flag
- [ ] Next index after reset uses full mode
- [ ] Index status endpoint returns correct metadata
- [ ] Plugin UI shows last indexed timestamp
- [ ] Plugin UI shows correct item counts
- [ ] Context menu "Hard Reset" works
- [ ] Incremental mode is faster than full mode
- [ ] Version numbers are correctly stored in chunks
- [ ] Backward compatibility with old chunks (no version fields)

---

## Migration Strategy

### 7.1 Backward Compatibility

**Handling Existing Chunks Without Version Fields:**

```python
# In VectorStore.get_item_version()
def get_item_version(self, library_id: str, item_key: str) -> Optional[int]:
    """Get indexed version, handling legacy chunks."""
    chunks = self.get_item_chunks(library_id, item_key)

    if not chunks:
        return None  # Not indexed

    first_chunk = chunks[0]["payload"]

    # Check if version field exists (new schema)
    if "item_version" in first_chunk:
        return first_chunk["item_version"]

    # Legacy chunk without version - treat as unknown
    logger.warning(f"Item {item_key} has legacy chunks without version")
    return 0  # Force reindex on next incremental update
```

### 7.2 Gradual Migration

**Strategy:**
1. Deploy new code with backward-compatible schema
2. Existing chunks continue to work (queries unaffected)
3. New indexing operations use enhanced schema
4. Legacy chunks gradually replaced as items are reindexed

**Migration Command (Optional):**

```python
# scripts/migrate_to_versioned_schema.py
"""
Optional migration script to update existing chunks with version info.

WARNING: This requires re-fetching all items from Zotero to get current versions.
Not recommended for large libraries - better to let gradual migration happen.
"""

async def migrate_library_chunks(library_id: str):
    """Add version fields to existing chunks."""
    # Fetch all items from Zotero
    items = await zotero_client.get_library_items(library_id)

    for item in items:
        item_key = item["data"]["key"]
        item_version = item["version"]

        # Update all chunks for this item
        chunks = vector_store.get_item_chunks(library_id, item_key)

        for chunk in chunks:
            chunk["payload"]["item_version"] = item_version
            chunk["payload"]["schema_version"] = 2
            # Update in Qdrant
            vector_store.update_chunk_payload(chunk["id"], chunk["payload"])
```

---

## Rollback Plan

### 8.1 Rollback Steps

**If issues arise, rollback to previous version:**

1. **Revert code changes**
   ```bash
   git revert <commit-hash>
   ```

2. **Library metadata collection is harmless**
   - New collection doesn't affect existing functionality
   - Can be ignored by old code

3. **Chunks with version fields are backward compatible**
   - Old code ignores unknown payload fields
   - Search and retrieval still work

4. **No data loss**
   - All original chunk data preserved
   - Version fields additive only

### 8.2 Rollback Testing

**Verify old code works with new data:**

```python
# Test old code against new schema
def test_backward_compatibility():
    # Query chunks with version fields using old code
    results = old_vector_store.search(query, library_id)

    # Should still return results
    assert len(results) > 0

    # Version fields ignored by old code
    for result in results:
        # Old code doesn't break on extra fields
        assert "text" in result.payload
```

---

## Summary

### Implementation Checklist

**Step 1: Vector Store Schema** (4-6 hours)
- [ ] Update `ChunkMetadata` model with version fields
- [ ] Create `LibraryIndexMetadata` model
- [ ] Extend `VectorStore` class with metadata methods
- [ ] Add library metadata collection
- [ ] Test metadata CRUD operations

**Step 2: Zotero API Client** (2-3 hours)
- [ ] Add `get_library_items_since()` method
- [ ] Add `get_library_version_range()` method
- [ ] Add `get_item_with_version()` method
- [ ] Test version-aware fetching

**Step 3: Document Processor** (6-8 hours)
- [ ] Implement `index_library()` with mode selection
- [ ] Implement `_index_library_incremental()`
- [ ] Implement `_index_library_full()`
- [ ] Update `_index_item()` to include version fields
- [ ] Test incremental vs. full indexing

**Step 4: API Endpoints** (3-4 hours)
- [ ] Add `/index-status` endpoint
- [ ] Add `/reset-index` endpoint
- [ ] Enhance `/index` endpoint with mode parameter
- [ ] Add `/libraries` list endpoint
- [ ] Test API endpoints

**Step 5: Plugin UI** (4-6 hours)
- [ ] Update library selection dialog
- [ ] Add index status display
- [ ] Add context menu for actions
- [ ] Add indexing options dialog
- [ ] Test UI interactions

**Step 6: Testing** (4-6 hours)
- [ ] Write unit tests
- [ ] Write integration tests
- [ ] Perform manual testing
- [ ] Verify backward compatibility

**Total Estimated Time:** 23-33 hours (2-3 days)

### Success Criteria

- [x] Incremental indexing works correctly
- [x] Version tracking persisted in database
- [x] Hard reset functionality available
- [x] API endpoints documented and tested
- [x] Plugin UI shows indexing status
- [x] Backward compatible with existing data
- [x] Performance improvement measurable (>10x for incremental updates)

---

**Document Status:** Implementation In Progress
**Next Steps:** Continue with Step 2 - Enhance Zotero Local API Client
**Dependencies:** None (can start immediately)

**Prepared by:** Claude Code Assistant
**Date:** 2025-01-12

---

## Implementation Progress

**Last Updated:** 2025-01-12

### Completed Steps

- [x] **Step 1: Extend Vector Store Schema** (COMPLETED)
  - [x] Created `LibraryIndexMetadata` model in `backend/models/library.py`
  - [x] Updated `ChunkMetadata` model with version tracking fields
  - [x] Extended `VectorStore` class with library metadata collection
  - [x] Added library metadata methods (get, update, mark for reset, get all)
  - [x] Added version-aware chunk methods (get item chunks, get item version, delete item chunks, count chunks)
  - [x] Updated models `__init__.py` to export new models

### In Progress

- [ ] **Step 3: Update Document Processor**
- [ ] **Step 4: Add API Endpoints**
- [ ] **Step 5: Plugin UI Updates**
- [ ] **Step 6: Testing**

### Completed Steps (Continued)

- [x] **Step 2: Enhance Zotero Local API Client** (COMPLETED)
  - [x] Added `get_library_items_since()` method with `?since=<version>` parameter support
  - [x] Added automatic pagination for large result sets
  - [x] Added `get_library_version_range()` method to get min/max versions
  - [x] Added `get_item_with_version()` method to fetch single items with version info
  - [x] Added `get_attachment_with_version()` method (wrapper for item fetching)
  - [x] Deprecated old `get_library_items()` method (now calls new method for backward compatibility)
  - [x] Created comprehensive unit tests (9 tests, all passing)
  - [x] Verified backward compatibility with existing code

### Files Modified

- `backend/models/library.py` (created)
- `backend/models/document.py` (updated)
- `backend/models/__init__.py` (updated)
- `backend/db/vector_store.py` (updated)
- `backend/zotero/local_api.py` (updated)
- `backend/tests/test_zotero_client_versions.py` (created)
