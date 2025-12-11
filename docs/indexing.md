# Indexing System Documentation

## Overview

The indexing system processes Zotero library items with PDF attachments, extracting text, generating embeddings, and storing them in a vector database for semantic search. The system supports incremental indexing based on Zotero's native version tracking.

## Architecture Components

### Core Services

#### DocumentProcessor

`backend/services/document_processor.py`

Orchestrates the indexing pipeline:

- PDF extraction → text chunking → embedding generation → vector storage
- Manages indexing modes: auto, incremental, full
- Handles progress tracking and cancellation
- Implements version-aware incremental updates

#### VectorStore

`backend/db/vector_store.py`

Qdrant-based storage with three collections:

- **document_chunks**: Vector embeddings with metadata payloads
- **deduplication**: Content hash → (library_id, item_key) mapping
- **library_metadata**: Per-library indexing state

#### ZoteroLocalAPI

`backend/zotero/local_api.py`

HTTP client for Zotero's local API (localhost:23119):

- Fetch items with version filtering (?since parameter)
- Get item children (attachments)
- Download PDF files via file:// redirects
- No authentication required

## Data Models

### LibraryIndexMetadata

`backend/models/library.py`

Tracks per-library indexing state:

```python
{
    "library_id": str,              # e.g., "1" for user library
    "library_type": "user"|"group",
    "library_name": str,
    "last_indexed_version": int,    # Highest version processed
    "last_indexed_at": str,         # ISO 8601 timestamp
    "total_items_indexed": int,
    "total_chunks": int,
    "indexing_mode": "full"|"incremental",
    "force_reindex": bool,          # Trigger hard reset
    "schema_version": int
}
```

### ChunkMetadata

`backend/models/document.py`

Stored in vector database payload:

```python
{
    "chunk_id": str,                    # "{library_id}:{item_key}:{attachment_key}:{index}"
    "library_id": str,
    "item_key": str,
    "attachment_key": str,
    "title": str,
    "authors": list[str],
    "year": int,
    "page_number": int,
    "text_preview": str,                # First 5 words for citation
    "chunk_index": int,
    "content_hash": str,                # SHA256 of PDF content

    # Version tracking (schema v2)
    "item_version": int,                # Zotero item version at indexing
    "attachment_version": int,          # Attachment version at indexing
    "indexed_at": str,                  # ISO 8601 timestamp
    "zotero_modified": str,             # Item's dateModified field
    "schema_version": int               # Default: 2
}
```

## Indexing Modes

### Auto Mode (Default)

```python
await processor.index_library(library_id, mode="auto")
```

- First-time indexing: Uses **full** mode
- Subsequent indexing: Uses **incremental** mode
- Respects `force_reindex` flag for hard reset

### Incremental Mode

```python
await processor.index_library(library_id, mode="incremental")
```

- Fetches items modified since `last_indexed_version` using `?since` parameter
- Compares existing chunk versions with current item versions
- New items: Index normally
- Updated items: Delete old chunks, reindex
- Unchanged items: Skip (shouldn't occur with `?since`, but defensive)
- Updates library metadata with new `last_indexed_version`

### Full Mode

```python
await processor.index_library(library_id, mode="full")
```

- Deletes all library chunks from vector store
- Deletes all deduplication records for library
- Fetches all items (no version filter)
- Indexes entire library from scratch
- Updates library metadata with max version seen

## Indexing Flow

### Full Indexing

```
1. Delete all library chunks (vector store)
2. Delete all deduplication records
3. Fetch all items from Zotero API
4. Filter items with PDF attachments
5. For each item:
   - Download PDFs
   - Check content hash (skip if duplicate)
   - Extract text with page numbers
   - Chunk semantically (spaCy boundaries)
   - Generate embeddings (batch)
   - Store chunks with version metadata
   - Record deduplication entry
6. Update library metadata
```

### Incremental Indexing

```
1. Get library metadata (last_indexed_version)
2. Fetch items modified since version via ?since parameter
3. Filter items with PDF attachments
4. For each modified item:
   - Check existing chunk version
   - If new: Index normally
   - If updated: Delete old chunks, reindex
5. Update library metadata with max version
```

### Item Processing

```
For each item with PDFs:
1. Extract authors, title, year from item data
2. Get item children (attachments)
3. Filter to PDF attachments
4. For each PDF:
   - Download file bytes
   - Compute SHA256 hash
   - Check deduplication table
   - Extract text pages (PDFExtractor)
   - Chunk text (TextChunker: max_chunk_size=512, overlap=50)
   - Generate embeddings (EmbeddingService: batch processing)
   - Create ChunkMetadata with version info
   - Store in vector database
   - Add deduplication record
```

## Version Tracking

### Zotero Version Semantics

- Each item has a `version` field (monotonically increasing integer)
- Versions are per-library, not global
- No guarantees about sequential numbers (gaps allowed)
- Attachments have separate version numbers
- `?since=N` parameter returns items with version > N

### Version Comparison Logic

```python
existing_version = vector_store.get_item_version(library_id, item_key)
current_version = item["version"]

if existing_version is None:
    # New item - index normally
elif existing_version < current_version:
    # Updated item - delete old chunks and reindex
else:
    # Up-to-date - skip
```

### Backward Compatibility

- Legacy chunks without version fields: `item_version` defaults to 0
- `get_item_version()` returns None if no chunks exist or version field missing
- Search queries work with mixed schema versions
- No forced migration required

## Deduplication

Content-based deduplication prevents reprocessing identical PDFs across libraries:

```python
content_hash = hashlib.sha256(pdf_bytes).hexdigest()
if vector_store.check_duplicate(content_hash):
    skip  # PDF already indexed
```

Deduplication record:

```python
{
    "content_hash": str,    # SHA256 of PDF content
    "library_id": str,
    "item_key": str,
    "relation_uri": str     # Optional owl:sameAs relation
}
```

## API Endpoints

### Index Library

```
POST /api/index/library/{library_id}?mode=auto|incremental|full
```

Query parameters:

- `mode`: Indexing mode (default: auto)
- `library_type`: user|group (default: user)
- `library_name`: Human-readable name

Response:

```json
{
    "success": true,
    "library_id": "1",
    "statistics": {
        "mode": "incremental",
        "items_processed": 5,
        "items_added": 2,
        "items_updated": 3,
        "chunks_added": 150,
        "chunks_deleted": 120,
        "elapsed_seconds": 45.2,
        "last_version": 12345
    }
}
```

### Get Index Status

```
GET /api/libraries/{library_id}/index-status
```

Response: LibraryIndexMetadata object

### Reset Index

```
POST /api/libraries/{library_id}/reset-index
```

Sets `force_reindex=True` for next indexing operation.

### List Indexed Libraries

```
GET /api/libraries/indexed
```

Returns array of LibraryIndexMetadata objects.

### Cancel Indexing

```
POST /api/index/library/{library_id}/cancel
```

Signals cancellation for ongoing indexing operation.

## Text Processing

### PDF Extraction

`backend/services/pdf_extractor.py`

- Extracts text with page numbers
- Returns list of (page_number, text) tuples
- Handles extraction errors gracefully

### Text Chunking

`backend/services/chunking.py`

- Semantic chunking using spaCy sentence boundaries
- max_chunk_size: 512 characters (default)
- chunk_overlap: 50 characters (default)
- Preserves page number metadata
- Generates text_preview (first 5 words) for citations

### Embedding Generation

`backend/services/embeddings.py`

- Batch processing for efficiency
- Configurable embedding model
- GPU acceleration support (MPS, CUDA)
- Returns list of embedding vectors

## Progress Tracking

### Progress Callback

```python
def progress_callback(current: int, total: int):
    print(f"Progress: {current}/{total}")

await processor.index_library(
    library_id="1",
    progress_callback=progress_callback
)
```

### Cancellation Support

```python
def cancellation_check() -> bool:
    return user_cancelled  # Return True to cancel

await processor.index_library(
    library_id="1",
    cancellation_check=cancellation_check
)
```

Raises `RuntimeError("Indexing cancelled by user")` if cancelled.

## Vector Store Operations

### Add Chunks

```python
# Single chunk
vector_store.add_chunk(doc_chunk)

# Batch
vector_store.add_chunks_batch(doc_chunks)
```

### Search

```python
results = vector_store.search(
    query_vector=embedding,
    limit=5,
    score_threshold=0.7,
    library_ids=["1", "123"]  # Optional filter
)
```

### Library Management

```python
# Get metadata
metadata = vector_store.get_library_metadata(library_id)

# Update metadata
vector_store.update_library_metadata(metadata)

# Mark for reset
vector_store.mark_library_for_reset(library_id)

# Delete library chunks
count = vector_store.delete_library_chunks(library_id)
```

### Version-Aware Queries

```python
# Get item version
version = vector_store.get_item_version(library_id, item_key)

# Get item chunks
chunks = vector_store.get_item_chunks(library_id, item_key)

# Delete item chunks
count = vector_store.delete_item_chunks(library_id, item_key)

# Count library chunks
count = vector_store.count_library_chunks(library_id)
```

## Configuration

### Settings

`backend/config/settings.py`

```python
ZOTERO_API_URL = "http://localhost:23119"  # Local API URL
QDRANT_STORAGE_PATH = Path("~/.local/share/zotero-rag/qdrant")
EMBEDDING_DIM = 768  # Model-specific
MAX_CHUNK_SIZE = 512
CHUNK_OVERLAP = 50
```

### Storage Paths

- Vector database: `~/.local/share/zotero-rag/qdrant/`
- Collections: document_chunks, deduplication, library_metadata

## Performance Considerations

### Batch Processing

- Embeddings generated in batches (configurable batch_size)
- Chunks added to vector store in batches
- Pagination for large item sets (100 items per request)

### Incremental Indexing Benefits

- 80-90% faster for libraries with no/few changes
- Reduced API calls (only fetches changed items)
- Lower CPU/memory usage
- Near-instant updates for small changes

### Scalability Estimates

| Library Size | Items | Chunks | DB Size | Index Time | Query Time |
|--------------|-------|--------|---------|------------|------------|
| Small        | 100   | 2,500  | 10 MB   | 5 min      | <100ms     |
| Medium       | 1,000 | 25,000 | 100 MB  | 30 min     | <200ms     |
| Large        | 10,000| 250,000| 1 GB    | 5 hours    | <500ms     |

## Known Limitations

1. **Deleted items not detected**: Incremental mode doesn't remove chunks for deleted Zotero items
   - Workaround: Periodic hard reset

2. **Attachment content changes**: Version may not increment if only PDF file changes (not metadata)
   - Impact: Low (rare scenario)
   - Workaround: Hard reset when needed

3. **Single library indexing**: Cannot index multiple libraries concurrently
   - Impact: Medium for users with many libraries
   - Future: Per-library locking

## Error Handling

- PDF extraction failures: Logged, item skipped
- Download failures: Logged, attachment skipped
- Embedding failures: Logged, item skipped
- Connection errors: Raised as ConnectionError
- Cancellation: Raises RuntimeError with cleanup

## Testing

### Unit Tests

- `backend/tests/test_incremental_indexing.py`: DocumentProcessor logic
- `backend/tests/test_zotero_client_versions.py`: Version API methods

### Integration Tests

- `backend/tests/test_api_incremental_indexing.py`: Full API workflow

### Manual Testing

- `docs/implementation/incremental-indexing-manual-test-checklist.md`

## Migration from Legacy System

### Schema v1 to v2

- Old chunks lack version fields (item_version, attachment_version, indexed_at)
- No migration script required
- Gradual migration: chunks replaced naturally as items reindex
- Version queries treat legacy chunks as version 0

## References

- Implementation: [dev/done/implementation/incremental-indexing-summary.md](../dev/done/implementation/incremental-indexing-summary.md)
- Strategy analysis: [dev/done/implementation/indexing-and-embedding-strategies.md](../dev/done/implementation/indexing-and-embedding-strategies.md)
- Zotero Web API: <https://www.zotero.org/support/dev/web_api/v3/syncing>
- Qdrant documentation: <https://qdrant.tech/documentation/>
