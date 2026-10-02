# Configurable Kreuzberg Timeout + Auto-Repair for Timed-Out Attachments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Kreuzberg extraction timeout cap configurable via settings/env var, let a single extraction call request a scaled-up timeout, and have the "Fix Unavailable" dialog's "Search & Fix" button automatically retry `skipReason: 'timeout'` rows with double the timeout instead of immediately giving up on them.

**Architecture:** Backend: `KreuzbergExtractor._compute_timeout()` gains a `cap` (from new `Settings.kreuzberg_timeout_seconds`, replacing the hardcoded 1800s constant) and a per-call `multiplier` (default 1.0, unused by normal indexing). Both are threaded through `DocumentProcessor._process_attachment_bytes()` down to the existing `POST /api/index/document` / `/document/async` upload endpoints as an optional `timeout_multiplier` form field. Plugin: `RemoteIndexer._uploadAttachment()` gains a `timeoutMultiplier` option that becomes that form field; `fix-unavailable.js`'s `searchAndFix()` calls it with `multiplier=2` for `skipReason === 'timeout'` rows (but *not* `skipReason === 'no text'` rows, which are a content problem a timeout can't fix) via a new `ZoteroRAGPlugin.retryTimeoutSkippedAttachment()` helper, pruning the store entry on success via a new `removeSkippedServerItems()` (mirrors the existing `removeDownloadFailedItems()`). No new backend endpoint, no subprocess/CLI script involved — this reuses the exact same client-push upload path the plugin already uses for regular indexing.

**Tech Stack:** Python/FastAPI (backend), vanilla JS in Zotero's chrome context (plugin), `unittest`/`node --test` for testing.

**Out of scope (do not implement):** `skipReason === 'no text'` rows (genuinely empty extraction — a longer timeout can't produce text that isn't there) keep the existing immediate "Not indexable" behavior. The server-side cron/`bin/index_libraries.py` path is untouched — this plan only wires the client-driven repair action; picking up timeout-cap changes automatically on the nightly cron is a separate, larger piece of work (incremental-mode version-cursor skipping) that was explicitly deferred.

---

### Task 1: Backend — configurable Kreuzberg timeout cap

**Files:**
- Modify: `backend/config/settings.py:97-101` (add new field after `kreuzberg_url`)
- Modify: `backend/services/extraction/kreuzberg.py:26-35,54-68`
- Modify: `backend/services/extraction/__init__.py:26-66`
- Modify: `backend/services/document_processor.py:265-273`
- Create: `backend/tests/test_kreuzberg_extractor.py`
- Modify: `.env.dist:94-96`

- [ ] **Step 1: Write the failing test for `_compute_timeout`'s configurable cap and multiplier**

Create `backend/tests/test_kreuzberg_extractor.py`:

```python
"""Unit tests for the Kreuzberg extraction adapter's timeout computation."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.services.extraction.kreuzberg import KreuzbergExtractor, _compute_timeout


class TestComputeTimeout(unittest.TestCase):
    def test_scales_with_content_size(self):
        small = _compute_timeout(10_000, "application/pdf")
        large = _compute_timeout(10_000_000, "application/pdf")
        self.assertGreater(large, small)

    def test_floor_applies_to_tiny_files(self):
        self.assertEqual(_compute_timeout(1, "application/pdf"), 60)

    def test_default_cap_is_1800_seconds(self):
        # A huge PDF should saturate at the default cap, not grow unbounded.
        self.assertEqual(_compute_timeout(10_000_000_000, "application/pdf"), 1800)

    def test_custom_cap_overrides_default(self):
        self.assertEqual(
            _compute_timeout(10_000_000_000, "application/pdf", cap=600), 600
        )

    def test_multiplier_scales_both_computed_value_and_cap(self):
        # A file whose computed (unscaled) timeout would already hit the
        # default cap must still get more time when multiplier > 1 — the
        # cap itself has to scale, not just the raw size/rate division,
        # otherwise doubling the timeout for an already-capped huge file
        # would be a no-op.
        capped = _compute_timeout(10_000_000_000, "application/pdf", cap=1800, multiplier=1.0)
        doubled = _compute_timeout(10_000_000_000, "application/pdf", cap=1800, multiplier=2.0)
        self.assertEqual(capped, 1800)
        self.assertEqual(doubled, 3600)

    def test_multiplier_scales_a_below_cap_value_too(self):
        base = _compute_timeout(300_000, "application/pdf", cap=1800, multiplier=1.0)
        doubled = _compute_timeout(300_000, "application/pdf", cap=1800, multiplier=2.0)
        self.assertEqual(doubled, base * 2)


class TestKreuzbergExtractorTimeoutWiring(unittest.IsolatedAsyncioTestCase):
    async def test_extract_and_chunk_uses_configured_cap_and_multiplier(self):
        extractor = KreuzbergExtractor(timeout_cap=600)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=[{"chunks": []}])

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "backend.services.extraction.kreuzberg.httpx.AsyncClient",
            return_value=mock_client,
        ) as mock_client_cls:
            await extractor.extract_and_chunk(
                b"x" * 10_000_000_000, "application/pdf", timeout_multiplier=2.0
            )

        # cap=600, multiplier=2.0 → expect the doubled cap, not the default 1800-based one.
        mock_client_cls.assert_called_once_with(timeout=1200)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest backend/tests/test_kreuzberg_extractor.py -v`
Expected: FAIL — `_compute_timeout() got an unexpected keyword argument 'cap'` (and `KreuzbergExtractor.__init__() got an unexpected keyword argument 'timeout_cap'`).

- [ ] **Step 3: Make `_compute_timeout` accept `cap` and `multiplier`, and wire `KreuzbergExtractor` to carry a configured cap**

In `backend/services/extraction/kreuzberg.py`, replace lines 26-35:

```python
_TIMEOUT_FLOOR = 60       # seconds
_TIMEOUT_CAP_DEFAULT = 1800    # seconds (30 min) — overridable via Settings.kreuzberg_timeout_seconds
_BYTES_PER_SECOND_PDF = 3_000    # OCR-heavy; slow per byte
_BYTES_PER_SECOND_OTHER = 10_000


def _compute_timeout(
    content_size: int,
    mime_type: str,
    cap: int = _TIMEOUT_CAP_DEFAULT,
    multiplier: float = 1.0,
) -> int:
    """Return a per-request timeout scaled to document size and type.

    `multiplier` scales both the size-based computed value and the cap by the
    same factor — scaling only the raw value would be a no-op for any file
    already large enough to saturate the cap, which is exactly the case a
    repair retry with a longer timeout needs to help.
    """
    rate = _BYTES_PER_SECOND_PDF if mime_type == "application/pdf" else _BYTES_PER_SECOND_OTHER
    scaled_cap = int(cap * multiplier)
    scaled_value = int((content_size // rate) * multiplier)
    return max(_TIMEOUT_FLOOR, min(scaled_cap, scaled_value))
```

Then update `KreuzbergExtractor.__init__` (lines 54-68) to accept and store a configured cap:

```python
    def __init__(
        self,
        kreuzberg_url: str = "http://localhost:8100",
        max_chunk_size: int = 512,
        chunk_overlap: int = 50,
        ocr_enabled: bool = True,
        timeout_cap: int = _TIMEOUT_CAP_DEFAULT,
    ):
        """
        Args:
            kreuzberg_url: Base URL of the kreuzberg sidecar (e.g. "http://localhost:8100").
            max_chunk_size: Maximum characters per chunk.
            chunk_overlap: Overlap characters between consecutive chunks.
            ocr_enabled: Whether to attempt OCR on image-only pages.
            timeout_cap: Upper bound (seconds) for the per-request timeout computed
                from document size — see _compute_timeout(). Normally comes from
                Settings.kreuzberg_timeout_seconds.
        """
        self._kreuzberg_url = kreuzberg_url.rstrip("/")
        self._ocr_enabled = ocr_enabled
        self._timeout_cap = timeout_cap
        self._config: dict[str, Any] = {
            "chunking": {
                "max_characters": max_chunk_size,
                "overlap": chunk_overlap,
            },
            "force_ocr": ocr_enabled,
        }
        logger.debug(
            f"Initialized KreuzbergExtractor (url={kreuzberg_url}, "
            f"max_chars={max_chunk_size}, overlap={chunk_overlap}, ocr={ocr_enabled}, "
            f"timeout_cap={timeout_cap})"
        )
```

And update `extract_and_chunk` (currently lines 82-98) to accept `timeout_multiplier` and pass both through:

```python
    async def extract_and_chunk(
        self,
        content: bytes,
        mime_type: str,
        timeout_multiplier: float = 1.0,
    ) -> list[ExtractionChunk]:
        """
        Send document bytes to the kreuzberg sidecar and return extraction chunks.

        Args:
            content: Raw document bytes.
            mime_type: MIME type of the document (e.g. "application/pdf").
            timeout_multiplier: Scales both the computed per-request timeout and
                this instance's configured cap by this factor. Used only by the
                Fix Unavailable repair action for attachments that previously hit
                `skipped_timeout` — normal indexing always uses the default 1.0.

        Returns:
            List of ExtractionChunk objects, empty if extraction fails.
        """
        url = f"{self._kreuzberg_url}/extract"
        timeout = _compute_timeout(len(content), mime_type, cap=self._timeout_cap, multiplier=timeout_multiplier)
        logger.debug(
            f"kreuzberg request: mime={mime_type} size={len(content)} timeout={timeout}s "
            f"(cap={self._timeout_cap}, multiplier={timeout_multiplier})"
        )
```

(The rest of the method body is unchanged — only the `timeout = _compute_timeout(...)` line and the log message change.)

- [ ] **Step 4: Thread the configured cap through `create_document_extractor` and the new `Settings` field**

In `backend/services/extraction/__init__.py`, update the factory (lines 26-66):

```python
def create_document_extractor(
    backend: str = "kreuzberg",
    max_chunk_size: int = 512,
    chunk_overlap: int = 50,
    ocr_enabled: bool = True,
    kreuzberg_url: str = "http://localhost:8100",
    kreuzberg_timeout_cap: int = 1800,
) -> DocumentExtractor:
    """
    Factory: create a DocumentExtractor for the named backend.

    Args:
        backend: One of "kreuzberg" or "legacy".
        max_chunk_size: Maximum characters per chunk.
        chunk_overlap: Overlap between consecutive chunks.
        ocr_enabled: Whether to enable OCR (Kreuzberg only).
        kreuzberg_url: Base URL of the kreuzberg sidecar (kreuzberg backend only).
        kreuzberg_timeout_cap: Upper bound (seconds) for the per-request timeout
            computed from document size (kreuzberg backend only).

    Returns:
        Configured DocumentExtractor instance.

    Raises:
        ValueError: If the backend name is not recognised.
    """
    match backend:
        case "kreuzberg":
            return KreuzbergExtractor(
                kreuzberg_url=kreuzberg_url,
                max_chunk_size=max_chunk_size,
                chunk_overlap=chunk_overlap,
                ocr_enabled=ocr_enabled,
                timeout_cap=kreuzberg_timeout_cap,
            )
        case "legacy":
            return LegacyExtractor(
                max_chunk_size=max_chunk_size,
                chunk_overlap=chunk_overlap,
            )
        case _:
            available = ("kreuzberg", "legacy")
            raise ValueError(
                f"Unknown extraction backend '{backend}'. Available: {available}"
            )
```

In `backend/config/settings.py`, add a new field right after `kreuzberg_url` (after line 101):

```python
    kreuzberg_timeout_seconds: int = Field(
        default=1800,
        description="Upper bound (seconds) for the kreuzberg sidecar's per-request timeout, "
                    "which is otherwise scaled down automatically for smaller documents. "
                    "Raise this if large OCR-heavy PDFs or HTML snapshots hit skipped_timeout."
    )
```

In `backend/services/document_processor.py`, update the `create_document_extractor` call (lines 267-273):

```python
            document_extractor = create_document_extractor(
                backend=settings.extractor_backend,
                max_chunk_size=max_chunk_size,
                chunk_overlap=chunk_overlap,
                ocr_enabled=settings.ocr_enabled,
                kreuzberg_url=settings.kreuzberg_url,
                kreuzberg_timeout_cap=settings.kreuzberg_timeout_seconds,
            )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_kreuzberg_extractor.py -v`
Expected: PASS (all 7 tests)

- [ ] **Step 6: Fix the stale `.env.dist` documentation now that the setting actually works**

In `.env.dist`, replace lines 94-96:

```
# Upper bound (seconds) for the kreuzberg sidecar's per-request timeout — the
# actual per-request value is scaled down automatically for smaller documents.
# Large HTML snapshots and OCR-heavy PDFs may need more than the 1800s (30 min) default.
# KREUZBERG_TIMEOUT_SECONDS=1800
```

- [ ] **Step 7: Run the full backend test suite to check for regressions**

Run: `uv run pytest backend/tests/ -x -q`
Expected: PASS (no regressions — `create_document_extractor` and `KreuzbergExtractor.__init__` changes are purely additive with defaults matching prior behavior)

- [ ] **Step 8: Commit**

```bash
git add backend/config/settings.py backend/services/extraction/kreuzberg.py \
  backend/services/extraction/__init__.py backend/services/document_processor.py \
  backend/tests/test_kreuzberg_extractor.py .env.dist
git commit -m "feat(extraction): make Kreuzberg timeout cap configurable via KREUZBERG_TIMEOUT_SECONDS"
```

---

### Task 2: Backend — thread a per-call `timeout_multiplier` through `DocumentProcessor`

**Files:**
- Modify: `backend/services/extraction/base.py:33-49`
- Modify: `backend/services/extraction/legacy.py:40-44`
- Modify: `backend/services/document_processor.py:1105-1230,1316-1371`
- Modify: `backend/tests/test_document_processor.py:170-172,996-998` (update existing assertions)
- Test: `backend/tests/test_document_processor.py` (new test appended)

- [ ] **Step 1: Write the failing test for `timeout_multiplier` reaching the extractor**

Append to `backend/tests/test_document_processor.py` (inside `TestDocumentProcessor`, e.g. right after `test_index_library_html_attachment`, before the blank lines at line 999-1001):

```python
    async def test_process_attachment_bytes_passes_timeout_multiplier_to_extractor(self):
        """A caller-supplied timeout_multiplier must reach the extractor unchanged —
        this is how the Fix Unavailable repair action asks for a longer timeout on
        a single retry without changing the server's default behavior."""
        from backend.models.document import DocumentMetadata

        self.mock_extractor.extract_and_chunk.return_value = _make_extraction_chunks(("content", 1))
        self.mock_embedding_service.embed_batch.return_value = [[0.1, 0.2]]
        self.mock_vector_store.check_duplicate.return_value = None
        self.mock_vector_store.find_cross_library_duplicate.return_value = None
        self.mock_vector_store.add_chunks_batch.return_value = ["id1"]

        doc_metadata = DocumentMetadata(
            library_id="test_lib", item_key="ITEM1", attachment_key="ATT1",
            title="T", authors=[], year=None, item_type=None,
        )

        await self.processor._process_attachment_bytes(
            file_bytes=b"fake pdf bytes",
            mime_type="application/pdf",
            doc_metadata=doc_metadata,
            item_version=1,
            attachment_version=1,
            item_modified="2026-01-01T00:00:00Z",
            timeout_multiplier=2.0,
        )

        self.mock_extractor.extract_and_chunk.assert_called_once_with(
            b"fake pdf bytes", "application/pdf", timeout_multiplier=2.0
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest backend/tests/test_document_processor.py -k test_process_attachment_bytes_passes_timeout_multiplier_to_extractor -v`
Expected: FAIL — `_process_attachment_bytes() got an unexpected keyword argument 'timeout_multiplier'`

- [ ] **Step 3: Update `base.py` and `legacy.py` signatures (no-op for legacy, which has no timeout concept)**

In `backend/services/extraction/base.py`, replace the abstract method (lines 33-49):

```python
    @abstractmethod
    async def extract_and_chunk(
        self,
        content: bytes,
        mime_type: str,
        timeout_multiplier: float = 1.0,
    ) -> list[ExtractionChunk]:
        """
        Extract text from document bytes and split into chunks.

        Args:
            content: Raw document bytes.
            mime_type: MIME type hint (e.g. "application/pdf", "text/html").
            timeout_multiplier: Scales the per-request extraction timeout for
                backends that enforce one (Kreuzberg). Ignored by backends with
                no timeout concept (e.g. Legacy).

        Returns:
            Ordered list of ExtractionChunk objects.  Empty list if no text
            could be extracted (e.g. image-only PDF without OCR configured).
        """
```

In `backend/services/extraction/legacy.py`, update the signature (lines 40-44):

```python
    async def extract_and_chunk(
        self,
        content: bytes,
        mime_type: str,
        timeout_multiplier: float = 1.0,
    ) -> list[ExtractionChunk]:
```

(Body unchanged — `timeout_multiplier` is accepted and ignored.)

- [ ] **Step 4: Thread `timeout_multiplier` through `DocumentProcessor._process_attachment_bytes` and `_extract_pdf_in_parts`**

In `backend/services/document_processor.py`, update `_process_attachment_bytes`'s signature (lines 1105-1114):

```python
    async def _process_attachment_bytes(
        self,
        file_bytes: bytes,
        mime_type: str,
        doc_metadata: "DocumentMetadata",
        item_version: int,
        attachment_version: int,
        item_modified: str,
        on_progress: Optional[Callable[[str], None]] = None,
        force_extraction: bool = False,
        timeout_multiplier: float = 1.0,
    ) -> AttachmentProcessingResult:
```

Add one line to its docstring's Args (after the existing `force_extraction` line, still inside the same docstring block):

```python
            timeout_multiplier: Scales the extraction timeout for this call only
                (passed through to the extractor). Used by the Fix Unavailable
                repair action to retry a previously skipped_timeout attachment
                with more time; normal indexing always uses the default 1.0.
```

Update the two call sites inside the same method (lines 1207-1220):

```python
        if mime_type == "application/pdf" and len(file_bytes) > settings.pdf_split_threshold:
            try:
                chunks = await self._extract_pdf_in_parts(
                    file_bytes, attachment_key, settings.pdf_split_target_part_size,
                    on_progress=on_progress,
                    timeout_multiplier=timeout_multiplier,
                )
            except KreuzbergParsingError as e:
                logger.warning(f"Skipping attachment {attachment_key} (parse error — unsplittable PDF): {e}")
                return AttachmentProcessingResult(chunks_written=0, status="skipped_parse_error", error_detail=str(e))
        else:
            if on_progress:
                on_progress("Extracting text...")
            try:
                chunks = await self.document_extractor.extract_and_chunk(
                    file_bytes, mime_type, timeout_multiplier=timeout_multiplier
                )
```

(The `except` blocks following stay exactly as-is.)

Update `_extract_pdf_in_parts`'s signature and its own `extract_and_chunk` call (lines 1316-1322, 1360-1366):

```python
    async def _extract_pdf_in_parts(
        self,
        pdf_bytes: bytes,
        attachment_key: str,
        target_part_bytes: int,
        on_progress: Optional[Callable[[str], None]] = None,
        timeout_multiplier: float = 1.0,
    ) -> list[ExtractionChunk]:
```

```python
            try:
                part_chunks = await self.document_extractor.extract_and_chunk(
                    part_bytes, "application/pdf", timeout_multiplier=timeout_multiplier
                )
```

- [ ] **Step 5: Update the two existing tests whose exact-args assertions now need the new kwarg**

In `backend/tests/test_document_processor.py`, update line 170-172 (inside `test_index_library_with_pdf_success`):

```python
        self.mock_extractor.extract_and_chunk.assert_called_once_with(
            b"fake pdf bytes", "application/pdf", timeout_multiplier=1.0
        )
```

And line 996-998 (inside `test_index_library_html_attachment`):

```python
        self.mock_extractor.extract_and_chunk.assert_called_once_with(
            b"<html><body>Hello</body></html>", "text/html", timeout_multiplier=1.0
        )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_document_processor.py -v`
Expected: PASS (all tests, including the 2 updated and 1 new)

- [ ] **Step 7: Run the full backend test suite to check for regressions**

Run: `uv run pytest backend/tests/ -x -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add backend/services/extraction/base.py backend/services/extraction/legacy.py \
  backend/services/document_processor.py backend/tests/test_document_processor.py
git commit -m "feat(extraction): thread a per-call timeout_multiplier through DocumentProcessor"
```

---

### Task 3: Backend — expose `timeout_multiplier` on the document upload endpoints

**Files:**
- Modify: `backend/api/document_upload.py:250-267,589-661,669-745,968-1010` (exact ranges below)
- Test: `backend/tests/test_document_upload_authorization.py` (new test appended)

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_document_upload_authorization.py`:

```python
    def test_upload_document_accepts_timeout_multiplier_field(self):
        self._set_identity(ZoteroIdentity(user_id=1, username="u", targets=["users/1"]))
        metadata = json.dumps({
            "library_id": "u1",
            "item_key": "ITEM1",
            "attachment_key": "ATT1",
        })
        r = self.client.post(
            "/api/index/document",
            files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
            data={"metadata": metadata, "timeout_multiplier": "2.0"},
        )
        # Not 422/400 — the field is accepted and parsed without error. (Testing
        # mode's extractor stub makes this a cheap smoke test, not a full
        # end-to-end timeout check — that's covered by Task 2's unit tests.)
        self.assertEqual(r.status_code, 200)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest backend/tests/test_document_upload_authorization.py -k test_upload_document_accepts_timeout_multiplier_field -v`
Expected: FAIL only if the form field causes a 422 — check first; FastAPI ignores unknown form fields by default, so this test may already pass. If it already passes, skip straight to Step 3 to wire the field through (the test alone doesn't prove the value is *used*; Task 2's tests already cover that part of the chain) and re-run after Step 3 to confirm it still passes with the field actually wired.

- [ ] **Step 3: Add the `timeout_multiplier` form field to `_parse_upload_request` and thread it to `_execute_upload`**

In `backend/api/document_upload.py`, update `_parse_upload_request`'s signature and return (it starts at line 968):

```python
async def _parse_upload_request(
    file: UploadFile, metadata: str, identity: Optional[ZoteroIdentity], timeout_multiplier: float = 1.0
):
    """Parse and validate the common multipart upload fields.

    Returns a tuple of all parsed fields needed by both sync and async endpoints.
    Raises HTTPException 400 on validation errors, or 403 if the caller's identity
    is not authorized for the requested library_id.
    """
```

Update the function's final `return` statement (currently lines 1015-1019) to add `timeout_multiplier` as its last element:

```python
    return (
        meta_dict, doc_metadata, library_id, item_key, attachment_key, user_id,
        library_type, mime_type, item_version, attachment_version, item_modified,
        file_bytes, timeout_multiplier,
    )
```

Then update both call sites to unpack it and both endpoint signatures to accept it as a `Form` field, and `_execute_upload`/`_run_task` to accept and use it:

In `_execute_upload`'s signature (lines 250-267), add one parameter:

```python
async def _execute_upload(
    *,
    file_bytes: bytes,
    content_hash: str,
    doc_metadata: DocumentMetadata,
    library_id: str,
    library_type: str,
    item_key: str,
    attachment_key: str,
    mime_type: str,
    item_version: int,
    attachment_version: int,
    item_modified: str,
    library_name: str,
    vector_store: VectorStore,
    embedding_service,
    on_progress: Optional[Callable[[str], None]] = None,
    timeout_multiplier: float = 1.0,
) -> DocumentUploadResult:
```

Update its call into `_process_attachment_bytes` (currently lines 326-334):

```python
        proc_result = await processor._process_attachment_bytes(
            file_bytes=file_bytes,
            mime_type=mime_type,
            doc_metadata=doc_metadata,
            item_version=item_version,
            attachment_version=attachment_version,
            item_modified=item_modified,
            on_progress=on_progress,
            timeout_multiplier=timeout_multiplier,
        )
```

In `upload_and_index_document` (the sync endpoint, lines 589-603), add the form field and pass it through:

```python
async def upload_and_index_document(
    http_request: Request,
    file: UploadFile = File(..., description="Raw attachment bytes"),
    metadata: str = Form(
        ...,
        description=(
            "JSON string with fields: library_id, library_type, item_key, "
            "attachment_key, mime_type, item_version, attachment_version, "
            "title, authors (array), year, item_type, "
            "zotero_modified (ISO 8601 string)"
        ),
    ),
    timeout_multiplier: float = Form(
        1.0,
        description="Scales the extraction timeout for this upload only. Used by the "
                    "Fix Unavailable repair action to retry a previously skipped_timeout "
                    "attachment with more time.",
    ),
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
):
```

Update its body's call (currently lines 629-661) to pass `timeout_multiplier` into both `_parse_upload_request` and `_execute_upload`:

```python
    meta_dict, doc_metadata, library_id, item_key, attachment_key, user_id, \
        library_type, mime_type, item_version, attachment_version, item_modified, \
        file_bytes, timeout_multiplier = await _parse_upload_request(
            file, metadata, identity, timeout_multiplier
        )
```

```python
    return await _execute_upload(
        file_bytes=file_bytes,
        content_hash=content_hash,
        doc_metadata=doc_metadata,
        library_id=library_id,
        library_type=library_type,
        item_key=item_key,
        attachment_key=attachment_key,
        mime_type=mime_type,
        item_version=item_version,
        attachment_version=attachment_version,
        item_modified=item_modified,
        library_name=meta_dict.get("library_name", ""),
        vector_store=vector_store,
        embedding_service=embedding_service,
        timeout_multiplier=timeout_multiplier,
    )
```

Do the same for `upload_and_index_document_async`. Update its signature (lines 669-675):

```python
async def upload_and_index_document_async(
    http_request: Request,
    file: UploadFile = File(..., description="Raw attachment bytes"),
    metadata: str = Form(...),
    timeout_multiplier: float = Form(
        1.0,
        description="Scales the extraction timeout for this upload only. Used by the "
                    "Fix Unavailable repair action to retry a previously skipped_timeout "
                    "attachment with more time.",
    ),
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
    vector_store: VectorStore = Depends(get_vector_store),
):
```

Update its body's unpacking (currently lines 686-688):

```python
    meta_dict, doc_metadata, library_id, item_key, attachment_key, user_id, \
        library_type, mime_type, item_version, attachment_version, item_modified, \
        file_bytes, timeout_multiplier = await _parse_upload_request(
            file, metadata, identity, timeout_multiplier
        )
```

And add `timeout_multiplier=timeout_multiplier` to the `asyncio.create_task(_run_task(...))` call's kwargs (currently lines 729-745):

```python
    asyncio.create_task(_run_task(
        task_id,
        file_bytes=file_bytes,
        content_hash=content_hash,
        doc_metadata=doc_metadata,
        library_id=library_id,
        library_type=library_type,
        item_key=item_key,
        attachment_key=attachment_key,
        mime_type=mime_type,
        item_version=item_version,
        attachment_version=attachment_version,
        item_modified=item_modified,
        library_name=meta_dict.get("library_name", ""),
        vector_store=vector_store,
        embedding_service=embedding_service,
        timeout_multiplier=timeout_multiplier,
    ))
```

`_run_task` (lines 414-435) already forwards `**kwargs` straight to `_execute_upload` — no change needed there.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest backend/tests/test_document_upload_authorization.py -v`
Expected: PASS

- [ ] **Step 5: Run the full backend test suite to check for regressions**

Run: `uv run pytest backend/tests/ -x -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/api/document_upload.py backend/tests/test_document_upload_authorization.py
git commit -m "feat(api): accept an optional timeout_multiplier on the document upload endpoints"
```

---

### Task 4: Plugin — `RemoteIndexer._uploadAttachment` gains a `timeoutMultiplier` option

**Files:**
- Modify: `plugin/src/remote_indexer.js:786-906`

`plugin/test/remote_indexer.test.js` exists, but its vm-context harness (`loadRemoteIndexer()`, lines 17-28) stubs only `Zotero` — it has no `IOUtils`, `FormData`, `Blob`, or `fetch`/`_apiFetch` stubs, all of which `_uploadAttachment` needs. Building that infrastructure from scratch is out of scope for this plan; this step is covered by Task 7's manual verification instead (consistent with how this file's other network-calling methods, e.g. `indexLibrary()`, also have no direct unit coverage — only the trash-exclusion query-building logic in `countIndexableAttachments` is unit-tested, per the file's existing contents).

- [ ] **Step 1: Add the `timeoutMultiplier` option to `_uploadAttachment`**

In `plugin/src/remote_indexer.js`, update the JSDoc and signature (lines 786-801):

```js
	/**
	 * Upload a single attachment to the backend.
	 *
	 * @param {Object} opts
	 * @param {AttachmentInfo & {zoteroItem: any, parentItem: any, filePath: string|null}} opts.att
	 * @param {string} opts.libraryId
	 * @param {string} opts.libraryType
	 * @param {string} opts.backendURL
	 * @param {number|null} [opts.userId]
	 * @param {function(Record<string,string>=): Record<string,string>} opts.getAuthHeaders
	 * @param {function(string): void} opts.log
	 * @param {AbortSignal} [opts.signal]
	 * @param {function(string): void} [opts.onStatusUpdate]
	 * @param {number} [opts.timeoutMultiplier] - Scales the backend's extraction timeout for
	 *   this upload only (used by the Fix Unavailable repair action). Defaults to 1.0 (no change).
	 * @returns {Promise<{rateLimitHeaders: Record<string,string>|null, parseError?: boolean, skippedEmpty?: boolean, skippedTimeout?: boolean, errorDetail?: string|null}>}
	 */
	async _uploadAttachment({ att, libraryId, libraryType, backendURL, userId, getAuthHeaders, log, signal, onStatusUpdate = null, timeoutMultiplier = 1.0 }) {
```

Update the `formData` construction (currently lines 834-836) to include the new field:

```js
		const formData = new FormData();
		formData.append('file', new Blob([/** @type {any} */ (bytes)], { type: att.mime_type }), att.attachment_key);
		formData.append('metadata', JSON.stringify(metadata));
		if (timeoutMultiplier !== 1.0) {
			formData.append('timeout_multiplier', String(timeoutMultiplier));
		}
```

- [ ] **Step 2: Manually verify the plugin test suite still passes**

Run: `cd plugin && node --test`
Expected: PASS (no existing tests cover `_uploadAttachment`'s FormData contents, so this is a no-regression check, not new coverage)

- [ ] **Step 3: Commit**

```bash
git add plugin/src/remote_indexer.js
git commit -m "feat(plugin): let _uploadAttachment request a scaled-up extraction timeout"
```

---

### Task 5: Plugin — `removeSkippedServerItems()` + `retryTimeoutSkippedAttachment()` on `ZoteroRAGPlugin`

**Files:**
- Modify: `plugin/src/zotero-rag.js` (add two new methods near the existing skipped-server-store methods, `:2256-2353`)
- Test: `plugin/test/zotero-rag.test.js` (new tests appended)

- [ ] **Step 1: Write the failing tests, using the file's existing `makeStubs`/`loadPlugin` harness**

`plugin/test/zotero-rag.test.js` already has `makeStubs(attachmentsByKey)` (an in-memory fake filesystem + `Zotero.Items` stub, lines 24-48) and `loadPlugin(zoteroStub, ioUtilsStub, pathUtilsStub, extra)` (lines 58-70), used by the existing `removeDownloadFailedItems` tests (lines 83-112) — mirror those exactly for the skipped-server store. Append:

```js
test('removeSkippedServerItems prunes only the given keys, preserving the rest', async () => {
	const fakeAttachment = (key) => ({
		deleted: false,
		parentItemID: null,
		key,
		getCreators: () => [],
		getField: () => '',
	});
	const { zotero, ioUtils, pathUtils } = makeStubs({
		KEEP: fakeAttachment('KEEP'),
		REMOVE: fakeAttachment('REMOVE'),
	});
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);

	await plugin.storeSkippedServerItems('u1', [
		{ key: 'KEEP', reason: 'skipped_timeout' },
		{ key: 'REMOVE', reason: 'skipped_timeout' },
	]);
	await plugin.removeSkippedServerItems('u1', ['REMOVE']);

	const results = await plugin._getSkippedServerAttachments(1);
	assert.strictEqual(results.length, 1);
	assert.strictEqual(results[0].attachmentItem.key, 'KEEP');
});

test('removeSkippedServerItems is a no-op when the store does not exist yet', async () => {
	const { zotero, ioUtils, pathUtils } = makeStubs();
	const plugin = loadPlugin(zotero, ioUtils, pathUtils);

	// Must not throw even though no file has ever been written
	await plugin.removeSkippedServerItems('u1', ['ANY']);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd plugin && node --test test/zotero-rag.test.js`
Expected: FAIL — `plugin.removeSkippedServerItems is not a function`

- [ ] **Step 3: Implement `removeSkippedServerItems`, mirroring `removeDownloadFailedItems`**

In `plugin/src/zotero-rag.js`, add this method right after `storeSkippedServerItems` (after line 2293, before the `_getSkippedServerAttachments` method at line 2301):

```js
	/**
	 * Remove attachment keys from the persistent skipped-server store once
	 * they've been successfully reindexed (e.g. a Fix Unavailable repair retry
	 * with a longer timeout succeeded) — without this, storeSkippedServerItems's
	 * merge is one-directional and a fixed entry would keep reappearing on every
	 * subsequent Fix Unavailable refresh/reopen, same rationale as
	 * removeDownloadFailedItems.
	 * @param {string} backendLibraryId - Backend library ID
	 * @param {string[]} keysToRemove - Attachment keys that are now successfully indexed
	 * @returns {Promise<void>}
	 */
	async removeSkippedServerItems(backendLibraryId, keysToRemove) {
		if (!keysToRemove || keysToRemove.length === 0) return;
		const zoteroLibraryID = this._resolveZoteroLibraryID(backendLibraryId);
		if (!zoteroLibraryID) return;
		const filePath = this._skippedServerFilePath(zoteroLibraryID);
		/** @type {Array<{key: string, reason: string}>} */
		let existing = [];
		try {
			// @ts-ignore
			const text = await IOUtils.readUTF8(filePath);
			existing = JSON.parse(text);
		} catch (_) {
			return;
		}
		const toRemove = new Set(keysToRemove);
		const remaining = existing.filter(e => !toRemove.has(e.key));
		if (remaining.length === existing.length) return;
		try {
			// @ts-ignore
			await IOUtils.writeUTF8(filePath, JSON.stringify(remaining));
		} catch (e) {
			this.log(`[removeSkippedServerItems] Failed to write skipped-server file: ${e}`);
		}
	},
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd plugin && node --test test/zotero-rag.test.js`
Expected: PASS

- [ ] **Step 5: Add `retryTimeoutSkippedAttachment()` — the method that actually performs the repair upload**

No new unit test for this one: it is a thin orchestration wrapper around `RemoteIndexer._uploadAttachment` (already covered by Task 4) plus reading live Zotero item fields (`getFilePathAsync`, `getField`, etc.) that the existing test harness for this file does not stub (per the file's own comment about `window.arguments`-gated side-effect-free loading — see `fix-unavailable.test.js`'s header comment). This mirrors how `_tryDownloadAttachment`/`_searchAndFixUnavailableAttachment` (the two methods `fix-unavailable.js` already calls the same way) have no unit tests either — only `fix-unavailable.js`'s own orchestration logic around them is tested (Task 6). Manual verification is covered in Task 7.

Add this method to `plugin/src/zotero-rag.js`, near `removeSkippedServerItems` (added in Step 3):

```js
	/**
	 * Retry indexing a single attachment that previously hit skipped_timeout,
	 * with a doubled server-side extraction timeout. Used by the Fix Unavailable
	 * dialog's "Search & Fix" button — unlike a normal indexing run (which skips
	 * anything already in its version cache, including prior timeouts), this
	 * always re-uploads regardless of cache state.
	 * @param {any} attachmentItem - Zotero attachment item
	 * @param {any} parentItem - Zotero parent item (or the attachment itself if standalone)
	 * @param {number} libraryID - Zotero internal library ID
	 * @returns {Promise<{fixed: boolean, stillTimedOut: boolean, error?: string}>}
	 */
	async retryTimeoutSkippedAttachment(attachmentItem, parentItem, libraryID) {
		try {
			const library = Zotero.Libraries.get(libraryID);
			const libraryType = library ? library.libraryType : 'user';
			const backendLibraryId = this.getBackendLibraryId(libraryID);

			const att = {
				item_key: parentItem ? parentItem.key : attachmentItem.key,
				attachment_key: attachmentItem.key,
				mime_type: attachmentItem.attachmentContentType || 'application/pdf',
				item_version: parentItem ? (parentItem.version || 0) : (attachmentItem.version || 0),
				attachment_version: attachmentItem.version || 0,
				zoteroItem: attachmentItem,
				parentItem,
				filePath: null,
			};

			const result = await RemoteIndexer._uploadAttachment({
				att,
				libraryId: backendLibraryId,
				libraryType,
				backendURL: this.backendURL,
				userId: this.getCurrentZoteroUserId ? this.getCurrentZoteroUserId() : null,
				getAuthHeaders: (extra) => this.getAuthHeaders(extra),
				log: (msg) => this.log(msg),
				timeoutMultiplier: 2.0,
			});

			if (result.skippedTimeout) {
				return { fixed: false, stillTimedOut: true };
			}
			if (result.parseError || result.skippedEmpty) {
				// A doubled timeout surfaced a different, non-timeout failure —
				// treat as "not fixed, not a timeout anymore" rather than retry-forever.
				return { fixed: false, stillTimedOut: false, error: result.errorDetail || 'Extraction failed' };
			}
			return { fixed: true, stillTimedOut: false };
		} catch (e) {
			const msg = e instanceof Error ? e.message : String(e);
			return { fixed: false, stillTimedOut: false, error: msg };
		}
	},
```

`getBackendLibraryId(libraryID)` already exists on `ZoteroRAGPlugin` (`plugin/src/zotero-rag.js:813-820`) — it maps a Zotero-internal numeric `libraryID` to the backend's `"u{zoteroUserId}"` / group-numeric-ID string convention, which is exactly what `fix-unavailable.js`'s `backendLibraryId` is already populated from.

- [ ] **Step 6: Run the full plugin test suite to check for regressions**

Run: `cd plugin && node --test`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add plugin/src/zotero-rag.js plugin/test/zotero-rag.test.js
git commit -m "feat(plugin): add removeSkippedServerItems and retryTimeoutSkippedAttachment"
```

---

### Task 6: Plugin — `fix-unavailable.js` retries `skipReason: 'timeout'` rows by default

**Files:**
- Modify: `plugin/src/fix-unavailable.js:444-605`
- Test: `plugin/test/fix-unavailable.test.js` (new tests appended)

- [ ] **Step 1: Write the failing tests**

Append to `plugin/test/fix-unavailable.test.js`:

```js
test('searchAndFix retries skipReason timeout rows via retryTimeoutSkippedAttachment instead of marking them not-found immediately', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.libraryID = 1;
	dialog.isRunning = false;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'TIMEOUT1' }, parentItem: { key: 'PARENT1' }, skipReason: 'timeout', isLinked: false },
	];
	const retryCalls = [];
	const removedCalls = [];
	dialog.plugin = {
		retryTimeoutSkippedAttachment: async (att, parent, libId) => {
			retryCalls.push({ key: att.key, libId });
			return { fixed: true, stillTimedOut: false };
		},
		removeSkippedServerItems: async (libId, keys) => { removedCalls.push({ libId, keys }); },
	};

	await dialog.searchAndFix();

	assert.strictEqual(retryCalls.length, 1);
	assert.strictEqual(retryCalls[0].key, 'TIMEOUT1');
	assert.strictEqual(removedCalls.length, 1);
	assert.deepStrictEqual(removedCalls[0].keys, ['TIMEOUT1']);
	// Fixed row is removed from the table immediately, same as other fix paths.
	assert.strictEqual(dialog.items.length, 0);
});

test('searchAndFix keeps a still-timed-out row visible with an updated status, without pruning the store', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.libraryID = 1;
	dialog.isRunning = false;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'STILLSLOW' }, parentItem: { key: 'PARENT1' }, skipReason: 'timeout', isLinked: false },
	];
	let removeCalled = false;
	dialog.plugin = {
		retryTimeoutSkippedAttachment: async () => ({ fixed: false, stillTimedOut: true }),
		removeSkippedServerItems: async () => { removeCalled = true; },
	};

	await dialog.searchAndFix();

	assert.strictEqual(removeCalled, false);
	assert.strictEqual(dialog.items.length, 1);
	assert.strictEqual(dialog.rowStatus.get(0)?.cssClass, 'not-found');
});

test('searchAndFix does not retry skipReason "no text" rows — a timeout retry cannot fix genuinely empty extraction', async () => {
	const dialog = loadDialog();
	dialog.backendLibraryId = 'u1';
	dialog.libraryID = 1;
	dialog.isRunning = false;
	dialog.rowStatus = new Map();
	dialog.selected = new Set([0]);
	dialog.items = [
		{ attachmentItem: { key: 'EMPTY1' }, parentItem: { key: 'PARENT1' }, skipReason: 'no text', isLinked: false },
	];
	let retryCalled = false;
	dialog.plugin = {
		retryTimeoutSkippedAttachment: async () => { retryCalled = true; return { fixed: true, stillTimedOut: false }; },
		removeSkippedServerItems: async () => {},
	};

	await dialog.searchAndFix();

	assert.strictEqual(retryCalled, false);
	assert.strictEqual(dialog.rowStatus.get(0)?.cssClass, 'not-found');
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd plugin && node --test test/fix-unavailable.test.js`
Expected: FAIL — the first two tests fail because `retryTimeoutSkippedAttachment` is never called (current code immediately marks these rows `not-found` without calling the plugin); the third test currently passes already (good — confirms the "no text" bucket's existing behavior is the baseline we must not change).

- [ ] **Step 3: Split `skippedServerIndices` into a retryable `timeout` bucket and a non-retryable `no text` bucket, and add the retry phase**

In `plugin/src/fix-unavailable.js`, replace the bucketing block (lines 449-458):

```js
		const indices = this.getSelectedIndices();
		const parseErrorIndices  = indices.filter(i => this.items[i].isParseError);
		const timeoutIndices     = indices.filter(i => this.items[i].skipReason === 'timeout');
		const emptyTextIndices   = indices.filter(i => this.items[i].skipReason === 'no text');
		const linkedIndices      = indices.filter(i => !this.items[i].isParseError && !this.items[i].skipReason && this.items[i].isLinked);
		const importedIndices    = indices.filter(i => !this.items[i].isParseError && !this.items[i].skipReason && !this.items[i].isLinked);

		for (const i of parseErrorIndices)  this.setRowStatus(i, 'not-found', 'Binary data — delete and replace');
		for (const i of emptyTextIndices)   this.setRowStatus(i, 'not-found', 'Not indexable — delete or reindex after upgrade');
		for (const i of linkedIndices)      this.setRowStatus(i, 'not-found', 'Linked file — fix path in Zotero');
		for (const i of importedIndices)    this.setRowStatus(i, 'searching', 'Queued...');
		for (const i of timeoutIndices)     this.setRowStatus(i, 'searching', 'Retrying with longer timeout...');
```

Immediately after that block (still before the existing "Phase 1: batched sync downloads" comment/loop), add the new retry phase:

```js
		// Phase 0: retry skipReason='timeout' rows server-side with a doubled
		// extraction timeout. Unlike skipReason='no text' (genuinely empty —
		// no timeout can produce text that isn't there), a 'timeout' row's file
		// downloaded fine; only Kreuzberg's parsing pass ran out of time, so a
		// longer timeout can plausibly succeed. Sequential (not batched like
		// Phase 1) since these are exactly the largest/slowest files — running
		// several OCR-heavy extractions concurrently risks the Kreuzberg
		// sidecar's own memory limits.
		/** @type {Array<number>} */
		const timeoutFixedIndices = [];
		let timeoutStillFailed = 0;
		if (timeoutIndices.length > 0) {
			this.setStatus(`Retrying ${timeoutIndices.length} timed-out file(s) with a longer timeout...`);
			for (const i of timeoutIndices) {
				const info = this.items[i];
				try {
					const result = await this.plugin.retryTimeoutSkippedAttachment(
						info.attachmentItem, info.parentItem, this.libraryID
					);
					if (result.fixed) {
						this.setRowStatus(i, 'fixed', 'Fixed (longer timeout)');
						timeoutFixedIndices.push(i);
					} else if (result.stillTimedOut) {
						this.setRowStatus(i, 'not-found', 'Still times out — delete or raise the limit further');
						timeoutStillFailed++;
					} else {
						this.setRowStatus(i, 'error', `Retry failed: ${result.error}`, result.error);
						timeoutStillFailed++;
					}
				} catch (e) {
					const msg = e instanceof Error ? e.message : String(e);
					this.setRowStatus(i, 'error', `Error: ${msg}`, msg);
					timeoutStillFailed++;
					console.error(`fix-unavailable: timeout retry error for item ${info.zoteroID}: ${msg}`);
				}
			}
		}
		if (timeoutFixedIndices.length > 0 && this.plugin?.removeSkippedServerItems) {
			try {
				await this.plugin.removeSkippedServerItems(
					this.backendLibraryId, timeoutFixedIndices.map(i => this.items[i].attachmentItem.key)
				);
			} catch (e) {
				console.error(`fix-unavailable: failed to prune fixed skipped-server entries: ${e}`);
			}
		}
```

- [ ] **Step 4: Fold `timeoutFixedIndices` into the existing fixed-row bookkeeping and counts**

Replace the existing three-line counter block:

```js
		let fixed    = downloadResults.filter(r => r.downloaded).length;
		let notFound = linkedIndices.length + parseErrorIndices.length + skippedServerIndices.length;
		let errors   = 0;
```

with:

```js
		let fixed    = downloadResults.filter(r => r.downloaded).length + timeoutFixedIndices.length;
		let notFound = linkedIndices.length + parseErrorIndices.length + emptyTextIndices.length + timeoutStillFailed;
		let errors   = 0;
```

(`timeoutStillFailed` already counts both "still times out" and "retry errored" outcomes together — see Step 3's loop — so it folds entirely into `notFound` here; `errors` keeps its original meaning, Phase 2's copy-failed/exception count, unchanged.)

Update `allFixedIndices` (currently built from `downloadResults` and `phase2FixedIndices`) to also include `timeoutFixedIndices`:

```js
		const allFixedIndices = [
			...downloadResults.filter(r => r.downloaded).map(r => r.index),
			...phase2FixedIndices,
			...timeoutFixedIndices,
		];
```

Update the `fixedDownloadFailedKeys` filter just below — no change needed there, it already filters by `serverDownloadFailed`, and `timeoutFixedIndices` rows have `skipReason` set instead, so they're correctly excluded from that specific prune call (they're pruned separately via `removeSkippedServerItems` in Step 3 above).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd plugin && node --test test/fix-unavailable.test.js`
Expected: PASS (all tests, including the 3 new ones)

- [ ] **Step 6: Run the full plugin test suite to check for regressions**

Run: `cd plugin && node --test`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add plugin/src/fix-unavailable.js plugin/test/fix-unavailable.test.js
git commit -m "feat(fix-unavailable): retry timeout rows with a longer timeout by default on Search & Fix"
```

---

### Task 7: Manual verification

No automated end-to-end test exercises the full chain (Zotero UI → plugin → HTTP → Kreuzberg sidecar), consistent with how the rest of this dialog's interactive flows are verified (see `CLAUDE.md`'s "Live Query Debugging" section). Verify by hand:

- [ ] **Step 1: Start the backend and plugin dev servers**

Follow `CLAUDE.md`'s "Live Server" / "Hot Reload Plugin Development Server" sections (`npm start` for the backend; the scaffold's dev command for the plugin).

- [ ] **Step 2: Confirm `KREUZBERG_TIMEOUT_SECONDS` is read**

```bash
KREUZBERG_TIMEOUT_SECONDS=120 uv run python -c "
from backend.config.settings import get_settings, reset_settings
import os
reset_settings()
print(get_settings().kreuzberg_timeout_seconds)
"
```
Expected output: `120`

- [ ] **Step 3: Reproduce a `skipped_timeout` item against the test-rag-plugin group library**

Per `CLAUDE.md`'s "Live Query Debugging" guidance, use the `test-rag-plugin` group library (`groups/6297749`) for this — never a real personal/project library. Temporarily set a very low `KREUZBERG_TIMEOUT_SECONDS` (e.g. `5`) in `.env`, restart the backend, index a moderately sized PDF item in that library via the plugin's normal "Index" flow, and confirm it lands in `skipped-server-<libraryID>.json` with `reason: "skipped_timeout"` and shows up in Fix Unavailable with "timeout" in both the Type and Status columns (matching the original screenshot this plan is based on).

- [ ] **Step 4: Verify the repair action**

Restore a sane `KREUZBERG_TIMEOUT_SECONDS` (or remove the override to use the 1800s default) and restart the backend. Open Fix Unavailable for that library, select the timed-out row, click "Search & Fix Selected", and confirm:
- The row's status transitions to "Retrying with longer timeout..." then "Fixed (longer timeout)".
- The row disappears from the table immediately (no manual Refresh needed).
- Reopening Fix Unavailable (fresh `populateTable()`) does *not* show it again — confirming `skipped-server-<libraryID>.json` was pruned.
- The item is now genuinely searchable (its content shows up in a RAG query against that library).

- [ ] **Step 5: Verify a "no text" row is unaffected**

Confirm an item with `skipReason: 'no text'` still goes straight to "Not indexable — delete or reindex after upgrade" on Search & Fix, with no retry attempt (no new network request to `/api/index/document`) — this is the control case proving the timeout-specific fix didn't change the empty-text path.

---

## Summary of files touched

- `backend/config/settings.py` — new `kreuzberg_timeout_seconds` field
- `backend/services/extraction/kreuzberg.py` — configurable cap + per-call multiplier
- `backend/services/extraction/base.py`, `backend/services/extraction/legacy.py` — signature parity
- `backend/services/extraction/__init__.py` — factory threads the cap through
- `backend/services/document_processor.py` — threads `timeout_multiplier` end-to-end
- `backend/api/document_upload.py` — new `timeout_multiplier` form field on both upload endpoints
- `.env.dist` — un-stale the `KREUZBERG_TIMEOUT_SECONDS` documentation
- `plugin/src/remote_indexer.js` — `_uploadAttachment` gains `timeoutMultiplier`
- `plugin/src/zotero-rag.js` — new `removeSkippedServerItems`, `retryTimeoutSkippedAttachment`
- `plugin/src/fix-unavailable.js` — `searchAndFix()` retries `timeout` rows by default
- New/updated tests in `backend/tests/test_kreuzberg_extractor.py` (new), `backend/tests/test_document_processor.py`, `backend/tests/test_document_upload_authorization.py`, `plugin/test/zotero-rag.test.js`, `plugin/test/fix-unavailable.test.js`
