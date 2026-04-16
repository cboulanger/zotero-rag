# Zotero RAG Application - Architecture Documentation

## Table of Contents

- [Overview](#overview)
- [System Architecture](#system-architecture)
- [Component Details](#component-details)
  - [Backend Services](#backend-services)
  - [Zotero Plugin](#zotero-plugin)
- [Data Flow](#data-flow)
- [Configuration System](#configuration-system)
- [Key Design Decisions](#key-design-decisions)
- [Performance Considerations](#performance-considerations)
- [Security & Privacy](#security--privacy)

---

## Overview

The Zotero RAG Application is a Retrieval-Augmented Generation (RAG) system that integrates with Zotero to enable semantic search and question answering across your research library. The system consists of two main components:

1. **FastAPI Backend**: Python-based service handling document indexing, vector search, and LLM inference
2. **Zotero Plugin**: JavaScript plugin providing a user interface within Zotero

The backend can run locally (on the same machine as Zotero) or on a remote server. In local mode the backend reads documents directly from the filesystem. In remote mode the plugin uploads document bytes over the network.

### Key Features

- **Semantic Search**: Ask natural language questions about your research library
- **Multi-Library Support**: Query across multiple Zotero libraries simultaneously
- **Local & Remote LLMs**: Support for both local models (quantized) and remote APIs (OpenAI, Anthropic, KISSKI)
- **Smart Citations**: Answers include source citations with page numbers and text anchors
- **Real-Time Progress**: Live progress updates during library indexing
- **Flexible Configuration**: Hardware presets for different deployment scenarios
- **Remote Server Support**: Backend can run on a separate machine with optional API key authentication

### Technology Stack

**Backend:**

- Python 3.12 with `uv` package manager
- FastAPI for REST API and Server-Sent Events (SSE)
- Qdrant for vector database
- sentence-transformers for embeddings
- transformers + bitsandbytes for local LLM inference
- PyZotero + direct local API for Zotero integration (local mode only)
- Kreuzberg for document extraction (PDF, HTML, DOCX, EPUB; Rust-based, native async)
- python-multipart for document upload (remote mode)

**Plugin:**

- JavaScript (Firefox extension environment)
- Zotero 7/8 plugin architecture
- HTML5 + CSS3 for UI (no XUL dependency)
- EventSource API for SSE streaming
- IOUtils API for local file reading (remote mode upload)

---

## System Architecture

### Local Mode (default)

```text
┌────────────────────────────────────────────────────────────────┐
│                      Zotero Plugin (Frontend)                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │   Dialog UI  │  │ Menu Item    │  │  Backend Client      │  │
│  │  - Question  │  │ - Tools Menu │  │  - HTTP/REST         │  │
│  │  - Libraries │  │              │  │  - SSE Streaming     │  │
│  │  - Progress  │  │              │  │  - Error Handling    │  │
│  └──────────────┘  └──────────────┘  └──────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
                              │
                   HTTP/SSE (configurable URL,
                   default: localhost:8119)
                              ▼
┌────────────────────────────────────────────────────────────────┐
│                    FastAPI Backend (Python)                    │
│                                                                │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │                      API Layer (REST/SSE)                 │ │
│  │  /api/config  /api/libraries  /api/index  /api/query      │ │
│  └───────────────────────────────────────────────────────────┘ │
│                              │                                 │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │                     Service Layer                         │ │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐   │ │
│  │  │ Document     │ │ RAG Engine   │ │ Embedding        │   │ │
│  │  │ Processor    │ │              │ │ Service          │   │ │
│  │  │              │ │ - Retrieval  │ │ - Local Models   │   │ │
│  │  │ - Extraction │ │ - Generation │ │ - Remote APIs    │   │ │
│  │  │ - Chunking   │ │ - Citations  │ │ - Caching        │   │ │
│  │  │ - Indexing   │ │              │ │                  │   │ │
│  │  └──────────────┘ └──────────────┘ └──────────────────┘   │ │
│  │                                                           │ │
│  │  ┌──────────────┐                  ┌──────────────────┐   │ │
│  │  │ LLM Service  │                  │ Zotero Client    │   │ │
│  │  │              │                  │ (local mode only)│   │ │
│  │  │ - Local      │                  │ - Local API      │   │ │
│  │  │ - Remote     │                  │ - HTTP Client    │   │ │
│  │  │ - Quantized  │                  │ - Library Access │   │ │
│  │  └──────────────┘                  └──────────────────┘   │ │
│  └───────────────────────────────────────────────────────────┘ │
│                              │                                 │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │                     Data Layer                            │ │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐   │ │
│  │  │ Vector Store │ │ Model Cache  │ │ Config Manager   │   │ │
│  │  │  (Qdrant)    │ │              │ │                  │   │ │
│  │  │              │ │ - Embeddings │ │ - Presets        │   │ │
│  │  │ - Chunks     │ │ - LLM Weights│ │ - Settings       │   │ │
│  │  │ - Dedup      │ │              │ │                  │   │ │
│  │  └──────────────┘ └──────────────┘ └──────────────────┘   │ │
│  └───────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────┘
                              │
                     HTTP (localhost:23119)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Zotero Desktop Application                 │
│                    (Local API on port 23119)                    │
│                                                                 │
│  - Libraries, Collections, Items                                │
│  - PDF Attachments                                              │
│  - Full-text Content                                            │
│  - Metadata (Authors, Titles, Years)                            │
└─────────────────────────────────────────────────────────────────┘
```

### Remote Mode

When the plugin's `backendURL` does not contain `localhost` or `127.0.0.1`, remote mode is activated. The plugin reads attachment bytes locally and uploads them to the backend; the backend needs no access to Zotero or the local filesystem.

```text
┌────────────────────────────────────────────────────────────────┐
│             Zotero Plugin + Zotero Desktop (local machine)     │
│                                                                │
│  Dialog UI → checkAndMonitorIndexing (remote branch)          │
│     ↓                                                          │
│  RemoteIndexer.indexLibrary()                                  │
│     1. GET /api/libraries/{id}/check-indexed                   │
│        (find which attachments need uploading)                 │
│     2. IOUtils.read(localPath) → bytes                         │
│     3. POST /api/index/document  (multipart: bytes + metadata) │
│     4. Show progress per document                              │
└────────────────────────────────────────────────────────────────┘
              │  HTTPS (configurable, X-API-Key auth)
              ▼
┌────────────────────────────────────────────────────────────────┐
│           FastAPI Backend (remote server / container)          │
│                                                                │
│  POST /api/index/document                                      │
│     → validate API key                                         │
│     → DocumentProcessor._process_attachment_bytes()           │
│        (dedup check → extract → embed → store)                 │
│                                                                │
│  POST /api/libraries/{id}/check-indexed                        │
│     → VectorStore.get_item_version() per attachment            │
│     → return needs_indexing + reason per attachment            │
└────────────────────────────────────────────────────────────────┘
```

**Mode detection:** `backendURL` is stored in the `extensions.zotero-rag.backendURL` Zotero preference (configurable in the plugin's Preferences pane, default `http://localhost:8119`). If the URL does not contain `localhost` or `127.0.0.1`, remote mode is used automatically.

---

## Component Details

### Backend Services

The backend is organized into a layered architecture with clear separation of concerns:

#### 1. API Layer

**Entry Point:** [backend/main.py](../backend/main.py)

- FastAPI application setup
- Optional API key middleware (all endpoints except `/`, `/health`, `/api/version`)
- Configurable CORS (`ALLOWED_ORIGINS` env var)
- Conditional Zotero connectivity check at startup (`REQUIRE_ZOTERO` env var)
- Lifespan context management

**Configuration API:** [backend/api/config.py](../backend/api/config.py)

- `GET /api/config` - Get available presets and current configuration
- `POST /api/config` - Update configuration settings
- `GET /api/version` - Version compatibility checking (exempt from API key auth)

**Libraries API:** [backend/api/libraries.py](../backend/api/libraries.py)

- `GET /api/libraries` - List available Zotero libraries
- `GET /api/libraries/{library_id}/status` - Check indexing status (legacy)
- `GET /api/libraries/{library_id}/index-status` - Get detailed indexing metadata (version tracking, item counts, timestamps)
- `POST /api/libraries/{library_id}/reset-index` - Mark library for hard reset (full reindex)
- `GET /api/libraries/indexed` - List all indexed libraries with metadata

**Indexing API:** [backend/api/indexing.py](../backend/api/indexing.py)

- `POST /api/index/library/{library_id}` - Start library indexing with mode selection (auto/incremental/full) — **local mode only**
  - Query parameters: `mode` (auto/incremental/full), `library_type`, `library_name`
- `GET /api/index/library/{library_id}/progress` - Stream indexing progress via SSE (supports `?api_key=` for EventSource compatibility)
- `POST /api/index/library/{library_id}/cancel` - Cancel ongoing indexing operation

**Document Upload API:** [backend/api/document_upload.py](../backend/api/document_upload.py)

New endpoints for remote mode:

- `POST /api/libraries/{library_id}/check-indexed` — accepts `CheckIndexedRequest` (list of attachment keys + versions); returns `CheckIndexedResponse` with `needs_indexing: bool` and `reason` (`"not_indexed"` | `"version_changed"` | `"up_to_date"`) per attachment
- `POST /api/index/document` — accepts multipart form data (`file`: raw bytes, `metadata`: JSON string); validates API key, runs `DocumentProcessor._process_attachment_bytes()`, returns `DocumentUploadResult`

**Query API:** [backend/api/query.py](../backend/api/query.py)

- `POST /api/query` - Submit RAG query and get answer with citations

#### 2. Service Layer

**Document Processor:** [backend/services/document_processor.py](../backend/services/document_processor.py)

- Orchestrates the complete indexing pipeline
- Supports incremental indexing (version-based change detection)
- Three indexing modes: auto, incremental, and full
- Fetches items from Zotero libraries with version tracking (local mode)
- Delegates extraction and chunking to a `DocumentExtractor` implementation
- Generates embeddings and stores chunks in vector database with version metadata
- Handles deduplication via content hashing
- `_process_attachment_bytes(file_bytes, mime_type, doc_metadata, ...)` — shared processing core used by both the local Zotero-API path and the remote upload endpoint
- Provides progress callbacks and cancellation support

**Document Extraction:** [backend/services/extraction/](../backend/services/extraction/)

Pluggable extraction adapter pattern. Supported MIME types: `application/pdf`, `text/html`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `application/epub+zip`.

- **`DocumentExtractor`** (ABC) — `extract_and_chunk(content: bytes, mime_type: str) → list[ExtractionChunk]`
- **`KreuzbergExtractor`** (default) — Rust-based, native async, 91+ formats via [Kreuzberg](https://kreuzberg.dev/). Chunking and page tracking built-in.
- **`LegacyExtractor`** — Wraps the original `PDFExtractor` (pypdf) + `TextChunker` (spaCy) pipeline. PDF-only; kept for fallback.
- **`create_document_extractor(backend, max_chunk_size, chunk_overlap, ocr_enabled)`** — factory function; backend selectable via `extractor_backend` setting.

**Embedding Service:** [backend/services/embeddings.py](../backend/services/embeddings.py)

- Abstract interface for embedding generation
- Local models via sentence-transformers
- Remote APIs (OpenAI, Cohere)
- Content-hash based caching
- Batch processing support

**LLM Service:** [backend/services/llm.py](../backend/services/llm.py)

- Abstract interface for LLM inference
- Local models with transformers + quantization (4-bit, 8-bit)
- Remote APIs (OpenAI, Anthropic, KISSKI)
- Lazy model loading
- Device-aware (CPU, CUDA, MPS)
- OpenAI-compatible API support

**RAG Query Engine:** [backend/services/rag_engine.py](../backend/services/rag_engine.py)

- Complete RAG pipeline implementation
- Query embedding generation
- Vector similarity search with library filtering
- Context assembly from retrieved chunks
- LLM prompt construction
- Answer generation with source tracking
- Citation formatting (item_id, page, text_anchor, score)

#### 3. Data Layer

**Vector Store:** [backend/db/vector_store.py](../backend/db/vector_store.py)

- Qdrant client wrapper
- Persistent storage in user data directory
- Three collections:
  - `document_chunks` - Document chunks with embeddings and version metadata
  - `deduplication` - Content-hash based deduplication tracking
  - `library_metadata` - Library-level indexing state
- CRUD operations for chunks and library metadata
- Version-aware chunk operations (get, delete by item)
- Similarity search with filtering
- Deduplication checking

**Document Models:** [backend/models/document.py](../backend/models/document.py)

- Pydantic models for type safety:
  - `DocumentMetadata` - Source document information
  - `ChunkMetadata` - Chunk-specific metadata with page numbers and version tracking
  - `DocumentChunk` - Text chunk with embedding and metadata
  - `SearchResult` - Search result with score
  - `DeduplicationRecord` - Deduplication tracking

**Library Models:** [backend/models/library.py](../backend/models/library.py)

- `LibraryIndexMetadata` - Library indexing state tracking:
  - Last indexed version number
  - Last indexed timestamp
  - Total items and chunks counts
  - Indexing mode (full/incremental)
  - Force reindex flag

**Configuration System:**

- **Presets:** [backend/config/presets.py](../backend/config/presets.py)
  - Hardware presets: `apple-silicon-32gb`, `high-memory`, `cpu-only`, `remote-openai`, `apple-silicon-kisski`, `remote-kisski`, `windows-test`
  - Model configurations for embeddings and LLMs
  - Memory budgets and quantization settings
- **Settings:** [backend/config/settings.py](../backend/config/settings.py)
  - Environment variable configuration
  - Path expansion for model weights and vector DB
  - Dynamic API key handling
  - Remote deployment settings (`api_key`, `allowed_origins`, `require_zotero`)

**Zotero Integration (local mode only):**

- **Local API Client:** [backend/zotero/local_api.py](../backend/zotero/local_api.py)
  - Direct HTTP interface to Zotero local server (localhost:23119)
  - Async operations for listing libraries, items, attachments
  - Version-aware item fetching with `?since=<version>` parameter
  - Library version range detection
  - PDF download and full-text extraction
  - No API key required

### Zotero Plugin

The plugin provides a user-friendly interface within Zotero for asking questions and creating note items with answers.

#### Plugin Architecture

**Bootstrap:** [plugin/src/bootstrap.js](../plugin/src/bootstrap.js)

- Plugin lifecycle management (install, startup, shutdown, uninstall)
- Window load/unload handlers
- Minimal code - delegates to main plugin object

**Main Plugin Logic:** [plugin/src/zotero-rag.js](../plugin/src/zotero-rag.js)

- Global `ZoteroRAG` object
- Menu integration (Tools → "Ask Question")
- Backend communication (HTTP + SSE)
- `backendURL` loaded from `extensions.zotero-rag.backendURL` preference
- `apiKey` loaded from `extensions.zotero-rag.apiKey` preference
- `getAuthHeaders(extra)` — builds `{"X-API-Key": ...}` header map when key is set
- `addApiKeyParam(url)` — appends `?api_key=` to SSE URLs (EventSource can't set headers)
- Library selection logic
- Note creation with HTML formatting
- Version compatibility checking
- Concurrent query management

**Dialog UI:** [plugin/src/dialog.xhtml](../plugin/src/dialog.xhtml) + [plugin/src/dialog.js](../plugin/src/dialog.js)

- HTML5-based dialog (no XUL dependency)
- Question input, library selection, progress display
- Indexing mode selection (auto/incremental/full)
- Library metadata display (last indexed, item counts, chunk counts)
- SSE streaming for indexing progress (API key appended as query param)
- Operation cancellation support (abort button)
- All `fetch()` calls include `X-API-Key` header via `plugin.getAuthHeaders()`
- Remote vs local mode branching in `checkAndMonitorIndexing()`
- Status messages and error handling

**Remote Indexer:** [plugin/src/remote_indexer.js](../plugin/src/remote_indexer.js)

Coordinates document upload when `backendURL` is remote. Loaded as a subscript in `dialog.xhtml`.

- `RemoteIndexer.indexLibrary({libraryId, libraryType, backendURL, getAuthHeaders, onProgress, isCancelled})`
  1. Collect all locally-stored attachments with indexable MIME types
  2. POST `/api/libraries/{id}/check-indexed` to find which need uploading
  3. For each attachment needing upload: `IOUtils.read(path)` → multipart `FormData` → POST `/api/index/document`
  4. Calls `onProgress` callback after each document
- `_collectAttachments()` — queries Zotero JS API, filters by storage type and MIME type
- `_checkIndexed()` — batch version check; falls back to "upload all" on error
- `_uploadAttachment()` — reads bytes and posts multipart form data with full item metadata

**Preferences:** [plugin/src/preferences.xhtml](../plugin/src/preferences.xhtml) + [plugin/src/preferences.js](../plugin/src/preferences.js)

- Backend URL configuration (`extensions.zotero-rag.backendURL`, default `http://localhost:8119`)
- API key configuration (`extensions.zotero-rag.apiKey`) — shown only when URL is not localhost/127.0.0.1
- Max concurrent queries setting
- HTML-based preferences pane
- Custom CSS styling: [plugin/src/preferences.css](../plugin/src/preferences.css)

**Localization:** [plugin/locale/en-US/zotero-rag.ftl](../plugin/locale/en-US/zotero-rag.ftl)

- Fluent localization format
- English strings (extensible to other languages)

**Build System:** [scripts/build-plugin.js](../scripts/build-plugin.js)

- Node.js build script
- Creates XPI archive from plugin source
- Output: `plugin/dist/zotero-rag-{version}.xpi`

---

## Data Flow

### Indexing Workflow — Local Mode

```text
1. User selects "Ask Question" from Tools menu
   ↓
2. Plugin fetches list of available libraries from backend
   ↓
3. Plugin displays library metadata (last indexed, item counts)
   ↓
4. User selects libraries, indexing mode (auto/incremental/full), and enters question
   ↓
5. Plugin checks indexing status for each library
   ↓
6. For libraries needing indexing:
   a. Plugin triggers indexing via POST /api/index/library/{id}?mode={mode}
   b. Backend starts background indexing task with cancellation support
   c. Plugin subscribes to SSE progress stream
   d. Progress updates displayed in real-time
   e. User can cancel operation at any time
   ↓
7. When all libraries indexed, plugin submits query
```

**Backend Indexing Pipeline (local mode):**

```text
1. DocumentProcessor.index_library(library_id, mode)
   ↓
2. Get or create library metadata from library_metadata collection
   ↓
3. Determine effective mode (auto → incremental if previously indexed, else full)
   ↓
4. Fetch items from Zotero via ZoteroLocalAPI:
   - Incremental mode: Use ?since=<last_version> to get only new/modified items
   - Full mode: Fetch all items
   ↓
5. Filter items with indexable attachments (PDF, HTML, DOCX, EPUB)
   ↓
6. For each item:
   a. Check for cancellation (abort if requested)
   b. Compare versions (incremental mode: skip if version unchanged)
   c. Delete old chunks if item updated (incremental mode)
   d. Download attachment file bytes
   e. Call _process_attachment_bytes() → extract → embed → store
   ↓
7. Update library metadata (last_indexed_version, timestamp, counts, mode)
   ↓
8. Return statistics (mode, items processed/added/updated, chunks added/deleted, timing)
```

### Indexing Workflow — Remote Mode

```text
1. Plugin detects remote backend (URL not localhost/127.0.0.1)
   ↓
2. RemoteIndexer.indexLibrary() runs:
   a. Collect all locally-stored attachments (Zotero JS API)
   b. POST /api/libraries/{id}/check-indexed → get list of which need uploading
   ↓
3. For each attachment needing upload:
   a. IOUtils.read(localFilePath) → bytes
   b. Build FormData: file bytes + JSON metadata (title, authors, year, DOI, etc.)
   c. POST /api/index/document with X-API-Key header
   d. Backend: validate → dedup check → _process_attachment_bytes() → store
   e. Plugin updates progress display
   ↓
4. When all attachments processed, plugin submits query
```

### Query Workflow

```text
1. User submits question with selected libraries
   ↓
2. Plugin sends POST /api/query request
   ↓
3. Backend RAGEngine.query():
   a. Generate query embedding (EmbeddingService)
   b. Search vector store for similar chunks (top_k, min_score)
   c. Filter by library_ids
   d. Assemble context from retrieved chunks
   e. Build LLM prompt with context
   f. Generate answer (LLMService)
   g. Extract source citations (item_id, page, text_anchor)
   ↓
4. Plugin receives QueryResult (answer + sources)
   ↓
5. Plugin creates note in current collection:
   - Question as heading
   - Answer as body
   - Citations as bulleted list with Zotero links
   - Metadata footer (timestamp, libraries)
   ↓
6. Success message displayed to user
```

---

## Configuration System

### Hardware Presets

The system includes hardware presets optimized for different deployment scenarios:

#### 1. `apple-silicon-32gb`

- **Target:** Apple Silicon Macs with 32GB RAM
- **Embedding:** nomic-ai/nomic-embed-text-v1.5 (Neural Engine, ~550MB)
- **LLM:** Mistral-7B-Instruct-v0.3 (4-bit quantized, ~4GB)
- **Total Memory:** ~10GB
- **Device:** MPS (Apple Silicon GPU)

#### 2. `high-memory`

- **Target:** Systems with >24GB RAM (GPU or Apple Silicon)
- **Embedding:** sentence-transformers/all-mpnet-base-v2
- **LLM:** Mistral-7B-Instruct-v0.3 (8-bit quantized)
- **Total Memory:** ~16GB
- **Device:** CUDA / auto

#### 3. `cpu-only`

- **Target:** Systems without GPU
- **Embedding:** all-MiniLM-L6-v2 (~80MB)
- **LLM:** TinyLlama-1.1B (4-bit quantized)
- **Total Memory:** ~3GB
- **Device:** CPU

#### 4. `remote-openai`

- **Target:** Using OpenAI or Anthropic APIs
- **Embedding:** OpenAI Embeddings API (remote)
- **LLM:** gpt-4o-mini or equivalent (remote)
- **Total Memory:** ~1GB (minimal local requirements)
- **API Key:** `OPENAI_API_KEY`

#### 5. `apple-silicon-kisski`

- **Target:** Apple Silicon (16–32GB) with GWDG KISSKI remote LLM
- **Embedding:** nomic-ai/nomic-embed-text-v1.5 (local, Neural Engine)
- **LLM:** mistral-large-instruct via KISSKI (remote, 128k context)
- **Total Memory:** ~2GB
- **API Key:** `KISSKI_API_KEY`

#### 6. `remote-kisski`

- **Target:** Any machine with GWDG KISSKI Academic Cloud
- **Embedding:** all-MiniLM-L6-v2 (local, for privacy)
- **LLM:** mistral-large-instruct via KISSKI (remote, 128k context)
- **Total Memory:** ~1GB
- **API Key:** `KISSKI_API_KEY`
- **Base URL:** `https://chat-ai.academiccloud.de/v1`

#### 7. `windows-test`

- **Target:** Windows (avoids PyTorch local models)
- **Embedding:** OpenAI Embeddings API (remote)
- **LLM:** mistral-large-instruct via KISSKI (remote)
- **Total Memory:** ~0.5GB (everything remote)
- **API Keys:** `OPENAI_API_KEY`, `KISSKI_API_KEY`

### Configuration Files

**.env (from .env.dist template):**

```bash
# Hardware preset selection
MODEL_PRESET=cpu-only

# Storage paths
MODEL_CACHE_DIR=~/.cache/zotero-rag/models
VECTOR_DB_PATH=~/.local/share/zotero-rag/qdrant

# API keys (for remote presets)
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
KISSKI_API_KEY=your-kisski-key

# Remote server deployment (optional)
API_KEY=your-secret-key          # Required X-API-Key header when set
ALLOWED_ORIGINS=https://myhost   # CORS allowed origins (default: *)
REQUIRE_ZOTERO=false             # Skip Zotero connectivity check (remote deployments)
```

**Plugin Preferences:**

```
extensions.zotero-rag.backendURL = http://localhost:8119
extensions.zotero-rag.apiKey     = (empty for local, set for remote)
extensions.zotero-rag.maxQueries = 5
```

The `backendURL` preference is the single configuration point for server location. Changing it to a remote URL automatically switches the plugin to remote mode.

---

## Key Design Decisions

### 1. Zotero Local API vs PyZotero

**Decision:** Use Zotero Local API (localhost:23119) as primary interface for local mode

**Rationale:**

- No API key required
- Direct access to local data
- Faster than cloud API
- No rate limiting
- Access to full-text content

### 2. Vector Database Choice

**Decision:** Qdrant

**Rationale:**

- Excellent Python client
- Persistent local storage
- Efficient similarity search
- Payload filtering capabilities
- No external service required

### 3. Embedding Strategy

**Decision:** Content-hash based caching

**Rationale:**

- Avoid recomputing embeddings for same text
- Significant performance improvement for re-indexing
- SHA256 hash ensures uniqueness
- Stored in vector database for persistence

### 4. Document Extraction Adapter Pattern

**Decision:** Pluggable `DocumentExtractor` ABC with Kreuzberg as the default backend

**Rationale:**

- Decouples the indexing pipeline from any specific extraction library
- Kreuzberg: Rust-based, native async, 91+ formats, 9-50× faster than pypdf+spaCy
- Legacy pypdf+spaCy fallback preserved for comparison and compatibility
- Supports HTML, DOCX, EPUB attachments in addition to PDF
- Backend selectable at runtime via `extractor_backend` setting without code changes

**Implementation:**

- `DocumentExtractor` ABC mirrors existing `EmbeddingService`/`LLMService` pattern
- `ExtractionChunk` carries `text`, `page_number`, and `chunk_index`
- `create_document_extractor()` factory mirrors `create_embedding_service()`
- `KreuzbergExtractor` uses `ExtractionConfig(chunking=ChunkingConfig(...), disable_ocr=...)`

### 5. LLM Flexibility

**Decision:** Support both local (quantized) and remote (API) models

**Rationale:**

- Local: Privacy, no cost, offline capability
- Remote: Higher quality, no hardware requirements
- Hardware presets make configuration easy
- Users choose based on their needs

### 6. Plugin UI Technology

**Decision:** HTML5 + CSS3 (no XUL dependency)

**Rationale:**

- Future-proof for Zotero 8+
- Standards-compliant web technologies
- Easier maintenance than legacy XUL
- Better styling control with CSS

### 7. Progress Streaming

**Decision:** Server-Sent Events (SSE)

**Rationale:**

- Simple unidirectional streaming
- Native browser support (EventSource)
- No WebSocket complexity
- Perfect for progress updates

EventSource limitation: cannot set custom headers. Worked around by supporting `?api_key=` query parameter in the SSE endpoint alongside the `X-API-Key` header.

### 8. Incremental Indexing

**Decision:** Version-based incremental indexing with metadata tracking

**Rationale:**

- 80% faster updates by processing only new/modified items
- Leverages Zotero's version field for efficient change detection
- Library-level metadata tracks indexing state
- Three modes (auto/incremental/full) give users control
- Prevents wasted reprocessing of unchanged documents

**Implementation:**

- Each chunk stores `item_version` and `attachment_version`
- `library_metadata` collection tracks `last_indexed_version`
- Zotero API `?since=<version>` parameter fetches only changes
- Automatic detection of metadata updates (title, author changes)
- Hard reset API for manual full reindexing

### 9. Remote Server Support

**Decision:** Plugin-side file upload with optional API key authentication

**Rationale:**

- Backend on a remote/more-powerful machine requires a different document delivery mechanism
- Document extraction and embedding are bytes-based throughout — no filesystem assumptions
- Shared `_process_attachment_bytes()` core avoids code duplication between local and remote paths
- API key is optional: local mode works without any authentication, remote mode enforces it when `API_KEY` is set

**Implementation:**

- Mode detection: `backendURL` not containing `localhost`/`127.0.0.1`
- Plugin reads files via Firefox `IOUtils.read()` — available in Zotero's JS environment
- `check-indexed` batch endpoint minimises unnecessary uploads (only changed/new attachments)
- `X-API-Key` header for all endpoints; `?api_key=` query param for SSE (EventSource limitation)
- `REQUIRE_ZOTERO=false` skips Zotero local API connectivity check for remote deployments

### 10. Operation Cancellation

**Decision:** Cooperative cancellation with backend cleanup

**Rationale:**

- Prevents zombie processes from piling up
- User control over long-running operations
- Graceful shutdown preserves database integrity
- Cancellation check in processing loops

**Implementation:**

- Frontend: Cancel button aborts SSE streams and calls cancel endpoint
- Backend: Job status flag checked in each iteration
- Document processor raises `RuntimeError` on cancellation
- SSE stream sends cancellation event to frontend

### 11. Testing Strategy

**Decision:** Mock-based unit tests + real integration tests

**Rationale:**

- Fast unit tests without external dependencies
- Integration tests validate with real data
- Separation allows CI/CD and manual testing
- Comprehensive coverage without slowdowns

---

## Performance Considerations

### Indexing Performance

**Factors:**

- PDF size and count
- Embedding model speed (local vs remote)
- Chunk size and overlap
- Vector database batch insertion
- Indexing mode (incremental vs full)

**Optimization:**

- **Incremental indexing** - 80% faster by processing only changes
- Version-based change detection using Zotero's `?since=` parameter
- Batch embedding generation
- Content-hash deduplication (skip re-indexing)
- Progress callbacks for user feedback
- Async I/O for Zotero API calls
- Cancellation support prevents wasted processing

### Query Performance

**Factors:**

- Vector search speed (top_k parameter)
- LLM inference time (model size, quantization)
- Context assembly (retrieved chunk count)

**Optimization:**

- Efficient vector search with Qdrant
- Quantized models reduce memory and latency
- Configurable top_k and min_score thresholds
- Embedding cache for repeated queries

### Memory Footprint

**Hardware Presets:**

- `apple-silicon-32gb`: ~10GB
- `high-memory`: ~16GB
- `cpu-only`: ~3GB
- `remote-*` / `windows-test`: ~0.5–2GB (minimal local)

**Strategies:**

- Lazy model loading (load on first use)
- Quantization (4-bit, 8-bit) reduces model size
- Configurable model cache directory
- Vector DB persistent storage (not in RAM)

---

## Security & Privacy

### Data Privacy

- Document bytes and embeddings stay on the backend server (local or chosen remote)
- No cloud storage of research documents unless a remote backend is explicitly configured
- Optional remote LLM APIs use HTTPS

### API Authentication

- **Local mode (default):** No authentication required; backend only reachable on localhost
- **Remote mode:** Set `API_KEY` env var on the backend to require `X-API-Key: <key>` on all requests
  - Health check (`/`, `/health`, `/api/version`) is exempt from API key check
  - SSE endpoint accepts key as `?api_key=` query param (EventSource API limitation)
  - Plugin stores key in `extensions.zotero-rag.apiKey` preference; UI shows it only when URL is remote

### CORS Configuration

- Default `ALLOWED_ORIGINS=["*"]` works for local development
- For remote deployments set `ALLOWED_ORIGINS=https://your-domain` to restrict origins

### Plugin Security

- HTML escaping prevents XSS in note content
- Backend URL validation in preferences
- No external script loading

### Deployment Recommendation

For remote deployments, run the backend behind a reverse proxy (e.g., Caddy or nginx) with TLS termination. Set `API_KEY`, restrict `ALLOWED_ORIGINS`, and set `REQUIRE_ZOTERO=false`.

---

## Implementation Documentation

- [Remote Server Support Implementation](implementation/remote-server-support.md)
- [Incremental Indexing Implementation](implementation/incremental-indexing.md)

### CLI Documentation

- [CLI Commands Reference](cli.md)

### External Documentation

- [Zotero Plugin Development](https://www.zotero.org/support/dev/client_coding)
- [Zotero 8 for Developers](https://www.zotero.org/support/dev/zotero_8_for_developers)
- [Qdrant Documentation](https://qdrant.tech/documentation/)
- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [sentence-transformers](https://www.sbert.net/)

---

## Recent Updates

**Version 1.1 - January 2025:**
- Added incremental indexing with version-based change detection
- Implemented operation cancellation support
- Added library metadata tracking (last indexed, item counts, version numbers)
- Enhanced API with new endpoints for index status and cancellation
- Improved plugin UI with mode selection and status display

**Version 1.3 - April 2025:**
- Replaced monolithic PDF extraction pipeline with pluggable `DocumentExtractor` adapter pattern
- Introduced Kreuzberg as the default extraction backend (Rust-based, native async, 91+ formats)
- Extended indexable MIME types: PDF, HTML, DOCX, EPUB
- Added `extractor_backend` setting (`kreuzberg` or `legacy`)
- Legacy pypdf + spaCy pipeline retained as `LegacyExtractor` fallback

**Version 1.4 - April 2026:**

- **Remote server support**: backend can now run on a separate machine
- New document upload API: `POST /api/index/document` (multipart bytes + metadata) and `POST /api/libraries/{id}/check-indexed` (batch version check)
- Extracted `DocumentProcessor._process_attachment_bytes()` as shared processing core for both local and remote paths
- Plugin-side `RemoteIndexer` (`plugin/src/remote_indexer.js`): reads attachment bytes via `IOUtils.read()`, uploads via multipart form data
- Automatic local/remote mode detection based on `backendURL` (configurable via plugin preferences)
- Optional API key authentication (`API_KEY` env var, `X-API-Key` header; `?api_key=` param for SSE)
- Configurable CORS origins (`ALLOWED_ORIGINS` env var)
- `REQUIRE_ZOTERO=false` setting to skip Zotero local API check in remote deployments
- Plugin preferences: API key field shown/hidden based on whether URL is local or remote
- Updated hardware presets: `apple-silicon-32gb`, `high-memory`, `cpu-only`, `remote-openai`, `apple-silicon-kisski`, `remote-kisski`, `windows-test`

---

**Document Version:** 1.4
**Last Updated:** April 2026
