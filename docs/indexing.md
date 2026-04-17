# Indexing System Documentation

## Overview

The indexing system processes Zotero library items with supported attachments (PDF, HTML, DOCX, EPUB), extracting text, generating embeddings, and storing them in a vector database for semantic search. The system supports incremental indexing based on Zotero's native version tracking.

## Architecture Components

### Core Services

#### DocumentProcessor

`backend/services/document_processor.py`

Orchestrates the indexing pipeline:

- Document extraction + chunking → embedding generation → vector storage
- Manages indexing modes: auto, incremental, full
- Handles progress tracking and cancellation
- Implements version-aware incremental updates
- Delegates extraction/chunking to a `DocumentExtractor` implementation (Kreuzberg by default)

#### DocumentExtractor (Adapter)

`backend/services/extraction/`

Pluggable extraction layer supporting multiple backends and MIME types.

**Supported MIME types:** `application/pdf`, `text/html`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `application/epub+zip`

| Class | Backend | Notes |
|-------|---------|-------|
| `KreuzbergExtractor` | Kreuzberg (Rust) | Default; native async; 91+ formats; PDFium-based |
| `LegacyExtractor` | pypdf + spaCy | PDF-only fallback |

Select backend via `extractor_backend` setting (`kreuzberg` or `legacy`).

#### VectorStore

`backend/db/vector_store.py`

Qdrant-based storage with three collections:

- **document_chunks**: Vector embeddings with metadata payloads
- **deduplication**: Content hash → (library_id, item_key) mapping
- **library_metadata**: Per-library indexing state

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

Indexing mode is chosen by the plugin's `RemoteIndexer` and influences what it uploads:

### Auto Mode (Default)

- First-time indexing: calls `check-indexed` with all attachments; uploads everything not yet indexed (**full** behaviour)
- Subsequent indexing: calls `check-indexed` with current versions; uploads only new or changed attachments (**incremental** behaviour)
- A hard reset (`reset-index`) sets `force_reindex=True`, causing the backend to wipe existing chunks before processing the next upload

### Incremental Mode

- Calls `check-indexed` with current attachment versions
- Uploads only attachments where `needs_indexing=true` (new or version changed)
- Backend updates `last_indexed_version` after each document

### Full Mode

- Triggers a hard reset first (`POST /api/libraries/{id}/reset-index`)
- Then uploads all attachments, regardless of indexed status

## Indexing Flow

### Plugin Side (all modes)

```
1. Collect locally-stored attachments (Zotero JS API, supported MIME types)
2. POST /api/libraries/{id}/check-indexed → list of {needs_indexing, reason} per attachment
3. For each attachment where needs_indexing=true:
   - IOUtils.read(localPath) → file bytes
   - Build multipart FormData (file bytes + JSON metadata)
   - POST /api/index/document
   - Update progress display
```

### Backend Side (per uploaded document)

```
For each POST /api/index/document:
1. Validate API key (if API_KEY is configured)
2. Parse multipart form: file bytes + metadata JSON
3. Compute SHA256 content hash
4. Check deduplication table (skip if already indexed with same hash)
5. Delete existing chunks for this attachment (if updating)
6. Extract text + chunks (DocumentExtractor: max_chunk_size=512, overlap=50)
7. Generate embeddings (EmbeddingService: batch processing)
8. Create ChunkMetadata with version info
9. Store chunks in vector database
10. Add deduplication record
11. Update library metadata (last_indexed_version, total_chunks)
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

### Check Indexed Status

```
POST /api/libraries/{library_id}/check-indexed
```

Batch endpoint used by the plugin to determine which attachments need uploading.

Request body:

```json
{
    "library_id": "1",
    "library_type": "user",
    "attachments": [
        {
            "item_key": "ABC123",
            "attachment_key": "DEF456",
            "mime_type": "application/pdf",
            "item_version": 42,
            "attachment_version": 7
        }
    ]
}
```

Response:

```json
{
    "library_id": "1",
    "statuses": [
        {
            "item_key": "ABC123",
            "attachment_key": "DEF456",
            "needs_indexing": true,
            "reason": "version_changed"
        }
    ]
}
```

`reason` values: `"not_indexed"`, `"version_changed"`, `"up_to_date"`

### Upload Document

```
POST /api/index/document
```

Accepts multipart form data. Used by the plugin in remote mode to upload attachment bytes directly.

Form fields:

- `file`: raw file bytes
- `metadata`: JSON string with item metadata

```json
{
    "library_id": "1",
    "library_type": "user",
    "item_key": "ABC123",
    "attachment_key": "DEF456",
    "mime_type": "application/pdf",
    "item_version": 42,
    "attachment_version": 7,
    "title": "...",
    "authors": ["..."],
    "year": 2023,
    "abstract": "...",
    "doi": "...",
    "url": "...",
    "zotero_uri": "zotero://..."
}
```

Response:

```json
{
    "success": true,
    "attachment_key": "DEF456",
    "chunks_added": 12
}
```

Requires `X-API-Key` header when `API_KEY` is set on the backend.

## Text Processing

### Document Extraction and Chunking

`backend/services/extraction/`

Extraction and chunking are handled together by a `DocumentExtractor` implementation:

```python
chunks: list[ExtractionChunk] = await extractor.extract_and_chunk(file_bytes, mime_type)
# ExtractionChunk(text: str, page_number: int | None, chunk_index: int)
```

**KreuzbergExtractor** (default):
- Rust-based library, native async, handles 91+ formats
- Chunking config: `max_chars=512`, `max_overlap=50` (default)
- Page numbers tracked via `chunk.metadata['first_page']` (1-based)
- OCR support (Tesseract); disable via `ocr_enabled=False`
- Graceful fallback to no-OCR if Tesseract not installed

**LegacyExtractor** (fallback, PDF-only):
- pypdf for extraction, spaCy for sentence-boundary chunking
- Equivalent defaults: `max_chunk_size=512`, `chunk_overlap=50`

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
QDRANT_STORAGE_PATH = Path("~/.local/share/zotero-rag/qdrant")
EMBEDDING_DIM = 768  # Model-specific
MAX_CHUNK_SIZE = 512
CHUNK_OVERLAP = 50
EXTRACTOR_BACKEND = "kreuzberg"  # or "legacy" for pypdf+spaCy
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

- `backend/tests/test_document_processor.py`: DocumentProcessor core logic (uses mock extractor)
- `backend/tests/test_incremental_indexing.py`: Incremental mode and version comparison

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

- Implementation: [docs/history/implementation/incremental-indexing-summary.md](history/implementation/incremental-indexing-summary.md)
- Strategy analysis: [docs/history/implementation/indexing-and-embedding-strategies.md](history/implementation/indexing-and-embedding-strategies.md)
- Zotero Web API: <https://www.zotero.org/support/dev/web_api/v3/syncing>
- Qdrant documentation: <https://qdrant.tech/documentation/>
