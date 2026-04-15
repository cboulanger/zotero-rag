# Document Ingestion Framework Assessment & Refactoring Plan

## Context

The current zotero-rag pipeline implements document extraction and chunking from scratch using `pypdf` and `spaCy`. Two mature frameworks — **Kreuzberg** and **Docling** — could replace these components. This document assesses both, proposes an adapter architecture that makes the backend framework-agnostic, and recommends which to implement first.

---

## Current Pipeline (built from scratch)

| Stage | File | Library | Limitation |
|---|---|---|---|
| PDF text extraction | `backend/services/pdf_extractor.py` | `pypdf` | Text-layer PDFs only; no OCR; no other formats |
| Semantic chunking | `backend/services/chunking.py` | `spaCy` | Custom size/overlap loop; 50 MB model download |
| Orchestration | `backend/services/document_processor.py` | custom | Glues extraction + chunking + Qdrant writes |

**Not in scope (unchanged):** Qdrant vector store, embeddings service, LLM service, Zotero API wrapper, FastAPI layer.

---

## Framework Comparison

| Aspect | **Kreuzberg** | **Docling** |
|---|---|---|
| Core language | Rust (PyO3 bindings) | Python (PyTorch ML models) |
| Formats supported | 91+ | ~38 |
| License | **ELv2** (personal use OK; no competing SaaS) | **MIT** (fully open) |
| PDF speed | 10–100 MB/s | ~1–2 pages/sec (model inference per page) |
| Install size | ~71 MB | 1 GB+ (PyTorch + ML models) |
| Native async | **Yes** (matches FastAPI architecture) | No (sync only; thread pool workaround needed) |
| OCR | Tesseract / EasyOCR / PaddleOCR | EasyOCR |
| Chunking | Built-in (`ChunkingConfig`) | Built-in (`HybridChunker`, structure-aware) |
| Embeddings | Built-in (FastEmbed/ONNX) | Not built-in |
| Layout analysis | Basic (PDFium) | **Excellent** (ML-based: reading order, tables, columns) |
| Cold start | Minimal | High (model loading) |

### When Docling wins
- Complex multi-column academic papers where reading order and table structure matter
- Scientific/patent documents where structural metadata (sections, figures) is critical for the downstream task

### When Kreuzberg wins
- **Async FastAPI architecture** — native async avoids blocking the event loop
- High-volume batch indexing (9–50x faster than Docling)
- Broad Zotero attachment types (HTML snapshots, DOCX, EPUB)
- Lighter deployment footprint (PyTorch already present for local LLMs but not needed for extraction)

### Recommendation: **Kreuzberg**

For the zotero-rag use case (indexing a personal research library of text-layer PDFs + occasional HTML/DOCX), Kreuzberg is the better fit:
- Native async avoids blocking the FastAPI event loop (Docling requires thread pool wrapping)
- Speed matters for full re-indexes of large libraries
- OCR fills the scanned-PDF gap
- PDFium (Kreuzberg) handles reading order well for typical academic papers
- ELv2 is acceptable for personal/internal use

Docling would be the better choice only if structured table/figure extraction from scientific papers became a primary goal beyond RAG.

---

## Proposed Architecture: `DocumentExtractor` Adapter

Introduce a thin `DocumentExtractor` abstraction — the same pattern already used for `EmbeddingService` and `LLMService` — so the extraction backend is swappable via config.

### New module structure

```
backend/services/extraction/
├── __init__.py
├── base.py            # DocumentExtractor ABC + ExtractionChunk dataclass
├── legacy.py          # LegacyExtractor  (wraps existing pypdf + spaCy; zero behavior change)
├── kreuzberg.py       # KreuzbergExtractor  (recommended)
└── docling.py         # DoclingExtractor    (future / optional)
```

### `base.py` — shared interface

```python
@dataclass
class ExtractionChunk:
    text: str
    page_number: int | None
    chunk_index: int
    content_hash: str          # SHA256 of text

class DocumentExtractor(ABC):
    @abstractmethod
    async def extract_and_chunk(
        self,
        content: bytes,
        mime_type: str,
    ) -> list[ExtractionChunk]: ...
```

### `kreuzberg.py` — recommended implementation

```python
class KreuzbergExtractor(DocumentExtractor):
    async def extract_and_chunk(self, content, mime_type):
        result = await kreuzberg.extract_bytes(
            content,
            config=ExtractionConfig(
                chunking=ChunkingConfig(
                    chunk_size=self.chunk_size,
                    chunk_overlap=self.chunk_overlap,
                ),
                ocr=OcrConfig(enabled=True, backend="tesseract"),
            ),
        )
        return [ExtractionChunk(...) for chunk in result.chunks]
```

### `legacy.py` — zero-change fallback

```python
class LegacyExtractor(DocumentExtractor):
    async def extract_and_chunk(self, content, mime_type):
        # delegates to existing PDFExtractor + TextChunker unchanged
        ...
```

### Factory (mirrors `create_embedding_service`)

```python
def create_document_extractor(config) -> DocumentExtractor:
    match config.extractor_backend:
        case "kreuzberg": return KreuzbergExtractor(config)
        case "docling":   return DoclingExtractor(config)
        case _:           return LegacyExtractor(config)
```

### `DocumentProcessor` change

Replace the current two-step extraction in `_index_item()`:

```python
# Before:
pages = self.pdf_extractor.extract_from_bytes(pdf_bytes)
chunks = self.chunker.chunk_pages(pages)

# After:
chunks = await self.extractor.extract_and_chunk(pdf_bytes, mime_type)
```

Broaden the MIME type filter in `_filter_items_with_pdfs()` to include
`text/html`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `application/epub+zip`.
Kreuzberg handles format detection from the bytes when a MIME type hint is passed.

---

## Files to Create / Modify

| Action | File | Change |
|---|---|---|
| Create | `backend/services/extraction/__init__.py` | Package init + factory export |
| Create | `backend/services/extraction/base.py` | `DocumentExtractor` ABC + `ExtractionChunk` |
| Create | `backend/services/extraction/legacy.py` | Thin wrapper around existing `PDFExtractor` + `TextChunker` |
| Create | `backend/services/extraction/kreuzberg.py` | Kreuzberg adapter |
| Modify | `backend/services/document_processor.py` | Swap to `DocumentExtractor`; broaden MIME filter |
| Modify | `backend/config/presets.py` | Add `extractor_backend` field to each preset |
| Modify | `backend/config/settings.py` | Add `extractor_backend: str = "kreuzberg"` |
| Modify | `backend/pyproject.toml` | Add `kreuzberg[tesseract]`; keep `pypdf` + `spacy` for legacy fallback |
| Keep   | `backend/services/pdf_extractor.py` | Unchanged (used by `LegacyExtractor`) |
| Keep   | `backend/services/chunking.py` | Unchanged (used by `LegacyExtractor`) |

---

## Implementation Phases

### Phase 1 — Adapter skeleton (no behavior change)
1. Create `extraction/` package with `base.py` and `legacy.py`
2. `LegacyExtractor.extract_and_chunk()` delegates to existing `PDFExtractor` + `TextChunker`
3. `DocumentProcessor` receives a `DocumentExtractor` (default: `LegacyExtractor`)
4. Run `uv run pytest` — all tests must pass with zero behavior change

### Phase 2 — Kreuzberg adapter
1. `uv pip install "kreuzberg[tesseract]"` and add to `pyproject.toml`
2. Implement `KreuzbergExtractor`; map `ExtractionResult.chunks` → `ExtractionChunk` list with page numbers
3. Set `extractor_backend = "kreuzberg"` as default in settings
4. Validate chunk quality on 3–5 sample PDFs vs legacy output
5. Test with a library containing scanned PDFs → verify OCR chunks appear in Qdrant

### Phase 3 — Expand attachment types
1. Rename `_filter_items_with_pdfs()` → `_filter_indexed_attachments()` in `document_processor.py`
2. Broaden MIME type list; pass `mime_type` to `extract_and_chunk()`
3. Test with Zotero items that have HTML snapshot or DOCX attachments

### Phase 4 — Docling adapter (optional, future)
- Implement `DoclingExtractor` wrapping `DocumentConverter` in `asyncio.get_event_loop().run_in_executor()`
- Only add if layout quality of academic PDFs is insufficient with Kreuzberg

---

## Verification

1. `uv run pytest` after each phase — existing tests must pass throughout
2. Index a library with scanned PDFs (Phase 2) → chunks non-empty; run a RAG query against them
3. Compare page number metadata in Qdrant: Kreuzberg output must match legacy output for same PDF
4. Index a library with HTML snapshot attachments (Phase 3) → items appear in query results
