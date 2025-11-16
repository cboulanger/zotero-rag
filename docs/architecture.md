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

### Key Features

- **Semantic Search**: Ask natural language questions about your research library
- **Multi-Library Support**: Query across multiple Zotero libraries simultaneously
- **Local & Remote LLMs**: Support for both local models (quantized) and remote APIs (OpenAI, Anthropic, KISSKI)
- **Smart Citations**: Answers include source citations with page numbers and text anchors
- **Real-Time Progress**: Live progress updates during library indexing
- **Flexible Configuration**: Hardware presets for different deployment scenarios

### Technology Stack

**Backend:**

- Python 3.12 with `uv` package manager
- FastAPI for REST API and Server-Sent Events (SSE)
- Qdrant for vector database
- sentence-transformers for embeddings
- transformers + bitsandbytes for local LLM inference
- PyZotero + direct local API for Zotero integration

**Plugin:**

- JavaScript (Firefox extension environment)
- Zotero 7/8 plugin architecture
- HTML5 + CSS3 for UI (no XUL dependency)
- EventSource API for SSE streaming

---

## System Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                      Zotero Plugin (Frontend)                    │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │   Dialog UI  │  │ Menu Item    │  │  Backend Client      │  │
│  │  - Question  │  │ - Tools Menu │  │  - HTTP/REST         │  │
│  │  - Libraries │  │              │  │  - SSE Streaming     │  │
│  │  - Progress  │  │              │  │  - Error Handling    │  │
│  └──────────────┘  └──────────────┘  └──────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ HTTP/SSE (localhost:8119)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    FastAPI Backend (Python)                      │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │                      API Layer (REST/SSE)                   │ │
│  │  /api/config  /api/libraries  /api/index  /api/query       │ │
│  └────────────────────────────────────────────────────────────┘ │
│                              │                                   │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │                     Service Layer                           │ │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐   │ │
│  │  │ Document     │ │ RAG Engine   │ │ Embedding        │   │ │
│  │  │ Processor    │ │              │ │ Service          │   │ │
│  │  │              │ │ - Retrieval  │ │ - Local Models   │   │ │
│  │  │ - PDF Extract│ │ - Generation │ │ - Remote APIs    │   │ │
│  │  │ - Chunking   │ │ - Citations  │ │ - Caching        │   │ │
│  │  │ - Indexing   │ │              │ │                  │   │ │
│  │  └──────────────┘ └──────────────┘ └──────────────────┘   │ │
│  │                                                              │ │
│  │  ┌──────────────┐                  ┌──────────────────┐   │ │
│  │  │ LLM Service  │                  │ Zotero Client    │   │ │
│  │  │              │                  │                  │   │ │
│  │  │ - Local      │                  │ - Local API      │   │ │
│  │  │ - Remote     │                  │ - HTTP Client    │   │ │
│  │  │ - Quantized  │                  │ - Library Access │   │ │
│  │  └──────────────┘                  └──────────────────┘   │ │
│  └────────────────────────────────────────────────────────────┘ │
│                              │                                   │
│  ┌────────────────────────────────────────────────────────────┐ │
│  │                     Data Layer                              │ │
│  │  ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐   │ │
│  │  │ Vector Store │ │ Model Cache  │ │ Config Manager   │   │ │
│  │  │  (Qdrant)    │ │              │ │                  │   │ │
│  │  │              │ │ - Embeddings │ │ - Presets        │   │ │
│  │  │ - Chunks     │ │ - LLM Weights│ │ - Settings       │   │ │
│  │  │ - Dedup      │ │              │ │                  │   │ │
│  │  └──────────────┘ └──────────────┘ └──────────────────┘   │ │
│  └────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ HTTP (localhost:23119)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Zotero Desktop Application                  │
│                    (Local API on port 23119)                     │
│                                                                  │
│  - Libraries, Collections, Items                                │
│  - PDF Attachments                                              │
│  - Full-text Content                                            │
│  - Metadata (Authors, Titles, Years)                            │
└─────────────────────────────────────────────────────────────────┘
```

---

## Component Details

### Backend Services

The backend is organized into a layered architecture with clear separation of concerns:

#### 1. API Layer

**Entry Point:** [backend/main.py](../backend/main.py)

- FastAPI application setup
- CORS configuration for local development
- Lifespan context management
- Health check endpoint

**Configuration API:** [backend/api/config.py](../backend/api/config.py)

- `GET /api/config` - Get available presets and current configuration
- `POST /api/config` - Update configuration settings
- `GET /api/version` - Version compatibility checking

**Libraries API:** [backend/api/libraries.py](../backend/api/libraries.py)

- `GET /api/libraries` - List available Zotero libraries
- `GET /api/libraries/{library_id}/status` - Check indexing status (legacy)
- `GET /api/libraries/{library_id}/index-status` - Get detailed indexing metadata (version tracking, item counts, timestamps)
- `POST /api/libraries/{library_id}/reset-index` - Mark library for hard reset (full reindex)
- `GET /api/libraries/indexed` - List all indexed libraries with metadata

**Indexing API:** [backend/api/indexing.py](../backend/api/indexing.py)

- `POST /api/index/library/{library_id}` - Start library indexing with mode selection (auto/incremental/full)
  - Query parameters: `mode` (auto/incremental/full), `library_type`, `library_name`
- `GET /api/index/library/{library_id}/progress` - Stream indexing progress via SSE
- `POST /api/index/library/{library_id}/cancel` - Cancel ongoing indexing operation

**Query API:** [backend/api/query.py](../backend/api/query.py)

- `POST /api/query` - Submit RAG query and get answer with citations

#### 2. Service Layer

**Document Processor:** [backend/services/document_processor.py](../backend/services/document_processor.py)

- Orchestrates the complete indexing pipeline
- Supports incremental indexing (version-based change detection)
- Three indexing modes: auto, incremental, and full
- Fetches items from Zotero libraries with version tracking
- Extracts text from PDFs with page tracking
- Chunks text semantically
- Generates embeddings
- Stores chunks in vector database with version metadata
- Handles deduplication via content hashing
- Provides progress callbacks and cancellation support

**PDF Extractor:** [backend/services/pdf_extractor.py](../backend/services/pdf_extractor.py)

- Extracts text from PDF files using pypdf
- Tracks page numbers for each text segment
- Handles corrupted/invalid PDFs gracefully
- Supports byte streams and file paths

**Text Chunker:** [backend/services/chunking.py](../backend/services/chunking.py)

- Semantic chunking using spaCy (lazy model loading)
- Sentence-boundary aware chunking
- Configurable chunk size and overlap
- Generates text previews (first 5 words) for citation anchors
- Content hash generation for deduplication

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
  - `library_metadata` - Library-level indexing state (NEW)
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
  - Hardware presets: `mac-mini-m4-16gb`, `cpu-only`, `gpu-high-memory`, `remote-openai`, `remote-kisski`
  - Model configurations for embeddings and LLMs
  - Memory budgets and quantization settings
- **Settings:** [backend/config/settings.py](../backend/config/settings.py)
  - Environment variable configuration
  - Path expansion for model weights and vector DB
  - Dynamic API key handling

**Zotero Integration:**

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
- Library selection logic
- Note creation with HTML formatting
- Version compatibility checking
- Concurrent query management

**Dialog UI:** [plugin/src/dialog.xhtml](../plugin/src/dialog.xhtml) + [plugin/src/dialog.js](../plugin/src/dialog.js)

- HTML5-based dialog (no XUL dependency)
- Question input, library selection, progress display
- Indexing mode selection (auto/incremental/full)
- Library metadata display (last indexed, item counts, chunk counts)
- SSE streaming for indexing progress
- Operation cancellation support (abort button)
- Status messages and error handling
- Inline CSS styling

**Preferences:** [plugin/src/preferences.xhtml](../plugin/src/preferences.xhtml) + [plugin/src/preferences.js](../plugin/src/preferences.js)

- Backend URL configuration
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

### Indexing Workflow

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

**Backend Indexing Pipeline:**

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
5. Filter items with PDF attachments
   ↓
6. For each item:
   a. Check for cancellation (abort if requested)
   b. Compare versions (incremental mode: skip if version unchanged)
   c. Delete old chunks if item updated (incremental mode)
   d. Download PDF file
   e. Extract text with page numbers (PDFExtractor)
   f. Check for duplicates (content hash)
   g. Chunk text semantically (TextChunker)
   h. Generate embeddings (EmbeddingService)
   i. Store chunks with version metadata in vector database
   j. Call progress callback
   ↓
7. Update library metadata (last_indexed_version, timestamp, counts, mode)
   ↓
8. Return statistics (mode, items processed/added/updated, chunks added/deleted, timing)
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

The system includes five hardware presets optimized for different deployment scenarios:

#### 1. `mac-mini-m4-16gb` (Default)

- **Target:** Mac Mini M4 with 16GB RAM
- **Embedding:** nomic-embed-text-v1.5 (~550MB)
- **LLM:** Qwen2.5-3B-Instruct (4-bit quantized, ~2GB)
- **Total Memory:** ~6-7GB (leaves headroom for system and Qdrant)
- **Device:** MPS (Apple Silicon GPU)

#### 2. `cpu-only`

- **Target:** Systems without GPU
- **Embedding:** all-MiniLM-L6-v2 (~80MB)
- **LLM:** TinyLlama-1.1B (4-bit quantized)
- **Total Memory:** ~2-3GB
- **Device:** CPU

#### 3. `gpu-high-memory`

- **Target:** Systems with dedicated GPU and >24GB RAM
- **Embedding:** sentence-transformers/all-mpnet-base-v2
- **LLM:** Mistral-7B-Instruct (8-bit quantized)
- **Total Memory:** ~10-12GB
- **Device:** CUDA

#### 4. `remote-openai`

- **Target:** Using OpenAI or Anthropic APIs
- **Embedding:** all-MiniLM-L6-v2 (local, for privacy)
- **LLM:** GPT-4, GPT-3.5, or Claude (remote)
- **Total Memory:** ~1GB (minimal local requirements)
- **API Key:** `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`

#### 5. `remote-kisski`

- **Target:** GWDG KISSKI Academic Cloud
- **Embedding:** all-MiniLM-L6-v2 (local, for privacy)
- **LLM:** meta-llama/Llama-3.3-70B-Instruct (128k context)
- **Total Memory:** ~1GB
- **Base URL:** `https://chat-ai.academiccloud.de/v1`
- **API Key:** `KISSKI_API_KEY`

### Configuration Files

**.env (from .env.dist template):**

```bash
# Hardware preset selection
MODEL_PRESET=mac-mini-m4-16gb

# Storage paths
MODEL_CACHE_DIR=~/.cache/zotero-rag/models
VECTOR_DB_PATH=~/.local/share/zotero-rag/qdrant

# API keys (for remote presets)
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
KISSKI_API_KEY=your-kisski-key
```

**Plugin Preferences:**

```
extensions.zotero-rag.backendURL = http://localhost:8119
extensions.zotero-rag.maxQueries = 5
```

---

## Key Design Decisions

### 1. Zotero Local API vs PyZotero

**Decision:** Use Zotero Local API (localhost:23119) as primary interface

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

### 4. Chunking Approach

**Decision:** Semantic chunking with spaCy at sentence boundaries

**Rationale:**

- Preserves semantic coherence
- Better than fixed-size chunking for academic papers
- Maintains document structure (paragraphs)
- Page number tracking for precise citations

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

### 9. Operation Cancellation

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

### 10. Testing Strategy

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

- `mac-mini-m4-16gb`: ~6-7GB
- `cpu-only`: ~2-3GB
- `gpu-high-memory`: ~10-12GB
- `remote-*`: ~1GB (minimal)

**Strategies:**

- Lazy model loading (load on first use)
- Quantization (4-bit, 8-bit) reduces model size
- Configurable model cache directory
- Vector DB persistent storage (not in RAM)

---

## Security & Privacy

### Local-First Architecture

- All data stays local (PDFs, embeddings, vector DB)
- No cloud storage of research documents
- Local API requires no authentication
- Optional remote LLM APIs use HTTPS

### Plugin Security

- HTML escaping prevents XSS in note content
- Backend URL validation in preferences
- CORS configured for localhost only
- No external script loading

### API Security

- CORS middleware for local development
- No authentication (local-only deployment)
- Future: Add token-based auth for remote access

### Implementation Documentation

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

---

**Document Version:** 1.1
**Last Updated:** January 2025
**Project Status:** Phase 4 (Integration & Polish) - Step 5 Complete (Incremental Indexing)
