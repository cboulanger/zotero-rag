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
| ------- | --------- | ------- |
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

```text
1. Collect locally-stored attachments (Zotero JS API, supported MIME types)
2. POST /api/libraries/{id}/check-indexed → list of {needs_indexing, reason} per attachment
3. For each attachment where needs_indexing=true:
   - IOUtils.read(localPath) → file bytes
   - Build multipart FormData (file bytes + JSON metadata)
   - POST /api/index/document
   - Update progress display
```

### Backend Side (per uploaded document)

```text
For each POST /api/index/document:
1. Validate API key (if API_KEY is configured)
2. Parse multipart form: file bytes + metadata JSON
3. Compute SHA256 content hash
4. Check same-library deduplication table (skip if already indexed with same hash)
5. Check cross-library deduplication table (copy chunks from other library if hash match found)
   → status: "copied_cross_library" — skips steps 6-7
6. Delete existing chunks for this attachment (if updating)
7. Extract text + chunks (DocumentExtractor: max_chunk_size=512, overlap=50)
8. Generate embeddings (EmbeddingService: batch processing)
9. Create ChunkMetadata with version info
10. Store chunks in vector database
11. Add deduplication record
12. Update library metadata (last_indexed_version, total_chunks)
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

Content-based deduplication avoids reprocessing identical files. Two levels are checked in order:

### Same-library deduplication

If the same file was already indexed in the same library (e.g. duplicate attachment), it is skipped entirely.

```python
content_hash = hashlib.sha256(file_bytes).hexdigest()
if vector_store.check_duplicate(content_hash, library_id):
    skip  # already indexed in this library
```

### Cross-library chunk reuse

If the same file exists in a *different* library (common when exporting items from a personal library into thematic group libraries), the pre-computed chunks and embeddings are copied directly — no extraction, no embedding call. This is triggered when `find_cross_library_duplicate` returns a match.

```python
cross_record = vector_store.find_cross_library_duplicate(content_hash, current_library_id)
if cross_record and vector_store.get_item_chunks(cross_record.library_id, cross_record.item_key):
    vector_store.copy_chunks_cross_library(source=cross_record, target=current_item)
    # status: "copied_cross_library"
```

The copy updates all identity fields (library_id, item_key, chunk_id, title, authors, year, timestamps) while preserving text content, page numbers, and the embedding vector verbatim.

### Deduplication record

```python
{
    "content_hash": str,    # SHA256 of raw file bytes
    "library_id": str,
    "item_key": str,
    "relation_uri": str     # Optional owl:sameAs relation (reserved)
}
```

A new deduplication record is written for the target library after a cross-library copy, so subsequent uploads of the same file to that library are caught at the cheaper same-library check.

## API Endpoints

### Get Index Status

```text
GET /api/libraries/{library_id}/index-status
```

Response: LibraryIndexMetadata object

### Reset Index

```text
POST /api/libraries/{library_id}/reset-index
```

Sets `force_reindex=True` for next indexing operation.

### List Indexed Libraries

```text
GET /api/libraries/indexed
```

Returns array of LibraryIndexMetadata objects.

### Check Indexed Status

```text
POST /api/libraries/{library_id}/check-indexed
```

Batch endpoint used by the plugin to determine which attachments need uploading.

Request body:

```json
{
    "library_id": "1",
    "library_type": "user",
    "force_refresh": false,
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

`force_refresh` (default `false`): when `true`, the cached result for this batch is bypassed and a fresh query is sent to the vector store. The fresh result is still written to the cache, so subsequent batches or other clients can benefit from it. The plugin sets this to `true` automatically when `mode === "full"`.

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

#### Server-side caching

The result of `get_item_versions_bulk` is cached in memory for 5 minutes, keyed by `library_id + md5(sorted item_keys)`. This avoids repeated Qdrant scroll queries when multiple clients check the same library in quick succession (e.g. on first plugin startup across multiple Zotero instances sharing a remote backend).

Cache invalidation (all entries for a library are cleared) happens in two cases:

1. A document is successfully indexed via `POST /api/index/document`
2. An abstract is successfully indexed via `POST /api/index/abstract`

`force_refresh: true` does **not** invalidate other cached entries — it only skips reading the cache for the current batch, then writes the fresh result back. This means even a large full reindex populates the cache batch by batch, and a second concurrent client can still get cache hits for batches already fetched.

The cache is an in-memory dict and is not shared across workers. In multi-worker deployments (Qdrant server mode with `--workers > 1`), each worker maintains its own cache — invalidation is per-worker, so a cache hit within the TTL window may still return slightly stale data. This is acceptable because staleness is bounded by the 5-minute TTL and the next successful upload resets the cache on the worker that served it.

### Upload Document

```text
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

### Cross-Library Deduplication

```python
# Find a dedup record for content_hash in any library except current_library_id
record = vector_store.find_cross_library_duplicate(content_hash, current_library_id)

# Copy chunks from source item into target item (different library)
# Returns number of chunks written; 0 if source has no chunks
count = vector_store.copy_chunks_cross_library(
    source_library_id, source_item_key,
    target_library_id, target_item_key, target_attachment_key,
    target_doc_metadata, target_item_version, target_attachment_version,
    target_item_modified,
)
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

### Cross-Library Reuse Benefits

When items are exported from a personal library into group libraries, the attachment files are byte-identical. Cross-library chunk reuse eliminates redundant work for those duplicates:

- Skips document extraction (Kreuzberg/PDF parsing)
- Skips embedding generation (no model inference)
- Cost: two Qdrant scroll calls on `DEDUP_COLLECTION` + one upsert of the copied points
- Expected speedup: proportional to extraction + embedding time (typically 10–100×)

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

## Schema Versioning and Metadata Migration

Every chunk stored in Qdrant carries a `schema_version` integer in its payload.
The current version is defined as `CURRENT_SCHEMA_VERSION` in `backend/models/document.py`.
Bumping this constant is the trigger for the lightweight metadata migration flow described below.

### When to bump the version

Bump `CURRENT_SCHEMA_VERSION` whenever a new payload field is added that you want to
filter or search on (e.g. `item_type` was added in v3). You do **not** need to bump it
for changes that only affect vector content — those are handled by full re-indexing.

### How migration works (no re-embedding required)

1. **Detection** — When the plugin calls `check-indexed`, the backend reads both
   `item_version` and `schema_version` from each item's Qdrant payload via
   `get_item_states_bulk()`. Items whose `schema_version < CURRENT_SCHEMA_VERSION`
   but are otherwise up-to-date receive `needs_metadata_update: true` in the response.

2. **Collection** — The plugin collects those items after the check-indexed loop and
   calls `_sendMetadataUpdates()`, which POSTs the current Zotero bibliographic
   metadata (title, authors, year, item_type, …) to:

   ```http
   POST /api/index/items/metadata
   { "library_id": "...", "items": [{ "item_key": "ABC", "title": "...", ... }] }
   ```

   No file bytes are sent; the metadata comes from the Zotero parent item in memory.

3. **Application** — `batch_update_metadata()` calls `vector_store.update_item_metadata()`
   for each item, which uses Qdrant's `set_payload()` to patch the new fields on all
   existing chunks for that item and writes `schema_version: CURRENT_SCHEMA_VERSION`.
   Vectors are untouched — no re-embedding occurs.

### Graceful degradation

Items that are never re-checked (user never clicks "Re-index") keep their old
`schema_version`. The new fields will be absent or `None` in those payloads, so
metadata filters on the new field simply won't match them. Nothing breaks; coverage
improves incrementally as items are checked.

### Adding a new payload field — checklist

1. Bump `CURRENT_SCHEMA_VERSION` in `backend/models/document.py`.
2. Add the field to the payload dict in `add_chunk()` and `add_chunks_batch()` in
   `backend/db/vector_store.py`.
3. Add a Qdrant payload index for it in `_ensure_chunks_indexes()` if you need to
   filter on it efficiently (keyword, integer, or text index as appropriate).
4. Add the field to `ItemMetadataUpdate` in `backend/api/document_upload.py`.
5. Include it in the `fields` dict inside `update_item_metadata()`.
6. The plugin's `_sendMetadataUpdates()` already reads all standard Zotero bibliographic
   fields from the parent item, so no plugin-side changes are needed for standard fields.

## References

- Implementation: [docs/history/implementation/incremental-indexing-summary.md](history/implementation/incremental-indexing-summary.md)
- Strategy analysis: [docs/history/implementation/indexing-and-embedding-strategies.md](history/implementation/indexing-and-embedding-strategies.md)
- Zotero Web API: <https://www.zotero.org/support/dev/web_api/v3/syncing>
- Qdrant documentation: <https://qdrant.tech/documentation/>
