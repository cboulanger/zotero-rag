# Zotero RAG Application - Master Implementation Plan

## Project Overview

A Zotero-integrated RAG (Retrieval-Augmented Generation) system consisting of:
- **FastAPI Backend**: Handles embeddings, vector database, LLM inference, and exposes REST/SSE APIs
- **Zotero Plugin**: Provides UI for querying libraries and creates note items with answers

## Architecture

```
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
- **Language**: JavaScript (Firefox extension environment)
- **Key APIs**:
  - Zotero plugin API (UI, notes, collections)
  - Zotero local data API (localhost:23119)
- **Reference**: [zotero-sample-plugin/src-2.0](../zotero-sample-plugin/src-2.0)
- **Documentation**:
  - https://www.zotero.org/support/dev/client_coding
  - https://www.zotero.org/support/dev/zotero_8_for_developers

### Backend RAG Stack
- **Framework**: FastAPI (Python 3.13)
- **Embedding Models**:
  - Local: sentence-transformers, nomic-embed-text-v1.5
  - Remote option: OpenAI/Cohere embeddings API
- **Vector Database**: Qdrant (supports both in-memory and persistent modes)
- **LLM Options**:
  - Local: Transformers + quantized models (Qwen2.5, Llama 3, Mistral)
  - Remote: OpenAI API, Anthropic API, vLLM endpoints
- **PDF Processing**: PyPDF2, pdfplumber, or pypdf
- **Zotero Access**: pyzotero library
- **Package Management**: uv (Python 3.13)

### Reusable Implementation from zoterorag
- PyZotero library integration for accessing libraries
- Qdrant vector database usage patterns
- Multimodal document processing (text + images from PDFs)
- Batch indexing pipeline

## Implementation Steps

### Phase 1: Backend Foundation
1. **Project Setup**
   - Initialize FastAPI project with uv
   - Set up project structure with modules:
     - `api/` - FastAPI routes
     - `services/` - Core business logic (embeddings, LLM, indexing)
     - `models/` - Data models and schemas
     - `db/` - Vector database interface
     - `zotero/` - Zotero API client wrapper
   - Configure environment variables (.env)

2. **Zotero Integration Module**
   - Implement Zotero local API client (localhost:23119)
   - Create methods to:
     - List libraries
     - Get library items with metadata
     - Retrieve PDF attachments
     - Extract full-text content
   - Write unit tests

3. **Embedding Service**
   - Implement modular embedding interface supporting:
     - Local models (sentence-transformers)
     - Remote APIs (OpenAI, Cohere)
   - Configuration-based model selection
   - Batch processing for efficiency
   - Write unit tests

4. **Vector Database Layer**
   - Set up Qdrant client with configurable storage (in-memory/persistent)
   - Implement collections for:
     - Document chunks (text embeddings)
     - Metadata (Zotero item info)
   - CRUD operations for vectors
   - Similarity search functionality
   - Write unit tests

5. **Document Processing Pipeline**
   - PDF text extraction from Zotero attachments
   - Text chunking strategies (semantic, fixed-size)
   - Metadata enrichment from Zotero items
   - Batch indexing with progress tracking
   - Write unit tests

6. **LLM Service**
   - Modular LLM interface supporting:
     - Local models (transformers with quantization)
     - Remote APIs (OpenAI, Anthropic, vLLM)
   - Configuration-based model selection
   - Context window management
   - Write unit tests

7. **RAG Query Engine**
   - Implement retrieval logic:
     - Query embedding
     - Vector similarity search
     - Context assembly from retrieved chunks
   - LLM prompting with retrieved context
   - Response generation
   - Write unit tests

### Phase 2: API Endpoints
8. **REST API Routes**
   - `POST /api/index/library/{library_id}` - Trigger library indexing
   - `GET /api/libraries` - List available libraries
   - `GET /api/libraries/{library_id}/status` - Check indexing status
   - `POST /api/query` - Submit RAG query
   - `GET /api/config` - Get available models/config
   - `POST /api/config` - Update model configuration

9. **Server-Sent Events (SSE)**
   - `GET /api/index/library/{library_id}/progress` - Stream indexing progress
   - Event types: started, progress (percentage), completed, error
   - Implement proper SSE formatting and connection management

10. **API Testing**
    - Write integration tests for all endpoints
    - Test SSE streaming behavior
    - Error handling validation

### Phase 3: Zotero Plugin
11. **Plugin Scaffold**
    - Create manifest.json (Zotero 7/8 compatible)
    - Set up bootstrap.js with lifecycle hooks
    - Configure build process for XPI generation

12. **UI Implementation**
    - Create dialog XUL/HTML:
      - Question text input
      - Library multi-select dropdown with checkboxes
      - Submit button
      - Progress bar (hidden by default)
      - Status messages area
    - Implement localization (en-US strings)
    - Style with plugin CSS

13. **Menu Integration**
    - Add "Ask Question" menu item under Tools
    - Implement keyboard shortcut (optional)
    - Handle dialog open/close events

14. **Backend Communication**
    - HTTP client for FastAPI endpoints
    - SSE client for progress streaming
    - Error handling and retry logic
    - Timeout management

15. **Library Selection Logic**
    - Get currently selected library/collection
    - Populate multi-select with all available libraries
    - Default to current library (checked)
    - Validate at least one library is selected

16. **Indexing Progress UI**
    - Subscribe to SSE progress endpoint
    - Show/update progress bar
    - Handle indexing completion
    - Handle errors gracefully (non-blocking)

17. **Note Creation**
    - Get currently selected collection
    - Create standalone note item
    - Format note content:
      - Question as title/header
      - Answer as body
      - Metadata (timestamp, libraries queried)
    - Handle note creation errors

18. **Plugin Testing**
    - Manual testing in Zotero 7/8
    - Test all UI interactions
    - Test with multiple libraries
    - Test error scenarios

### Phase 4: Integration & Polish
19. **End-to-End Testing**
    - Full workflow: plugin → backend → note creation
    - Test with real Zotero libraries
    - Performance testing with large libraries
    - Multi-library query validation

20. **Configuration & Documentation**
    - Backend configuration guide (model selection, hardware requirements)
    - Plugin installation instructions
    - API documentation
    - Troubleshooting guide

21. **Error Handling & Edge Cases**
    - Network failures
    - Backend unavailable
    - No results found
    - Invalid library IDs
    - Unsupported attachment types

## Open Questions & Decisions

### Backend
- [ ] **Model Selection Strategy**: Default local model for CPU vs GPU systems? Recommend specific models?
  - CPU: Smaller models (Qwen2.5-3B, TinyLlama)
  - GPU: Medium models (Qwen2.5-7B, Mistral-7B)
  - Need hardware detection or user configuration?

- [ ] **Vector Database Storage**: Default to in-memory or persistent? Where to store persistent data?
  - Suggest: Persistent by default in user data directory
  - Configuration option for location

- [ ] **Chunking Strategy**: What chunk size and overlap for academic papers?
  - Academic papers have distinct structures (abstract, sections, references)
  - Consider semantic chunking vs fixed-size

- [ ] **Authentication**: Does the backend need authentication/API keys, or assume local-only trusted access?
  - Suggest: Start with no auth (localhost-only), add optional auth later

- [ ] **Multi-library Merging**: How to handle duplicate documents across libraries? Merge or keep separate?
  - Need to decide on deduplication strategy

- [ ] **Caching**: Should we cache embeddings to avoid re-computing on re-indexing?
  - Suggest: Yes, cache based on document hash

### Plugin
- [ ] **Backend Discovery**: How does plugin find backend? Hardcoded localhost:8000 or configurable?
  - Suggest: Configurable in plugin preferences, default localhost:8000

- [ ] **Concurrent Queries**: Allow multiple simultaneous queries or queue them?
  - Suggest: Allow concurrent, limit to reasonable number (3-5)

- [ ] **Note Format**: Plain text, Markdown, or HTML notes? Include citations?
  - Suggest: Markdown with citations to source items

- [ ] **Progress Granularity**: What level of progress detail? Per-document, per-batch, percentage only?
  - Suggest: Percentage with current document count

- [ ] **Offline Behavior**: What happens if backend is unreachable? Queue queries or fail immediately?
  - Suggest: Fail with clear error message, don't queue

- [ ] **Zotero 8 Compatibility**: Are there breaking changes from Zotero 7 to 8 we need to handle?
  - Need to verify plugin API compatibility

### Cross-Cutting
- [ ] **Data Privacy**: Should we include privacy/data retention policies? Is data stored beyond vector DB?
  - Suggest: Document that all data stays local

- [ ] **Logging**: What level of logging? User-accessible logs?
  - Suggest: Configurable log level, logs to standard locations

- [ ] **Updates**: How to handle backend/plugin version mismatches?
  - Suggest: Version check API endpoint, warn user

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
