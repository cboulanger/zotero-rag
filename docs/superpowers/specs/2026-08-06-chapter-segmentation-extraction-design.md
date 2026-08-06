# Extracting Chapter Segmentation into a Standalone Repository — Design Spec

## 1. Goal

The chapter-segmentation feature (branch `feature/chapter-segmentation-linking`)
has grown far past the size and scope of a single feature of zotero-rag: it
comprises a multi-strategy PDF chapter-detection engine, an LLM-fallback
path, an OCR pipeline, and a large evaluation harness with its own
ground-truth corpus, redacted public evaluation cache, and accuracy
reporting — none of which is inherently about RAG or about Zotero. This
spec defines how to split that engine + evaluation harness out into its own
git repository, consumed by zotero-rag as a versioned dependency, while
keeping all Zotero-specific persistence, review-queue, and API/plugin code
in zotero-rag itself.

A second, explicit goal is decoupling from Kreuzberg: today OCR support is
reached only through zotero-rag's own `KreuzbergExtractor`. The new
repository must define its own minimal OCR interface so it can run fully
standalone (its own CLI, against a local PDF, no Zotero/RAG app involved)
with Kreuzberg as one *optional* pluggable backend, not a hard dependency.

## 2. Non-goals

- No behavior change to the chapter-segmentation algorithm itself — this is
  a structural extraction, not a redesign of the detection/fusion logic.
- No change to the Zotero-specific persistence format (`extra`-field chapter
  links, review queue JSON schema) — `chapter_link_store.py`,
  `chapter_retrofit.py`, `chapter_upload.py`, `review_queue_store.py`, and
  `backend/api/chapter_linking.py` are unaffected in behavior; they move to
  importing the new package instead of local sibling modules.
- No change to zotero-rag's main RAG-indexing extraction path
  (`backend/services/extraction/kreuzberg.py`, `document_processor.py`) —
  that is a separate `DocumentExtractor` abstraction serving a different
  purpose (chunking arbitrary documents for embedding) and is out of scope.
- No PyPI publication in this pass — the new repo is consumed via a `uv`
  git dependency, not a published package.

## 3. Scope boundary: `chapter_segmentation.py` splits vertically, not as a whole file

Tracing what the existing evaluation scripts already import from
`chapter_segmentation.py` (`analyze_attachment`,
`analyze_attachment_with_strategies`, `analyze_attachment_with_llm_fallback`,
`extract_page_texts_for_analysis`, `pages_need_ocr`, …) shows the real
boundary already exists implicitly: those are pure functions — page texts
and book metadata in, chapter candidates out. Only the top-level `run()`
(used by the production API path, `backend/api/chapter_linking.py`) touches
`get_settings()`, `review_queue_store`, and `ZoteroLibraryCache` directly.

`chapter_evidence/zotero_catalog_strategy.py`, despite its name, has no
import of any Zotero SDK — it takes a plain `dict[str, list[dict]]` shaped
like Zotero item JSON. It is portable to the new repo as-is.

### Moves to the new repo

- `backend/services/chapter_common.py`
- `backend/services/chapter_evidence/` (`types.py`, `fusion.py`,
  `outline_strategy.py`, `crossref_strategy.py`, `zotero_catalog_strategy.py`)
- The pure analysis functions currently in `chapter_segmentation.py`
  (everything except `run()`'s Zotero-client/settings/review-queue glue)
- The pure OCR/caching helpers in `chapter_ocr.py` (`detect_language`,
  `ocr_pdf_pages`, `load_cached_ocr`, `save_ocr_cache`,
  `slice_single_page_pdf`), refactored around the new `OcrBackend` protocol
  (§4)
- `backend/evaluation/book-segmentation/` in its entirety: `manifest.json`,
  all `*.expected.json` ground truth, `public-cache/`, `README.md`,
  `RESULTS.md`, `CLAUDE.md`
- `backend/evaluation/harness.py`
- `scripts/evaluation_redaction/` in its entirety
- `scripts/ocr_evaluation_pdfs.py`, `scripts/generate_public_evaluation_cache.py`,
  `scripts/fetch_evaluation_pdfs.py`, `scripts/ground_truth_helper.py`,
  `scripts/evaluate_chapter_segmentation_strategies.py`,
  `scripts/evaluate_chapter_segmentation_llm_fallback.py`
- The corresponding test files: `test_chapter_common.py`,
  `test_chapter_evidence_*.py`, `test_chapter_segmentation.py`,
  `test_chapter_segmentation_accuracy.py`,
  `test_chapter_segmentation_strategies.py`, `test_chapter_ocr.py`,
  `test_evaluation_harness.py`, `test_evaluation_redaction.py`,
  `test_public_evaluation_cache_parity.py`

### Stays in zotero-rag

- `backend/services/chapter_link_store.py`, `chapter_retrofit.py`,
  `chapter_upload.py`, `review_queue_store.py`
- `backend/api/chapter_linking.py`, `backend/api/admin_pages.py`,
  `backend/templates/admin_review.html`, `admin_run.html`
- `backend/zotero/library_cache.py`
- The plugin UI (`chapterActions.js`, `chapterLinks.js`, `fuzzyMatch.js`,
  `match-chapter-dialog.*`)
- A new thin orchestrator module (replacing what remains of
  `chapter_segmentation.run()`): fetches the attachment via `ZoteroWebAPI`,
  builds `BookContext`, constructs strategy instances (including its own
  `ZoteroCatalogMetadataStrategy`, fed from `ZoteroLibraryCache`), wraps its
  `LLMService` and Kreuzberg OCR backend to satisfy the new package's
  protocols, calls into the package, and persists results via
  `review_queue_store` / settings-configured cache paths.

## 4. Pluggable OCR backends

The new package defines a minimal protocol:

```python
class OcrBackend(Protocol):
    async def ocr_pdf_pages(
        self, content: bytes, *, language: str | None = None
    ) -> list[str]:
        """Return one text string per physical page, 0-indexed."""
```

This shifts page-slicing (currently pypdf-based single-page slicing in
`chapter_ocr.py`) *into* the backend implementation — a backend receives
whole-document bytes and decides for itself how to talk to its OCR engine
(per-page requests, whole-document request split by page, etc.), rather
than the engine core assuming a per-page chunking scheme tied to one
provider's API shape.

The package ships **two** reference implementations, so it has a real
zero-infrastructure story rather than trading one hard Kreuzberg dependency
for another:

### `KreuzbergOcrBackend` (optional extra: `[kreuzberg]`)

A self-contained adapter calling a Kreuzberg sidecar's HTTP API, independent
of zotero-rag's own `backend/services/extraction/kreuzberg.py` (which serves
the unrelated RAG-indexing/chunking pipeline and must not change). Gated
behind `pip install chapter-segmentation[kreuzberg]` (`httpx` as the extra
dependency). This is what zotero-rag's orchestrator uses in production —
the sidecar is already part of its deployment, so there is no reason to run
OCR any other way there.

### `TesseractOcrBackend` (optional extra: `[tesseract]`) — the standalone default

A local-binary adapter with no daemon, no network call, and no container:
renders each PDF page to a raster image with `pymupdf` (pure Python, no
external binary) and shells out to the `tesseract` CLI via `pytesseract`
for text recognition. The only non-Python prerequisite is the `tesseract`
binary and its language data on `PATH` — a single-line install
(`apt-get install tesseract-ocr tesseract-ocr-deu tesseract-ocr-fra
tesseract-ocr-spa` on Debian/Ubuntu CI images, `brew install tesseract
tesseract-lang` on macOS, or the Windows installer) rather than a
container image to pull or build. The backend checks `shutil.which("tesseract")`
at construction time and raises a `RuntimeError` naming the exact install
command for the current platform, rather than failing deep inside a
subprocess call with an opaque error.

Language handling needs no new mapping: `detect_language()`'s existing
output (`"deu"`, `"fra"`, `"spa"`, `"eng"`, or the combined
`"eng+deu+fra+spa"`) is already Tesseract's own `-l` flag syntax, because
Kreuzberg's OCR is itself Tesseract under the hood (per the deployment
notes in zotero-rag's `CLAUDE.md`) — both backends consume the same
language string unchanged.

A podman-container backend (running a minimal OCR image directly, without
Kreuzberg's full sidecar) was considered and rejected: it would still need
image build/pull, port allocation, and health-check/wait-for-ready polling —
the same category of plumbing zotero-rag's own `bin/container.mjs` already
carries for the *real* Kreuzberg sidecar — just to reach the same
underlying engine (Tesseract) one layer removed. The binary backend reaches
it directly, with substantially less to install, start, or fail in CI.

The standalone CLI (§7) defaults to `TesseractOcrBackend` when no
`--ocr-backend` flag is given, and to `KreuzbergOcrBackend` when one is
requested and configured — so `pip install chapter-segmentation[tesseract]`
plus one OS-level install command is enough to OCR a scanned book with zero
containers involved. zotero-rag's orchestrator continues to request the
`[kreuzberg]` extra explicitly, since it already runs that sidecar for other
purposes.

Any result caching keyed by content hash (as `chapter_ocr.py` does today)
is retained inside the package, backend-agnostic — a cache entry doesn't
record which backend produced it, since both are expected to converge on
the same OCR engine's output.

## 5. Pluggable LLM client

A second minimal protocol:

```python
class LLMClient(Protocol):
    async def generate(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
        is_valid: Callable[[str], bool] | None = None,
    ) -> str: ...
```

zotero-rag's existing `LLMService` ABC already exposes exactly this
`generate()` signature, so the orchestrator passes its `LLMService` instance
into the package directly — no adapter code needed (structural typing).

The LLM-fallback strategy remains optional throughout the package's API
(`llm_client: LLMClient | None = None`): the outline/Crossref/catalog
strategies alone are enough for the package's standalone CLI to be useful
with zero LLM configured.

## 6. Metadata/evidence strategies

`MetadataStrategy` / `StructureStrategy` (already `Protocol`s in
`chapter_evidence/types.py`) move unchanged — they are already structurally
decoupled from any concrete implementation. `zotero_catalog_strategy.py`
moves too, per §3's finding that it only depends on a plain dict shape, not
a Zotero SDK import. Its naming may be revisited during implementation
(e.g. `catalog_strategy.py` / `CatalogMetadataStrategy`) to reflect that any
caller supplying Zotero-item-shaped dicts can use it — cosmetic, not
required for the split to work.

zotero-rag's orchestrator continues to build this strategy from
`ZoteroLibraryCache` output, exactly as `chapter_segmentation.run()` does
today — only the strategy class's import path changes.

## 7. New repository layout

Proposed package name: **`chapter-segmentation`** (import name
`chapter_segmentation`) — open to renaming before the repo is created.

```text
chapter-segmentation/
├── src/chapter_segmentation/
│   ├── common.py
│   ├── ocr.py                       # OcrBackend Protocol + caching helpers
│   ├── ocr_backends/
│   │   ├── kreuzberg.py             # optional extra: [kreuzberg]
│   │   └── tesseract.py             # optional extra: [tesseract], standalone default
│   ├── llm.py                       # LLMClient Protocol
│   ├── evidence/
│   │   ├── types.py
│   │   ├── fusion.py
│   │   ├── outline_strategy.py
│   │   ├── crossref_strategy.py
│   │   └── zotero_catalog_strategy.py
│   ├── segmentation.py              # analyze_attachment / with_strategies / with_llm_fallback
│   └── cli.py                       # standalone CLI: segment a local PDF, no Zotero needed
├── evaluation/
│   ├── manifest.json, *.expected.json, public-cache/,
│   │   README.md, RESULTS.md, CLAUDE.md
│   ├── harness.py
│   └── redaction/                   # scripts/evaluation_redaction/* content
├── tests/
├── pyproject.toml
└── README.md
```

`cli.py` is new: a cleaned-up version of the existing evaluation scripts
(which already operate on a local PDF + manifest entry, with no Zotero
dependency) becomes the package's first-class standalone entry point — point
it at a PDF, get chapter candidates back, independent of zotero-rag. Per §4,
it defaults to `TesseractOcrBackend`, so a fresh `pip install
chapter-segmentation[tesseract]` (plus the OS-level `tesseract` binary) is
enough to OCR a scanned book with no container involved.

## 8. Dependency and versioning

zotero-rag's `pyproject.toml` depends on the new repo via `uv`'s native git
dependency support:

```toml
"chapter-segmentation @ git+https://github.com/cboulanger/chapter-segmentation.git@v0.1.0"
```

The new repo tags releases (`v0.1.0`, `v0.2.0`, …); bumping the pinned tag
in zotero-rag is the upgrade mechanism — no package index required.

For active side-by-side development, zotero-rag adds a `[tool.uv.sources]`
local-path override (editable) pointing at a sibling checkout of the new
repo. This only affects local dev environments; the git+tag dependency
remains authoritative for CI and deployed builds.

`rapidfuzz` and `langdetect` are dropped from zotero-rag's direct
dependencies once the split lands (pulled in transitively through the new
package). `spacy` and `pypdf` remain direct zotero-rag dependencies, since
`backend/services/chunking.py` and the RAG-indexing PDF-splitting path use
them independently of chapter segmentation.

The new package's own `pyproject.toml` declares the two OCR backends as
optional extras, so a plain `pip install chapter-segmentation` pulls in
neither `httpx` nor `pymupdf`/`pytesseract`:

```toml
[project.optional-dependencies]
kreuzberg = ["httpx>=0.27"]
tesseract = ["pytesseract>=0.3", "pymupdf>=1.24", "pillow>=10"]
```

zotero-rag depends on `chapter-segmentation[kreuzberg]`; the standalone CLI
install path is `chapter-segmentation[tesseract]`.

## 9. History migration

The moved files' git history is preserved via `git filter-repo` run on a
disposable clone of zotero-rag: filtered to exactly the paths listed in
§3's "moves" list, with those paths remapped to the new repo's
`src/chapter_segmentation/...` / `evaluation/...` layout. The result becomes
the new repository's initial commit history, pushed to a new GitHub repo.

Back in zotero-rag, the current feature branch then deletes the moved
paths, adds the git dependency (§8), and the thin orchestrator (§3) plus
protocol adapters (§4, §5) get written to replace what `chapter_segmentation.run()`
used to do inline. Per the earlier decision to do this *before* the current
feature branch is finished, the remaining Zotero-integration work
(chapter linking, review UI, retrofit, upload) continues on top of the new
package rather than the in-tree modules. The exact command sequence,
file-by-file rewrite order, and test-migration steps belong in the
implementation plan (`writing-plans`), not this design doc.

## 10. Testing

- The new repo's own test suite (moved wholesale per §3) runs standalone
  with `uv run pytest`, with no zotero-rag checkout required — the
  evaluation harness in particular must work purely off the committed
  `public-cache/` corpus with no live Kreuzberg sidecar for the default
  (non-integration) run, matching today's behavior.
- `TesseractOcrBackend` gets its own integration-marked tests that actually
  invoke the `tesseract` binary against a small fixture PDF. Because this
  needs only a CI-installed system package (no sidecar, no network), the
  new repo's CI workflow can install `tesseract-ocr` + language packs and
  run these as part of the normal test job — real-OCR coverage in CI that
  today's Kreuzberg-sidecar-only setup doesn't have.
- zotero-rag's test suite is updated to import from the new package's
  installed path instead of local `backend.services.chapter_*` modules; the
  thin orchestrator and protocol adapters (§4, §5) get their own new unit
  tests in zotero-rag, since that glue code doesn't exist yet.
- The container smoke test and startup-sequence test are re-run once the
  new git dependency is added, to confirm the built image still resolves
  and installs it correctly.
