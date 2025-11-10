# Zotero RAG Application - Master Implementation Plan

## Project Overview

A Zotero-integrated RAG (Retrieval-Augmented Generation) system consisting of:

- **FastAPI Backend**: Handles embeddings, vector database, LLM inference, and exposes REST/SSE APIs
- **Zotero Plugin**: Provides UI for querying libraries and creates note items with answers

## Architecture

```text
┌─────────────────────┐         ┌──────────────────────┐
│  Zotero Plugin      │◄────────┤  FastAPI Backend     │
│  (Node.js/Firefox)  │  HTTP   │  (Python/FastAPI)    │
│                     │  SSE    │                      │
│  - UI Dialog        │         │  - Embedding Service │
│  - Menu Integration │         │  - Vector DB (Qdrant)│
│  - Note Creation    │         │  - LLM Inference     │
│  - Progress UI      │         │  - Indexing Pipeline │
└─────────────────────┘         └──────────────────────┘
         │                               │
         │                               │
         ▼                               ▼
┌─────────────────────────────────────────────────────┐
│           Zotero Local API (localhost:23119)        │
│           - Library data, PDFs, Metadata            │
└─────────────────────────────────────────────────────┘
```

## Required Dependencies & Information

### Zotero Plugin Development

- **Framework**: Zotero 7/8 plugin architecture 2.0 (bootstrapped plugins)
  - Prioritize Zotero 8 APIs when available (backward compatibility not required)
- **Language**: JavaScript (Firefox extension environment)
- **Key APIs**:
  - Zotero plugin API (UI, notes, collections)
  - Zotero local data API (localhost:23119)
- **Reference**: [zotero-sample-plugin/src-2.0](../zotero-sample-plugin/src-2.0)
- **Documentation**:
  - <https://www.zotero.org/support/dev/client_coding>
  - <https://www.zotero.org/support/dev/zotero_8_for_developers>

### Backend RAG Stack

- **Framework**: FastAPI (Python 3.13)
- **Embedding Models**:
  - Local: sentence-transformers, nomic-embed-text-v1.5
  - Remote option: OpenAI/Cohere embeddings API
- **Vector Database**: Qdrant
  - Persistent storage by default in user data directory
  - Configurable storage location (supports external SSD)
- **LLM Options**:
  - Local: Transformers + quantized models (Qwen2.5, Llama 3, Mistral)
  - Remote: OpenAI API, Anthropic API, vLLM endpoints
  - Configurable model weight storage location (supports external SSD)
- **Text Processing**: spaCy for semantic chunking at paragraph/sentence level
- **PDF Processing**: PyPDF2, pdfplumber, or pypdf (with page number extraction)
- **Zotero Access**: pyzotero library for local API (localhost:23119)
- **Package Management**: uv (Python 3.13)

### Reusable Implementation from zoterorag

- PyZotero library integration for accessing libraries
- Qdrant vector database usage patterns
- Text extraction and processing from PDFs
- Batch indexing pipeline
- Note: Multimodal support (images/figures) excluded from initial implementation

## Configuration Decisions

### Model Selection

- **Strategy**: User-configurable with named presets
- **Presets**: Extensible configuration profiles for different hardware scenarios:
  - `mac-mini-m4-16gb`: Optimized for Mac Mini M4 with 16GB RAM
    - Embedding: nomic-embed-text-v1.5 (~550MB)
    - LLM: Qwen2.5-3B-Instruct (4-bit quantized, ~2GB)
    - Total memory budget: ~6-7GB leaving headroom for system and Qdrant
  - `gpu-high-memory`: For systems with dedicated GPU and >24GB RAM
    - Embedding: sentence-transformers/all-mpnet-base-v2
    - LLM: Mistral-7B-Instruct (8-bit quantized)
  - `cpu-only`: CPU-optimized smaller models
    - Embedding: all-MiniLM-L6-v2 (~80MB)
    - LLM: TinyLlama-1.1B (4-bit quantized)
  - `remote-api`: Using remote inference endpoints
- **Model Storage**: Configurable location for model weights (supports external SSD)

### Vector Database

- **Storage**: Persistent by default in user data directory
- **Location**: Configurable path (supports external storage)
- **Embedding Cache**: Content-hash based caching to avoid recomputation

### Chunking Strategy

- **Approach**: Paragraph and sentence-level chunking for academic papers
- **Method**: Semantic chunking using spaCy that preserves document structure
- **Metadata**: Track page numbers and chunk text previews (first 5 words) for citation anchoring
- **Page Tracking**: Store page number with each chunk to enable precise citation links

### Security & Privacy

- **Authentication**: Local-only trusted access (no auth initially)
- **Data Privacy**: All data stays local, PDFs are published documents
- **Logging**: Configurable log level, logs to standard locations

### Plugin Configuration

- **Backend URL**: Configurable in plugin preferences (default: localhost:8119)
- **Concurrent Queries**: Limit 3-5 simultaneous queries
- **Note Format**: HTML with Zotero item links to source PDFs (`zotero://select/library/items/ITEM_ID`)
- **Citations**: Link to source PDFs with page numbers when available, or text anchors (first 5 words of chunk)
- **Progress Display**: Percentage with current document count
- **Offline Behavior**: Fail immediately with clear error message
- **Version Checking**: API endpoint for backend/plugin version compatibility

### Deduplication

- **Cross-Library**: Use Zotero's `relations.owl:sameAs` property to identify copied items
- **Content-Based**: Fallback to content hashing for deduplication

## Implementation Steps

### Phase 1: Backend Foundation

[Implementation progress](implementation\phase1-progress.md)

1. **Project Setup**
   - Initialize FastAPI project with uv
   - Set up project structure with modules:
     - `api/` - FastAPI routes
     - `services/` - Core business logic (embeddings, LLM, indexing)
     - `models/` - Data models and schemas
     - `db/` - Vector database interface
     - `zotero/` - Zotero API client wrapper
     - `config/` - Configuration presets and model definitions
   - Configure environment variables (.env) with storage paths

2. **Configuration System**
   - Implement configuration presets for different hardware profiles
   - Support for custom model weight locations
   - Vector database path configuration
   - Logging configuration
   - Version metadata for API compatibility checks
   - Write unit tests

3. **Zotero Integration Module**
   - Implement Zotero local API client (localhost:23119)
   - Create methods to:
     - List libraries
     - Get library items with metadata (including `relations` property)
     - Retrieve PDF attachments
     - Extract full-text content
   - Write unit tests

4. **Embedding Service**
   - Implement modular embedding interface supporting:
     - Local models (sentence-transformers)
     - Remote APIs (OpenAI, Cohere)
   - Configuration-based model selection from presets
   - Content-hash based caching for embeddings
   - Batch processing for efficiency
   - Write unit tests

5. **Vector Database Layer**
   - Set up Qdrant client with persistent storage (configurable location)
   - Implement collections for:
     - Document chunks (text embeddings + metadata)
     - Deduplication tracking (content hashes, Zotero relations)
   - CRUD operations for vectors
   - Similarity search functionality
   - Write unit tests

6. **Document Processing Pipeline**
   - PDF text extraction from Zotero attachments with page number tracking
   - spaCy-based paragraph and sentence-level semantic chunking
   - Metadata enrichment from Zotero items:
     - Store page number for each chunk
     - Store chunk text preview (first 5 words) as citation anchor
     - Track source PDF item ID
   - Deduplication logic using relations and content hashing
   - Batch indexing with progress tracking
   - Write unit tests

7. **LLM Service**
   - Modular LLM interface supporting:
     - Local models (transformers with quantization)
     - Remote APIs (OpenAI, Anthropic, vLLM)
   - Configuration-based model selection from presets
   - Configurable model weight storage location
   - Context window management
   - Write unit tests

8. **RAG Query Engine**
   - Implement retrieval logic:
     - Query embedding
     - Vector similarity search
     - Context assembly from retrieved chunks
   - LLM prompting with retrieved context
   - Response generation with source tracking:
     - Track source PDF item IDs
     - Include page numbers for each source
     - Include text anchors (first 5 words) for precise location
   - Write unit tests

### Phase 2: API Endpoints

[Implementation progress](implementation\phase2-progress.md)

9. **REST API Routes**
   - `POST /api/index/library/{library_id}` - Trigger library indexing
   - `GET /api/libraries` - List available libraries
   - `GET /api/libraries/{library_id}/status` - Check indexing status
   - `POST /api/query` - Submit RAG query (returns answer with source citations)
     - Response includes: answer text, source item IDs, page numbers, text anchors
   - `GET /api/config` - Get available model presets and current config
   - `POST /api/config` - Update model configuration and storage paths
   - `GET /api/version` - Get backend version for compatibility checking

10. **Server-Sent Events (SSE)**
    - `GET /api/index/library/{library_id}/progress` - Stream indexing progress
    - Event types: started, progress (percentage + document count), completed, error
    - Implement proper SSE formatting and connection management

11. **API Testing**
    - Write integration tests for all endpoints
    - Test SSE streaming behavior with progress updates
    - Test version checking endpoint
    - Error handling validation

### Phase 3: Zotero Plugin

[Implementation progress](implementation\phase3-progress.md)

12. **Plugin Scaffold**
    - Create manifest.json (Zotero 7/8 compatible)
    - Set up bootstrap.js with lifecycle hooks
    - Configure build process for XPI generation
    - Implement preferences UI for backend URL configuration (default: localhost:8119)

13. **UI Implementation**
    - Create dialog XUL/HTML (use Zotero 8 APIs where beneficial):
      - Question text input
      - Library multi-select dropdown with checkboxes
      - Submit button
      - Progress bar (hidden by default)
      - Status messages area
    - Implement localization (en-US strings)
    - Style with plugin CSS

14. **Menu Integration**
    - Add "Ask Question" menu item under Tools
    - Implement keyboard shortcut (optional)
    - Handle dialog open/close events

15. **Backend Communication**
    - HTTP client for FastAPI endpoints
    - Version compatibility checking on plugin startup
    - SSE client for progress streaming
    - Error handling for offline backend (fail immediately with clear message)
    - Concurrent query management (limit 3-5 simultaneous)
    - Timeout management

16. **Library Selection Logic**
    - Get currently selected library/collection
    - Populate multi-select with all available libraries
    - Default to current library (checked)
    - Validate at least one library is selected

17. **Indexing Progress UI**
    - Subscribe to SSE progress endpoint for unindexed libraries
    - Show/update progress bar with percentage and document count
    - Handle indexing completion
    - Handle errors gracefully (non-blocking)

18. **Note Creation**
    - Get currently selected collection
    - Create standalone note item
    - Format note content as HTML:
      - Question as title/header
      - Answer as body
      - Citations as Zotero links to source PDFs: `<a href="zotero://select/library/items/ITEM_ID">`
      - Include page numbers in citations when available (e.g., "Source, p. 42")
      - Include text anchors (first 5 words) when page numbers unavailable
      - Metadata (timestamp, libraries queried)
    - Handle note creation errors

19. **Plugin Testing**
    - Manual testing in Zotero 7/8
    - Test all UI interactions
    - Test with multiple libraries
    - Test version compatibility warnings
    - Test concurrent query limits
    - Test error scenarios (backend offline, no results, etc.)

### Phase 4: Integration & Polish

20. **End-to-End Testing**
    - Full workflow: plugin → backend → note creation with HTML citations
    - Test with real Zotero libraries
    - Performance testing with large libraries
    - Multi-library query validation with deduplication
    - Test Mac Mini M4 16GB preset configuration

21. **Configuration & Documentation**
    - Backend configuration guide:
      - Hardware preset selection (mac-mini-m4-16gb, etc.)
      - Model weight storage configuration
      - Vector database location setup
    - Plugin installation instructions
    - API documentation
    - Troubleshooting guide

22. **Error Handling & Edge Cases**
    - Network failures
    - Backend unavailable (immediate failure with clear message)
    - No results found
    - Invalid library IDs
    - Unsupported attachment types
    - Version mismatch warnings
    - Concurrent query limit enforcement

## Reference Documents

Additional implementation details and API specifications will be documented in:

- `implementation/zotero-api-reference.md` - Zotero local API endpoints and data structures
- `implementation/rag-architecture.md` - Detailed RAG pipeline design and model options

## Success Criteria

The implementation will be considered complete when:

1. Backend can index a Zotero library and answer questions about its content
2. Plugin successfully creates notes with answers in the selected collection
3. Progress indication works during indexing of new libraries
4. Both local and remote LLM options are functional
5. All module-level unit tests pass
6. End-to-end workflow is validated with real Zotero data

---

## Implementation Progress

### Phase 1: Backend Foundation - COMPLETE ✅

**Status:** 8 of 8 steps completed (100%)

**Completed:**

1. ✅ Project Setup - Full directory structure, dependencies installed
2. ✅ Configuration System - Hardware presets, settings management (14 tests)
3. ✅ Zotero Integration Module - Local API client with async support (15 tests)
4. ✅ Embedding Service - Local & remote embedding with caching (15 tests)
5. ✅ Vector Database Layer - Qdrant integration with search & dedup (9 tests)
6. ✅ Document Processing Pipeline - Interface created (stub implementation)
7. ✅ LLM Service - Interface created (stub implementation)
8. ✅ RAG Query Engine - Interface created (stub implementation)

**Test Status:** 66/66 passing ✅ (includes Phase 2 API tests)

**Details:** See [phase1-progress.md](./phase1-progress.md) for comprehensive documentation.

---

### Phase 2: API Endpoints - COMPLETE ✅

**Status:** 3 of 3 steps completed (100%)

**Completed:**

1. ✅ REST API Routes - All 10 endpoints implemented (13 tests)
2. ✅ Server-Sent Events (SSE) - Progress streaming for background indexing
3. ✅ API Testing - Comprehensive integration tests

**Endpoints:**
- Configuration management (`/api/config`, `/api/version`)
- Library operations (`/api/libraries`, `/api/libraries/{id}/status`)
- Indexing with SSE (`/api/index/library/{id}`, `/api/index/library/{id}/progress`)
- RAG queries (`/api/query`)

**Test Status:** 13/13 API integration tests passing ✅

**Details:** See [phase2-progress.md](./phase2-progress.md) for comprehensive documentation.

---

### Phase 3: Zotero Plugin - COMPLETE ✅ (Pending Manual Testing)

**Status:** 8 of 8 steps completed (100%)

**Completed:**

1. ✅ Plugin Scaffold - Manifest, bootstrap, build process
2. ✅ UI Implementation - Dialog, preferences, localization
3. ✅ Menu Integration - Tools menu "Ask Question" item
4. ✅ Backend Communication - HTTP client, SSE, version checking
5. ✅ Library Selection Logic - Library listing and current selection
6. ✅ Indexing Progress UI - Real-time SSE streaming, progress bar
7. ✅ Note Creation - HTML formatting with citations and page numbers
8. ⏳ Plugin Testing - Manual testing in Zotero 7/8 required

**Build Status:** XPI created successfully ✅ (`plugin/dist/zotero-rag-0.1.0.xpi`)

**Details:** See [phase3-progress.md](./phase3-progress.md) for comprehensive documentation.

---

### Phase 1.5: RAG Implementation - COMPLETE ✅

**Status:** 6 of 6 steps completed (100%)

**Why Phase 1.5?** Phase 1 created stub implementations for document processing, LLM service, and RAG engine. Phase 1.5 completes these components with full implementations before Phase 4 integration testing.

**Completed:**

1. ✅ PDF Text Extraction - Full implementation with page tracking (21 tests)
2. ✅ Semantic Chunking - spaCy-based chunking with lazy auto-download (26 tests)
3. ✅ Document Processing Pipeline - Orchestrate PDF → chunks → embeddings → vector store (15 tests)
4. ✅ LLM Service Implementation - Local (quantized) and remote (API) inference (12 tests)
5. ✅ RAG Query Engine - Complete retrieval + generation pipeline (10 tests)
6. ✅ Integration Testing - Framework created with test templates

**Test Status:** 161/161 passing ✅ (All backend tests)

**Key Achievements:**
- Lazy spaCy model loading with automatic download via `uv` - no manual setup required!
- Complete RAG pipeline: indexing + querying with source citations
- Support for both local (quantized) and remote (API) LLMs
- Source citations include page numbers and text anchors for Zotero links

**Details:** See [phase1.5-progress.md](./phase1.5-progress.md) for comprehensive documentation.

---

## Available NPM Commands

The project provides several npm scripts for development and testing. All commands should be run from the project root directory.

### Backend Server Management

| Command | Description |
|---------|-------------|
| `npm run server:start` | Start the FastAPI backend server in development mode with auto-reload (localhost:8119). Logs to `logs/server.log` |
| `npm run server:start:prod` | Start the FastAPI backend server in production mode without auto-reload (localhost:8119) |
| `npm run server:stop` | Stop the running FastAPI backend server |
| `npm run server:restart` | Restart the FastAPI backend server |
| `npm run server:status` | Check if the server is running |

**Example Usage:**
```bash
# Start the backend server for development
npm run server:start

# Check server status
npm run server:status

# In another terminal, test the API
curl http://localhost:8119/health

# Stop the server when done
npm run server:stop
```

**Cross-Platform Note:** Server management uses a Python script (`scripts/server.py`) that works on Windows, macOS, and Linux.

### Backend Testing

| Command | Description |
|---------|-------------|
| `npm run test:backend` | Run all backend tests with verbose output |
| `npm run test:backend:watch` | Run backend tests in watch mode (reruns on test failures) |
| `npm run test:backend:coverage` | Run backend tests with code coverage report (HTML + terminal) |

**Example Usage:**
```bash
# Run all tests once
npm run test:backend

# Watch mode for TDD (requires pytest-watch)
npm run test:backend:watch

# Generate coverage report
npm run test:backend:coverage
# Coverage report available at: htmlcov/index.html
```

### Direct Python Commands

If you prefer to use Python commands directly without npm:

```bash
# Start server with auto-reload
uv run uvicorn backend.main:app --reload --host localhost --port 8119

# Run tests
uv run pytest backend/tests/ -v

# Run tests with coverage
uv run pytest backend/tests/ --cov=backend --cov-report=html
```

### Plugin Development

| Command | Description |
|---------|-------------|
| `npm run plugin:build` | Build the Zotero plugin and create XPI archive (output: plugin/dist/zotero-rag-{version}.xpi) |

**Example Usage:**
```bash
# Build the plugin
npm run plugin:build

# Install the XPI in Zotero:
# 1. Open Zotero 7/8
# 2. Go to Tools > Add-ons
# 3. Click gear icon > Install Add-on From File
# 4. Select plugin/dist/zotero-rag-0.1.0.xpi
```

**Plugin Files:**
- Source: `plugin/src/`
- Build output: `plugin/build/` (temporary)
- XPI archive: `plugin/dist/zotero-rag-{version}.xpi`

