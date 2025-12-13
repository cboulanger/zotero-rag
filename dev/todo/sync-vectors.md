# Implementation Plan: Vector Database Synchronization

## Overview

Enable syncing of Qdrant vector databases with remote storage backends (WebDAV, S3) to allow searches without local indexing. The system will intelligently cache and sync vector databases based on Zotero library versions.

## Current Architecture Analysis

### Existing Components

1. **VectorStore** ([backend/db/vector_store.py](backend/db/vector_store.py))
   - Uses Qdrant for local vector storage
   - Storage path: `~/.local/share/zotero-rag/qdrant/`
   - Three collections: `document_chunks`, `deduplication`, `library_metadata`
   - Per-library metadata includes `last_indexed_version` for incremental sync

2. **LibraryIndexMetadata** ([backend/models/library.py](backend/models/library.py))
   - Tracks indexing state per library
   - Key fields: `library_id`, `last_indexed_version`, `last_indexed_at`, `total_chunks`
   - Already has version tracking foundation

3. **Qdrant Features** (v1.15.1)
   - Supports collection snapshots (full backups)
   - No native incremental sync
   - Local storage is file-based (RocksDB)

## Design Decisions & Best Practices

### Approach 1: Full Snapshot Sync (Recommended for MVP)

**Strategy**: Sync entire Qdrant collection snapshots as compressed archives.

**Pros**:

- Simple, robust implementation
- Leverages Qdrant's native snapshot feature
- Atomic operations (all or nothing)
- Easy rollback
- Works with existing version tracking

**Cons**:

- Higher bandwidth usage for large libraries
- No delta updates
- Slower for incremental changes

**When to use**: Libraries with infrequent updates, shared team libraries, archive scenarios

### Approach 2: Chunk-Level Delta Sync (Advanced)

**Strategy**: Track and sync individual chunk changes using library version metadata.

**Pros**:

- Efficient bandwidth usage
- Fast incremental updates
- Granular control

**Cons**:

- Complex implementation
- Requires change tracking
- Higher maintenance overhead
- Conflict resolution complexity

**When to use**: Large libraries (10k+ items), frequent updates, bandwidth-constrained environments

### Approach 3: Hybrid (Recommended for Production)

**Strategy**: Use full snapshots for initial sync, chunk-level deltas for incremental updates.

**Decision Matrix**:

```python
if not local_exists or remote_newer_by_threshold(weeks=4):
    sync_full_snapshot()
elif remote_version > local_version:
    sync_delta_chunks()
```

## Implementation Plan

### Phase 1: Storage Backend Abstraction

**Goal**: Create pluggable remote storage interface with WebDAV and S3 implementations.

#### 1.1 Storage Interface Design

**File**: `backend/storage/base.py`

```python
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional
from datetime import datetime

class RemoteStorageBackend(ABC):
    """Abstract base class for remote storage backends."""

    @abstractmethod
    async def upload_file(self, local_path: Path, remote_path: str,
                         metadata: dict = None) -> bool:
        """Upload file to remote storage."""
        pass

    @abstractmethod
    async def download_file(self, remote_path: str, local_path: Path) -> bool:
        """Download file from remote storage."""
        pass

    @abstractmethod
    async def exists(self, remote_path: str) -> bool:
        """Check if remote file exists."""
        pass

    @abstractmethod
    async def get_metadata(self, remote_path: str) -> Optional[dict]:
        """Get file metadata (size, modified_at, custom metadata)."""
        pass

    @abstractmethod
    async def delete_file(self, remote_path: str) -> bool:
        """Delete remote file."""
        pass

    @abstractmethod
    async def list_files(self, remote_prefix: str) -> list[str]:
        """List files in remote directory."""
        pass
```

**Metadata Schema** (stored with snapshot):

```json
{
    "library_id": "6297749",
    "library_version": 12345,
    "snapshot_version": "v1",
    "created_at": "2025-12-11T13:00:00Z",
    "total_chunks": 25000,
    "total_items": 1000,
    "qdrant_version": "1.15.1",
    "schema_version": 2,
    "compression": "gzip",
    "checksum_sha256": "abc123..."
}
```

#### 1.2 WebDAV Implementation

**File**: `backend/storage/webdav.py`

**Dependencies**: `webdavclient3` or `httpx` with WebDAV methods

```python
class WebDAVStorage(RemoteStorageBackend):
    """WebDAV remote storage implementation."""

    def __init__(self, base_url: str, username: str, password: str,
                 base_path: str = "/zotero-rag/vectors/"):
        """
        Initialize WebDAV storage.

        Args:
            base_url: WebDAV server URL (e.g., https://webdav.example.com)
            username: WebDAV username
            password: WebDAV password
            base_path: Base path for vector storage
        """
        self.base_url = base_url.rstrip('/')
        self.base_path = base_path
        self.client = WebDAVClient({
            'webdav_hostname': base_url,
            'webdav_login': username,
            'webdav_password': password
        })

    async def upload_file(self, local_path: Path, remote_path: str,
                         metadata: dict = None) -> bool:
        """Upload with metadata as sidecar .meta.json file."""
        # Upload main file
        # Upload metadata sidecar
        pass
```

**Configuration** (add to `.env.dist`):

```bash
# WebDAV Sync Configuration
SYNC_ENABLED=false
SYNC_BACKEND=webdav  # or s3
SYNC_WEBDAV_URL=https://webdav.example.com
SYNC_WEBDAV_USERNAME=user
SYNC_WEBDAV_PASSWORD=pass
SYNC_WEBDAV_BASE_PATH=/zotero-rag/vectors/
SYNC_AUTO_PULL=true  # Auto-pull on startup
SYNC_AUTO_PUSH=false # Auto-push after indexing
```

#### 1.3 S3 Implementation

**File**: `backend/storage/s3.py`

**Dependencies**: `aioboto3` (async S3 client)

```python
class S3Storage(RemoteStorageBackend):
    """Amazon S3 / S3-compatible storage implementation."""

    def __init__(self, bucket: str, region: str = "us-east-1",
                 prefix: str = "zotero-rag/vectors/",
                 endpoint_url: Optional[str] = None,
                 access_key: Optional[str] = None,
                 secret_key: Optional[str] = None):
        """
        Initialize S3 storage.

        Args:
            bucket: S3 bucket name
            region: AWS region
            prefix: Key prefix for vector storage
            endpoint_url: Custom endpoint for S3-compatible services (MinIO, etc.)
            access_key: AWS access key (if not using IAM)
            secret_key: AWS secret key
        """
        self.bucket = bucket
        self.prefix = prefix
        # Use aioboto3 session
        pass

    async def upload_file(self, local_path: Path, remote_path: str,
                         metadata: dict = None) -> bool:
        """Upload with metadata in S3 object metadata."""
        # Use multipart upload for large files
        # Store metadata in S3 object user metadata
        pass
```

**Configuration** (add to `.env.dist`):

```bash
# S3 Sync Configuration
SYNC_S3_BUCKET=my-zotero-vectors
SYNC_S3_REGION=us-east-1
SYNC_S3_PREFIX=zotero-rag/vectors/
SYNC_S3_ENDPOINT_URL=  # For MinIO, DigitalOcean Spaces, etc.
SYNC_S3_ACCESS_KEY=
SYNC_S3_SECRET_KEY=
```

**Tasks**:

- [ ] Create `backend/storage/` directory structure
- [ ] Implement `base.py` with abstract interface
- [ ] Implement `webdav.py` with webdavclient3
- [ ] Implement `s3.py` with aioboto3
- [ ] Add storage backend factory
- [ ] Write unit tests for each backend
- [ ] Update `pyproject.toml` dependencies (make storage backends optional)

---

### Phase 2: Snapshot Management

**Goal**: Create/restore Qdrant collection snapshots with compression and checksums.

#### 2.1 Snapshot Service

**File**: `backend/services/snapshot_manager.py`

```python
class SnapshotManager:
    """Manages Qdrant collection snapshots for sync."""

    def __init__(self, vector_store: VectorStore,
                 temp_dir: Path = Path("/tmp/zotero-rag-snapshots")):
        self.vector_store = vector_store
        self.temp_dir = temp_dir
        self.temp_dir.mkdir(parents=True, exist_ok=True)

    async def create_snapshot(self, library_id: str) -> Path:
        """
        Create snapshot for a specific library's collections.

        Process:
        1. Get library metadata to determine version
        2. Create Qdrant snapshots for all 3 collections
        3. Package into tar.gz with metadata.json
        4. Compute SHA256 checksum
        5. Return path to snapshot file

        Returns:
            Path to snapshot file (e.g., library_6297749_v12345.tar.gz)
        """
        pass

    async def restore_snapshot(self, snapshot_path: Path,
                              library_id: str) -> bool:
        """
        Restore snapshot for a library.

        Process:
        1. Verify checksum
        2. Extract tar.gz
        3. Validate metadata matches library_id
        4. Delete existing library collections (if any)
        5. Restore Qdrant snapshots
        6. Update library metadata

        Returns:
            True if successful
        """
        pass

    async def get_snapshot_info(self, snapshot_path: Path) -> dict:
        """Extract metadata without full restore."""
        pass
```

**Snapshot Structure**:

```
library_6297749_v12345.tar.gz
├── metadata.json                    # Snapshot metadata
├── document_chunks.snapshot         # Qdrant snapshot
├── deduplication.snapshot          # Qdrant snapshot
├── library_metadata.snapshot       # Qdrant snapshot
└── checksums.txt                   # SHA256 per file
```

**Qdrant Snapshot API Usage**:

```python
# Create snapshot
snapshot = client.create_snapshot(collection_name="document_chunks")
# Returns snapshot name, e.g., "document_chunks-2025-12-11-13-00-00.snapshot"

# Restore snapshot
client.recover_snapshot(
    collection_name="document_chunks",
    snapshot_name="document_chunks-2025-12-11-13-00-00.snapshot"
)
```

**Tasks**:

- [ ] Implement `SnapshotManager` class
- [ ] Create snapshot creation logic with Qdrant API
- [ ] Implement tar.gz compression
- [ ] Add SHA256 checksum validation
- [ ] Implement snapshot restore logic
- [ ] Add cleanup for temporary files
- [ ] Write integration tests
- [ ] Handle edge cases (corrupted snapshots, version mismatches)

---

### Phase 3: Sync Orchestration

**Goal**: Coordinate version checking, pull/push operations, and conflict resolution.

#### 3.1 Sync Service

**File**: `backend/services/vector_sync.py`

```python
class VectorSyncService:
    """Orchestrates vector database synchronization."""

    def __init__(self,
                 vector_store: VectorStore,
                 snapshot_manager: SnapshotManager,
                 storage_backend: RemoteStorageBackend,
                 zotero_client: ZoteroLocalAPI):
        self.vector_store = vector_store
        self.snapshot_manager = snapshot_manager
        self.storage = storage_backend
        self.zotero_client = zotero_client

    async def should_pull(self, library_id: str) -> tuple[bool, str]:
        """
        Determine if library should be pulled from remote.

        Decision logic:
        1. No local copy exists → PULL
        2. Remote doesn't exist → NO_PULL
        3. Remote version > local version → PULL
        4. Local version >= remote version → NO_PULL

        Returns:
            (should_pull, reason)
        """
        pass

    async def pull_library(self, library_id: str,
                          force: bool = False) -> dict:
        """
        Pull library vectors from remote storage.

        Process:
        1. Check if pull is needed (unless force=True)
        2. Download snapshot from remote storage
        3. Validate snapshot metadata
        4. Restore snapshot to local Qdrant
        5. Update local library metadata
        6. Cleanup temporary files

        Returns:
            Statistics: downloaded_bytes, restore_time, chunks_restored
        """
        pass

    async def push_library(self, library_id: str,
                          force: bool = False) -> dict:
        """
        Push library vectors to remote storage.

        Process:
        1. Get local library metadata
        2. Check if push needed (unless force=True)
        3. Create snapshot
        4. Upload to remote storage with metadata
        5. Verify upload
        6. Cleanup temporary files

        Returns:
            Statistics: uploaded_bytes, snapshot_time, chunks_pushed
        """
        pass

    async def sync_library(self, library_id: str,
                          direction: Literal["auto", "pull", "push"] = "auto") -> dict:
        """
        Bidirectional sync with conflict detection.

        Auto mode logic:
        - Local newer: push
        - Remote newer: pull
        - Same version: no-op
        - Conflict (diverged): error, require manual resolution
        """
        pass

    async def list_remote_libraries(self) -> list[dict]:
        """List available libraries in remote storage."""
        pass
```

#### 3.2 Version Comparison Logic

```python
def compare_versions(local_meta: LibraryIndexMetadata,
                    remote_meta: dict) -> Literal["local_newer", "remote_newer", "same", "diverged"]:
    """
    Compare local and remote library versions.

    Diverged detection:
    - Both have been modified since last sync
    - Timestamps differ but versions are incomparable
    """
    if local_meta.last_indexed_version == remote_meta["library_version"]:
        return "same"

    if local_meta.last_indexed_version > remote_meta["library_version"]:
        return "local_newer"

    if local_meta.last_indexed_version < remote_meta["library_version"]:
        return "remote_newer"

    # Check for divergence using timestamps
    # (This shouldn't happen with Zotero's monotonic versions, but defensive)
    return "diverged"
```

**Tasks**:

- [ ] Implement `VectorSyncService` class
- [ ] Add version comparison logic
- [ ] Implement pull operation with validation
- [ ] Implement push operation with conflict detection
- [ ] Add bidirectional sync with auto-resolution
- [ ] Implement remote library listing
- [ ] Add progress callbacks for large transfers
- [ ] Write integration tests
- [ ] Add error recovery (partial downloads, network failures)

---

### Phase 4: API Integration

**Goal**: Expose sync operations through FastAPI endpoints and integrate with indexing workflow.

#### 4.1 API Endpoints

**File**: `backend/api/sync.py`

```python
from fastapi import APIRouter, HTTPException, BackgroundTasks
from backend.services.vector_sync import VectorSyncService

router = APIRouter(prefix="/api/vectors", tags=["vectors"])

@router.post("/{library_id}/pull")
async def pull_library(
    library_id: str,
    force: bool = False,
    background_tasks: BackgroundTasks = None
):
    """Pull library vectors from remote storage."""
    pass

@router.post("/{library_id}/push")
async def push_library(
    library_id: str,
    force: bool = False,
    background_tasks: BackgroundTasks = None
):
    """Push library vectors to remote storage."""
    pass

@router.post("/{library_id}/sync")
async def sync_library(
    library_id: str,
    direction: Literal["auto", "pull", "push"] = "auto"
):
    """Bidirectional sync with conflict detection."""
    pass

@router.get("/remote")
async def list_remote_libraries():
    """List available libraries in remote storage."""
    pass

@router.get("/{library_id}/sync-status")
async def get_sync_status(library_id: str):
    """
    Get sync status for a library.

    Returns:
        {
            "local_exists": true,
            "remote_exists": true,
            "local_version": 12345,
            "remote_version": 12340,
            "sync_status": "local_newer",
            "last_synced_at": "2025-12-11T10:00:00Z",
            "local_chunks": 25000,
            "remote_chunks": 24800
        }
    """
    pass
```

#### 4.2 Integration with Document Processor

**Modify**: `backend/services/document_processor.py`

Add optional auto-push after indexing:

```python
class DocumentProcessor:
    def __init__(self, ..., sync_service: Optional[VectorSyncService] = None,
                 auto_push: bool = False):
        self.sync_service = sync_service
        self.auto_push = auto_push

    async def index_library(self, ...):
        # ... existing indexing logic ...

        # After successful indexing
        if self.auto_push and self.sync_service:
            logger.info(f"Auto-pushing library {library_id} to remote storage")
            try:
                await self.sync_service.push_library(library_id)
            except Exception as e:
                logger.error(f"Auto-push failed: {e}")
                # Don't fail indexing if push fails
```

#### 4.3 Startup Logic

**Modify**: `backend/main.py`

Add auto-pull on startup:

```python
@app.on_event("startup")
async def startup_event():
    settings = get_settings()

    if settings.sync_enabled and settings.sync_auto_pull:
        logger.info("Checking for remote library updates...")
        sync_service = get_sync_service()

        # Pull all libraries that are newer remotely
        remote_libraries = await sync_service.list_remote_libraries()
        for lib in remote_libraries:
            should_pull, reason = await sync_service.should_pull(lib["library_id"])
            if should_pull:
                logger.info(f"Pulling library {lib['library_id']}: {reason}")
                await sync_service.pull_library(lib["library_id"])
```

**Tasks**:

- [ ] Create `backend/api/sync.py` with REST endpoints
- [ ] Add sync routes to main FastAPI app
- [ ] Integrate auto-push with DocumentProcessor
- [ ] Add startup auto-pull logic
- [ ] Create OpenAPI documentation for sync endpoints
- [ ] Add authentication/authorization for sync endpoints
- [ ] Write API integration tests
- [ ] Add rate limiting for sync operations

---

### Phase 5: Plugin Integration

**Goal**: Add sync UI and controls to Zotero plugin.

#### 5.1 Plugin UI Components

**File**: `plugin/src/ui/sync-dialog.js`

Add sync dialog to plugin UI:

```javascript
class SyncDialog {
    static async show() {
        // Show dialog with sync status for all libraries
        // Options: Pull, Push, Auto-sync
        // Progress bar for transfers
        // Show remote/local version comparison
    }

    static async syncLibrary(libraryId, direction) {
        // Call backend API
        // Show progress
        // Handle errors
    }
}
```

**Preferences Integration**:

Add sync settings to plugin preferences:

- Enable/disable auto-sync
- Choose backend (WebDAV/S3)
- Configure backend credentials
- Set auto-pull/push behavior

#### 5.2 Plugin Features

1. **Manual Sync Menu**
   - "Sync Vector Database" menu item
   - Per-library sync context menu

2. **Auto-Sync**
   - Sync before search (if remote is newer)
   - Sync after indexing (if auto-push enabled)

3. **Sync Status Indicator**
   - Show sync status in plugin UI
   - Indicator: synced, local-ahead, remote-ahead, conflict

**Tasks**:

- [ ] Create sync dialog UI component
- [ ] Add sync API client methods
- [ ] Integrate with plugin menu system
- [ ] Add preferences for sync configuration
- [ ] Implement auto-sync triggers
- [ ] Add sync status indicators
- [ ] Write plugin integration tests
- [ ] Update plugin documentation

---

### Phase 6: Advanced Features (Optional)

#### 6.1 Chunk-Level Delta Sync

For efficiency with large libraries and frequent updates.

**File**: `backend/services/delta_sync.py`

```python
class DeltaSyncService:
    """Efficient delta synchronization at chunk level."""

    async def compute_delta(self, library_id: str,
                           since_version: int) -> list[dict]:
        """
        Compute chunks changed since version.

        Uses chunk metadata item_version field to identify changes.
        """
        pass

    async def apply_delta(self, library_id: str,
                         delta: list[dict]) -> int:
        """Apply delta changes to local vector store."""
        pass

    async def sync_delta(self, library_id: str) -> dict:
        """
        Perform delta sync instead of full snapshot.

        Fallback to full sync if:
        - Delta too large (>50% of library)
        - Local/remote schema version mismatch
        - Corruption detected
        """
        pass
```

**Delta Format** (JSON):

```json
{
    "library_id": "6297749",
    "from_version": 12300,
    "to_version": 12345,
    "changes": [
        {
            "operation": "add",
            "item_key": "ABC123",
            "chunks": [
                {
                    "chunk_id": "...",
                    "vector": [...],
                    "payload": {...}
                }
            ]
        },
        {
            "operation": "delete",
            "item_key": "DEF456"
        },
        {
            "operation": "update",
            "item_key": "GHI789",
            "chunks": [...]
        }
    ],
    "checksum": "sha256:..."
}
```

#### 6.2 Compression Optimization

Use library-specific compression strategies:

```python
# Small libraries (<1000 chunks): gzip (fast)
# Medium libraries (1k-10k chunks): zstd (balanced)
# Large libraries (>10k chunks): xz (best compression)

def choose_compression(chunk_count: int) -> str:
    if chunk_count < 1000:
        return "gzip"
    elif chunk_count < 10000:
        return "zstd"
    else:
        return "xz"
```

#### 6.3 Multi-Library Batch Sync

Optimize syncing multiple libraries:

```python
async def sync_all_libraries(
    direction: Literal["pull", "push", "auto"] = "auto",
    parallel: int = 3  # Concurrent syncs
) -> dict:
    """Sync multiple libraries in parallel."""
    pass
```

#### 6.4 Sync Scheduling

Add cron-like scheduling for automatic sync:

```python
# In settings
SYNC_SCHEDULE_PULL="0 */6 * * *"  # Every 6 hours
SYNC_SCHEDULE_PUSH="0 1 * * *"    # Daily at 1 AM
```

**Tasks**:

- [ ] Implement delta sync computation
- [ ] Create delta format specification
- [ ] Add delta upload/download to storage backends
- [ ] Implement compression algorithm selection
- [ ] Add multi-library batch sync
- [ ] Implement sync scheduling with APScheduler
- [ ] Write performance benchmarks
- [ ] Document advanced features

---

## Configuration Updates

### Environment Variables

Add to `.env.dist`:

```bash
# =============================================================================
# Vector Database Sync Configuration
# =============================================================================

# Enable vector database synchronization
SYNC_ENABLED=false

# Storage backend: webdav, s3
SYNC_BACKEND=webdav

# Automatic sync behavior
SYNC_AUTO_PULL=true   # Pull on startup if remote is newer
SYNC_AUTO_PUSH=false  # Push after indexing completes

# Sync strategy: full, delta, hybrid
SYNC_STRATEGY=full

# Hybrid sync threshold (weeks before forcing full sync)
SYNC_HYBRID_FULL_THRESHOLD_WEEKS=4

# WebDAV Configuration (if SYNC_BACKEND=webdav)
SYNC_WEBDAV_URL=https://webdav.example.com
SYNC_WEBDAV_USERNAME=user
SYNC_WEBDAV_PASSWORD=password
SYNC_WEBDAV_BASE_PATH=/zotero-rag/vectors/

# S3 Configuration (if SYNC_BACKEND=s3)
SYNC_S3_BUCKET=my-zotero-vectors
SYNC_S3_REGION=us-east-1
SYNC_S3_PREFIX=zotero-rag/vectors/
SYNC_S3_ENDPOINT_URL=  # For S3-compatible services (MinIO, DigitalOcean Spaces)
SYNC_S3_ACCESS_KEY=
SYNC_S3_SECRET_KEY=

# Sync scheduling (cron format, leave empty to disable)
SYNC_SCHEDULE_PULL=
SYNC_SCHEDULE_PUSH=
```

### Settings Model

Update `backend/config/settings.py`:

```python
class Settings(BaseSettings):
    # ... existing settings ...

    # Sync configuration
    sync_enabled: bool = Field(default=False, description="Enable vector sync")
    sync_backend: Literal["webdav", "s3"] = Field(default="webdav")
    sync_auto_pull: bool = Field(default=True)
    sync_auto_push: bool = Field(default=False)
    sync_strategy: Literal["full", "delta", "hybrid"] = Field(default="full")
    sync_hybrid_full_threshold_weeks: int = Field(default=4)

    # WebDAV settings
    sync_webdav_url: Optional[str] = None
    sync_webdav_username: Optional[str] = None
    sync_webdav_password: Optional[str] = None
    sync_webdav_base_path: str = Field(default="/zotero-rag/vectors/")

    # S3 settings
    sync_s3_bucket: Optional[str] = None
    sync_s3_region: str = Field(default="us-east-1")
    sync_s3_prefix: str = Field(default="zotero-rag/vectors/")
    sync_s3_endpoint_url: Optional[str] = None
    sync_s3_access_key: Optional[str] = None
    sync_s3_secret_key: Optional[str] = None
```

---

## Testing Strategy

### Unit Tests

1. **Storage Backends**
   - Mock WebDAV/S3 servers
   - Test upload/download/exists/delete operations
   - Test metadata handling
   - Test error scenarios (network failures, auth errors)

2. **Snapshot Manager**
   - Test snapshot creation
   - Test snapshot restoration
   - Test checksum validation
   - Test corrupted snapshot handling

3. **Sync Service**
   - Test version comparison logic
   - Test pull/push operations
   - Test conflict detection
   - Test auto-resolution

### Integration Tests

1. **End-to-End Sync**
   - Index library → push → delete local → pull → verify
   - Test with real WebDAV server (testcontainers)
   - Test with MinIO (S3-compatible)

2. **Multi-Client Sync**
   - Two clients syncing same library
   - Test concurrent pushes
   - Test version conflicts

3. **Performance Tests**
   - Benchmark snapshot creation (various sizes)
   - Benchmark upload/download (various backends)
   - Benchmark delta sync vs full sync

---

## Deployment Considerations

### Storage Requirements

**Remote Storage Size Estimates**:

| Library Size | Items | Chunks  | Snapshot Size (gzip) | S3 Standard Cost* | WebDAV Equivalent |
|--------------|-------|---------|---------------------|-------------------|-------------------|
| Small        | 100   | 2,500   | ~15 MB              | $0.35/month       | Minimal           |
| Medium       | 1,000 | 25,000  | ~150 MB             | $3.45/month       | Negligible        |
| Large        | 10,000| 250,000 | ~1.5 GB             | $34.50/month      | Significant       |

*AWS S3 Standard pricing: ~$0.023/GB/month

### Network Considerations

- **Full sync bandwidth**: ~1.5 GB for 10k item library
- **Delta sync bandwidth**: ~10-50 MB typical (for 100 changed items)
- **Compression savings**: 70-85% (vectors compress well)

### Security

1. **Credentials Management**
   - Store in environment variables, never in code
   - Support AWS IAM roles (no keys needed)
   - Use WebDAV with HTTPS only

2. **Encryption**
   - All transfers use TLS
   - Optional: Encrypt snapshots at rest (AES-256)
   - S3 server-side encryption (SSE-S3)

3. **Access Control**
   - Implement API authentication for sync endpoints
   - Consider JWT tokens for plugin → backend auth
   - Support IAM policies for S3

---

## Migration Path

### For Existing Users

1. **No Action Required**: Sync is opt-in
2. **Enable Sync**: Configure storage backend, test with one library
3. **Gradual Rollout**: Enable auto-push after confidence established
4. **Full Adoption**: Enable auto-pull for seamless multi-device

### For New Users

1. **Guided Setup**: Plugin wizard for configuring sync on first run
2. **Preset Configurations**: Pre-configured for popular WebDAV providers (Nextcloud, ownCloud, Box)
3. **Documentation**: Step-by-step guides for each backend

---

## Alternative Approaches Considered

### 1. Qdrant Cloud

**Approach**: Use Qdrant's managed cloud service with native sync.

**Pros**:

- Built-in sync and replication
- No storage backend implementation needed
- Managed backups

**Cons**:

- Subscription cost ($49+/month for meaningful usage)
- Less control over data
- Privacy concerns for sensitive documents
- Vendor lock-in

**Decision**: Not recommended for open-source project. Users may prefer self-hosted solutions.

### 2. Direct Qdrant Replication

**Approach**: Run multiple Qdrant instances with replication.

**Pros**:

- Real-time sync
- Built-in conflict resolution

**Cons**:

- Requires always-on server infrastructure
- Complex setup for end users
- Higher resource usage

**Decision**: Not suitable for desktop application use case.

### 3. SQLite-Based Vector Storage

**Approach**: Replace Qdrant with SQLite + vector extension (e.g., sqlite-vss).

**Pros**:

- Single-file database (easy sync)
- Familiar tooling
- Simple backup/restore

**Cons**:

- Performance inferior to Qdrant for large datasets
- Less mature vector search capabilities
- Requires major refactoring

**Decision**: Not worth the trade-off for established Qdrant usage.

### 4. Git-Based Sync

**Approach**: Store vector data in Git LFS, sync via Git.

**Pros**:

- Version history
- Built-in conflict resolution
- Familiar workflow for developers

**Cons**:

- Git LFS not designed for binary vector data
- Poor performance for large files
- Confusing for non-technical users

**Decision**: Overkill and poor user experience.

---

## Success Metrics

### MVP Success Criteria

1. **Functionality**
   - [ ] Successfully sync library vectors between two machines
   - [ ] Automatic pull on startup when remote is newer
   - [ ] Manual push/pull via API and plugin UI

2. **Reliability**
   - [ ] 99% success rate for sync operations
   - [ ] Checksum validation prevents corruption
   - [ ] Graceful handling of network failures

3. **Performance**
   - [ ] Sync 1000-item library in <2 minutes (full)
   - [ ] Snapshot creation: <30 seconds for medium library
   - [ ] Download/upload: Limited by network bandwidth

4. **Usability**
   - [ ] One-click sync from plugin
   - [ ] Clear error messages
   - [ ] No manual intervention for normal operations

### Production Success Criteria

1. **Efficiency**
   - [ ] Delta sync 10x faster than full sync for <10% changes
   - [ ] Compression reduces bandwidth by 75%+

2. **Scalability**
   - [ ] Handle 10,000+ item libraries
   - [ ] Support 10+ libraries per user
   - [ ] Concurrent sync operations

---

## Documentation Plan

### User Documentation

1. **Quick Start Guide**
   - Enabling sync
   - Configuring WebDAV (with screenshots)
   - Configuring S3

2. **Use Cases**
   - Multi-device setup
   - Team collaboration
   - Backup strategy

3. **Troubleshooting**
   - Connection errors
   - Version conflicts
   - Storage quota issues

### Developer Documentation

1. **Architecture Overview**
   - Component diagram
   - Sync flow diagrams
   - Data formats

2. **API Reference**
   - Sync endpoints
   - Storage backend interface
   - Plugin integration

3. **Extending Storage Backends**
   - Adding new backends
   - Testing guidelines

---

## Timeline Estimate

**Phase 1**: Storage Abstraction - 3-4 days
**Phase 2**: Snapshot Management - 2-3 days
**Phase 3**: Sync Orchestration - 3-4 days
**Phase 4**: API Integration - 2 days
**Phase 5**: Plugin Integration - 2-3 days
**Phase 6**: Advanced Features - 5-7 days (optional)

**MVP Total**: 12-16 days
**Production-Ready**: 20-25 days

---

## Open Questions

1. **Snapshot Naming**: Use version number or timestamp?
   - Recommendation: Version number (library_6297749_v12345.tar.gz) for easy comparison

2. **Conflict Resolution**: How to handle diverged libraries?
   - Recommendation: Error + manual resolution (choose local or remote)
   - Future: Three-way merge using Zotero as source of truth

3. **Retention Policy**: Keep old snapshots or just latest?
   - Recommendation: Keep latest N versions (configurable, default 3) for rollback

4. **Delta Format**: JSON or binary?
   - Recommendation: JSON for MVP (easier debugging), binary (MessagePack) for optimization

5. **Partial Library Sync**: Sync subset of collections?
   - Recommendation: Phase 7 feature, library-level only for MVP

---

## References

### Qdrant Documentation

- [Snapshots API](https://qdrant.tech/documentation/concepts/snapshots/)
- [Distributed Deployment](https://qdrant.tech/documentation/guides/distributed_deployment/)

### Storage Protocols

- [WebDAV RFC 4918](https://datatracker.ietf.org/doc/html/rfc4918)
- [AWS S3 API Reference](https://docs.aws.amazon.com/s3/index.html)
- [S3-Compatible Services](https://min.io/docs/minio/linux/index.html)

### Best Practices

- [Syncing Large Binary Files](https://stackoverflow.com/questions/tagged/synchronization)
- [Vector Database Deployment Patterns](https://www.pinecone.io/learn/vector-database/)
- [Eventual Consistency Patterns](https://martinfowler.com/articles/patterns-of-distributed-systems/)

---

## Appendix: Code Examples

### Example: Simple Sync Flow

```python
from backend.services.vector_sync import VectorSyncService

# Initialize sync service
sync_service = VectorSyncService(
    vector_store=vector_store,
    snapshot_manager=snapshot_manager,
    storage_backend=webdav_storage,
    zotero_client=zotero_client
)

# Check if pull is needed
should_pull, reason = await sync_service.should_pull(library_id="6297749")
if should_pull:
    print(f"Pulling library: {reason}")
    stats = await sync_service.pull_library(library_id="6297749")
    print(f"Pulled {stats['chunks_restored']} chunks in {stats['restore_time']}s")

# After indexing, push to remote
stats = await sync_service.push_library(library_id="6297749")
print(f"Pushed {stats['chunks_pushed']} chunks, uploaded {stats['uploaded_bytes']} bytes")
```

### Example: Storage Backend Configuration

```python
from backend.storage.webdav import WebDAVStorage
from backend.storage.s3 import S3Storage

# WebDAV
storage = WebDAVStorage(
    base_url="https://cloud.example.com/remote.php/dav/files/user/",
    username="user",
    password="pass",
    base_path="zotero-rag/vectors/"
)

# S3
storage = S3Storage(
    bucket="my-vectors",
    region="us-east-1",
    prefix="zotero-rag/vectors/",
    access_key=os.getenv("AWS_ACCESS_KEY_ID"),
    secret_key=os.getenv("AWS_SECRET_ACCESS_KEY")
)

# MinIO (S3-compatible)
storage = S3Storage(
    bucket="vectors",
    endpoint_url="http://localhost:9000",
    access_key="minioadmin",
    secret_key="minioadmin",
    prefix="zotero-rag/"
)
```

---

## Conclusion

This implementation plan provides a robust, flexible solution for vector database synchronization with the following key benefits:

✅ **Pluggable Storage**: Support for WebDAV and S3 with easy extensibility
✅ **Version-Aware**: Leverages existing Zotero version tracking
✅ **Efficient**: Full snapshots for MVP, delta sync for optimization
✅ **User-Friendly**: Auto-sync, clear status indicators, one-click operations
✅ **Reliable**: Checksums, validation, error recovery
✅ **Self-Hosted**: No cloud dependencies, full data control

The phased approach allows for incremental development and testing, with MVP delivering core functionality and advanced features providing optimization for power users.
