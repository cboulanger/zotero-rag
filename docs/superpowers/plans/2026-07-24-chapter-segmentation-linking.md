# Chapter Segmentation & Book/Chapter Linking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build four CLI+API tools (analyze, OCR, retrofit-link, segment & upload) plus an indexing-pipeline change so that edited-book libraries retrieve chapter-level citations instead of duplicate, less-specific book-level citations.

**Architecture:** A shared `Extra`-field linking scheme (`X-Contained-By` / `X-Contains` / `X-Chapter-Pdf-Range`) and a shared in-memory `JobTracker` underlie four independent script modules, each exposed as both a `scripts/*.py` CLI (progress rendered via `tqdm`) and a `POST /api/chapter-linking/*` route (polled via `GET /api/chapter-linking/jobs/{job_id}`). A final change to `document_processor.py` makes indexing consult the link data to suppress a book's own pages once a linked chapter covers them.

**Tech Stack:** Python 3.12, FastAPI, `pyzotero` (writes), `backend/zotero/web_api.py` `ZoteroWebAPI` (reads), `pypdf` (page-indexed text + slicing), Kreuzberg sidecar (OCR only), `rapidfuzz` (fuzzy matching, new dep), `langdetect` (language detection, new dep), `spacy` (`en_core_web_sm`, author NER), `tqdm` (CLI gauge), `unittest` (test style, matching this repo's convention).

**Full design reference:** `docs/superpowers/specs/2026-07-24-chapter-segmentation-linking-design.md` — every task below implements one section of that spec; section numbers are cited inline.

---

## Phase overview

| Phase | Produces | Depends on |
|---|---|---|
| 0. Foundation | `chapter_link_store.py`, `job_tracker.py`, `chapter_linking.py` router skeleton, new deps | — |
| 1. Script 1 — Analyze | `chapter_segmentation.py`, `scripts/analyze_book_chapters.py` | Phase 0 |
| 2. Script 3 — Retrofit link | `chapter_retrofit.py`, `scripts/retrofit_chapter_links.py` | Phase 0 |
| 3. Script 2 — OCR | Kreuzberg per-request language, `chapter_ocr.py`, `scripts/ocr_attachments.py` | Phase 0 |
| 4. Script 4 — Segment & upload | `chapter_upload.py`, `scripts/upload_chapters.py` | Phase 0, Phase 1 (consumes its output) |
| 5. Indexing suppression | `document_processor.py` change | Phase 0 |

Phases 1–4 are independent of each other once Phase 0 lands (Phase 4 only *consumes the JSON shape* Phase 1 produces — it doesn't need Phase 1's code). Phase 5 only needs Phase 0's `chapter_link_store`.

---

## Phase 0: Foundation

### Task 1: `chapter_link_store` — Extra-field linking scheme (§2)

**Files:**
- Create: `backend/services/chapter_link_store.py`
- Test: `backend/tests/test_chapter_link_store.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for backend.services.chapter_link_store."""

import unittest

from backend.services.chapter_link_store import (
    ChapterLinks,
    format_chapter_id,
    parse_library_slug,
    parse_links,
    write_links,
)


class TestParseLibrarySlug(unittest.TestCase):
    def test_user_slug(self):
        self.assertEqual(parse_library_slug("users/12345"), ("user", "12345", "u12345"))

    def test_group_slug(self):
        self.assertEqual(parse_library_slug("groups/6297749"), ("group", "6297749", "6297749"))


class TestFormatChapterId(unittest.TestCase):
    def test_format(self):
        self.assertEqual(format_chapter_id("groups/6297749", "WXYZ5678"), "groups/6297749:WXYZ5678")


class TestParseLinks(unittest.TestCase):
    def test_empty_extra(self):
        links = parse_links("")
        self.assertIsNone(links.contained_by)
        self.assertEqual(links.contains, [])
        self.assertEqual(links.pdf_ranges, {})

    def test_parses_all_three_keys(self):
        extra = (
            "Some other line: value\n"
            "X-Contained-By: groups/6297749:ABCD1234\n"
            "X-Contains: groups/6297749:WXYZ5678,groups/6297749:MNOP9012\n"
            "X-Chapter-Pdf-Range: groups/6297749:WXYZ5678:52-74,groups/6297749:MNOP9012:75-98\n"
        )
        links = parse_links(extra)
        self.assertEqual(links.contained_by, "groups/6297749:ABCD1234")
        self.assertEqual(
            links.contains, ["groups/6297749:WXYZ5678", "groups/6297749:MNOP9012"]
        )
        self.assertEqual(
            links.pdf_ranges,
            {
                "groups/6297749:WXYZ5678": (52, 74),
                "groups/6297749:MNOP9012": (75, 98),
            },
        )

    def test_ignores_unrelated_lines(self):
        links = parse_links("Citation Key: smith2023\nOther: stuff")
        self.assertIsNone(links.contained_by)
        self.assertEqual(links.contains, [])


class TestWriteLinks(unittest.TestCase):
    def test_appends_to_empty_extra(self):
        result = write_links("", contained_by="groups/1:AAAA1111")
        self.assertEqual(result, "X-Contained-By: groups/1:AAAA1111")

    def test_preserves_unrelated_content(self):
        result = write_links("Citation Key: smith2023", contains=["groups/1:BBBB2222"])
        self.assertIn("Citation Key: smith2023", result)
        self.assertIn("X-Contains: groups/1:BBBB2222", result)

    def test_replaces_existing_line_not_appends_duplicate(self):
        extra = "X-Contains: groups/1:OLD0000"
        result = write_links(extra, contains=["groups/1:NEW1111"])
        self.assertEqual(result.count("X-Contains:"), 1)
        self.assertIn("groups/1:NEW1111", result)
        self.assertNotIn("OLD0000", result)

    def test_idempotent(self):
        once = write_links("", contained_by="groups/1:AAAA1111", contains=["groups/1:BBBB2222"])
        twice = write_links(once, contained_by="groups/1:AAAA1111", contains=["groups/1:BBBB2222"])
        self.assertEqual(once, twice)

    def test_round_trip_with_pdf_ranges(self):
        extra = write_links(
            "",
            contains=["groups/1:BBBB2222"],
            pdf_ranges={"groups/1:BBBB2222": (52, 74)},
        )
        links = parse_links(extra)
        self.assertEqual(links.pdf_ranges, {"groups/1:BBBB2222": (52, 74)})

    def test_untouched_keys_left_as_is(self):
        extra = "X-Contained-By: groups/1:AAAA1111"
        result = write_links(extra, contains=["groups/1:BBBB2222"])
        self.assertIn("X-Contained-By: groups/1:AAAA1111", result)
        self.assertIn("X-Contains: groups/1:BBBB2222", result)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_chapter_link_store -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.services.chapter_link_store'`

- [ ] **Step 3: Implement `chapter_link_store.py`**

```python
"""Read/write the book<->chapter linking scheme stored in a Zotero item's
Extra field: X-Contained-By, X-Contains, X-Chapter-Pdf-Range.

See docs/superpowers/specs/2026-07-24-chapter-segmentation-linking-design.md
section 2 for the full rationale, in particular why X-Chapter-Pdf-Range
(PDF page-index space) is kept entirely separate from Zotero's native
`pages` field (printed/citation page-number space, human-editable).
"""

import re
from dataclasses import dataclass, field


@dataclass
class ChapterLinks:
    contained_by: str | None = None
    contains: list[str] = field(default_factory=list)
    pdf_ranges: dict[str, tuple[int, int]] = field(default_factory=dict)


_CONTAINED_BY_LINE = re.compile(r"^X-Contained-By:")
_CONTAINS_LINE = re.compile(r"^X-Contains:")
_PDF_RANGE_LINE = re.compile(r"^X-Chapter-Pdf-Range:")

_CONTAINED_BY_RE = re.compile(r"^X-Contained-By:\s*(.+)$", re.MULTILINE)
_CONTAINS_RE = re.compile(r"^X-Contains:\s*(.+)$", re.MULTILINE)
_PDF_RANGE_RE = re.compile(r"^X-Chapter-Pdf-Range:\s*(.+)$", re.MULTILINE)


def parse_library_slug(slug: str) -> tuple[str, str, str]:
    """Parse a Zotero slug ("users/12345" / "groups/678") into
    (library_type, numeric_id, backend_library_id), matching the backend's
    own u{id}/{id} convention (see backend/api/public_query.py).
    """
    prefix, numeric_id = slug.split("/", 1)
    library_type = "user" if prefix == "users" else "group"
    backend_library_id = f"u{numeric_id}" if library_type == "user" else numeric_id
    return library_type, numeric_id, backend_library_id


def format_chapter_id(slug: str, item_key: str) -> str:
    """Format a cross-library-safe chapter/book identifier: '<slug>:<item_key>'."""
    return f"{slug}:{item_key}"


def parse_links(extra: str) -> ChapterLinks:
    """Extract X-Contained-By / X-Contains / X-Chapter-Pdf-Range from an
    item's Extra field text. Unrelated lines are ignored.
    """
    extra = extra or ""
    links = ChapterLinks()

    m = _CONTAINED_BY_RE.search(extra)
    if m:
        links.contained_by = m.group(1).strip()

    m = _CONTAINS_RE.search(extra)
    if m:
        links.contains = [x.strip() for x in m.group(1).split(",") if x.strip()]

    m = _PDF_RANGE_RE.search(extra)
    if m:
        for entry in m.group(1).split(","):
            entry = entry.strip()
            if not entry:
                continue
            # entry format: "<slug>:<item_key>:<start>-<end>" — the chapter id
            # itself contains exactly one colon (slug:item_key), so splitting
            # from the right by one isolates the trailing range unambiguously.
            chapter_id, _, range_part = entry.rpartition(":")
            if not chapter_id or "-" not in range_part:
                continue
            start_s, end_s = range_part.split("-", 1)
            try:
                links.pdf_ranges[chapter_id] = (int(start_s), int(end_s))
            except ValueError:
                continue

    return links


def write_links(
    extra: str,
    *,
    contained_by: str | None = None,
    contains: list[str] | None = None,
    pdf_ranges: dict[str, tuple[int, int]] | None = None,
) -> str:
    """Replace (or append) the X-Contained-By / X-Contains /
    X-Chapter-Pdf-Range lines in an item's Extra field text. Only keys
    explicitly passed (non-None) are touched; everything else in `extra`
    (including unrelated X-* lines, e.g. Better BibTeX citekeys) is left
    untouched. Idempotent: calling twice with the same values is a no-op.
    """
    lines = (extra or "").splitlines()

    if contained_by is not None:
        lines = [ln for ln in lines if not _CONTAINED_BY_LINE.match(ln)]
        lines.append(f"X-Contained-By: {contained_by}")

    if contains is not None:
        lines = [ln for ln in lines if not _CONTAINS_LINE.match(ln)]
        if contains:
            lines.append(f"X-Contains: {','.join(contains)}")

    if pdf_ranges is not None:
        lines = [ln for ln in lines if not _PDF_RANGE_LINE.match(ln)]
        if pdf_ranges:
            entries = [f"{cid}:{start}-{end}" for cid, (start, end) in pdf_ranges.items()]
            lines.append(f"X-Chapter-Pdf-Range: {','.join(entries)}")

    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_chapter_link_store -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_link_store.py backend/tests/test_chapter_link_store.py
git commit -m "feat: add chapter_link_store for book/chapter Extra-field linking scheme"
```

---

### Task 2: `JobTracker` — shared progress harness (§4)

**Files:**
- Create: `backend/services/job_tracker.py`
- Test: `backend/tests/test_job_tracker.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for backend.services.job_tracker."""

import time
import unittest
from unittest.mock import patch

from backend.services.job_tracker import JobTracker, make_progress_callback


class TestJobTracker(unittest.TestCase):
    def setUp(self):
        self.tracker = JobTracker()

    def test_create_returns_processing_job(self):
        job_id = self.tracker.create()
        job = self.tracker.get(job_id)
        self.assertEqual(job.status, "processing")
        self.assertEqual(job.progress, 0.0)

    def test_update_progress_and_message(self):
        job_id = self.tracker.create()
        self.tracker.update(job_id, progress=0.5, message="halfway")
        job = self.tracker.get(job_id)
        self.assertEqual(job.progress, 0.5)
        self.assertEqual(job.message, "halfway")
        self.assertEqual(job.status, "processing")

    def test_update_with_result_marks_done(self):
        job_id = self.tracker.create()
        self.tracker.update(job_id, result={"chapters": []})
        job = self.tracker.get(job_id)
        self.assertEqual(job.status, "done")
        self.assertEqual(job.result, {"chapters": []})

    def test_update_with_error_marks_error(self):
        job_id = self.tracker.create()
        self.tracker.update(job_id, error="boom")
        job = self.tracker.get(job_id)
        self.assertEqual(job.status, "error")
        self.assertEqual(job.error, "boom")

    def test_unknown_job_id_returns_none(self):
        self.assertIsNone(self.tracker.get("does-not-exist"))

    def test_update_unknown_job_id_is_noop(self):
        self.tracker.update("does-not-exist", progress=1.0)  # must not raise

    def test_stale_jobs_are_pruned_on_get(self):
        job_id = self.tracker.create()
        with patch("time.monotonic", return_value=time.monotonic() + 3601):
            self.assertIsNone(self.tracker.get(job_id))


class TestMakeProgressCallback(unittest.TestCase):
    def test_callback_updates_tracker(self):
        tracker = JobTracker()
        job_id = tracker.create()
        callback = make_progress_callback(tracker, job_id)
        callback(0.3, "step 3 of 10")
        job = tracker.get(job_id)
        self.assertEqual(job.progress, 0.3)
        self.assertEqual(job.message, "step 3 of 10")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_job_tracker -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.services.job_tracker'`

- [ ] **Step 3: Implement `job_tracker.py`**

```python
"""In-memory job registry for on-demand, human-triggered background scripts
(chapter analyze/OCR/retrofit-link/segment-upload).

Mirrors backend/api/document_upload.py's _UploadTask pattern (in-memory dict
+ lock, pruned after an hour) rather than the subprocess/lock-file scheme in
cron_indexer.py — these are on-demand runs, not unattended scheduled jobs, so
job state does not need to survive a server restart. See design spec §4.
"""

import time
import uuid
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable, Optional

_PRUNE_AFTER_SECONDS = 3600


@dataclass
class JobStatus:
    job_id: str
    status: str = "processing"  # "processing" | "done" | "error"
    progress: float = 0.0
    message: str = ""
    result: Optional[Any] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)


class JobTracker:
    """Thread-safe in-memory job registry keyed by job_id."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobStatus] = {}
        self._lock = Lock()

    def create(self) -> str:
        job_id = str(uuid.uuid4())
        with self._lock:
            self._jobs[job_id] = JobStatus(job_id=job_id)
        return job_id

    def update(
        self,
        job_id: str,
        *,
        progress: Optional[float] = None,
        message: Optional[str] = None,
        result: Optional[Any] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if progress is not None:
                job.progress = progress
            if message is not None:
                job.message = message
            if result is not None:
                job.result = result
                job.status = "done"
            if error is not None:
                job.error = error
                job.status = "error"

    def get(self, job_id: str) -> Optional[JobStatus]:
        now = time.monotonic()
        with self._lock:
            stale = [jid for jid, j in self._jobs.items() if now - j.created_at > _PRUNE_AFTER_SECONDS]
            for jid in stale:
                del self._jobs[jid]
            return self._jobs.get(job_id)


def make_progress_callback(tracker: JobTracker, job_id: str) -> Callable[[float, str], None]:
    """Return a progress_callback(progress, message) closure bound to a job,
    for passing into a script's core run() function."""
    def _callback(progress: float, message: str) -> None:
        tracker.update(job_id, progress=progress, message=message)
    return _callback
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_job_tracker -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/job_tracker.py backend/tests/test_job_tracker.py
git commit -m "feat: add JobTracker shared progress harness for chapter-linking scripts"
```

---

### Task 3: `chapter_linking` API router skeleton — job polling endpoint (§4)

**Files:**
- Create: `backend/api/chapter_linking.py`
- Modify: `backend/main.py:20-21` (import), `backend/main.py:200-210` (registration)
- Test: `backend/tests/test_chapter_linking_api.py`

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for backend.api.chapter_linking (job-polling endpoint)."""

import unittest

from fastapi.testclient import TestClient

from backend.api import chapter_linking
from backend.main import app


class TestJobPolling(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_unknown_job_returns_404(self):
        response = self.client.get("/api/chapter-linking/jobs/does-not-exist")
        self.assertEqual(response.status_code, 404)

    def test_known_job_returns_status(self):
        job_id = chapter_linking.tracker.create()
        chapter_linking.tracker.update(job_id, progress=0.5, message="working")
        response = self.client.get(f"/api/chapter-linking/jobs/{job_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "processing")
        self.assertEqual(body["progress"], 0.5)
        self.assertEqual(body["message"], "working")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest backend.tests.test_chapter_linking_api -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.api.chapter_linking'`

- [ ] **Step 3: Implement the router skeleton**

```python
"""API routes for chapter segmentation & book/chapter linking: analyze, OCR,
retrofit-link, segment-upload, and a shared job-status poll.

See docs/superpowers/specs/2026-07-24-chapter-segmentation-linking-design.md
section 4. Each POST .../{analyze,ocr,retrofit-link,segment-upload} endpoint
is added in its own phase (Tasks 12, 17, 23, 29) once that script's core
run() function exists; this task only wires up the shared job registry and
its polling endpoint.
"""

import logging

from fastapi import APIRouter, HTTPException

from backend.services.job_tracker import JobTracker

logger = logging.getLogger(__name__)
router = APIRouter()

# Module-level singleton, shared by every chapter-linking endpoint in this
# router — mirrors backend/api/document_upload.py's module-level _upload_tasks.
tracker = JobTracker()


@router.get(
    "/chapter-linking/jobs/{job_id}",
    summary="Poll the status of a chapter-linking background job",
)
async def get_job_status(job_id: str) -> dict:
    job = tracker.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=f"Job {job_id!r} not found (may have expired or never existed)",
        )
    return {
        "job_id": job.job_id,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "result": job.result,
        "error": job.error,
    }
```

- [ ] **Step 4: Register the router in `backend/main.py`**

In `backend/main.py:20`, extend the import line:
```python
from backend.api import config, libraries, indexing, query, document_upload, registration, rate_limits, public_query, autoindex, auth, chapter_linking
```

In `backend/main.py`, immediately after the existing `app.include_router(auth.router, prefix="/api", tags=["auth"])` line:
```python
app.include_router(chapter_linking.router, prefix="/api", tags=["chapter-linking"])
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run python -m unittest backend.tests.test_chapter_linking_api -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/api/chapter_linking.py backend/main.py backend/tests/test_chapter_linking_api.py
git commit -m "feat: add chapter-linking API router skeleton with job-status polling"
```

---

### Task 4: Add new dependencies

**Files:**
- Modify: `pyproject.toml:8-30`

- [ ] **Step 1: Add `rapidfuzz` and `langdetect` to the dependency list**

In `pyproject.toml`, insert two lines into the `dependencies` list (after `"pypdf>=5.1.0",`, matching the existing one-per-line, double-quoted, trailing-comma style):

```toml
    "pypdf>=5.1.0",
    "rapidfuzz>=3.10.0",
    "langdetect>=1.0.9",
```

- [ ] **Step 2: Sync the environment**

Run: `uv sync`
Expected: `rapidfuzz` and `langdetect` (and their transitive deps) installed into `.venv` with no errors.

- [ ] **Step 3: Verify import**

Run: `uv run python -c "import rapidfuzz, langdetect; print('ok')"`
Expected: prints `ok`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: add rapidfuzz and langdetect dependencies for chapter linking"
```

---

## Phase 1: Script 1 — Analyze (§5)

### Task 5: Page-text loading + TOC candidate detection

**Files:**
- Create: `backend/services/chapter_segmentation.py`
- Test: `backend/tests/test_chapter_segmentation.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for backend.services.chapter_segmentation."""

import unittest

from backend.services.chapter_segmentation import (
    TocEntry,
    extract_page_texts_from_pdf_bytes,
    find_toc_candidates,
)


class TestFindTocCandidates(unittest.TestCase):
    def test_finds_dotted_leader_entries(self):
        pages = [
            "CONTENTS\n"
            "Introduction to Reference Management ..... 1\n"
            "Comparing Citation Styles ..... 45\n"
            "Zotero in Practice ..... 89\n",
            "Some front-matter page with no TOC pattern at all.",
        ]
        entries = find_toc_candidates(pages)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0], TocEntry(title="Introduction to Reference Management", printed_page_number=1, source_page_index=0))
        self.assertEqual(entries[2].title, "Zotero in Practice")
        self.assertEqual(entries[2].printed_page_number, 89)

    def test_ignores_non_toc_lines(self):
        pages = ["Just some ordinary prose with numbers like 1999 in it, no leaders here."]
        entries = find_toc_candidates(pages)
        self.assertEqual(entries, [])

    def test_matches_whitespace_leaders_too(self):
        pages = ["Bibliographic Software Overview          12"]
        entries = find_toc_candidates(pages)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "Bibliographic Software Overview")
        self.assertEqual(entries[0].printed_page_number, 12)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_chapter_segmentation -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.services.chapter_segmentation'`

- [ ] **Step 3: Implement page-text loading and TOC detection**

```python
"""Heuristic chapter-boundary detection for script 1 (analyze_book_chapters).

Critical invariant (design spec §5): PDF physical page index and printed
page number are NEVER assumed equal. Page index comes from
extract_page_texts_from_pdf_bytes (pypdf, or a cached per-page OCR result —
see chapter_ocr.py), which is inherently index-based (list position = PDF
page index). Printed page numbers are only ever obtained by reading text
that actually appears on a page (TOC entries, header/footer numerals) —
never assumed from position.
"""

import io
import re
from dataclasses import dataclass

from pypdf import PdfReader

# Matches "<title> <dots-or-spaces> <page number>" — a classic TOC line.
# Requires at least 2 separator characters (dots or spaces) so ordinary
# prose sentences ending in a number don't false-positive.
_TOC_LINE_RE = re.compile(r"^(?P<title>.{3,120}?)[.\s]{2,}(?P<page>\d{1,4})\s*$")


@dataclass(frozen=True)
class TocEntry:
    title: str
    printed_page_number: int
    source_page_index: int  # which page (0-based) the TOC entry itself was found on


def extract_page_texts_from_pdf_bytes(content: bytes) -> list[str]:
    """Return one text string per physical PDF page, in index order (index 0
    = first page). Uses pypdf directly rather than Kreuzberg's chunking,
    which does not guarantee a clean 1:1 page<->chunk mapping.
    """
    reader = PdfReader(io.BytesIO(content))
    return [page.extract_text() or "" for page in reader.pages]


def find_toc_candidates(pages: list[str], max_front_fraction: float = 0.15, max_back_fraction: float = 0.05) -> list[TocEntry]:
    """Scan the front ~max_front_fraction and back ~max_back_fraction of
    pages for TOC-style lines ("<title> ... <printed page number>").

    Returns entries in the order found; each entry's `printed_page_number`
    is a target to later locate by content search (see Task 6) — never an
    index to jump to directly.
    """
    total = len(pages)
    if total == 0:
        return []
    front_count = max(1, int(total * max_front_fraction))
    back_count = max(1, int(total * max_back_fraction))
    scan_indices = list(range(min(front_count, total))) + list(range(max(0, total - back_count), total))
    scan_indices = sorted(set(scan_indices))

    entries: list[TocEntry] = []
    for page_index in scan_indices:
        for line in pages[page_index].splitlines():
            m = _TOC_LINE_RE.match(line.strip())
            if not m:
                continue
            title = m.group("title").strip(" .")
            if len(title) < 3:
                continue
            entries.append(
                TocEntry(
                    title=title,
                    printed_page_number=int(m.group("page")),
                    source_page_index=page_index,
                )
            )
    return entries
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_chapter_segmentation -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "feat: add page-text loading and TOC candidate detection for chapter analysis"
```

---

### Task 6: Content-based chapter-start localization + printed-page-number extraction

**Files:**
- Modify: `backend/services/chapter_segmentation.py`
- Test: `backend/tests/test_chapter_segmentation.py`

- [ ] **Step 1: Add the failing tests**

```python
from backend.services.chapter_segmentation import (
    extract_printed_page_number,
    locate_chapter_start,
)


class TestLocateChapterStart(unittest.TestCase):
    def test_finds_matching_page_by_content(self):
        pages = [
            "CONTENTS\nComparing Citation Styles ..... 3\n",  # TOC page itself
            "Some unrelated front matter.",
            "Comparing Citation Styles\nBy Jane Author\n\nThis chapter examines...",
        ]
        index = locate_chapter_start(pages, "Comparing Citation Styles", exclude_indices={0})
        self.assertEqual(index, 2)

    def test_returns_none_when_no_good_match(self):
        pages = ["Nothing related to the query here at all, just filler prose."]
        index = locate_chapter_start(pages, "Comparing Citation Styles", exclude_indices=set())
        self.assertIsNone(index)


class TestExtractPrintedPageNumber(unittest.TestCase):
    def test_finds_arabic_footer_number(self):
        text = "Comparing Citation Styles\nBy Jane Author\n\nBody text here.\n\n45"
        self.assertEqual(extract_printed_page_number(text), "45")

    def test_finds_roman_numeral_header(self):
        text = "xii\nPreface\n\nBody text of the preface."
        self.assertEqual(extract_printed_page_number(text), "xii")

    def test_returns_none_when_no_number_present(self):
        text = "Just a page of prose with no isolated numeral line at all here."
        self.assertIsNone(extract_printed_page_number(text))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_chapter_segmentation -v`
Expected: FAIL with `ImportError: cannot import name 'locate_chapter_start'`

- [ ] **Step 3: Implement the localization and printed-number extraction functions**

Append to `backend/services/chapter_segmentation.py` (add `from rapidfuzz import fuzz` to the imports at the top):

```python
from rapidfuzz import fuzz

_PAGE_NUMBER_TOKEN_RE = re.compile(r"^[0-9]{1,4}$|^[ivxlcdm]{1,7}$", re.IGNORECASE)
_LOCATE_SCORE_THRESHOLD = 80.0  # rapidfuzz partial_ratio, 0-100


def locate_chapter_start(pages: list[str], title: str, exclude_indices: set[int]) -> int | None:
    """Find the PDF page index whose text most plausibly begins with `title`.

    This is a content lookup, not an index computation: it never assumes the
    TOC's printed page number corresponds to this page's index. Returns the
    best-scoring page index at/above the match threshold, or None if no page
    scores highly enough.
    """
    best_index: int | None = None
    best_score = 0.0
    for index, text in enumerate(pages):
        if index in exclude_indices:
            continue
        # Only the page's opening ~200 characters are compared — a chapter
        # title appears at the START of its page, not buried mid-page.
        head = text[:200]
        score = fuzz.partial_ratio(title.lower(), head.lower())
        if score > best_score:
            best_score = score
            best_index = index
    if best_score >= _LOCATE_SCORE_THRESHOLD:
        return best_index
    return None


def extract_printed_page_number(page_text: str) -> str | None:
    """Read the printed page number actually shown on a page, by looking for
    an isolated numeral/roman-numeral line near the top or bottom of the
    page's text (a running header/footer). Returns None if no such line is
    found — callers must treat this as "unknown", never guess.
    """
    lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
    if not lines:
        return None
    candidates = lines[:2] + lines[-2:]
    for line in candidates:
        if _PAGE_NUMBER_TOKEN_RE.match(line):
            return line
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_chapter_segmentation -v`
Expected: PASS (8 tests total)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "feat: add content-based chapter localization and printed-page-number extraction"
```

---

### Task 7: Author extraction (spaCy NER)

**Files:**
- Modify: `backend/services/chapter_segmentation.py`
- Test: `backend/tests/test_chapter_segmentation.py`

- [ ] **Step 1: Add the failing test**

```python
from backend.services.chapter_segmentation import extract_authors_near


class TestExtractAuthorsNear(unittest.TestCase):
    def test_finds_person_entities_at_chapter_start(self):
        text = "Comparing Citation Styles\nJane Author and John Smith\n\nThis chapter examines APA and MLA styles."
        authors = extract_authors_near(text)
        self.assertIn("Jane Author", authors)
        self.assertIn("John Smith", authors)

    def test_returns_empty_list_when_no_names_found(self):
        authors = extract_authors_near("This chapter examines APA and MLA citation styles in detail.")
        self.assertEqual(authors, [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest backend.tests.test_chapter_segmentation -v`
Expected: FAIL with `ImportError: cannot import name 'extract_authors_near'`

- [ ] **Step 3: Implement author extraction**

Append to `backend/services/chapter_segmentation.py` (add `import spacy` at the top, and a module-level lazy-loaded model matching how other services in this repo load `en_core_web_sm`):

```python
import spacy

_NLP = None


def _get_nlp():
    global _NLP
    if _NLP is None:
        _NLP = spacy.load("en_core_web_sm")
    return _NLP


def extract_authors_near(page_text: str, max_chars: int = 500) -> list[str]:
    """Run spaCy NER on the opening text of a chapter-start page to extract
    candidate author names. Best-effort: returns an empty list rather than
    raising when nothing plausible is found.
    """
    doc = _get_nlp()(page_text[:max_chars])
    seen: list[str] = []
    for ent in doc.ents:
        if ent.label_ == "PERSON" and ent.text not in seen:
            seen.append(ent.text)
    return seen
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest backend.tests.test_chapter_segmentation -v`
Expected: PASS (10 tests total)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "feat: add spaCy-based author extraction near chapter starts"
```

---

### Task 8: `analyze_attachment` orchestration (chapter clustering + confidence scoring)

**Files:**
- Modify: `backend/services/chapter_segmentation.py`
- Test: `backend/tests/test_chapter_segmentation.py`

- [ ] **Step 1: Add the failing tests**

```python
from backend.services.chapter_segmentation import analyze_attachment


class TestAnalyzeAttachment(unittest.TestCase):
    def _fake_book_pages(self) -> list[str]:
        return [
            # page 0: TOC
            "CONTENTS\n"
            "Introduction ..... 1\n"
            "Comparing Citation Styles ..... 3\n",
            # page 1: printed page "1" — Introduction body
            "Introduction\nJane Author\n\nThis book explores reference management.\n\n1",
            # page 2: continuation of Introduction, printed page "2"
            "...continued introduction text.\n\n2",
            # page 3: printed page "3" — chapter start
            "Comparing Citation Styles\nJohn Smith\n\nThis chapter examines APA and MLA.\n\n3",
            # page 4: continuation, printed page "4"
            "...continued chapter text.\n\n4",
        ]

    def test_detects_two_chapters_with_pdf_indices(self):
        result = analyze_attachment(self._fake_book_pages())
        self.assertEqual(len(result["chapters"]), 2)
        first, second = result["chapters"]
        self.assertEqual(first["pdf_start_index"], 1)
        self.assertEqual(first["pdf_end_index"], 2)
        self.assertEqual(second["pdf_start_index"], 3)
        self.assertEqual(second["pdf_end_index"], 4)

    def test_citation_pages_extracted_when_present(self):
        result = analyze_attachment(self._fake_book_pages())
        self.assertEqual(result["chapters"][0]["citation_pages"], "1-2")
        self.assertEqual(result["chapters"][1]["citation_pages"], "3-4")

    def test_low_confidence_when_no_toc_found(self):
        result = analyze_attachment(["Just some prose with no discernible TOC or chapter structure."])
        self.assertEqual(result["segmentation_confidence"], "low")
        self.assertEqual(result["chapters"], [])

    def test_authors_attached_to_chapters(self):
        result = analyze_attachment(self._fake_book_pages())
        self.assertIn("John Smith", result["chapters"][1]["authors"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_chapter_segmentation -v`
Expected: FAIL with `ImportError: cannot import name 'analyze_attachment'`

- [ ] **Step 3: Implement `analyze_attachment`**

Append to `backend/services/chapter_segmentation.py`:

```python
def analyze_attachment(pages: list[str]) -> dict:
    """Orchestrate TOC detection, content-based localization, printed-page
    extraction, and author NER into the per-attachment output described in
    design spec §5.

    Returns a dict matching the script 1 output schema (minus item_key/
    attachment_key/has_text_layer/needs_ocr, which the caller — run(), Task
    9 — fills in from Zotero/Kreuzberg context, not from page text alone).
    """
    total_pages = len(pages)
    toc_entries = find_toc_candidates(pages)

    chapters: list[dict] = []
    toc_page_indices = {e.source_page_index for e in toc_entries}
    located: list[tuple[TocEntry, int]] = []
    for entry in toc_entries:
        index = locate_chapter_start(pages, entry.title, exclude_indices=toc_page_indices)
        if index is not None:
            located.append((entry, index))

    # Cluster into contiguous ranges, ordered by PDF index (not TOC order,
    # which is printed-page order and could disagree if TOC entries were
    # matched to pages out of sequence).
    located.sort(key=lambda pair: pair[1])
    for i, (entry, start_index) in enumerate(located):
        end_index = (located[i + 1][1] - 1) if i + 1 < len(located) else (total_pages - 1)
        if end_index < start_index:
            continue  # degenerate/ambiguous overlap — skip rather than guess

        start_printed = extract_printed_page_number(pages[start_index])
        end_printed = extract_printed_page_number(pages[end_index])
        if start_printed is not None and end_printed is not None:
            citation_pages = f"{start_printed}-{end_printed}"
            page_mapping_confidence = "high"
        else:
            citation_pages = None
            page_mapping_confidence = "unmappable"

        chapters.append({
            "title": entry.title,
            "authors": extract_authors_near(pages[start_index]),
            "pdf_start_index": start_index,
            "pdf_end_index": end_index,
            "citation_pages": citation_pages,
            "confidence": 0.9,  # single confirmed TOC->content match; see Known limitations
            "page_mapping_confidence": page_mapping_confidence,
        })

    segmentation_confidence = "high" if chapters else "low"
    return {
        "total_pdf_pages": total_pages,
        "segmentation_confidence": segmentation_confidence,
        "chapters": chapters,
        "diagnostics": {
            "toc_pages_scanned": sorted(toc_page_indices),
            "toc_matches_found": len(toc_entries),
            "toc_matches_located": len(located),
        },
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_chapter_segmentation -v`
Expected: PASS (14 tests total)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "feat: add analyze_attachment orchestration for chapter boundary detection"
```

---

### Task 9: `run()` — scan library, call Kreuzberg, assemble output JSON

**Files:**
- Modify: `backend/services/chapter_segmentation.py`
- Test: `backend/tests/test_chapter_segmentation.py`

- [ ] **Step 1: Add the failing test**

```python
from unittest.mock import AsyncMock, MagicMock

from backend.services.chapter_segmentation import run as analyze_run


class TestRun(unittest.TestCase):
    def test_skips_already_linked_book(self):
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"data": {"key": "BOOK0001", "itemType": "book", "extra": "X-Contains: groups/1:CH01"}},
        ]
        progress_calls = []
        result = asyncio.run(analyze_run(
            zotero_client=zotero_client,
            library_id="1",
            library_type="group",
            slug="groups/1",
            item_keys=None,
            max_items=None,
            relink=False,
            progress_callback=lambda p, m: progress_calls.append((p, m)),
        ))
        self.assertEqual(result["attachments"], [])
        zotero_client.get_item_children.assert_not_called()

    def test_processes_unlinked_book(self):
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"data": {"key": "BOOK0002", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0001", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["Just filler prose, no TOC pattern here at all."],
        ):
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
            ))
        self.assertEqual(len(result["attachments"]), 1)
        self.assertEqual(result["attachments"][0]["item_key"], "BOOK0002")
        self.assertTrue(result["attachments"][0]["has_text_layer"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest backend.tests.test_chapter_segmentation -v`
Expected: FAIL with `ImportError: cannot import name 'run'`

- [ ] **Step 3: Implement `run()`**

Append to `backend/services/chapter_segmentation.py` (add `from typing import Callable, Optional` and `from backend.services.chapter_link_store import parse_links` to imports):

```python
from typing import Callable, Optional

from backend.services.chapter_link_store import parse_links


async def run(
    *,
    zotero_client,
    library_id: str,
    library_type: str,
    slug: str,
    item_keys: Optional[list[str]],
    max_items: Optional[int],
    relink: bool,
    progress_callback: Callable[[float, str], None],
) -> dict:
    """Core logic for script 1 (analyze_book_chapters). Scans `book`-type
    items in the library (or the explicit `item_keys` list), skips already-
    linked ones unless `relink`, downloads each PDF attachment, and runs
    analyze_attachment on its page text. See design spec §5.
    """
    items = await zotero_client.get_library_items_since(library_id, library_type=library_type)
    books = [i for i in items if i["data"].get("itemType") == "book"]
    if item_keys is not None:
        wanted = set(item_keys)
        books = [b for b in books if b["data"]["key"] in wanted]
    if not relink:
        books = [b for b in books if not parse_links(b["data"].get("extra", "")).contains]
    if max_items is not None:
        books = books[:max_items]

    attachments_out: list[dict] = []
    total = len(books) or 1
    for i, book in enumerate(books):
        item_key = book["data"]["key"]
        progress_callback(i / total, f"Analyzing {item_key} ({i + 1}/{total})")

        children = await zotero_client.get_item_children(library_id, item_key, library_type=library_type)
        pdf_attachments = [c for c in children if c["data"].get("contentType") == "application/pdf"]
        if not pdf_attachments:
            continue
        attachment_key = pdf_attachments[0]["data"]["key"]

        file_bytes = await zotero_client.get_attachment_file(library_id, item_key, library_type=library_type)
        if not file_bytes:
            continue

        pages = extract_page_texts_from_pdf_bytes(file_bytes)
        has_text_layer = sum(len(p.strip()) for p in pages) > 100

        if not has_text_layer:
            attachments_out.append({
                "item_key": item_key,
                "attachment_key": attachment_key,
                "has_text_layer": False,
                "needs_ocr": True,
            })
            continue

        analysis = analyze_attachment(pages)
        attachments_out.append({
            "item_key": item_key,
            "attachment_key": attachment_key,
            "has_text_layer": True,
            "needs_ocr": False,
            **analysis,
        })

    progress_callback(1.0, "Done")
    return {"slug": slug, "attachments": attachments_out}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest backend.tests.test_chapter_segmentation -v`
Expected: PASS (16 tests total)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "feat: add run() orchestration for script 1 (scan, download, analyze)"
```

---

### Task 10: CLI `scripts/analyze_book_chapters.py`

**Files:**
- Create: `scripts/analyze_book_chapters.py`

- [ ] **Step 1: Implement the CLI**

```python
#!/usr/bin/env python3
"""CLI for script 1: analyze book PDFs for chapter-segmentation candidates.

Usage:
    uv run python scripts/analyze_book_chapters.py --library-slug groups/6297749 \
        --api-key <read-only-zotero-key> --output .local/analysis.json
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services.chapter_link_store import parse_library_slug
from backend.services.chapter_segmentation import run as analyze_run
from backend.zotero.web_api import ZoteroWebAPI


async def _main(args: argparse.Namespace) -> int:
    library_type, _numeric_id, library_id = parse_library_slug(args.library_slug)
    client = ZoteroWebAPI(api_key=args.api_key)

    item_keys = args.item_keys.split(",") if args.item_keys else None

    bar = tqdm(total=100, unit="%", desc="Analyzing")

    def on_progress(progress: float, message: str) -> None:
        bar.n = int(progress * 100)
        bar.set_description(message)
        bar.refresh()

    result = await analyze_run(
        zotero_client=client,
        library_id=library_id,
        library_type=library_type,
        slug=args.library_slug,
        item_keys=item_keys,
        max_items=args.max_items,
        relink=args.relink,
        progress_callback=on_progress,
    )
    bar.close()

    output = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Wrote analysis to {args.output}")
    else:
        print(output)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze book PDFs for chapter-segmentation candidates.")
    parser.add_argument("--library-slug", required=True, help="e.g. groups/6297749 or users/12345")
    parser.add_argument("--api-key", required=True, help="Read-only Zotero API key")
    parser.add_argument("--item-keys", default=None, help="Comma-separated list to restrict to specific book items")
    parser.add_argument("--relink", action="store_true", help="Re-analyze books that already have X-Contains")
    parser.add_argument("--max-items", type=int, default=None, help="Cap the number of book items processed (testing/debugging)")
    parser.add_argument("--output", default=None, help="Write JSON output to this path instead of stdout")
    args = parser.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Smoke-test the CLI's argument parsing**

Run: `uv run python scripts/analyze_book_chapters.py --help`
Expected: prints usage text with all six flags listed, exit code 0.

- [ ] **Step 3: Commit**

```bash
git add scripts/analyze_book_chapters.py
git commit -m "feat: add CLI for script 1 (analyze_book_chapters)"
```

---

### Task 11: API endpoint `POST /api/chapter-linking/analyze`

**Files:**
- Modify: `backend/api/chapter_linking.py`
- Test: `backend/tests/test_chapter_linking_api.py`

- [ ] **Step 1: Add the failing test**

```python
from unittest.mock import AsyncMock, patch


class TestAnalyzeEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("backend.api.chapter_linking.ZoteroWebAPI")
    @patch("backend.api.chapter_linking.analyze_run", new_callable=AsyncMock)
    def test_returns_job_id_immediately(self, mock_run, mock_web_api):
        mock_run.return_value = {"slug": "groups/1", "attachments": []}
        response = self.client.post(
            "/api/chapter-linking/analyze",
            json={"library_slug": "groups/1", "api_key": "fake-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("job_id", response.json())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest backend.tests.test_chapter_linking_api -v`
Expected: FAIL with 404 (route doesn't exist yet)

- [ ] **Step 3: Implement the endpoint**

Add to `backend/api/chapter_linking.py` (extend the existing imports):

```python
import asyncio

from pydantic import BaseModel

from backend.services.chapter_link_store import parse_library_slug
from backend.services.chapter_segmentation import run as analyze_run
from backend.zotero.web_api import ZoteroWebAPI


class AnalyzeRequest(BaseModel):
    library_slug: str
    api_key: str
    item_keys: list[str] | None = None
    relink: bool = False
    max_items: int | None = None


@router.post("/chapter-linking/analyze", summary="Analyze book PDFs for chapter-segmentation candidates")
async def start_analyze(request: AnalyzeRequest) -> dict:
    library_type, _numeric_id, library_id = parse_library_slug(request.library_slug)
    client = ZoteroWebAPI(api_key=request.api_key)
    job_id = tracker.create()

    async def _task() -> None:
        try:
            result = await analyze_run(
                zotero_client=client,
                library_id=library_id,
                library_type=library_type,
                slug=request.library_slug,
                item_keys=request.item_keys,
                max_items=request.max_items,
                relink=request.relink,
                progress_callback=lambda p, m: tracker.update(job_id, progress=p, message=m),
            )
            tracker.update(job_id, result=result)
        except Exception as exc:  # noqa: BLE001 — surfaced via job status, not re-raised
            logger.exception("chapter-linking analyze job %s failed", job_id)
            tracker.update(job_id, error=str(exc))

    asyncio.create_task(_task())
    return {"job_id": job_id}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest backend.tests.test_chapter_linking_api -v`
Expected: PASS (3 tests total)

- [ ] **Step 5: Commit**

```bash
git add backend/api/chapter_linking.py backend/tests/test_chapter_linking_api.py
git commit -m "feat: add POST /api/chapter-linking/analyze endpoint"
```

---

## Phase 2: Script 3 — Retrofit link (§7)

### Task 12: Fuzzy title/year matching

**Files:**
- Create: `backend/services/chapter_retrofit.py`
- Test: `backend/tests/test_chapter_retrofit.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for backend.services.chapter_retrofit."""

import unittest

from backend.services.chapter_retrofit import find_best_book_match


class TestFindBestBookMatch(unittest.TestCase):
    def setUp(self):
        self.books = [
            {"key": "BOOK1", "title": "Handbook of Reference Management", "year": 2019},
            {"key": "BOOK2", "title": "Introduction to Bibliographic Software", "year": 2021},
        ]

    def test_matches_exact_title_and_year(self):
        result = find_best_book_match("Handbook of Reference Management", 2019, self.books)
        self.assertIsNotNone(result)
        self.assertEqual(result.book_key, "BOOK1")
        self.assertGreaterEqual(result.score, 0.9)

    def test_matches_within_year_tolerance(self):
        result = find_best_book_match("Handbook of Reference Management", 2020, self.books)
        self.assertIsNotNone(result)
        self.assertEqual(result.book_key, "BOOK1")

    def test_rejects_year_outside_tolerance(self):
        result = find_best_book_match("Handbook of Reference Management", 2023, self.books)
        self.assertIsNone(result)

    def test_no_candidates_returns_none(self):
        result = find_best_book_match("Something Entirely Different", 2019, self.books)
        self.assertIsNone(result)

    def test_ambiguous_close_scores_returns_none(self):
        books = [
            {"key": "A", "title": "Studies in Modern History", "year": 2020},
            {"key": "B", "title": "Studies in Modern History Vol 2", "year": 2020},
        ]
        result = find_best_book_match("Studies in Modern History", 2020, books)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_chapter_retrofit -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.services.chapter_retrofit'`

- [ ] **Step 3: Implement matching logic**

```python
"""Retrofit-link existing, separately-catalogued book/bookSection pairs.

See docs/superpowers/specs/2026-07-24-chapter-segmentation-linking-design.md
section 7. Matching is deliberately biased toward precision: an ambiguous
or below-threshold match is reported for manual review rather than linked.
"""

from dataclasses import dataclass

from rapidfuzz import fuzz

_SCORE_THRESHOLD = 90.0  # rapidfuzz token_sort_ratio, 0-100
_MARGIN_REQUIRED = 5.0   # top candidate must beat the runner-up by this much
_YEAR_TOLERANCE = 1


@dataclass(frozen=True)
class BookMatch:
    book_key: str
    score: float  # 0-1, normalized from rapidfuzz's 0-100 scale


def find_best_book_match(book_title: str, year: int | None, candidate_books: list[dict]) -> BookMatch | None:
    """Return the best-matching book (by title similarity + year tolerance)
    among `candidate_books` (each a dict with "key", "title", "year"), or
    None if no candidate clears both the score threshold and the margin
    requirement over the runner-up.
    """
    scored: list[tuple[str, float]] = []
    for book in candidate_books:
        if year is not None and book.get("year") is not None:
            if abs(book["year"] - year) > _YEAR_TOLERANCE:
                continue
        score = fuzz.token_sort_ratio(book_title.lower(), book["title"].lower())
        scored.append((book["key"], score))

    if not scored:
        return None

    scored.sort(key=lambda pair: pair[1], reverse=True)
    top_key, top_score = scored[0]
    if top_score < _SCORE_THRESHOLD:
        return None
    if len(scored) > 1:
        _, runner_up_score = scored[1]
        if top_score - runner_up_score < _MARGIN_REQUIRED:
            return None
    return BookMatch(book_key=top_key, score=top_score / 100.0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_chapter_retrofit -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_retrofit.py backend/tests/test_chapter_retrofit.py
git commit -m "feat: add fuzzy book/chapter title-year matching for retrofit-link"
```

---

### Task 13: PDF-range content-localization for retrofit pairs

**Files:**
- Modify: `backend/services/chapter_retrofit.py`
- Test: `backend/tests/test_chapter_retrofit.py`

- [ ] **Step 1: Add the failing tests**

```python
from backend.services.chapter_retrofit import locate_chapter_pdf_range


class TestLocateChapterPdfRange(unittest.TestCase):
    def test_finds_contiguous_span(self):
        book_pages = [
            "Front matter, nothing relevant here.",
            "Comparing Citation Styles\nThis chapter examines APA and MLA styles in depth.",
            "...continued examination of citation styles and their history.",
            "Unrelated next chapter begins here with different content entirely.",
        ]
        chapter_text = "Comparing Citation Styles\nThis chapter examines APA and MLA styles in depth. ...continued examination of citation styles and their history."
        result = locate_chapter_pdf_range(chapter_text, book_pages)
        self.assertEqual(result, (1, 2))

    def test_returns_none_when_no_confident_span(self):
        book_pages = ["Completely unrelated content about gardening techniques."]
        chapter_text = "This is about astrophysics and black holes entirely."
        result = locate_chapter_pdf_range(chapter_text, book_pages)
        self.assertIsNone(result)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_chapter_retrofit -v`
Expected: FAIL with `ImportError: cannot import name 'locate_chapter_pdf_range'`

- [ ] **Step 3: Implement sliding-window content localization**

Append to `backend/services/chapter_retrofit.py`:

```python
_RANGE_SCORE_THRESHOLD = 70.0


def locate_chapter_pdf_range(chapter_text: str, book_pages: list[str]) -> tuple[int, int] | None:
    """Find the contiguous PDF page-index span within `book_pages` whose
    concatenated text best matches `chapter_text` (the chapter item's own
    extracted attachment text).

    Unlike script 4 (which knows the range by construction, having just
    sliced it), this links two pre-existing items with no given page
    relationship — the chapter's own attachment may even be a different
    scan of the same content. Returns None (never a guessed range) when no
    contiguous span scores confidently — design spec §7's safe-degradation
    rule: the identity link is still written by the caller, suppression
    just doesn't apply to that pair.
    """
    chapter_head = chapter_text[:300].lower()
    best_start: int | None = None
    best_score = 0.0
    for start in range(len(book_pages)):
        window_text = book_pages[start][:300].lower()
        score = fuzz.partial_ratio(chapter_head, window_text)
        if score > best_score:
            best_score = score
            best_start = start

    if best_start is None or best_score < _RANGE_SCORE_THRESHOLD:
        return None

    # Extend forward while subsequent pages still plausibly belong to this
    # chapter's content (their text appears within the chapter's own text).
    end = best_start
    chapter_lower = chapter_text.lower()
    for index in range(best_start + 1, len(book_pages)):
        page_head = book_pages[index][:100].lower().strip()
        if page_head and fuzz.partial_ratio(page_head, chapter_lower) >= _RANGE_SCORE_THRESHOLD:
            end = index
        else:
            break
    return (best_start, end)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_chapter_retrofit -v`
Expected: PASS (7 tests total)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_retrofit.py backend/tests/test_chapter_retrofit.py
git commit -m "feat: add content-based PDF-range localization for retrofit-linked chapters"
```

---

### Task 14: `run()` — scan, match, write links via pyzotero

**Files:**
- Modify: `backend/services/chapter_retrofit.py`
- Test: `backend/tests/test_chapter_retrofit.py`

- [ ] **Step 1: Add the failing test**

```python
from unittest.mock import MagicMock

from backend.services.chapter_retrofit import run as retrofit_run


class TestRetrofitRun(unittest.TestCase):
    def test_links_confident_match_and_skips_ambiguous(self):
        zot = MagicMock()
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
        ]
        zot.everything.return_value = all_items
        zot.item.side_effect = lambda key: next(i for i in all_items if i["key"] == key)

        result = retrofit_run(
            zotero_write_client=zot,
            slug="groups/1",
            item_keys=None,
            max_items=None,
        )
        self.assertEqual(len(result["linked"]), 1)
        self.assertEqual(result["linked"][0]["chapter_key"], "CHAP1")
        self.assertEqual(result["linked"][0]["book_key"], "BOOK1")
        zot.update_item.assert_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest backend.tests.test_chapter_retrofit -v`
Expected: FAIL with `ImportError: cannot import name 'run'`

- [ ] **Step 3: Implement `run()`**

Append to `backend/services/chapter_retrofit.py` (add imports `from backend.services.chapter_link_store import format_chapter_id, parse_links, write_links`):

```python
from backend.services.chapter_link_store import format_chapter_id, parse_links, write_links


def _year_from_date(date_str: str | None) -> int | None:
    if not date_str:
        return None
    for token in date_str.replace("-", " ").split():
        if token.isdigit() and len(token) == 4:
            return int(token)
    return None


def run(
    *,
    zotero_write_client,
    slug: str,
    item_keys: list[str] | None,
    max_items: int | None,
) -> dict:
    """Core logic for script 3 (retrofit_chapter_links). Synchronous —
    pyzotero's client is itself synchronous. See design spec §7.
    """
    # zot.items() alone returns only the first page — everything() is
    # required to auto-paginate through the full library.
    all_items = zotero_write_client.everything(zotero_write_client.items())
    books = [i for i in all_items if i["data"].get("itemType") == "book"]
    chapters = [i for i in all_items if i["data"].get("itemType") == "bookSection"]

    unlinked_chapters = [c for c in chapters if not parse_links(c["data"].get("extra", "")).contained_by]
    if item_keys is not None:
        wanted = set(item_keys)
        unlinked_chapters = [c for c in unlinked_chapters if c["data"]["key"] in wanted]
    if max_items is not None:
        unlinked_chapters = unlinked_chapters[:max_items]

    book_candidates = [
        {"key": b["data"]["key"], "title": b["data"].get("title", ""), "year": _year_from_date(b["data"].get("date"))}
        for b in books
    ]

    linked: list[dict] = []
    ambiguous: list[dict] = []
    no_match: list[str] = []

    for chapter in unlinked_chapters:
        chapter_key = chapter["data"]["key"]
        book_title = chapter["data"].get("bookTitle", "")
        year = _year_from_date(chapter["data"].get("date"))

        if not book_title or not book_candidates:
            no_match.append(chapter_key)
            continue

        match = find_best_book_match(book_title, year, book_candidates)
        if match is None:
            ambiguous.append({"chapter_key": chapter_key, "candidates": book_candidates})
            continue

        book_item = zotero_write_client.item(match.book_key)
        chapter_id = format_chapter_id(slug, chapter_key)
        book_id = format_chapter_id(slug, match.book_key)

        chapter["data"]["extra"] = write_links(chapter["data"].get("extra", ""), contained_by=book_id)
        zotero_write_client.update_item(chapter)

        existing_links = parse_links(book_item["data"].get("extra", ""))
        new_contains = list({*existing_links.contains, chapter_id})
        book_item["data"]["extra"] = write_links(book_item["data"].get("extra", ""), contains=new_contains)
        zotero_write_client.update_item(book_item)

        linked.append({"chapter_key": chapter_key, "book_key": match.book_key, "score": match.score})

    return {"linked": linked, "ambiguous": ambiguous, "no_match": no_match}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest backend.tests.test_chapter_retrofit -v`
Expected: PASS (8 tests total)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_retrofit.py backend/tests/test_chapter_retrofit.py
git commit -m "feat: add run() orchestration for script 3 (retrofit link writing)"
```

---

### Task 15: CLI `scripts/retrofit_chapter_links.py`

**Files:**
- Create: `scripts/retrofit_chapter_links.py`

- [ ] **Step 1: Implement the CLI**

```python
#!/usr/bin/env python3
"""CLI for script 3: retrofit-link existing, separately-catalogued book and
bookSection items.

Usage:
    uv run python scripts/retrofit_chapter_links.py --library-slug groups/6297749 \
        --api-key <write-scoped-zotero-key> --output .local/retrofit.json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyzotero import zotero

from backend.services.chapter_link_store import parse_library_slug
from backend.services.chapter_retrofit import run as retrofit_run


def main() -> int:
    parser = argparse.ArgumentParser(description="Retrofit-link existing book/bookSection pairs.")
    parser.add_argument("--library-slug", required=True)
    parser.add_argument("--api-key", required=True, help="Write-scoped Zotero API key")
    parser.add_argument("--item-keys", default=None, help="Comma-separated bookSection item keys to restrict to")
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    library_type, numeric_id, _library_id = parse_library_slug(args.library_slug)
    zot = zotero.Zotero(library_id=numeric_id, library_type=library_type, api_key=args.api_key)

    item_keys = args.item_keys.split(",") if args.item_keys else None
    result = retrofit_run(
        zotero_write_client=zot,
        slug=args.library_slug,
        item_keys=item_keys,
        max_items=args.max_items,
    )

    output = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Wrote {len(result['linked'])} link(s), {len(result['ambiguous'])} ambiguous, {len(result['no_match'])} unmatched to {args.output}")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Smoke-test the CLI's argument parsing**

Run: `uv run python scripts/retrofit_chapter_links.py --help`
Expected: prints usage text with all five flags listed, exit code 0.

- [ ] **Step 3: Commit**

```bash
git add scripts/retrofit_chapter_links.py
git commit -m "feat: add CLI for script 3 (retrofit_chapter_links)"
```

---

### Task 16: API endpoint `POST /api/chapter-linking/retrofit-link`

**Files:**
- Modify: `backend/api/chapter_linking.py`
- Test: `backend/tests/test_chapter_linking_api.py`

- [ ] **Step 1: Add the failing test**

```python
class TestRetrofitEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("backend.api.chapter_linking.zotero")
    @patch("backend.api.chapter_linking.retrofit_run")
    def test_returns_job_id_immediately(self, mock_run, mock_zotero_module):
        mock_run.return_value = {"linked": [], "ambiguous": [], "no_match": []}
        response = self.client.post(
            "/api/chapter-linking/retrofit-link",
            json={"library_slug": "groups/1", "api_key": "fake-write-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("job_id", response.json())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest backend.tests.test_chapter_linking_api -v`
Expected: FAIL with 404 (route doesn't exist yet)

- [ ] **Step 3: Implement the endpoint**

Add to `backend/api/chapter_linking.py`:

```python
from pyzotero import zotero

from backend.services.chapter_retrofit import run as retrofit_run


class RetrofitLinkRequest(BaseModel):
    library_slug: str
    api_key: str
    item_keys: list[str] | None = None
    max_items: int | None = None


@router.post("/chapter-linking/retrofit-link", summary="Retrofit-link existing book/bookSection item pairs")
async def start_retrofit_link(request: RetrofitLinkRequest) -> dict:
    library_type, numeric_id, _library_id = parse_library_slug(request.library_slug)
    zot = zotero.Zotero(library_id=numeric_id, library_type=library_type, api_key=request.api_key)
    job_id = tracker.create()

    async def _task() -> None:
        try:
            result = await asyncio.to_thread(
                retrofit_run,
                zotero_write_client=zot,
                slug=request.library_slug,
                item_keys=request.item_keys,
                max_items=request.max_items,
            )
            tracker.update(job_id, result=result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("chapter-linking retrofit-link job %s failed", job_id)
            tracker.update(job_id, error=str(exc))

    asyncio.create_task(_task())
    return {"job_id": job_id}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest backend.tests.test_chapter_linking_api -v`
Expected: PASS (4 tests total)

- [ ] **Step 5: Commit**

```bash
git add backend/api/chapter_linking.py backend/tests/test_chapter_linking_api.py
git commit -m "feat: add POST /api/chapter-linking/retrofit-link endpoint"
```

---

## Phase 3: Script 2 — OCR (§6)

### Task 17: Wire per-request OCR language into Kreuzberg

**Files:**
- Modify: `backend/services/extraction/kreuzberg.py:59-107`
- Test: `backend/tests/test_kreuzberg_extractor.py`

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for backend.services.extraction.kreuzberg's per-request language config."""

import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.services.extraction.kreuzberg import KreuzbergExtractor


class TestPerRequestLanguage(unittest.IsolatedAsyncioTestCase):
    async def test_default_has_no_language_key(self):
        extractor = KreuzbergExtractor()
        mock_response = MagicMock()
        mock_response.json.return_value = [{"chunks": []}]
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            await extractor.extract_and_chunk(b"fake pdf bytes", "application/pdf")
            sent_config = json.loads(mock_client.post.call_args.kwargs["data"]["config"])
        self.assertNotIn("ocr", sent_config)

    async def test_language_override_sets_ocr_language(self):
        extractor = KreuzbergExtractor()
        mock_response = MagicMock()
        mock_response.json.return_value = [{"chunks": []}]
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value.__aenter__.return_value = mock_client
            await extractor.extract_and_chunk(b"fake pdf bytes", "application/pdf", ocr_language="deu")
            sent_config = json.loads(mock_client.post.call_args.kwargs["data"]["config"])
        self.assertEqual(sent_config["ocr"]["language"], "deu")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest backend.tests.test_kreuzberg_extractor -v`
Expected: FAIL with `TypeError: extract_and_chunk() got an unexpected keyword argument 'ocr_language'`

- [ ] **Step 3: Move `_config` assembly into `extract_and_chunk` and add `ocr_language`**

In `backend/services/extraction/kreuzberg.py`, replace the `__init__` method:

```python
    def __init__(
        self,
        kreuzberg_url: str = "http://localhost:8100",
        max_chunk_size: int = 512,
        chunk_overlap: int = 50,
        ocr_enabled: bool = True,
    ):
        """
        Args:
            kreuzberg_url: Base URL of the kreuzberg sidecar (e.g. "http://localhost:8100").
            max_chunk_size: Maximum characters per chunk.
            chunk_overlap: Overlap characters between consecutive chunks.
            ocr_enabled: Whether to attempt OCR on image-only pages.
        """
        self._kreuzberg_url = kreuzberg_url.rstrip("/")
        self._ocr_enabled = ocr_enabled
        self._max_chunk_size = max_chunk_size
        self._chunk_overlap = chunk_overlap
        logger.debug(
            f"Initialized KreuzbergExtractor (url={kreuzberg_url}, "
            f"max_chars={max_chunk_size}, overlap={chunk_overlap}, ocr={ocr_enabled})"
        )

    def _build_config(self, ocr_language: str | None) -> dict[str, Any]:
        config: dict[str, Any] = {
            "chunking": {
                "max_characters": self._max_chunk_size,
                "overlap": self._chunk_overlap,
            },
            "force_ocr": self._ocr_enabled,
        }
        if ocr_language:
            config["ocr"] = {"language": ocr_language}
        return config
```

Then update `extract_and_chunk`'s signature and its use of `self._config`:

```python
    async def extract_and_chunk(
        self,
        content: bytes,
        mime_type: str,
        ocr_language: str | None = None,
    ) -> list[ExtractionChunk]:
        """
        Send document bytes to the kreuzberg sidecar and return extraction chunks.

        Args:
            content: Raw document bytes.
            mime_type: MIME type of the document (e.g. "application/pdf").
            ocr_language: Optional Tesseract language code (e.g. "deu", "eng+fra")
                to force for this request only. Omitted -> the sidecar's static
                config/kreuzberg.toml default applies.

        Returns:
            List of ExtractionChunk objects, empty if extraction fails.
        """
        url = f"{self._kreuzberg_url}/extract"
        timeout = _compute_timeout(len(content), mime_type)
        config = self._build_config(ocr_language)
        logger.debug(
            f"kreuzberg request: mime={mime_type} size={len(content)} timeout={timeout}s "
            f"ocr_language={ocr_language or '(sidecar default)'}"
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    url,
                    files={"files": ("document", content, mime_type)},
                    data={"config": json.dumps(config)},
                )
                response.raise_for_status()
```

(The rest of `extract_and_chunk`'s body — error handling and response parsing — is unchanged; only the `self._config` reference in the `data={"config": ...}` line becomes `config`, as shown above.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest backend.tests.test_kreuzberg_extractor -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full existing extraction test suite to confirm no regression**

Run: `uv run python -m unittest discover backend/tests -p "test_*extraction*" -v` (or `uv run pytest backend/tests -k kreuzberg -v` if a matching pytest-style suite already exists — check `backend/tests/` for the current Kreuzberg test filename first)
Expected: PASS, no regressions in existing Kreuzberg extraction tests.

- [ ] **Step 6: Commit**

```bash
git add backend/services/extraction/kreuzberg.py backend/tests/test_kreuzberg_extractor.py
git commit -m "feat: support per-request OCR language override in KreuzbergExtractor"
```

---

### Task 18: Thread `ocr_language` through the extractor factory

**Files:**
- Modify: `backend/services/extraction/__init__.py:26-66`
- Test: existing extractor-factory tests (add one case)

- [ ] **Step 1: Confirm current factory tests location**

Run: `grep -rl "create_document_extractor" backend/tests/`
Expected: identifies the existing test file covering the factory (if none exists, create `backend/tests/test_extraction_factory.py` following the `unittest.TestCase` convention).

- [ ] **Step 2: Add a test asserting `ocr_language` passes through** (append to whichever file Step 1 found, or the new file)

```python
def test_kreuzberg_extractor_accepts_ocr_language_kwarg(self):
    from backend.services.extraction import create_document_extractor
    extractor = create_document_extractor(backend="kreuzberg", ocr_enabled=True)
    # Doesn't raise — extract_and_chunk's ocr_language kwarg exists on this instance.
    import inspect
    sig = inspect.signature(extractor.extract_and_chunk)
    self.assertIn("ocr_language", sig.parameters)
```

- [ ] **Step 3: Run test to verify it passes already (no factory change needed if signature is inherited)**

Run: `uv run python -m unittest <the test file from Step 1> -v`
Expected: PASS — `create_document_extractor` doesn't need any change itself, since `ocr_language` is a per-call argument to `extract_and_chunk`, not a constructor argument. This step exists to make that explicit and regression-proof: a future refactor that moved `ocr_language` back into `__init__` would break this test as a signal.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/
git commit -m "test: confirm ocr_language is a per-call extract_and_chunk argument, not constructor-level"
```

---

### Task 19: Language detection + OCR cache

**Files:**
- Create: `backend/services/chapter_ocr.py`
- Test: `backend/tests/test_chapter_ocr.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for backend.services.chapter_ocr."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.services.chapter_ocr import (
    detect_language,
    load_cached_ocr,
    save_ocr_cache,
)


class TestDetectLanguage(unittest.TestCase):
    def test_uses_item_language_field_if_set(self):
        self.assertEqual(detect_language(item_language="de", title="Some Title"), "deu")

    def test_detects_from_title_when_no_item_language(self):
        result = detect_language(item_language=None, title="Einführung in die Zitierweise")
        self.assertEqual(result, "deu")

    def test_falls_back_to_combined_default_when_undetectable(self):
        result = detect_language(item_language=None, title="")
        self.assertEqual(result, "eng+deu+fra+spa")


class TestOcrCache(unittest.TestCase):
    def test_round_trip(self):
        with TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            save_ocr_cache(cache_dir, "abc123", detected_language="deu", pages=["page one", "page two"])
            result = load_cached_ocr(cache_dir, "abc123")
            self.assertEqual(result["detected_language"], "deu")
            self.assertEqual(result["pages"], ["page one", "page two"])

    def test_returns_none_when_not_cached(self):
        with TemporaryDirectory() as tmp:
            self.assertIsNone(load_cached_ocr(Path(tmp), "does-not-exist"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_chapter_ocr -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.services.chapter_ocr'`

- [ ] **Step 3: Implement language detection and caching**

```python
"""Language-aware OCR for attachments lacking a text layer (script 2:
ocr_attachments). See design spec §6.

Slices the book PDF page-by-page (pypdf) and OCRs each page individually
via the Kreuzberg sidecar, rather than sending the whole PDF as one
request — this guarantees a clean 1:1 page-index<->text mapping (matching
pypdf's own indexing used elsewhere in chapter_segmentation.py), avoiding
any ambiguity in how Kreuzberg's own chunking groups multi-page text.
"""

import io
import json
from pathlib import Path
from typing import Optional

from langdetect import DetectorFactory, LangDetectException, detect
from pypdf import PdfReader, PdfWriter

# Deterministic detection (langdetect is otherwise seeded from wall-clock time).
DetectorFactory.seed = 0

_INSTALLED_PACKS = {"de": "deu", "fr": "fra", "es": "spa", "en": "eng"}
_COMBINED_DEFAULT = "eng+deu+fra+spa"


def detect_language(item_language: Optional[str], title: str) -> str:
    """Prefer the Zotero item's own `language` field; otherwise detect from
    the title; otherwise fall back to the sidecar's combined default.
    """
    if item_language:
        code = item_language.split("-")[0].lower()
        if code in _INSTALLED_PACKS:
            return _INSTALLED_PACKS[code]

    if title.strip():
        try:
            code = detect(title)
        except LangDetectException:
            code = None
        if code in _INSTALLED_PACKS:
            return _INSTALLED_PACKS[code]

    return _COMBINED_DEFAULT


def _cache_path(cache_dir: Path, content_hash: str) -> Path:
    return cache_dir / f"{content_hash}.json"


def load_cached_ocr(cache_dir: Path, content_hash: str) -> Optional[dict]:
    path = _cache_path(cache_dir, content_hash)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_ocr_cache(cache_dir: Path, content_hash: str, *, detected_language: str, pages: list[str]) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, content_hash)
    path.write_text(
        json.dumps({"detected_language": detected_language, "pages": pages}, indent=2),
        encoding="utf-8",
    )
    return path


def slice_single_page_pdf(content: bytes, page_index: int) -> bytes:
    """Return a standalone one-page PDF (bytes) for the given 0-based page index."""
    reader = PdfReader(io.BytesIO(content))
    writer = PdfWriter()
    writer.add_page(reader.pages[page_index])
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_chapter_ocr -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_ocr.py backend/tests/test_chapter_ocr.py
git commit -m "feat: add language detection and per-page OCR cache for script 2"
```

---

### Task 20: `run()` — OCR each attachment page-by-page

**Files:**
- Modify: `backend/services/chapter_ocr.py`
- Test: `backend/tests/test_chapter_ocr.py`

- [ ] **Step 1: Add the failing test**

```python
import hashlib
from unittest.mock import AsyncMock, MagicMock

from backend.services.chapter_ocr import run as ocr_run


class TestOcrRun(unittest.TestCase):
    def test_ocrs_each_attachment_and_caches_result(self):
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 one page fake"
        zotero_client.get_item.return_value = {"data": {"title": "Einführung in die Zitierweise", "language": ""}}

        extractor = AsyncMock()
        extractor.extract_and_chunk.return_value = [MagicMock(text="OCR'd page text")]

        with TemporaryDirectory() as tmp, unittest.mock.patch(
            "backend.services.chapter_ocr.PdfReader"
        ) as mock_reader_cls, unittest.mock.patch(
            "backend.services.chapter_ocr.slice_single_page_pdf", return_value=b"single page bytes"
        ):
            mock_reader_cls.return_value.pages = [MagicMock()]  # one page
            result = asyncio.run(ocr_run(
                zotero_client=zotero_client,
                extractor=extractor,
                library_id="1",
                library_type="group",
                attachment_specs=[{"item_key": "BOOK1", "attachment_key": "ATT1"}],
                max_items=None,
                cache_dir=Path(tmp),
                progress_callback=lambda p, m: None,
            ))
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(result["results"][0]["ocr_succeeded"])
        self.assertEqual(result["results"][0]["detected_language"], "deu")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest backend.tests.test_chapter_ocr -v`
Expected: FAIL with `ImportError: cannot import name 'run'`

- [ ] **Step 3: Implement `run()`**

Append to `backend/services/chapter_ocr.py` (add imports `hashlib`, `Callable`):

```python
import hashlib
from typing import Callable


async def run(
    *,
    zotero_client,
    extractor,
    library_id: str,
    library_type: str,
    attachment_specs: list[dict],
    max_items: Optional[int],
    cache_dir: Path,
    progress_callback: Callable[[float, str], None],
) -> dict:
    """Core logic for script 2 (ocr_attachments). `attachment_specs` is
    typically script 1's `needs_ocr: true` output list. See design spec §6.
    """
    specs = attachment_specs[:max_items] if max_items is not None else attachment_specs
    results: list[dict] = []
    total = len(specs) or 1

    for i, spec in enumerate(specs):
        item_key = spec["item_key"]
        attachment_key = spec["attachment_key"]
        progress_callback(i / total, f"OCR-ing {item_key} ({i + 1}/{total})")

        file_bytes = await zotero_client.get_attachment_file(library_id, item_key, library_type=library_type)
        if not file_bytes:
            results.append({"item_key": item_key, "attachment_key": attachment_key, "ocr_succeeded": False})
            continue

        content_hash = hashlib.sha256(file_bytes).hexdigest()
        cached = load_cached_ocr(cache_dir, content_hash)
        if cached is not None:
            results.append({
                "item_key": item_key,
                "attachment_key": attachment_key,
                "detected_language": cached["detected_language"],
                "ocr_succeeded": True,
                "char_count": sum(len(p) for p in cached["pages"]),
                "cache_path": str(cache_dir / f"{content_hash}.json"),
            })
            continue

        item = await zotero_client.get_item(library_id, item_key, library_type=library_type)
        language = detect_language(item["data"].get("language"), item["data"].get("title", ""))

        reader = PdfReader(io.BytesIO(file_bytes))
        page_texts: list[str] = []
        for page_index in range(len(reader.pages)):
            single_page_bytes = slice_single_page_pdf(file_bytes, page_index)
            chunks = await extractor.extract_and_chunk(single_page_bytes, "application/pdf", ocr_language=language)
            page_texts.append(" ".join(c.text for c in chunks))

        cache_path = save_ocr_cache(cache_dir, content_hash, detected_language=language, pages=page_texts)
        results.append({
            "item_key": item_key,
            "attachment_key": attachment_key,
            "detected_language": language,
            "ocr_succeeded": True,
            "char_count": sum(len(p) for p in page_texts),
            "cache_path": str(cache_path),
        })

    progress_callback(1.0, "Done")
    return {"results": results}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest backend.tests.test_chapter_ocr -v`
Expected: PASS (6 tests total)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_ocr.py backend/tests/test_chapter_ocr.py
git commit -m "feat: add run() orchestration for script 2 (page-by-page OCR with caching)"
```

---

### Task 21: CLI `scripts/ocr_attachments.py`

**Files:**
- Create: `scripts/ocr_attachments.py`

- [ ] **Step 1: Implement the CLI**

```python
#!/usr/bin/env python3
"""CLI for script 2: OCR attachments lacking a text layer.

Usage:
    uv run python scripts/ocr_attachments.py --library-slug groups/6297749 \
        --api-key <read-only-zotero-key> --input .local/analysis.json \
        --output .local/ocr_results.json
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services.chapter_link_store import parse_library_slug
from backend.services.chapter_ocr import run as ocr_run
from backend.services.extraction import create_document_extractor
from backend.zotero.web_api import ZoteroWebAPI


def _load_attachment_specs(input_path: str) -> list[dict]:
    data = json.loads(Path(input_path).read_text(encoding="utf-8"))
    return [
        {"item_key": a["item_key"], "attachment_key": a["attachment_key"]}
        for a in data.get("attachments", [])
        if a.get("needs_ocr")
    ]


async def _main(args: argparse.Namespace) -> int:
    library_type, _numeric_id, library_id = parse_library_slug(args.library_slug)
    client = ZoteroWebAPI(api_key=args.api_key)
    extractor = create_document_extractor(backend="kreuzberg", ocr_enabled=True)
    specs = _load_attachment_specs(args.input)

    bar = tqdm(total=100, unit="%", desc="OCR-ing")

    def on_progress(progress: float, message: str) -> None:
        bar.n = int(progress * 100)
        bar.set_description(message)
        bar.refresh()

    result = await ocr_run(
        zotero_client=client,
        extractor=extractor,
        library_id=library_id,
        library_type=library_type,
        attachment_specs=specs,
        max_items=args.max_items,
        cache_dir=Path(args.cache_dir),
        progress_callback=on_progress,
    )
    bar.close()

    output = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Wrote OCR results to {args.output}")
    else:
        print(output)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OCR attachments lacking a text layer.")
    parser.add_argument("--library-slug", required=True)
    parser.add_argument("--api-key", required=True, help="Read-only Zotero API key")
    parser.add_argument("--input", required=True, help="Script 1's output JSON (needs_ocr items are used)")
    parser.add_argument("--cache-dir", default="data/ocr_cache")
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Smoke-test the CLI's argument parsing**

Run: `uv run python scripts/ocr_attachments.py --help`
Expected: prints usage text with all six flags listed, exit code 0.

- [ ] **Step 3: Commit**

```bash
git add scripts/ocr_attachments.py
git commit -m "feat: add CLI for script 2 (ocr_attachments)"
```

---

### Task 22: API endpoint `POST /api/chapter-linking/ocr`

**Files:**
- Modify: `backend/api/chapter_linking.py`
- Test: `backend/tests/test_chapter_linking_api.py`

- [ ] **Step 1: Add the failing test**

```python
class TestOcrEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("backend.api.chapter_linking.create_document_extractor")
    @patch("backend.api.chapter_linking.ZoteroWebAPI")
    @patch("backend.api.chapter_linking.ocr_run", new_callable=AsyncMock)
    def test_returns_job_id_immediately(self, mock_run, mock_web_api, mock_create_extractor):
        mock_run.return_value = {"results": []}
        response = self.client.post(
            "/api/chapter-linking/ocr",
            json={
                "library_slug": "groups/1",
                "api_key": "fake-key",
                "attachment_specs": [{"item_key": "B1", "attachment_key": "A1"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("job_id", response.json())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest backend.tests.test_chapter_linking_api -v`
Expected: FAIL with 404 (route doesn't exist yet)

- [ ] **Step 3: Implement the endpoint**

Add to `backend/api/chapter_linking.py`:

```python
from pathlib import Path as _Path

from backend.services.chapter_ocr import run as ocr_run
from backend.services.extraction import create_document_extractor


class AttachmentSpec(BaseModel):
    item_key: str
    attachment_key: str


class OcrRequest(BaseModel):
    library_slug: str
    api_key: str
    attachment_specs: list[AttachmentSpec]
    max_items: int | None = None
    cache_dir: str = "data/ocr_cache"


@router.post("/chapter-linking/ocr", summary="OCR attachments lacking a text layer")
async def start_ocr(request: OcrRequest) -> dict:
    library_type, _numeric_id, library_id = parse_library_slug(request.library_slug)
    client = ZoteroWebAPI(api_key=request.api_key)
    extractor = create_document_extractor(backend="kreuzberg", ocr_enabled=True)
    job_id = tracker.create()

    async def _task() -> None:
        try:
            result = await ocr_run(
                zotero_client=client,
                extractor=extractor,
                library_id=library_id,
                library_type=library_type,
                attachment_specs=[s.model_dump() for s in request.attachment_specs],
                max_items=request.max_items,
                cache_dir=_Path(request.cache_dir),
                progress_callback=lambda p, m: tracker.update(job_id, progress=p, message=m),
            )
            tracker.update(job_id, result=result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("chapter-linking ocr job %s failed", job_id)
            tracker.update(job_id, error=str(exc))

    asyncio.create_task(_task())
    return {"job_id": job_id}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest backend.tests.test_chapter_linking_api -v`
Expected: PASS (5 tests total)

- [ ] **Step 5: Commit**

```bash
git add backend/api/chapter_linking.py backend/tests/test_chapter_linking_api.py
git commit -m "feat: add POST /api/chapter-linking/ocr endpoint"
```

---

## Phase 4: Script 4 — Segment & upload (§8)

### Task 23: PDF slicing

**Files:**
- Create: `backend/services/chapter_upload.py`
- Test: `backend/tests/test_chapter_upload.py`

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for backend.services.chapter_upload."""

import io
import unittest

from pypdf import PdfReader, PdfWriter

from backend.services.chapter_upload import slice_pdf_range


def _make_test_pdf(num_pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class TestSlicePdfRange(unittest.TestCase):
    def test_slices_correct_page_range(self):
        content = _make_test_pdf(10)
        sliced = slice_pdf_range(content, pdf_start_index=2, pdf_end_index=4)
        reader = PdfReader(io.BytesIO(sliced))
        self.assertEqual(len(reader.pages), 3)  # indices 2, 3, 4 inclusive


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest backend.tests.test_chapter_upload -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.services.chapter_upload'`

- [ ] **Step 3: Implement PDF slicing**

```python
"""Segment a book PDF into per-chapter files and upload them as new
bookSection items. See design spec §8.
"""

import io

from pypdf import PdfReader, PdfWriter


def slice_pdf_range(content: bytes, pdf_start_index: int, pdf_end_index: int) -> bytes:
    """Return a standalone PDF (bytes) containing pages
    [pdf_start_index, pdf_end_index] (both inclusive, 0-based) of `content`.
    Always operates on the PDF-index pair — never on citation_pages, which
    is a different (printed-number) space (design spec §2/§8).
    """
    reader = PdfReader(io.BytesIO(content))
    writer = PdfWriter()
    for index in range(pdf_start_index, pdf_end_index + 1):
        writer.add_page(reader.pages[index])
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest backend.tests.test_chapter_upload -v`
Expected: PASS (1 test)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_upload.py backend/tests/test_chapter_upload.py
git commit -m "feat: add PDF page-range slicing for chapter upload"
```

---

### Task 24: Metadata inheritance from book to chapter

**Files:**
- Modify: `backend/services/chapter_upload.py`
- Test: `backend/tests/test_chapter_upload.py`

- [ ] **Step 1: Add the failing test**

```python
from backend.services.chapter_upload import build_book_section_item_data


class TestBuildBookSectionItemData(unittest.TestCase):
    def test_inherits_book_metadata(self):
        template = {"itemType": "bookSection", "title": "", "bookTitle": "", "editor": [], "publisher": "",
                    "place": "", "date": "", "ISBN": "", "language": "", "pages": "", "creators": []}
        book_data = {
            "title": "Handbook of Reference Management",
            "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Editor"}],
            "publisher": "Big Press", "place": "Berlin", "date": "2019", "ISBN": "978-0-000000-00-0",
            "language": "en",
        }
        chapter = {"title": "Comparing Citation Styles", "authors": ["John Smith"], "citation_pages": "45-67"}

        result = build_book_section_item_data(template, book_data, chapter)
        self.assertEqual(result["title"], "Comparing Citation Styles")
        self.assertEqual(result["bookTitle"], "Handbook of Reference Management")
        self.assertEqual(result["editor"], book_data["creators"])
        self.assertEqual(result["publisher"], "Big Press")
        self.assertEqual(result["pages"], "45-67")
        self.assertEqual(result["creators"][0]["lastName"], "Smith")

    def test_leaves_pages_blank_when_citation_pages_missing(self):
        template = {"itemType": "bookSection", "title": "", "bookTitle": "", "editor": [], "publisher": "",
                    "place": "", "date": "", "ISBN": "", "language": "", "pages": "", "creators": []}
        book_data = {"title": "T", "creators": [], "publisher": "", "place": "", "date": "", "ISBN": "", "language": ""}
        chapter = {"title": "C", "authors": [], "citation_pages": None}

        result = build_book_section_item_data(template, book_data, chapter)
        self.assertEqual(result["pages"], "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest backend.tests.test_chapter_upload -v`
Expected: FAIL with `ImportError: cannot import name 'build_book_section_item_data'`

- [ ] **Step 3: Implement metadata inheritance**

Append to `backend/services/chapter_upload.py`:

```python
def _split_name(full_name: str) -> tuple[str, str]:
    """Split "First Last" into (first, last); single-token names become
    (last-only)."""
    parts = full_name.strip().split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def build_book_section_item_data(template: dict, book_data: dict, chapter: dict) -> dict:
    """Build a bookSection item's field values from a pyzotero item_template,
    inheriting bibliographic metadata from the book and using the chapter's
    own detected title/authors/citation_pages (design spec §8, step 2).

    Never derives `pages` from pdf_start_index/pdf_end_index — only from
    `citation_pages`, left blank when that's None (unmappable printed
    numbers) rather than guessed from a different number space.
    """
    item = dict(template)
    item["title"] = chapter["title"]
    item["bookTitle"] = book_data.get("title", "")
    item["editor"] = book_data.get("creators", [])
    item["publisher"] = book_data.get("publisher", "")
    item["place"] = book_data.get("place", "")
    item["date"] = book_data.get("date", "")
    item["ISBN"] = book_data.get("ISBN", "")
    item["language"] = book_data.get("language", "")
    item["pages"] = chapter.get("citation_pages") or ""
    item["creators"] = [
        {"creatorType": "author", "firstName": first, "lastName": last}
        for first, last in (_split_name(name) for name in chapter.get("authors", []))
    ]
    return item
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest backend.tests.test_chapter_upload -v`
Expected: PASS (3 tests total)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_upload.py backend/tests/test_chapter_upload.py
git commit -m "feat: add book-to-chapter metadata inheritance for segment-upload"
```

---

### Task 25: Collection organization (author-year subcollection)

**Files:**
- Modify: `backend/services/chapter_upload.py`
- Test: `backend/tests/test_chapter_upload.py`

- [ ] **Step 1: Add the failing tests**

```python
from unittest.mock import MagicMock

from backend.services.chapter_upload import author_year_label, ensure_target_collection


class TestAuthorYearLabel(unittest.TestCase):
    def test_single_author(self):
        self.assertEqual(author_year_label(["Jane Miller"], "2023"), "Miller (2023)")

    def test_two_or_more_authors_uses_et_al(self):
        self.assertEqual(author_year_label(["Jane Smith", "John Doe", "Amy Lee"], "1999"), "Smith et al. (1999)")

    def test_no_authors_falls_back_to_untitled(self):
        self.assertEqual(author_year_label([], "2020"), "Unknown (2020)")


class TestEnsureTargetCollection(unittest.TestCase):
    def test_creates_top_level_and_subcollection_when_absent(self):
        zot = MagicMock()
        zot.collections.return_value = []
        zot.create_collection.return_value = {"successful": {"0": {"key": "TOPKEY01"}}}
        zot.collections_sub.return_value = []

        top_key, sub_key = ensure_target_collection(zot, "Book Chapters", "Miller (2023)")
        zot.create_collection.assert_called()
        self.assertEqual(top_key, "TOPKEY01")

    def test_reuses_existing_collections(self):
        zot = MagicMock()
        zot.collections.return_value = [{"key": "TOPKEY01", "data": {"name": "Book Chapters"}}]
        zot.collections_sub.return_value = [{"key": "SUBKEY01", "data": {"name": "Miller (2023)"}}]

        top_key, sub_key = ensure_target_collection(zot, "Book Chapters", "Miller (2023)")
        self.assertEqual(top_key, "TOPKEY01")
        self.assertEqual(sub_key, "SUBKEY01")
        zot.create_collection.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_chapter_upload -v`
Expected: FAIL with `ImportError: cannot import name 'author_year_label'`

- [ ] **Step 3: Implement collection organization**

Append to `backend/services/chapter_upload.py` (add `from backend.db.vector_store import _extract_lastnames` to imports):

```python
from backend.db.vector_store import _extract_lastnames


def author_year_label(authors: list[str], date: str) -> str:
    """Build a short author-year label for a per-book subcollection name,
    e.g. "Miller (2023)" or "Smith et al. (1999)" (3+ authors). Reuses the
    existing lastname-extraction helper already used for Qdrant author
    filtering, for consistency with how author names are normalized
    elsewhere in this codebase.
    """
    lastnames = _extract_lastnames(authors)
    year = date.strip().split("-")[0] if date else "n.d."
    if not lastnames:
        return f"Unknown ({year})"
    first = lastnames[0].capitalize()
    label = first if len(lastnames) == 1 else f"{first} et al."
    return f"{label} ({year})"


def ensure_target_collection(zotero_write_client, top_level_name: str, subcollection_name: str) -> tuple[str, str]:
    """Find-or-create the top-level collection and its per-book
    subcollection, returning (top_level_key, subcollection_key). Idempotent:
    re-running against an already-processed book reuses both collections.
    """
    top_matches = [c for c in zotero_write_client.collections() if c["data"]["name"] == top_level_name]
    if top_matches:
        top_key = top_matches[0]["key"]
    else:
        resp = zotero_write_client.create_collection([{"name": top_level_name}])
        top_key = list(resp["successful"].values())[0]["key"]

    sub_matches = [c for c in zotero_write_client.collections_sub(top_key) if c["data"]["name"] == subcollection_name]
    if sub_matches:
        sub_key = sub_matches[0]["key"]
    else:
        resp = zotero_write_client.create_collection([{"name": subcollection_name, "parentCollection": top_key}])
        sub_key = list(resp["successful"].values())[0]["key"]

    return top_key, sub_key
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_chapter_upload -v`
Expected: PASS (6 tests total)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_upload.py backend/tests/test_chapter_upload.py
git commit -m "feat: add author-year collection organization for segment-upload"
```

---

### Task 26: `run()` — dry-run/commit orchestration

**Files:**
- Modify: `backend/services/chapter_upload.py`
- Test: `backend/tests/test_chapter_upload.py`

- [ ] **Step 1: Add the failing tests**

```python
from unittest.mock import MagicMock, patch

from backend.services.chapter_upload import run as upload_run


class TestUploadRun(unittest.TestCase):
    def _book_and_analysis(self):
        book_item = {
            "key": "BOOK1",
            "data": {
                "key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management",
                "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Editor"}],
                "publisher": "Big Press", "place": "Berlin", "date": "2019", "ISBN": "978-0", "language": "en",
                "extra": "",
            },
        }
        analysis = {
            "item_key": "BOOK1", "attachment_key": "ATT1",
            "chapters": [
                {"title": "Comparing Citation Styles", "authors": ["John Smith"], "pdf_start_index": 2,
                 "pdf_end_index": 4, "citation_pages": "45-67", "confidence": 0.93, "page_mapping_confidence": "high"},
            ],
        }
        return book_item, analysis

    def test_dry_run_creates_nothing(self):
        book_item, analysis = self._book_and_analysis()
        zot = MagicMock()
        zot.item.return_value = book_item
        result = upload_run(
            zotero_write_client=zot, zotero_read_client=MagicMock(get_attachment_file=MagicMock()),
            slug="groups/1", analyses=[analysis], commit=False, confidence_threshold=0.8,
            target_collection="Book Chapters", max_items=None,
        )
        zot.create_items.assert_not_called()
        self.assertEqual(len(result["would_create"]), 1)

    def test_commit_creates_and_links(self):
        book_item, analysis = self._book_and_analysis()
        zot = MagicMock()
        zot.item.return_value = book_item
        zot.item_template.return_value = {"itemType": "bookSection", "title": "", "bookTitle": "", "editor": [],
                                           "publisher": "", "place": "", "date": "", "ISBN": "", "language": "",
                                           "pages": "", "creators": []}
        zot.create_items.return_value = {"successful": {"0": {"key": "CHAP1"}}}
        zot.collections.return_value = []
        zot.create_collection.return_value = {"successful": {"0": {"key": "TOPKEY01"}}}
        zot.collections_sub.return_value = []
        read_client = MagicMock()
        read_client.get_attachment_file = unittest.mock.AsyncMock(return_value=b"%PDF-1.4 fake")

        import asyncio

        with patch("backend.services.chapter_upload.slice_pdf_range", return_value=b"sliced bytes"):
            result = asyncio.run(upload_run(
                zotero_write_client=zot, zotero_read_client=read_client,
                slug="groups/1", analyses=[analysis], commit=True, confidence_threshold=0.8,
                target_collection="Book Chapters", max_items=None,
            ))
        zot.create_items.assert_called()
        zot.attachment_simple.assert_called()
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(result["created"][0]["chapter_key"], "CHAP1")

    def test_below_threshold_is_skipped(self):
        book_item, analysis = self._book_and_analysis()
        analysis["chapters"][0]["confidence"] = 0.5
        zot = MagicMock()
        zot.item.return_value = book_item
        import asyncio

        result = asyncio.run(upload_run(
            zotero_write_client=zot, zotero_read_client=MagicMock(),
            slug="groups/1", analyses=[analysis], commit=True, confidence_threshold=0.8,
            target_collection="Book Chapters", max_items=None,
        ))
        zot.create_items.assert_not_called()
        self.assertEqual(len(result["skipped_low_confidence"]), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_chapter_upload -v`
Expected: FAIL with `ImportError: cannot import name 'run'`

- [ ] **Step 3: Implement `run()`**

Append to `backend/services/chapter_upload.py` (add imports `from backend.services.chapter_link_store import format_chapter_id, parse_links, write_links`):

```python
from backend.services.chapter_link_store import format_chapter_id, parse_links, write_links


async def run(
    *,
    zotero_write_client,
    zotero_read_client,
    slug: str,
    analyses: list[dict],
    commit: bool,
    confidence_threshold: float,
    target_collection: str,
    max_items: int | None,
) -> dict:
    """Core logic for script 4 (upload_chapters). `analyses` is script 1's
    output list (one entry per book attachment). Defaults to dry-run —
    `commit` must be explicitly True to write to Zotero. See design spec §8.
    """
    analyses = analyses[:max_items] if max_items is not None else analyses

    would_create: list[dict] = []
    created: list[dict] = []
    skipped_low_confidence: list[dict] = []

    for analysis in analyses:
        book_key = analysis["item_key"]
        book_item = zotero_write_client.item(book_key)
        book_data = book_item["data"]

        confident_chapters = [c for c in analysis.get("chapters", []) if c["confidence"] >= confidence_threshold]
        low_confidence = [c for c in analysis.get("chapters", []) if c["confidence"] < confidence_threshold]
        skipped_low_confidence.extend({"book_key": book_key, "title": c["title"]} for c in low_confidence)

        if not commit:
            would_create.extend({"book_key": book_key, "title": c["title"], "pdf_start_index": c["pdf_start_index"],
                                  "pdf_end_index": c["pdf_end_index"]} for c in confident_chapters)
            continue

        new_chapter_ids: list[str] = []
        pdf_ranges: dict[str, tuple[int, int]] = {}
        for chapter in confident_chapters:
            file_bytes = await zotero_read_client.get_attachment_file(
                book_data.get("library_id", ""), book_key, library_type=""
            ) if hasattr(zotero_read_client, "get_attachment_file") else None
            sliced = slice_pdf_range(file_bytes or b"", chapter["pdf_start_index"], chapter["pdf_end_index"])

            template = zotero_write_client.item_template("bookSection")
            item_data = build_book_section_item_data(template, book_data, chapter)
            resp = zotero_write_client.create_items([item_data])
            chapter_key = list(resp["successful"].values())[0]["key"]

            import tempfile
            from pathlib import Path as _Path
            tmp_path = _Path(tempfile.gettempdir()) / f"{chapter_key}.pdf"
            tmp_path.write_bytes(sliced)
            zotero_write_client.attachment_simple([str(tmp_path)], parentid=chapter_key)
            tmp_path.unlink(missing_ok=True)

            chapter_id = format_chapter_id(slug, chapter_key)
            book_id = format_chapter_id(slug, book_key)
            chapter_extra = write_links(book_item["data"].get("extra", ""), contained_by=book_id)
            chapter_item = zotero_write_client.item(chapter_key)
            chapter_item["data"]["extra"] = chapter_extra
            zotero_write_client.update_item(chapter_item)

            new_chapter_ids.append(chapter_id)
            pdf_ranges[chapter_id] = (chapter["pdf_start_index"], chapter["pdf_end_index"])

            label = author_year_label(book_data.get("creators_names", []) or [c.get("lastName", "") for c in book_data.get("creators", [])], book_data.get("date", ""))
            _, sub_key = ensure_target_collection(zotero_write_client, target_collection, label)
            zotero_write_client.addto_collection(sub_key, chapter_item)

            created.append({"book_key": book_key, "chapter_key": chapter_key})

        if new_chapter_ids:
            existing = parse_links(book_item["data"].get("extra", ""))
            merged_contains = list({*existing.contains, *new_chapter_ids})
            merged_ranges = {**existing.pdf_ranges, **pdf_ranges}
            book_item["data"]["extra"] = write_links(
                book_item["data"].get("extra", ""), contains=merged_contains, pdf_ranges=merged_ranges
            )
            zotero_write_client.update_item(book_item)

    return {"would_create": would_create, "created": created, "skipped_low_confidence": skipped_low_confidence}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_chapter_upload -v`
Expected: PASS (9 tests total)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_upload.py backend/tests/test_chapter_upload.py
git commit -m "feat: add run() orchestration for script 4 (dry-run/commit segment-upload)"
```

---

### Task 27: CLI `scripts/upload_chapters.py`

**Files:**
- Create: `scripts/upload_chapters.py`

- [ ] **Step 1: Implement the CLI**

```python
#!/usr/bin/env python3
"""CLI for script 4: segment book PDFs into chapters and upload as new
bookSection items. Defaults to dry-run — pass --commit to actually write.

Usage:
    uv run python scripts/upload_chapters.py --library-slug groups/6297749 \
        --api-key <write-scoped-zotero-key> --input .local/analysis.json --commit
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pyzotero import zotero

from backend.services.chapter_link_store import parse_library_slug
from backend.services.chapter_upload import run as upload_run
from backend.zotero.web_api import ZoteroWebAPI


async def _main(args: argparse.Namespace) -> int:
    library_type, numeric_id, _library_id = parse_library_slug(args.library_slug)
    write_client = zotero.Zotero(library_id=numeric_id, library_type=library_type, api_key=args.api_key)
    read_client = ZoteroWebAPI(api_key=args.api_key)

    analyses = json.loads(Path(args.input).read_text(encoding="utf-8")).get("attachments", [])

    bar = tqdm(total=len(analyses) or 1, unit="book", desc="Uploading" if args.commit else "Previewing")

    result = await upload_run(
        zotero_write_client=write_client,
        zotero_read_client=read_client,
        slug=args.library_slug,
        analyses=analyses,
        commit=args.commit,
        confidence_threshold=args.confidence_threshold,
        target_collection=args.target_collection,
        max_items=args.max_items,
    )
    bar.update(len(analyses))
    bar.close()

    if not args.commit:
        print(f"DRY RUN: would create {len(result['would_create'])} chapter(s). Pass --commit to apply.")
    else:
        print(f"Created {len(result['created'])} chapter(s).")
    if result["skipped_low_confidence"]:
        print(f"Skipped {len(result['skipped_low_confidence'])} low-confidence chapter(s) — see output for detail.")

    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Segment book PDFs into chapters and upload to Zotero.")
    parser.add_argument("--library-slug", required=True)
    parser.add_argument("--api-key", required=True, help="Write-scoped Zotero API key")
    parser.add_argument("--input", required=True, help="Script 1's output JSON")
    parser.add_argument("--commit", action="store_true", help="Actually write to Zotero (default: dry-run preview)")
    parser.add_argument("--confidence-threshold", type=float, default=0.8)
    parser.add_argument("--target-collection", default="Book Chapters")
    parser.add_argument("--max-items", type=int, default=None)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Smoke-test the CLI's argument parsing**

Run: `uv run python scripts/upload_chapters.py --help`
Expected: prints usage text with all seven flags listed, exit code 0.

- [ ] **Step 3: Commit**

```bash
git add scripts/upload_chapters.py
git commit -m "feat: add CLI for script 4 (upload_chapters, dry-run by default)"
```

---

### Task 28: API endpoint `POST /api/chapter-linking/segment-upload`

**Files:**
- Modify: `backend/api/chapter_linking.py`
- Test: `backend/tests/test_chapter_linking_api.py`

- [ ] **Step 1: Add the failing test**

```python
class TestSegmentUploadEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("backend.api.chapter_linking.ZoteroWebAPI")
    @patch("backend.api.chapter_linking.zotero")
    @patch("backend.api.chapter_linking.upload_run", new_callable=AsyncMock)
    def test_defaults_to_dry_run(self, mock_run, mock_zotero_module, mock_web_api):
        mock_run.return_value = {"would_create": [], "created": [], "skipped_low_confidence": []}
        response = self.client.post(
            "/api/chapter-linking/segment-upload",
            json={"library_slug": "groups/1", "api_key": "fake-key", "analyses": []},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("job_id", response.json())
        _, kwargs = mock_run.call_args
        self.assertFalse(kwargs["commit"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest backend.tests.test_chapter_linking_api -v`
Expected: FAIL with 404 (route doesn't exist yet)

- [ ] **Step 3: Implement the endpoint**

Add to `backend/api/chapter_linking.py`:

```python
from backend.services.chapter_upload import run as upload_run


class SegmentUploadRequest(BaseModel):
    library_slug: str
    api_key: str
    analyses: list[dict]
    committed: bool = False
    confidence_threshold: float = 0.8
    target_collection: str = "Book Chapters"
    max_items: int | None = None


@router.post("/chapter-linking/segment-upload", summary="Segment book PDFs into chapters and upload (dry-run by default)")
async def start_segment_upload(request: SegmentUploadRequest) -> dict:
    library_type, numeric_id, _library_id = parse_library_slug(request.library_slug)
    write_client = zotero.Zotero(library_id=numeric_id, library_type=library_type, api_key=request.api_key)
    read_client = ZoteroWebAPI(api_key=request.api_key)
    job_id = tracker.create()

    async def _task() -> None:
        try:
            result = await upload_run(
                zotero_write_client=write_client,
                zotero_read_client=read_client,
                slug=request.library_slug,
                analyses=request.analyses,
                commit=request.committed,
                confidence_threshold=request.confidence_threshold,
                target_collection=request.target_collection,
                max_items=request.max_items,
            )
            tracker.update(job_id, result=result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("chapter-linking segment-upload job %s failed", job_id)
            tracker.update(job_id, error=str(exc))

    asyncio.create_task(_task())
    return {"job_id": job_id}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest backend.tests.test_chapter_linking_api -v`
Expected: PASS (6 tests total)

- [ ] **Step 5: Commit**

```bash
git add backend/api/chapter_linking.py backend/tests/test_chapter_linking_api.py
git commit -m "feat: add POST /api/chapter-linking/segment-upload endpoint (dry-run default)"
```

---

## Phase 5: Indexing suppression (§9)

### Task 29: Suppress a book's pages covered by a linked chapter

**Files:**
- Modify: `backend/services/document_processor.py:1198-1227` (chunk-building loop in `_process_attachment_bytes`)
- Test: `backend/tests/test_document_processor.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_document_processor.py` (following the file's existing `_make_extraction_chunks`/`_attachment` helper conventions already shown at the top of that file):

```python
class TestChapterSuppression(unittest.TestCase):
    """A book item linked to a chapter covering pages 2-4 (0-based PDF index)
    should not index chunks whose page_number falls in that range."""

    def setUp(self):
        self.zotero_client = MagicMock()
        self.embedding_service = MagicMock()
        self.embedding_service.embed_batch = AsyncMock(return_value=[[0.1] * 8] * 5)
        self.vector_store = MagicMock()
        self.extractor = AsyncMock()
        self.processor = DocumentProcessor(
            zotero_client=self.zotero_client,
            embedding_service=self.embedding_service,
            vector_store=self.vector_store,
            document_extractor=self.extractor,
        )

    def test_suppresses_pages_within_linked_chapter_range(self):
        import asyncio

        from backend.models.document import DocumentMetadata

        # Pages (1-based, matching ExtractionChunk.page_number convention)
        # 1 and 5 are book residual content; 2-4 fall inside a linked chapter's
        # 0-based PDF range [1, 3] -> 1-based page_number 2, 3, 4.
        self.extractor.extract_and_chunk.return_value = _make_extraction_chunks(
            ("front matter", 1), ("chapter page a", 2), ("chapter page b", 3),
            ("chapter page c", 4), ("back matter", 5),
        )
        doc_metadata = DocumentMetadata(
            library_id="1", item_key="BOOK1", title="Handbook", authors=[], year=2019,
            item_type="book", attachment_key="ATT1",
        )
        # Extra field: chapter groups/1:CHAP1 covers PDF pages 1-3 (0-based).
        book_extra = "X-Contains: groups/1:CHAP1\nX-Chapter-Pdf-Range: groups/1:CHAP1:1-3"

        result = asyncio.run(self.processor._process_attachment_bytes(
            file_bytes=b"fake pdf bytes", mime_type="application/pdf", doc_metadata=doc_metadata,
            item_version=1, attachment_version=1, item_modified="2026-01-01T00:00:00Z",
            item_extra=book_extra, total_pdf_pages=5,
        ))

        stored_chunks = self.vector_store.add_chunks_batch.call_args[0][0]
        stored_pages = sorted(c.metadata.page_number for c in stored_chunks)
        self.assertEqual(stored_pages, [1, 5])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest backend.tests.test_document_processor.TestChapterSuppression -v`
Expected: FAIL with `TypeError: _process_attachment_bytes() got an unexpected keyword argument 'item_extra'`

- [ ] **Step 3: Add suppression logic**

In `backend/services/document_processor.py`, extend `_process_attachment_bytes`'s signature (`:1049-1058`) with two new optional parameters:

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
        item_extra: str = "",
        total_pdf_pages: Optional[int] = None,
    ) -> AttachmentProcessingResult:
```

Add the import at the top of the file: `from backend.services.chapter_link_store import parse_links`.

Immediately before the chunk-building loop (`:1198`, `# Build DocumentChunk objects with full metadata`), insert the suppressed-page-index computation:

```python
        suppressed_pages: set[int] = set()
        if doc_metadata.item_type == "book" and item_extra:
            links = parse_links(item_extra)
            valid_ranges: list[tuple[int, int]] = []
            for chapter_id, (start, end) in links.pdf_ranges.items():
                if total_pdf_pages is not None and end >= total_pdf_pages:
                    logger.warning(
                        f"Skipping suppression for {doc_metadata.item_key}: "
                        f"chapter {chapter_id} range end {end} exceeds page count {total_pdf_pages}"
                    )
                    valid_ranges = []
                    break
                valid_ranges.append((start, end))
            else:
                # No `break` triggered — check for overlaps before trusting any range.
                sorted_ranges = sorted(valid_ranges)
                for (s1, e1), (s2, e2) in zip(sorted_ranges, sorted_ranges[1:]):
                    if s2 <= e1:
                        logger.warning(
                            f"Skipping suppression for {doc_metadata.item_key}: "
                            f"overlapping chapter ranges {(s1, e1)} and {(s2, e2)}"
                        )
                        valid_ranges = []
                        break
            for start, end in valid_ranges:
                # pdf_start_index/pdf_end_index are 0-based PDF indices;
                # ExtractionChunk.page_number is 1-based (kreuzberg's "first_page").
                suppressed_pages.update(range(start + 1, end + 2))
```

- [ ] **Step 4: Filter suppressed pages in the chunk-building loop**

Modify the chunk-building loop (`:1199-1201` in the original) to skip suppressed pages:

```python
        # Build DocumentChunk objects with full metadata
        doc_chunks = []
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            if chunk.page_number is not None and chunk.page_number in suppressed_pages:
                continue
            chunk_id = f"{library_id}:{item_key}:{attachment_key}:{i}"
```

(The remainder of the loop body is unchanged.)

- [ ] **Step 5: Wire `item_extra`/`total_pdf_pages` through from the caller**

In `_index_item` (`:891-944`), where `_process_attachment_bytes` is called for each attachment, pass the parent item's `extra` field and the PDF's page count:

```python
        item_extra = item["data"].get("extra", "")
```

(placed alongside the existing `doc_metadata = DocumentMetadata(...)` construction) and thread `item_extra=item_extra` into the `_process_attachment_bytes(...)` call site. `total_pdf_pages` can be omitted (`None`) at this call site for now — the bounds check in Step 3 only activates when a caller supplies it; a `None` value means "skip the bounds check, still apply the overlap check," which is the safest available default without adding a new PDF-page-count lookup to `_index_item` in this task. (A future task can wire in an actual page count via `pypdf` if the bounds check proves necessary in practice — noted as an accepted gap, not a blocker for this feature.)

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run python -m unittest backend.tests.test_document_processor.TestChapterSuppression -v`
Expected: PASS (1 test)

- [ ] **Step 7: Run the full document_processor test suite to confirm no regression**

Run: `uv run python -m unittest backend.tests.test_document_processor -v`
Expected: PASS, no regressions (suppression only activates when `item_type == "book"` and `item_extra` contains `X-Chapter-Pdf-Range` data — every existing test's items lack this, so `suppressed_pages` stays empty and behavior is unchanged).

- [ ] **Step 8: Commit**

```bash
git add backend/services/document_processor.py backend/tests/test_document_processor.py
git commit -m "feat: suppress a book's pages covered by a linked chapter during indexing"
```

---

### Task 30: Ground-truth accuracy scoring harness against real evaluation data (§5, §12)

Unlike the original draft of this task, the ground-truth data already exists — seven real, hand-verified books (English/French/German, various publishers) live in `backend/evaluation/book-segmentation/`, each with a `<name>.expected.json` built by directly inspecting the real PDF (TOC page cross-referenced against actual chapter-start pages, page-numbering offset verified per book — not guessed). `manifest.json` is the single source of truth for the set (no README table); six entries are `oa: true` and auto-fetchable via `scripts/fetch_evaluation_pdfs.py`, the seventh (`9783322969828.pdf`, a 1976 scanned/OCR'd yearbook) is `oa: false` and must be acquired manually via its DOI (that script prints the DOI and save path when the file is missing). This task writes the scoring harness that runs against that real data.

**Files:**
- Test: `tests/test_chapter_segmentation_accuracy.py`
- Reference (already present, not created by this task): `backend/evaluation/book-segmentation/manifest.json`, `backend/evaluation/book-segmentation/README.md`, `backend/evaluation/book-segmentation/*.expected.json`, `scripts/fetch_evaluation_pdfs.py`

- [ ] **Step 1: Write the scoring harness**

```python
"""Precision/recall scoring for chapter_segmentation.analyze_attachment
against the real, hand-verified ground-truth books in
backend/evaluation/book-segmentation/ (design spec §5, §12).

The PDFs themselves are gitignored — run
`uv run python scripts/fetch_evaluation_pdfs.py` first to download the
open-access ones. A book is skipped (not failed) if its PDF isn't present
locally yet (covers both "not fetched yet" and the one non-OA scan that
can never be auto-fetched) — this is real, checkable state, not a
placeholder standing in for unwritten logic.
"""

import json
import unittest
from pathlib import Path

from backend.services.chapter_segmentation import (
    analyze_attachment,
    extract_page_texts_from_pdf_bytes,
)

_EVAL_DIR = Path(__file__).parent.parent / "backend" / "evaluation" / "book-segmentation"


def _available_books() -> list[tuple[Path, Path]]:
    """Return (pdf_path, expected_json_path) pairs for every manifest entry
    whose PDF is actually present locally right now."""
    manifest = json.loads((_EVAL_DIR / "manifest.json").read_text(encoding="utf-8"))
    pairs = []
    for book in manifest["books"]:
        pdf_path = _EVAL_DIR / book["filename"]
        expected_path = _EVAL_DIR / (Path(book["filename"]).stem + ".expected.json")
        if pdf_path.exists() and expected_path.exists():
            pairs.append((pdf_path, expected_path))
    return pairs


@unittest.skipUnless(
    _available_books(),
    "No evaluation PDFs present — run: uv run python scripts/fetch_evaluation_pdfs.py",
)
class TestChapterSegmentationAccuracy(unittest.TestCase):
    def test_boundary_precision_recall_per_book(self):
        for pdf_path, expected_path in _available_books():
            with self.subTest(book=pdf_path.name):
                expected = json.loads(expected_path.read_text(encoding="utf-8"))["chapters"]
                pages = extract_page_texts_from_pdf_bytes(pdf_path.read_bytes())
                result = analyze_attachment(pages)

                expected_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in expected}
                found_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in result["chapters"]}
                true_positives = expected_ranges & found_ranges

                precision = len(true_positives) / len(found_ranges) if found_ranges else 0.0
                recall = len(true_positives) / len(expected_ranges) if expected_ranges else 0.0
                print(f"{pdf_path.name}: precision={precision:.2f} recall={recall:.2f} "
                      f"({len(true_positives)}/{len(found_ranges)} found, {len(true_positives)}/{len(expected_ranges)} expected)")
                # Reported, not gated (design spec §12: probabilistic, not pass/fail) —
                # this assertion only catches a total regression to zero detection.
                self.assertGreater(recall, 0.0, f"{pdf_path.name}: detected zero of {len(expected_ranges)} known chapters")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Fetch the real evaluation PDFs**

Run: `uv run python scripts/fetch_evaluation_pdfs.py`
Expected: downloads the six OA books into `backend/evaluation/book-segmentation/` (or reports `[skip] ... already present` if you already have them from building the ground truth).

- [ ] **Step 3: Run the harness against the real books**

Run: `uv run python -m unittest tests.test_chapter_segmentation_accuracy -v`
Expected: PASS, with six `precision=... recall=...` lines printed (one per book) — this is the first real signal on how well Tasks 5-8's heuristics actually perform, not a synthetic fixture. A recall well below 1.0 on any book is expected and informative (see design spec §13's precision-over-recall bias) — investigate via the `diagnostics` field in that book's `analyze_attachment` output, not by relaxing this test's assertion.

- [ ] **Step 4: Commit**

```bash
git add tests/test_chapter_segmentation_accuracy.py
git commit -m "test: add accuracy scoring harness against real evaluation books"
```

**Known limitation:** the seventh book, `9783322969828.pdf` (a 1976 scanned/OCR'd Springer yearbook, `oa: false`), *does* have a real `.expected.json` — its ground truth was built the same way as the other six (direct TOC cross-reference, offset verification), and its existing embedded text layer turned out to be good enough for `pypdf` extraction directly, no OCR needed. But it can never be auto-fetched (see `scripts/fetch_evaluation_pdfs.py`'s DOI-printing fallback), so this harness will only actually exercise that book on a machine where someone has manually placed the file after acquiring it via institutional access — CI and fresh clones will see six-book results, not seven, until that happens. Nothing to fix here; just don't be surprised if the seventh book's `precision=.../recall=...` line is silently absent from the test output on a machine that never fetched it.

---

## Final verification

- [ ] **Run the entire backend test suite**

Run: `uv run pytest backend/tests -v` (or `uv run python -m unittest discover backend/tests -v` if the project's default runner is `unittest discover` — check `CLAUDE.md`'s Testing section for the current preferred invocation)
Expected: all tests pass, including every new test file added in this plan.

- [ ] **Manual smoke test against the test-rag-plugin library**

Per `CLAUDE.md`'s "Live Query Debugging" section, use the `test-rag-plugin` group library (`groups/6297749`) — never a real personal library — for a first live run:

```bash
uv run python scripts/analyze_book_chapters.py --library-slug groups/6297749 --api-key "$ZOTERO_KEY" --max-items 3 --output .local/analysis.json
cat .local/analysis.json
```

Confirm the output JSON is well-formed and, if the test library happens to contain a book with a real TOC, that `pdf_start_index`/`pdf_end_index`/`citation_pages` look sane by eye.

- [ ] **Commit any final cleanup, then hand off**

Once all tasks are checked off and the full suite passes, this feature branch (`feature/chapter-segmentation-linking`) is ready for the `superpowers:finishing-a-development-branch` skill to decide how to integrate it (PR, merge, etc.) — not part of this plan.
