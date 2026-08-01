# Chapter Segmentation Strategy Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace chapter-segmentation's single fixed heuristic/LLM pipeline with three pluggable, opportunistically-run strategies (PDF outline read, Crossref-by-ISBN lookup, same-library Zotero-catalog lookup) that feed a bounded fusion step, falling back to today's unchanged heuristic/LLM pipeline whenever none of the three produce anything usable for a book.

**Architecture:** A new `backend/services/chapter_evidence/` package holds each strategy's data types and logic in its own file (`types.py`, `outline_strategy.py`, `crossref_strategy.py`, `zotero_catalog_strategy.py`, `fusion.py`). A small set of helpers shared between the new strategies and the existing heuristic (`_is_part_divider`, `_is_back_matter`, `_normalized_title`, a date-to-year parser) move into a new `backend/services/chapter_common.py`. `chapter_segmentation.py` gains one new orchestration function, `analyze_attachment_with_strategies`, that runs the three strategies, merges their output, and — only when that merge is empty — delegates to the existing `analyze_attachment` / `analyze_attachment_with_llm_fallback` unchanged. `run()` is updated to construct the strategy objects once per library scan and call the new orchestration function instead of the old two.

**Tech Stack:** Python 3.12, pypdf (PDF outline reading), rapidfuzz (fuzzy title alignment, already a dependency), httpx (Crossref HTTP calls, already a dependency), pytest/unittest (`unittest.TestCase` / `unittest.IsolatedAsyncioTestCase`, matching this codebase's existing convention).

**Reference:** `docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md` — read this first if anything below is unclear about *why*, not just *what*.

---

## Before you start

Run the existing chapter-segmentation test suite once to establish a clean baseline — every task below that touches shared code re-runs a subset of this:

```bash
uv run pytest backend/tests/test_chapter_segmentation.py backend/tests/test_chapter_retrofit.py backend/tests/test_chapter_linking_api.py -q
```

Expected: all pass (some may be skipped if evaluation PDFs aren't present locally — that's fine, this project's evaluation PDFs are gitignored).

---

### Task 1: Extract shared helpers into `chapter_common.py`

Pure refactor — moves `_is_part_divider`, `_is_back_matter`, `_normalized_title` out of `backend/services/chapter_segmentation.py`, and `_year_from_date` out of `backend/services/chapter_retrofit.py` (renamed `year_from_date`, dropping the leading underscore since it's now a shared, cross-module utility — the same promotion pattern this codebase already used for `parse_json_object`/`parse_json_array` in `backend/utils/llm_json.py`), into one new module both the existing heuristic and the new strategies can import without a circular dependency.

**Files:**
- Create: `backend/services/chapter_common.py`
- Create: `backend/tests/test_chapter_common.py`
- Modify: `backend/services/chapter_segmentation.py` (remove lines defining `_PART_DIVIDER_RE`, `_BACK_MATTER_TITLES`, `_normalized_title`, `_is_part_divider`, `_is_back_matter`; add one import)
- Modify: `backend/services/chapter_retrofit.py` (remove `_year_from_date`; add one import; update its 2 call sites)

- [ ] **Step 1: Write the new module**

```python
# backend/services/chapter_common.py
"""Helpers shared between backend/services/chapter_segmentation.py's
PDF-internal heuristic and the backend/services/chapter_evidence/
strategies (external/local metadata sources) -- kept in one place so
"is this title a part-divider/back-matter section" and "what year does
this date string represent" are answered identically everywhere. See
docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md
section 9.
"""

import re
import unicodedata

# Entries that are structural markers, not chapters: part dividers
# ("Teil 1: ...", "Part II ...", "PARTIE I. ...") and standard back-matter
# lists (index, contributors, bibliography, ...).
_PART_DIVIDER_RE = re.compile(
    r"^(?:teil|part|partie|section|abschnitt)\b[\s.:]*(?:[0-9]+|[ivxlcdm]+)?\b", re.IGNORECASE
)
_BACK_MATTER_TITLES = {
    "contributors", "notes on contributors", "about the authors", "about the contributors",
    "index", "indexes", "name index", "subject index",
    "register", "sachregister", "personenregister", "namensregister",
    "bibliography", "bibliographie", "literatur", "literaturverzeichnis", "references",
    "quellenverzeichnis", "acknowledgments", "acknowledgements", "danksagung", "remerciements",
    "glossary", "glossar", "glossaire", "abbreviations", "abkurzungsverzeichnis", "abkurzungen",
    "list of figures", "list of tables", "tabellenverzeichnis", "abbildungsverzeichnis",
    "les auteurs", "auteurs", "autorinnen und autoren", "die autorinnen und autoren",
    "verzeichnis der autorinnen und autoren", "zu den autorinnen und autoren",
    "liste des auteurs", "memento", "autorinnenverzeichnis", "autorenverzeichnis",
    "contents", "table of contents", "inhalt", "inhaltsverzeichnis", "inhaltsubersicht",
    "sommaire", "table des matieres",
}


def _normalized_title(title: str) -> str:
    decomposed = unicodedata.normalize("NFKD", title.lower())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", stripped)).strip()


def _is_part_divider(title: str) -> bool:
    return _PART_DIVIDER_RE.match(title) is not None


def _is_back_matter(title: str) -> bool:
    normalized = _normalized_title(title)
    if normalized in _BACK_MATTER_TITLES:
        return True
    # Author-marker TOCs fold the author into the entry title
    # ("MEMENTO par Lucie Daudin") -- test the part before the marker too.
    stripped = re.split(r"\b(?:par|by)\b", normalized, maxsplit=1)[0].strip()
    return stripped in _BACK_MATTER_TITLES


def year_from_date(date_str: str | None) -> int | None:
    """Extracts a 4-digit year token from a Zotero `date` field string
    ("2019-05", "May 2019", ...). Returns None on anything unparseable --
    never raises."""
    if not date_str:
        return None
    for token in date_str.replace("-", " ").split():
        if token.isdigit() and len(token) == 4:
            return int(token)
    return None
```

- [ ] **Step 2: Write tests for the new module**

```python
# backend/tests/test_chapter_common.py
"""Unit tests for backend.services.chapter_common."""

import unittest

from backend.services.chapter_common import (
    _is_back_matter,
    _is_part_divider,
    _normalized_title,
    year_from_date,
)


class TestIsPartDivider(unittest.TestCase):
    def test_recognizes_part_and_teil(self):
        self.assertTrue(_is_part_divider("Part I"))
        self.assertTrue(_is_part_divider("Teil 2: Grundlagen"))
        self.assertTrue(_is_part_divider("PARTIE I. Introduction"))

    def test_does_not_flag_ordinary_titles(self):
        self.assertFalse(_is_part_divider("Introduction"))
        self.assertFalse(_is_part_divider("Comparing Citation Styles"))


class TestIsBackMatter(unittest.TestCase):
    def test_recognizes_known_titles(self):
        self.assertTrue(_is_back_matter("Bibliography"))
        self.assertTrue(_is_back_matter("Inhaltsverzeichnis"))
        self.assertTrue(_is_back_matter("Sommaire"))

    def test_does_not_flag_ordinary_titles(self):
        self.assertFalse(_is_back_matter("Comparing Citation Styles"))


class TestNormalizedTitle(unittest.TestCase):
    def test_strips_accents_and_punctuation(self):
        self.assertEqual(_normalized_title("Sommaire!"), "sommaire")
        self.assertEqual(_normalized_title("Über Recht"), "uber recht")


class TestYearFromDate(unittest.TestCase):
    def test_extracts_year_from_iso_style(self):
        self.assertEqual(year_from_date("2019-05"), 2019)

    def test_extracts_year_from_prose_style(self):
        self.assertEqual(year_from_date("May 2019"), 2019)

    def test_returns_none_for_empty_or_missing(self):
        self.assertIsNone(year_from_date(None))
        self.assertIsNone(year_from_date(""))

    def test_returns_none_when_no_four_digit_token(self):
        self.assertIsNone(year_from_date("n/a"))
```

- [ ] **Step 3: Run the new tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_common.py -q`
Expected: PASS (6 tests)

- [ ] **Step 4: Remove the moved code from `chapter_segmentation.py`**

Delete these definitions from `backend/services/chapter_segmentation.py` (they currently sit just above `_TRAILING_BLANK_PAGE_MAX_CHARS`, right after the `_ROMAN_PREFIX_RE` line — leave `_PART_DIVIDER_RE`'s sibling `_ROMAN_PREFIX_RE` in place, it is NOT part of this move):

```python
_PART_DIVIDER_RE = re.compile(
    r"^(?:teil|part|partie|section|abschnitt)\b[\s.:]*(?:[0-9]+|[ivxlcdm]+)?\b", re.IGNORECASE
)
_BACK_MATTER_TITLES = {
    ...  # the whole set literal
}


def _normalized_title(title: str) -> str:
    ...


def _is_part_divider(title: str) -> bool:
    ...


def _is_back_matter(title: str) -> bool:
    ...
```

Add this import near the top of the file, alongside the other `backend.services.*` imports:

```python
from backend.services.chapter_common import _is_back_matter, _is_part_divider, _normalized_title
```

- [ ] **Step 5: Remove `_year_from_date` from `chapter_retrofit.py`**

Delete this function from `backend/services/chapter_retrofit.py`:

```python
def _year_from_date(date_str: str | None) -> int | None:
    if not date_str:
        return None
    for token in date_str.replace("-", " ").split():
        if token.isdigit() and len(token) == 4:
            return int(token)
    return None
```

Add this import near the top of the file:

```python
from backend.services.chapter_common import year_from_date
```

Update its two call sites (in `find_best_book_match`'s caller `find_matches`, and inside `find_matches` itself) from `_year_from_date(...)` to `year_from_date(...)`.

- [ ] **Step 6: Run the full existing chapter-segmentation/retrofit suite to confirm no regression**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py backend/tests/test_chapter_retrofit.py backend/tests/test_chapter_common.py -q`
Expected: PASS, same pass count as the "Before you start" baseline plus the 6 new tests.

- [ ] **Step 7: Commit**

```bash
git add backend/services/chapter_common.py backend/tests/test_chapter_common.py \
        backend/services/chapter_segmentation.py backend/services/chapter_retrofit.py
git commit -m "refactor: extract shared chapter-title/date helpers into chapter_common.py"
```

---

### Task 2: `chapter_evidence` package scaffolding and shared types

**Files:**
- Create: `backend/services/chapter_evidence/__init__.py` (empty)
- Create: `backend/services/chapter_evidence/types.py`
- Create: `backend/tests/test_chapter_evidence_types.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_chapter_evidence_types.py
"""Unit tests for backend.services.chapter_evidence.types."""

import unittest

from backend.services.chapter_evidence.types import BookContext, ChapterCandidate, _first_page_number


class TestFirstPageNumber(unittest.TestCase):
    def test_parses_range(self):
        self.assertEqual(_first_page_number("85-113"), 85)

    def test_parses_single_page(self):
        self.assertEqual(_first_page_number("45"), 45)

    def test_returns_none_for_empty_string(self):
        self.assertIsNone(_first_page_number(""))

    def test_returns_none_for_unparseable_text(self):
        self.assertIsNone(_first_page_number("n/a"))


class TestChapterCandidateDefaults(unittest.TestCase):
    def test_defaults(self):
        c = ChapterCandidate(title="Introduction")
        self.assertEqual(c.authors, ())
        self.assertIsNone(c.printed_page_number)
        self.assertIsNone(c.pdf_page_index)
        self.assertIsNone(c.chapter_doi)
        self.assertEqual(c.source, "heuristic")
        self.assertEqual(c.metadata_confidence, 1.0)

    def test_is_frozen_and_hashable(self):
        c = ChapterCandidate(title="Introduction")
        self.assertIsInstance(hash(c), int)
        with self.assertRaises(Exception):
            c.title = "Something Else"


class TestBookContext(unittest.TestCase):
    def test_construction(self):
        ctx = BookContext(
            item_key="B1", isbn="9783031466373", title="Some Book",
            editors=("Jane Editor",), publisher="Acme Press", year=2020,
        )
        self.assertEqual(ctx.isbn, "9783031466373")
        self.assertEqual(ctx.editors, ("Jane Editor",))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_evidence_types.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.services.chapter_evidence'`

- [ ] **Step 3: Create the package and implementation**

```bash
touch backend/services/chapter_evidence/__init__.py
```

```python
# backend/services/chapter_evidence/types.py
"""Shared types for the chapter_evidence strategy pipeline. See design spec
docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md
section 3 and 4.
"""

from dataclasses import dataclass
from typing import Protocol


def _first_page_number(range_str: str) -> int | None:
    """Parses a printed page-range string ("85-113") into its first page
    number (85). Returns None on anything unparseable -- never raises."""
    if not range_str:
        return None
    first = range_str.split("-")[0].strip()
    return int(first) if first.isdigit() else None


@dataclass(frozen=True)
class ChapterCandidate:
    title: str
    authors: tuple[str, ...] = ()
    printed_page_number: int | None = None   # from a TOC/metadata source;
    # never assumed equal to pdf_page_index.
    pdf_page_index: int | None = None          # set only by a direct-
    # localization strategy (currently: outline). None means "still needs
    # content-search localization."
    chapter_doi: str | None = None             # set by Crossref, or by the
    # zotero_catalog strategy when the matched bookSection item has its own
    # populated DOI field.
    source: str = "heuristic"                  # "outline" | "crossref" |
    # "zotero_catalog" | "outline+crossref" | "outline+zotero_catalog" |
    # "heuristic" | "llm"
    metadata_confidence: float = 1.0            # how certain the SOURCE
    # (not the eventual PDF location) is that this candidate is the right
    # chapter of the right book -- fixed at 1.0 for outline/Crossref
    # candidates, graduated for zotero_catalog candidates.


@dataclass(frozen=True)
class BookContext:
    item_key: str
    isbn: str | None
    title: str
    editors: tuple[str, ...]
    publisher: str | None
    year: int | None


class MetadataStrategy(Protocol):
    def applicable(self, context: BookContext) -> bool: ...
    async def fetch(self, context: BookContext) -> list[ChapterCandidate]: ...


class StructureStrategy(Protocol):
    def applicable(self, pdf_bytes: bytes) -> bool: ...
    def extract(self, pdf_bytes: bytes) -> list[ChapterCandidate]: ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_evidence_types.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_evidence/
git commit -m "feat: add chapter_evidence package with shared ChapterCandidate/BookContext types"
```

---

### Task 3: `OutlineStructureStrategy` — PDF `/Outlines` read

**Files:**
- Create: `backend/services/chapter_evidence/outline_strategy.py`
- Create: `backend/tests/test_chapter_evidence_outline.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_chapter_evidence_outline.py
"""Unit tests for backend.services.chapter_evidence.outline_strategy."""

import io
import unittest

from pypdf import PdfWriter

from backend.services.chapter_evidence.outline_strategy import (
    OutlineStructureStrategy,
    extract_outline_candidates,
)


def _build_pdf(num_pages: int, outline_entries: list[tuple[str, int]] | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    for title, page_number in outline_entries or []:
        writer.add_outline_item(title, page_number)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestExtractOutlineCandidates(unittest.TestCase):
    def test_extracts_top_level_entries_with_page_index(self):
        # 5 entries over 100 pages -> 20 pages/entry, comfortably within the
        # plausibility band (3-150 pages/entry).
        entries = [(f"Chapter {i}", i * 20) for i in range(5)]
        pdf_bytes = _build_pdf(100, entries)
        candidates = extract_outline_candidates(pdf_bytes)
        self.assertEqual(len(candidates), 5)
        self.assertEqual(candidates[0].title, "Chapter 0")
        self.assertEqual(candidates[0].pdf_page_index, 0)
        self.assertEqual(candidates[1].pdf_page_index, 20)
        self.assertEqual(candidates[0].source, "outline")
        self.assertIsNone(candidates[0].chapter_doi)
        self.assertIsNone(candidates[0].printed_page_number)

    def test_returns_empty_list_when_no_outline(self):
        pdf_bytes = _build_pdf(10)
        self.assertEqual(extract_outline_candidates(pdf_bytes), [])

    def test_returns_empty_list_on_malformed_pdf(self):
        self.assertEqual(extract_outline_candidates(b"not a pdf at all"), [])

    def test_filters_part_divider_and_back_matter_titles(self):
        entries = [
            ("Part I", 0), ("Chapter One", 5), ("Chapter Two", 25),
            ("Chapter Three", 45), ("Bibliography", 65),
        ]
        pdf_bytes = _build_pdf(100, entries)
        candidates = extract_outline_candidates(pdf_bytes)
        titles = [c.title for c in candidates]
        self.assertNotIn("Part I", titles)
        self.assertNotIn("Bibliography", titles)
        self.assertIn("Chapter One", titles)

    def test_rejects_implausibly_sparse_outline(self):
        # Only 2 top-level entries (e.g. Intro/Conclusion) over 400 pages
        # implies real chapters are nested one level down (a Part-divider
        # top level) -- 200 pages/entry exceeds the 150 max.
        entries = [("Introduction", 0), ("Conclusion", 390)]
        pdf_bytes = _build_pdf(400, entries)
        self.assertEqual(extract_outline_candidates(pdf_bytes), [])

    def test_rejects_single_entry_outline(self):
        pdf_bytes = _build_pdf(50, [("Only Entry", 0)])
        self.assertEqual(extract_outline_candidates(pdf_bytes), [])

    def test_ignores_nested_child_entries(self):
        # Build a PDF whose outline has one top-level entry with a nested
        # child -- the child must not be surfaced as its own candidate.
        writer = PdfWriter()
        for _ in range(60):
            writer.add_blank_page(width=200, height=200)
        parent = writer.add_outline_item("Part I", 0)
        writer.add_outline_item("Nested Chapter", 5, parent=parent)
        writer.add_outline_item("Chapter Two", 30)
        buf = io.BytesIO()
        writer.write(buf)
        candidates = extract_outline_candidates(buf.getvalue())
        titles = [c.title for c in candidates]
        self.assertNotIn("Nested Chapter", titles)


class TestOutlineStructureStrategy(unittest.TestCase):
    def test_applicable_true_and_extract_matches_module_function(self):
        entries = [(f"Chapter {i}", i * 20) for i in range(5)]
        pdf_bytes = _build_pdf(100, entries)
        strategy = OutlineStructureStrategy()
        self.assertTrue(strategy.applicable(pdf_bytes))
        self.assertEqual(len(strategy.extract(pdf_bytes)), 5)

    def test_applicable_false_when_no_outline(self):
        pdf_bytes = _build_pdf(10)
        strategy = OutlineStructureStrategy()
        self.assertFalse(strategy.applicable(pdf_bytes))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_evidence_outline.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.services.chapter_evidence.outline_strategy'`

- [ ] **Step 3: Write the implementation**

```python
# backend/services/chapter_evidence/outline_strategy.py
"""PDF embedded outline (bookmark) read. See design spec
docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md
section 5.1.
"""

import io
import logging

from pypdf import PdfReader

from backend.services.chapter_common import _is_back_matter, _is_part_divider
from backend.services.chapter_evidence.types import ChapterCandidate

logger = logging.getLogger(__name__)

_MIN_ENTRIES = 2
_MIN_PAGES_PER_ENTRY = 3
_MAX_PAGES_PER_ENTRY = 150


def extract_outline_candidates(content: bytes) -> list[ChapterCandidate]:
    """Reads the PDF's embedded outline/bookmark catalog and returns one
    ChapterCandidate per TOP-LEVEL entry that survives the
    _is_part_divider/_is_back_matter filters, each with pdf_page_index
    resolved directly -- no content search needed. Nested (child) outline
    entries are not surfaced as separate chapters (see design spec 5.1's
    top-level-only limitation). Returns [] if the PDF has no outline
    catalog, reading it raises (malformed/encrypted PDF), the filtered
    result has fewer than 2 entries, or the average pages-per-entry ratio
    is implausible (a sparse top level usually means real chapters are
    nested one level down, e.g. under Part dividers) -- never raises.
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        outline = reader.outline
    except Exception:
        logger.info("extract_outline_candidates: failed to read PDF/outline", exc_info=True)
        return []

    entries: list[ChapterCandidate] = []
    for item in outline:
        if isinstance(item, list):
            continue  # nested (child) entries -- not surfaced in Phase 1
        try:
            page_index = reader.get_destination_page_number(item)
        except Exception:
            continue
        title = str(item.title).strip()
        if not title or _is_part_divider(title) or _is_back_matter(title):
            continue
        entries.append(ChapterCandidate(title=title, pdf_page_index=page_index, source="outline"))

    if len(entries) < _MIN_ENTRIES:
        logger.info("extract_outline_candidates: too few top-level entries (%d), rejecting", len(entries))
        return []

    total_pages = len(reader.pages)
    pages_per_entry = total_pages / len(entries)
    if not (_MIN_PAGES_PER_ENTRY <= pages_per_entry <= _MAX_PAGES_PER_ENTRY):
        logger.info(
            "extract_outline_candidates: implausible pages-per-entry ratio %.1f "
            "(%d entries, %d pages), rejecting",
            pages_per_entry, len(entries), total_pages,
        )
        return []

    return entries


class OutlineStructureStrategy:
    def applicable(self, pdf_bytes: bytes) -> bool:
        return len(extract_outline_candidates(pdf_bytes)) > 0

    def extract(self, pdf_bytes: bytes) -> list[ChapterCandidate]:
        return extract_outline_candidates(pdf_bytes)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_evidence_outline.py -q`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_evidence/outline_strategy.py backend/tests/test_chapter_evidence_outline.py
git commit -m "feat: add PDF outline (bookmark) chapter-candidate extraction strategy"
```

---

### Task 4: Settings fields for Crossref

**Files:**
- Modify: `backend/config/settings.py`
- Create: `backend/tests/test_settings_crossref.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_settings_crossref.py
"""Unit tests for the Crossref-related Settings fields."""

import unittest

from backend.config.settings import Settings, reset_settings


class TestCrossrefSettings(unittest.TestCase):
    def tearDown(self):
        reset_settings()

    def test_crossref_contact_email_defaults_to_none(self):
        settings = Settings()
        self.assertIsNone(settings.crossref_contact_email)

    def test_crossref_cache_path_defaults_under_data_path(self):
        settings = Settings()
        self.assertEqual(settings.crossref_cache_path, settings.data_path / "system" / "crossref_cache")

    def test_crossref_cache_path_can_be_overridden(self):
        settings = Settings(crossref_cache_path="/tmp/custom-crossref-cache")
        self.assertEqual(str(settings.crossref_cache_path), "/tmp/custom-crossref-cache")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_settings_crossref.py -q`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'crossref_contact_email'`

- [ ] **Step 3: Add the fields**

In `backend/config/settings.py`, add two fields directly after the existing `review_queue_path` field:

```python
    crossref_contact_email: Optional[str] = Field(
        default=None,
        description="Contact email sent as Crossref's 'mailto' polite-pool "
                    "parameter. Optional -- omitted requests use Crossref's "
                    "public pool.",
    )
    crossref_cache_path: Optional[Path] = Field(
        default=None,
        description="Directory for cached Crossref chapter lookups, keyed by "
                    "ISBN. Defaults to <data_path>/system/crossref_cache.",
    )
```

Add `"crossref_cache_path"` to the `expand_path` field_validator's field list:

```python
    @field_validator("data_path", "model_weights_path", "vector_db_path", "log_file", "registrations_path", "autoindex_keys_path", "review_queue_path", "crossref_cache_path", mode="before")
```

Add the default-fill line to `set_derived_paths`:

```python
        if self.crossref_cache_path is None:
            self.crossref_cache_path = self.data_path / "system" / "crossref_cache"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_settings_crossref.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/config/settings.py backend/tests/test_settings_crossref.py
git commit -m "feat: add crossref_contact_email/crossref_cache_path settings"
```

---

### Task 5: `CrossrefMetadataStrategy` — chapter lookup by ISBN

**Files:**
- Create: `backend/services/chapter_evidence/crossref_strategy.py`
- Create: `backend/tests/test_chapter_evidence_crossref.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_chapter_evidence_crossref.py
"""Unit tests for backend.services.chapter_evidence.crossref_strategy."""

import json
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.services.chapter_evidence.crossref_strategy import (
    CrossrefMetadataStrategy,
    fetch_crossref_chapters,
    normalize_isbn,
)
from backend.services.chapter_evidence.types import BookContext


class TestNormalizeIsbn(unittest.TestCase):
    def test_extracts_isbn13(self):
        self.assertEqual(normalize_isbn("978-3-031-46637-3"), "9783031466373")

    def test_falls_back_to_isbn10(self):
        self.assertEqual(normalize_isbn("0-306-40615-2"), "0306406152")

    def test_picks_isbn13_over_isbn10_when_both_present(self):
        self.assertEqual(normalize_isbn("0306406152; 978-3-031-46637-3"), "9783031466373")

    def test_returns_none_for_empty(self):
        self.assertIsNone(normalize_isbn(""))

    def test_returns_none_for_unparseable(self):
        self.assertIsNone(normalize_isbn("not an isbn"))


def _crossref_response(items: list[dict]) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"message": {"items": items}}
    response.raise_for_status = MagicMock()
    return response


class TestFetchCrossrefChapters(unittest.IsolatedAsyncioTestCase):
    async def test_parses_book_chapter_records_only(self):
        items = [
            {"type": "book", "title": ["The Whole Book"], "DOI": "10.1/book"},
            {
                "type": "book-chapter", "title": ["Introduction"],
                "author": [{"given": "Jane", "family": "Author"}],
                "page": "1-20", "DOI": "10.1/ch1",
            },
        ]
        http_client = AsyncMock()
        http_client.get = AsyncMock(return_value=_crossref_response(items))
        result = await fetch_crossref_chapters("9783031466373", http_client, cache_dir=None, contact_email=None)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].title, "Introduction")
        self.assertEqual(result[0].authors, ("Jane Author",))
        self.assertEqual(result[0].printed_page_number, 1)
        self.assertEqual(result[0].chapter_doi, "10.1/ch1")
        self.assertEqual(result[0].source, "crossref")
        self.assertEqual(result[0].metadata_confidence, 1.0)

    async def test_returns_empty_list_for_unregistered_isbn(self):
        http_client = AsyncMock()
        http_client.get = AsyncMock(return_value=_crossref_response([]))
        result = await fetch_crossref_chapters("0000000000000", http_client, cache_dir=None, contact_email=None)
        self.assertEqual(result, [])

    async def test_network_exception_is_swallowed(self):
        import httpx
        http_client = AsyncMock()
        http_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
        result = await fetch_crossref_chapters("9783031466373", http_client, cache_dir=None, contact_email=None)
        self.assertEqual(result, [])

    async def test_cache_hit_short_circuits_without_http_call(self):
        with TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            http_client = AsyncMock()
            http_client.get = AsyncMock(return_value=_crossref_response([
                {
                    "type": "book-chapter", "title": ["Introduction"],
                    "author": [], "page": "1-20", "DOI": "10.1/ch1",
                },
            ]))
            first = await fetch_crossref_chapters("9783031466373", http_client, cache_dir=cache_dir, contact_email=None)
            self.assertEqual(len(first), 1)
            http_client.get.assert_called_once()

            second = await fetch_crossref_chapters("9783031466373", http_client, cache_dir=cache_dir, contact_email=None)
            self.assertEqual(len(second), 1)
            http_client.get.assert_called_once()  # still once -- cache hit, no new call

    async def test_empty_result_is_still_cached(self):
        with TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            http_client = AsyncMock()
            http_client.get = AsyncMock(return_value=_crossref_response([]))
            await fetch_crossref_chapters("0000000000000", http_client, cache_dir=cache_dir, contact_email=None)
            http_client.get.assert_called_once()
            await fetch_crossref_chapters("0000000000000", http_client, cache_dir=cache_dir, contact_email=None)
            http_client.get.assert_called_once()  # still once


class TestCrossrefMetadataStrategy(unittest.IsolatedAsyncioTestCase):
    def _context(self, isbn):
        return BookContext(item_key="B1", isbn=isbn, title="Some Book", editors=(), publisher=None, year=None)

    async def test_applicable_false_without_isbn(self):
        strategy = CrossrefMetadataStrategy(AsyncMock(), cache_dir=None, contact_email=None)
        self.assertFalse(strategy.applicable(self._context(None)))

    async def test_applicable_true_with_isbn(self):
        strategy = CrossrefMetadataStrategy(AsyncMock(), cache_dir=None, contact_email=None)
        self.assertTrue(strategy.applicable(self._context("9783031466373")))

    async def test_fetch_returns_empty_without_isbn(self):
        strategy = CrossrefMetadataStrategy(AsyncMock(), cache_dir=None, contact_email=None)
        result = await strategy.fetch(self._context(None))
        self.assertEqual(result, [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_evidence_crossref.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.services.chapter_evidence.crossref_strategy'`

- [ ] **Step 3: Write the implementation**

```python
# backend/services/chapter_evidence/crossref_strategy.py
"""Crossref chapter lookup by ISBN. See design spec
docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md
section 5.2.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from backend.services.chapter_evidence.types import BookContext, ChapterCandidate, _first_page_number

logger = logging.getLogger(__name__)

_CROSSREF_BASE_URL = "https://api.crossref.org/works"
_MAX_RETRIES = 3
_DEFAULT_RETRY_DELAY_SECONDS = 1.0


def normalize_isbn(raw: str) -> Optional[str]:
    """Extracts the first ISBN-13 (13 digits after stripping separators)
    from a Zotero ISBN field, falling back to the first ISBN-10 (10
    digits, last may be 'X'), or None if nothing usable is present."""
    if not raw:
        return None
    candidates = re.split(r"[;\s]+", raw.strip())
    stripped = [re.sub(r"[-\s]", "", c) for c in candidates if c.strip()]
    for c in stripped:
        if len(c) == 13 and c.isdigit():
            return c
    for c in stripped:
        if len(c) == 10 and c[:-1].isdigit() and (c[-1].isdigit() or c[-1].upper() == "X"):
            return c
    return None


def _cache_path(cache_dir: Path, isbn: str) -> Path:
    return cache_dir / f"{isbn}.json"


def _load_cache(cache_dir: Optional[Path], isbn: str) -> Optional[list[ChapterCandidate]]:
    if cache_dir is None:
        return None
    path = _cache_path(cache_dir, isbn)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        ChapterCandidate(
            title=c["title"], authors=tuple(c["authors"]),
            printed_page_number=c["printed_page_number"], pdf_page_index=c["pdf_page_index"],
            chapter_doi=c["chapter_doi"], source=c["source"], metadata_confidence=c["metadata_confidence"],
        )
        for c in data["chapters"]
    ]


def _save_cache(cache_dir: Optional[Path], isbn: str, candidates: list[ChapterCandidate]) -> None:
    if cache_dir is None:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "isbn": isbn,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "chapters": [
            {
                "title": c.title, "authors": list(c.authors),
                "printed_page_number": c.printed_page_number, "pdf_page_index": c.pdf_page_index,
                "chapter_doi": c.chapter_doi, "source": c.source,
                "metadata_confidence": c.metadata_confidence,
            }
            for c in candidates
        ],
    }
    _cache_path(cache_dir, isbn).write_text(json.dumps(payload), encoding="utf-8")


def _parse_crossref_item(item: dict) -> Optional[ChapterCandidate]:
    if item.get("type") != "book-chapter":
        return None
    titles = item.get("title") or []
    if not titles:
        return None
    authors = tuple(
        f"{a.get('given', '')} {a.get('family', '')}".strip()
        for a in item.get("author", []) if a.get("family")
    )
    page = item.get("page")
    printed_page_number = _first_page_number(page) if page else None
    return ChapterCandidate(
        title=titles[0], authors=authors, printed_page_number=printed_page_number,
        chapter_doi=item.get("DOI"), source="crossref",
    )


async def fetch_crossref_chapters(
    isbn: str,
    http_client: httpx.AsyncClient,
    cache_dir: Optional[Path],
    contact_email: Optional[str],
) -> list[ChapterCandidate]:
    """GET .../works?filter=isbn:{isbn}, keeping only type=="book-chapter"
    records. Cached on disk by ISBN (including empty results, so a
    known-unregistered book is never re-queried on repeat runs). Any
    network, HTTP-status, or JSON-shape failure is logged and treated as
    an empty (uncached) result -- never raises.
    """
    cached = _load_cache(cache_dir, isbn)
    if cached is not None:
        return cached

    params: dict[str, str | int] = {
        "filter": f"isbn:{isbn}",
        "select": "DOI,title,author,page,type,container-title",
        "rows": 100,
    }
    if contact_email:
        params["mailto"] = contact_email

    response = None
    for _attempt in range(_MAX_RETRIES):
        try:
            response = await http_client.get(_CROSSREF_BASE_URL, params=params, timeout=10.0)
        except httpx.HTTPError:
            logger.warning("fetch_crossref_chapters: network error for ISBN %s", isbn, exc_info=True)
            return []
        if response.status_code != 429:
            break
        retry_after = response.headers.get("Retry-After") if response.headers else None
        delay = float(retry_after) if retry_after and retry_after.isdigit() else _DEFAULT_RETRY_DELAY_SECONDS
        await asyncio.sleep(delay)
    else:
        logger.warning("fetch_crossref_chapters: exhausted retries (429) for ISBN %s", isbn)
        return []

    try:
        response.raise_for_status()
        data = response.json()
        items = data["message"]["items"]
    except Exception:
        logger.warning("fetch_crossref_chapters: bad response for ISBN %s", isbn, exc_info=True)
        return []

    candidates = [c for item in items if (c := _parse_crossref_item(item)) is not None]
    _save_cache(cache_dir, isbn, candidates)
    return candidates


class CrossrefMetadataStrategy:
    def __init__(self, http_client: httpx.AsyncClient, cache_dir: Optional[Path], contact_email: Optional[str]):
        self._http_client = http_client
        self._cache_dir = cache_dir
        self._contact_email = contact_email

    def applicable(self, context: BookContext) -> bool:
        return context.isbn is not None

    async def fetch(self, context: BookContext) -> list[ChapterCandidate]:
        if context.isbn is None:
            return []
        return await fetch_crossref_chapters(context.isbn, self._http_client, self._cache_dir, self._contact_email)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_evidence_crossref.py -q`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_evidence/crossref_strategy.py backend/tests/test_chapter_evidence_crossref.py
git commit -m "feat: add Crossref chapter-lookup-by-ISBN strategy"
```

---

### Task 6: `ZoteroCatalogMetadataStrategy` — same-library exact `bookTitle` lookup

**Files:**
- Create: `backend/services/chapter_evidence/zotero_catalog_strategy.py`
- Create: `backend/tests/test_chapter_evidence_zotero_catalog.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_chapter_evidence_zotero_catalog.py
"""Unit tests for backend.services.chapter_evidence.zotero_catalog_strategy."""

import unittest

from backend.services.chapter_evidence.types import BookContext
from backend.services.chapter_evidence.zotero_catalog_strategy import (
    ZoteroCatalogMetadataStrategy,
    find_zotero_catalog_candidates,
    score_zotero_catalog_candidate,
)


def _book_section(
    title, book_title, isbn="", date="", publisher="", pages="",
    authors=None, editors=None, doi=None,
):
    creators = [
        {"creatorType": "author", "firstName": f.split()[0], "lastName": f.split()[-1]}
        for f in (authors or [])
    ] + [
        {"creatorType": "editor", "firstName": f.split()[0], "lastName": f.split()[-1]}
        for f in (editors or [])
    ]
    data = {
        "itemType": "bookSection", "key": f"CH-{title[:5]}", "title": title,
        "bookTitle": book_title, "ISBN": isbn, "date": date, "publisher": publisher,
        "pages": pages, "creators": creators, "extra": "",
    }
    if doi is not None:
        data["DOI"] = doi
    return {"data": data}


class TestScoreZoteroCatalogCandidate(unittest.TestCase):
    def _context(self, **overrides):
        defaults = dict(
            item_key="B1", isbn="9783031466373", title="Some Book",
            editors=("Jane Editor",), publisher="Acme Press", year=2020,
        )
        defaults.update(overrides)
        return BookContext(**defaults)

    def test_exact_isbn_match_scores_one(self):
        context = self._context()
        candidate = _book_section("Ch1", "Some Book", isbn="9783031466373")["data"]
        self.assertEqual(score_zotero_catalog_candidate(candidate, context), 1.0)

    def test_title_only_match_scores_base(self):
        context = self._context(isbn=None, publisher=None, year=None, editors=())
        candidate = _book_section("Ch1", "Some Book")["data"]
        self.assertEqual(score_zotero_catalog_candidate(candidate, context), 0.6)

    def test_year_match_adds_bonus(self):
        context = self._context(isbn=None, publisher=None, editors=())
        candidate = _book_section("Ch1", "Some Book", date="2020-01")["data"]
        self.assertAlmostEqual(score_zotero_catalog_candidate(candidate, context), 0.75)

    def test_publisher_match_adds_bonus(self):
        context = self._context(isbn=None, year=None, editors=())
        candidate = _book_section("Ch1", "Some Book", publisher="Acme Press")["data"]
        self.assertAlmostEqual(score_zotero_catalog_candidate(candidate, context), 0.75)

    def test_editor_overlap_adds_bonus(self):
        context = self._context(isbn=None, publisher=None, year=None)
        candidate = _book_section("Ch1", "Some Book", editors=["Jane Editor"])["data"]
        self.assertAlmostEqual(score_zotero_catalog_candidate(candidate, context), 0.8)

    def test_score_is_capped_at_one(self):
        context = self._context(isbn=None)
        candidate = _book_section(
            "Ch1", "Some Book", date="2020-01", publisher="Acme Press", editors=["Jane Editor"],
        )["data"]
        self.assertEqual(score_zotero_catalog_candidate(candidate, context), 1.0)


class TestFindZoteroCatalogCandidates(unittest.TestCase):
    def _context(self, title="Some Book", **overrides):
        defaults = dict(item_key="B1", isbn=None, title=title, editors=(), publisher=None, year=None)
        defaults.update(overrides)
        return BookContext(**defaults)

    def test_returns_empty_when_no_title_match(self):
        result = find_zotero_catalog_candidates(self._context(), {})
        self.assertEqual(result, [])

    def test_returns_candidate_for_exact_title_match(self):
        item = _book_section(
            "Introduction", "Some Book", pages="1-20", authors=["Jane Author"], doi="10.1/ch1",
        )
        index = {"Some Book": [item]}
        result = find_zotero_catalog_candidates(self._context(), index)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].title, "Introduction")
        self.assertEqual(result[0].authors, ("Jane Author",))
        self.assertEqual(result[0].printed_page_number, 1)
        self.assertEqual(result[0].chapter_doi, "10.1/ch1")
        self.assertEqual(result[0].source, "zotero_catalog")

    def test_does_not_fuzzy_match_similar_titles(self):
        item = _book_section("Introduction", "Some Book (2nd ed.)")
        index = {"Some Book (2nd ed.)": [item]}
        result = find_zotero_catalog_candidates(self._context(title="Some Book"), index)
        self.assertEqual(result, [])

    def test_deduplicates_by_title_keeping_higher_score(self):
        weak = _book_section("Introduction", "Some Book")
        strong = _book_section("Introduction", "Some Book", isbn="9783031466373")
        index = {"Some Book": [weak, strong]}
        result = find_zotero_catalog_candidates(self._context(isbn="9783031466373"), index)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].metadata_confidence, 1.0)


class TestZoteroCatalogMetadataStrategy(unittest.IsolatedAsyncioTestCase):
    async def test_applicable_and_fetch(self):
        item = _book_section("Introduction", "Some Book")
        strategy = ZoteroCatalogMetadataStrategy({"Some Book": [item]})
        context = BookContext(item_key="B1", isbn=None, title="Some Book", editors=(), publisher=None, year=None)
        self.assertTrue(strategy.applicable(context))
        result = await strategy.fetch(context)
        self.assertEqual(len(result), 1)

    async def test_not_applicable_when_title_absent(self):
        strategy = ZoteroCatalogMetadataStrategy({})
        context = BookContext(item_key="B1", isbn=None, title="Some Book", editors=(), publisher=None, year=None)
        self.assertFalse(strategy.applicable(context))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_evidence_zotero_catalog.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.services.chapter_evidence.zotero_catalog_strategy'`

- [ ] **Step 3: Write the implementation**

```python
# backend/services/chapter_evidence/zotero_catalog_strategy.py
"""Same-library exact-bookTitle lookup of already-catalogued bookSection
items. See design spec
docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md
section 5.3.
"""

from backend.services.chapter_common import year_from_date
from backend.services.chapter_evidence.types import BookContext, ChapterCandidate, _first_page_number

_BASE_SCORE = 0.6
_YEAR_BONUS = 0.15
_PUBLISHER_BONUS = 0.15
_EDITOR_OVERLAP_BONUS = 0.2


def _last_names(creators: list[dict], creator_type: str) -> set[str]:
    return {
        c.get("lastName", "").strip().lower()
        for c in creators
        if c.get("creatorType") == creator_type and c.get("lastName", "").strip()
    }


def score_zotero_catalog_candidate(book_section_data: dict, context: BookContext) -> float:
    """0.0-1.0. An exact match on the candidate's own (book-inherited) ISBN
    field against context.isbn short-circuits to 1.0. Otherwise starts from
    a base score for the bookTitle match already required to reach this
    function at all, then adds one bonus per additional corroborating
    field, capped at 1.0. These constants are initial estimates, not
    empirically calibrated -- see design spec 5.3 for the recalibration
    plan.
    """
    candidate_isbn = (book_section_data.get("ISBN") or "").replace("-", "").strip()
    context_isbn = (context.isbn or "").replace("-", "").strip()
    if candidate_isbn and context_isbn and candidate_isbn == context_isbn:
        return 1.0

    score = _BASE_SCORE

    candidate_year = year_from_date(book_section_data.get("date"))
    if candidate_year is not None and context.year is not None and candidate_year == context.year:
        score += _YEAR_BONUS

    candidate_publisher = (book_section_data.get("publisher") or "").strip().lower()
    context_publisher = (context.publisher or "").strip().lower()
    if candidate_publisher and context_publisher and candidate_publisher == context_publisher:
        score += _PUBLISHER_BONUS

    candidate_editors = _last_names(book_section_data.get("creators", []), "editor")
    context_editor_last_names = {name.split()[-1].lower() for name in context.editors if name.strip()}
    if candidate_editors and context_editor_last_names and candidate_editors & context_editor_last_names:
        score += _EDITOR_OVERLAP_BONUS

    return min(score, 1.0)


def find_zotero_catalog_candidates(
    context: BookContext, book_sections_by_title: dict[str, list[dict]],
) -> list[ChapterCandidate]:
    """Looks up book_sections_by_title[context.title.strip()] -- an exact,
    non-fuzzy string match on the bookSection item's own `bookTitle` field
    against the book's `title` field. No match key -> []. Each matching
    bookSection item becomes one ChapterCandidate; candidates are sorted by
    printed_page_number (unknowns last) to approximate book order, then
    deduplicated by title, keeping the higher-metadata_confidence one.
    """
    matches = book_sections_by_title.get(context.title.strip(), [])
    candidates: list[ChapterCandidate] = []
    for item in matches:
        data = item["data"]
        authors = tuple(
            f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
            for c in data.get("creators", [])
            if c.get("creatorType") == "author"
        )
        pages = data.get("pages") or ""
        candidates.append(ChapterCandidate(
            title=data.get("title", ""),
            authors=authors,
            printed_page_number=_first_page_number(pages) if pages else None,
            chapter_doi=data.get("DOI") or None,
            source="zotero_catalog",
            metadata_confidence=score_zotero_catalog_candidate(data, context),
        ))

    candidates.sort(key=lambda c: (c.printed_page_number is None, c.printed_page_number or 0))

    deduped: dict[str, ChapterCandidate] = {}
    for c in candidates:
        existing = deduped.get(c.title)
        if existing is None or c.metadata_confidence > existing.metadata_confidence:
            deduped[c.title] = c
    return list(deduped.values())


class ZoteroCatalogMetadataStrategy:
    def __init__(self, book_sections_by_title: dict[str, list[dict]]):
        self._book_sections_by_title = book_sections_by_title

    def applicable(self, context: BookContext) -> bool:
        return context.title.strip() in self._book_sections_by_title

    async def fetch(self, context: BookContext) -> list[ChapterCandidate]:
        return find_zotero_catalog_candidates(context, self._book_sections_by_title)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_evidence_zotero_catalog.py -q`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_evidence/zotero_catalog_strategy.py backend/tests/test_chapter_evidence_zotero_catalog.py
git commit -m "feat: add same-library exact-bookTitle Zotero catalog strategy"
```

---

### Task 7: Fusion — `merge_metadata_sources` and `merge_candidates`

**Files:**
- Create: `backend/services/chapter_evidence/fusion.py`
- Create: `backend/tests/test_chapter_evidence_fusion.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_chapter_evidence_fusion.py
"""Unit tests for backend.services.chapter_evidence.fusion."""

import unittest

from backend.services.chapter_evidence.fusion import merge_candidates, merge_metadata_sources
from backend.services.chapter_evidence.types import ChapterCandidate


class TestMergeMetadataSources(unittest.TestCase):
    def test_single_source_passthrough(self):
        candidates = [ChapterCandidate(title="Introduction", source="crossref")]
        result = merge_metadata_sources([candidates])
        self.assertEqual(result, candidates)

    def test_all_empty_returns_empty(self):
        self.assertEqual(merge_metadata_sources([[], []]), [])

    def test_higher_confidence_candidate_wins_aligned_pair(self):
        crossref = [ChapterCandidate(title="Introduction to the Subject", source="crossref", metadata_confidence=1.0)]
        catalog = [ChapterCandidate(title="Introduction to the Subject", source="zotero_catalog", metadata_confidence=0.6)]
        result = merge_metadata_sources([crossref, catalog])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].source, "crossref")

    def test_tie_prefers_crossref(self):
        crossref = [ChapterCandidate(title="Introduction to the Subject", source="crossref", metadata_confidence=0.8)]
        catalog = [ChapterCandidate(title="Introduction to the Subject", source="zotero_catalog", metadata_confidence=0.8)]
        result = merge_metadata_sources([crossref, catalog])
        self.assertEqual(result[0].source, "crossref")

    def test_exact_doi_agreement_forces_confidence_to_one(self):
        crossref = [ChapterCandidate(
            title="Introduction to the Subject", source="crossref",
            metadata_confidence=1.0, chapter_doi="10.1234/abc",
        )]
        catalog = [ChapterCandidate(
            title="Introduction to the Subject", source="zotero_catalog",
            metadata_confidence=0.6, chapter_doi="10.1234/abc",
        )]
        result = merge_metadata_sources([crossref, catalog])
        self.assertEqual(result[0].metadata_confidence, 1.0)

    def test_unmatched_entries_from_both_lists_are_kept(self):
        crossref = [ChapterCandidate(title="Chapter One", source="crossref")]
        catalog = [ChapterCandidate(title="Totally Different Chapter", source="zotero_catalog")]
        result = merge_metadata_sources([crossref, catalog])
        titles = {c.title for c in result}
        self.assertEqual(titles, {"Chapter One", "Totally Different Chapter"})


class TestMergeCandidates(unittest.TestCase):
    def test_passthrough_when_metadata_empty(self):
        outline = [ChapterCandidate(title="Chapter One", pdf_page_index=5, source="outline")]
        self.assertEqual(merge_candidates(outline, []), outline)

    def test_passthrough_when_outline_empty(self):
        metadata = [ChapterCandidate(title="Chapter One", source="crossref")]
        self.assertEqual(merge_candidates([], metadata), metadata)

    def test_matched_pair_combines_outline_location_with_metadata_fields(self):
        outline = [ChapterCandidate(title="Chapter One", pdf_page_index=10, source="outline")]
        metadata = [ChapterCandidate(
            title="Chapter One", authors=("Jane Author",), chapter_doi="10.1234/xyz",
            source="crossref", metadata_confidence=1.0,
        )]
        result = merge_candidates(outline, metadata)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].pdf_page_index, 10)
        self.assertEqual(result[0].authors, ("Jane Author",))
        self.assertEqual(result[0].source, "outline+crossref")

    def test_unmatched_outline_entry_kept_without_localization(self):
        outline = [
            ChapterCandidate(title="Foreword", pdf_page_index=2, source="outline"),
            ChapterCandidate(title="Chapter One", pdf_page_index=10, source="outline"),
        ]
        metadata = [ChapterCandidate(title="Chapter One", source="crossref")]
        result = merge_candidates(outline, metadata)
        by_title = {c.title: c for c in result}
        self.assertIn("Foreword", by_title)
        self.assertIsNone(by_title["Foreword"].chapter_doi)
        self.assertEqual(by_title["Foreword"].pdf_page_index, 2)

    def test_unmatched_metadata_entry_kept_without_pdf_page_index(self):
        outline = [ChapterCandidate(title="Chapter One", pdf_page_index=10, source="outline")]
        metadata = [
            ChapterCandidate(title="Chapter One", source="crossref"),
            ChapterCandidate(title="Chapter Two", source="crossref"),
        ]
        result = merge_candidates(outline, metadata)
        chapter_two = next(c for c in result if c.title == "Chapter Two")
        self.assertIsNone(chapter_two.pdf_page_index)
        self.assertEqual(chapter_two.source, "crossref")

    def test_filters_structural_entries_from_both_lists(self):
        outline = [
            ChapterCandidate(title="Part I", pdf_page_index=0, source="outline"),
            ChapterCandidate(title="Chapter One", pdf_page_index=10, source="outline"),
        ]
        metadata = [ChapterCandidate(title="Chapter One", source="crossref")]
        result = merge_candidates(outline, metadata)
        self.assertEqual([c.title for c in result], ["Chapter One"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_evidence_fusion.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.services.chapter_evidence.fusion'`

- [ ] **Step 3: Write the implementation**

```python
# backend/services/chapter_evidence/fusion.py
"""Fusion of candidate chapter lists from multiple strategies. See design
spec docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md
section 6.
"""

from dataclasses import replace

from rapidfuzz import fuzz

from backend.services.chapter_common import _is_back_matter, _is_part_divider
from backend.services.chapter_evidence.types import ChapterCandidate

_ALIGN_SCORE_THRESHOLD = 70.0


def _align(list_a: list[ChapterCandidate], list_b: list[ChapterCandidate]) -> list[tuple[int, int]]:
    """Returns index pairs (i, j) into list_a/list_b that fuzzy-match on
    title, honoring a monotonic-order constraint: once (i, j) is matched,
    no later pair may use an index <= j from list_b. Greedy, processes
    list_a in order -- mirrors the "TOC listing order is book order"
    constraint chapter_segmentation.py's _locate_toc_entries already uses.
    """
    pairs: list[tuple[int, int]] = []
    last_j = -1
    for i, a in enumerate(list_a):
        best_j = None
        best_score = 0.0
        for j in range(last_j + 1, len(list_b)):
            score = fuzz.token_sort_ratio(a.title.lower(), list_b[j].title.lower())
            if score >= _ALIGN_SCORE_THRESHOLD and score > best_score:
                best_score = score
                best_j = j
        if best_j is not None:
            pairs.append((i, best_j))
            last_j = best_j
    return pairs


def _filter_structural(candidates: list[ChapterCandidate]) -> list[ChapterCandidate]:
    return [c for c in candidates if not _is_part_divider(c.title) and not _is_back_matter(c.title)]


def _merge_two_metadata_lists(
    primary: list[ChapterCandidate], secondary: list[ChapterCandidate],
) -> list[ChapterCandidate]:
    pairs = _align(primary, secondary)
    matched_primary = {i for i, _ in pairs}
    matched_secondary = {j for _, j in pairs}
    merged: list[ChapterCandidate] = []
    for i, j in pairs:
        a, b = primary[i], secondary[j]
        winner = a if a.metadata_confidence >= b.metadata_confidence else b
        if a.chapter_doi and b.chapter_doi and a.chapter_doi == b.chapter_doi:
            winner = replace(winner, metadata_confidence=1.0)
        merged.append(winner)
    merged.extend(c for i, c in enumerate(primary) if i not in matched_primary)
    merged.extend(c for j, c in enumerate(secondary) if j not in matched_secondary)
    return merged


def merge_metadata_sources(strategy_results: list[list[ChapterCandidate]]) -> list[ChapterCandidate]:
    """Consolidates candidate lists from multiple MetadataStrategy
    instances, in priority order (earlier lists win ties), into one list.
    See design spec section 6.1. With a single non-empty source, returns it
    unchanged.
    """
    non_empty = [r for r in strategy_results if r]
    if not non_empty:
        return []
    result = list(non_empty[0])
    for next_list in non_empty[1:]:
        result = _merge_two_metadata_lists(result, next_list)
    return result


def merge_candidates(
    outline_candidates: list[ChapterCandidate],
    metadata_candidates: list[ChapterCandidate],
) -> list[ChapterCandidate]:
    """Merges the outline's direct-localization candidates with the
    (already consolidated) metadata candidates. See design spec section 6.2.
    """
    outline_filtered = _filter_structural(outline_candidates)
    metadata_filtered = _filter_structural(metadata_candidates)

    if not outline_filtered:
        return metadata_filtered
    if not metadata_filtered:
        return outline_filtered

    pairs = _align(outline_filtered, metadata_filtered)
    matched_outline = {i for i, _ in pairs}
    matched_metadata = {j for _, j in pairs}

    merged: list[ChapterCandidate] = []
    for i, j in pairs:
        outline_entry = outline_filtered[i]
        metadata_entry = metadata_filtered[j]
        merged.append(replace(
            metadata_entry,
            pdf_page_index=outline_entry.pdf_page_index,
            source=f"outline+{metadata_entry.source}",
        ))
    merged.extend(c for i, c in enumerate(outline_filtered) if i not in matched_outline)
    merged.extend(c for j, c in enumerate(metadata_filtered) if j not in matched_metadata)

    merged.sort(key=lambda c: c.pdf_page_index if c.pdf_page_index is not None else 10 ** 9)
    return merged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_evidence_fusion.py -q`
Expected: PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_evidence/fusion.py backend/tests/test_chapter_evidence_fusion.py
git commit -m "feat: add two-stage fusion of outline/Crossref/Zotero-catalog candidates"
```

---

### Task 8: `analyze_attachment_with_strategies` orchestration

Adds the new orchestration function, `build_book_context` helper, and the `_OUTLINE_CONFIDENCE` constant to `backend/services/chapter_segmentation.py`. Does **not** modify `analyze_attachment` or `analyze_attachment_with_llm_fallback`.

**Files:**
- Modify: `backend/services/chapter_segmentation.py`
- Create: `backend/tests/test_chapter_segmentation_strategies.py`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_chapter_segmentation_strategies.py
"""Unit tests for backend.services.chapter_segmentation.analyze_attachment_with_strategies
and build_book_context. See design spec
docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md
sections 6 and 8.
"""

import io
import unittest

from pypdf import PdfWriter

from backend.services.chapter_evidence.types import BookContext, ChapterCandidate
from backend.services.chapter_segmentation import (
    analyze_attachment,
    analyze_attachment_with_strategies,
    build_book_context,
)


def _blank_pdf(num_pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _pdf_with_outline(num_pages: int, entries: list[tuple[str, int]]) -> bytes:
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    for title, page_number in entries:
        writer.add_outline_item(title, page_number)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class _FakeMetadataStrategy:
    """Minimal stand-in for the MetadataStrategy protocol -- structurally
    identical to CrossrefMetadataStrategy/ZoteroCatalogMetadataStrategy's
    own applicable()/fetch() shape, used here to isolate orchestration
    tests from real HTTP/library-index construction."""

    def __init__(self, candidates: list[ChapterCandidate]):
        self._candidates = candidates

    def applicable(self, context: BookContext) -> bool:
        return bool(self._candidates)

    async def fetch(self, context: BookContext) -> list[ChapterCandidate]:
        return self._candidates


def _context(**overrides) -> BookContext:
    defaults = dict(item_key="B1", isbn=None, title="Some Book", editors=(), publisher=None, year=None)
    defaults.update(overrides)
    return BookContext(**defaults)


_FILLER = "Unrelated body filler text, nothing chapter-related in this passage at all."

# 20 pages, chapters starting at indices 5 and 12 -- deliberately well
# outside _toc_scan_indices(pages)'s front/back exclusion zone (indices
# {0,1,2,19} for a 20-page document), matching the same padding convention
# backend/tests/test_chapter_segmentation.py's own
# test_llm_toc_extraction_fires_when_heuristic_finds_nothing already uses
# and explains: a chapter starting inside that scan zone can never be
# content-search-located, since _locate_toc_entries excludes those pages
# from candidate consideration entirely.
_TWO_CHAPTER_PAGES = [
    _FILLER,  # 0
    _FILLER,  # 1
    _FILLER,  # 2
    _FILLER,  # 3
    _FILLER,  # 4
    "Introduction\nJane Author\n\nBody text opening the chapter.\n\n1",  # 5
    "...continued introduction text with real body content here.\n\n2",  # 6
    "...more continued introduction text with real body content.\n\n3",  # 7
    "...final continued introduction text with real body content.\n\n4",  # 8
    _FILLER,  # 9
    _FILLER,  # 10
    _FILLER,  # 11
    "Comparing Citation Styles\n\nJohn Smith\n\nBody text opening this chapter.\n\n5",  # 12
    "...continued citation styles text with real body content here.\n\n6",  # 13
    "...more continued citation styles text with real body content.\n\n7",  # 14
    "...final continued citation styles text with real body content.\n\n8",  # 15
    _FILLER,  # 16
    _FILLER,  # 17
    _FILLER,  # 18
    _FILLER,  # 19
]


class TestAnalyzeAttachmentWithStrategiesFallback(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_to_analyze_attachment_when_all_strategies_empty(self):
        pages = ["Just filler prose, nothing chapter-related here at all. " * 5] * 3
        pdf_bytes = _blank_pdf(3)
        result = await analyze_attachment_with_strategies(
            pages, pdf_bytes, _context(), _FakeMetadataStrategy([]), crossref_strategy=None,
        )
        expected = analyze_attachment(pages)
        self.assertEqual(result["chapters"], expected["chapters"])
        self.assertEqual(result["diagnostics"]["strategies_used"], [])
        self.assertEqual(result["diagnostics"]["outline_candidates_found"], 0)


class TestAnalyzeAttachmentWithStrategiesOutlineOnly(unittest.IsolatedAsyncioTestCase):
    async def test_outline_only_uses_direct_localization_and_fixed_confidence(self):
        pdf_bytes = _pdf_with_outline(20, [("Introduction", 5), ("Comparing Citation Styles", 12)])
        result = await analyze_attachment_with_strategies(
            _TWO_CHAPTER_PAGES, pdf_bytes, _context(), _FakeMetadataStrategy([]), crossref_strategy=None,
        )
        chapters = sorted(result["chapters"], key=lambda c: c["pdf_start_index"])
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0]["title"], "Introduction")
        self.assertEqual(chapters[0]["pdf_start_index"], 5)
        self.assertEqual(chapters[0]["source"], "outline")
        self.assertEqual(chapters[0]["confidence"], 0.98)
        self.assertEqual(chapters[1]["pdf_start_index"], 12)
        self.assertEqual(result["diagnostics"]["strategies_used"], ["outline"])


class TestAnalyzeAttachmentWithStrategiesCrossrefOnly(unittest.IsolatedAsyncioTestCase):
    async def test_crossref_only_localizes_via_content_search(self):
        pdf_bytes = _blank_pdf(20)  # no outline
        crossref_candidates = [
            ChapterCandidate(
                title="Introduction", authors=("Jane Author",), printed_page_number=1,
                chapter_doi="10.1/ch1", source="crossref", metadata_confidence=1.0,
            ),
            ChapterCandidate(
                title="Comparing Citation Styles", authors=("John Smith",), printed_page_number=5,
                chapter_doi="10.1/ch2", source="crossref", metadata_confidence=1.0,
            ),
        ]
        result = await analyze_attachment_with_strategies(
            _TWO_CHAPTER_PAGES, pdf_bytes, _context(isbn="9783031466373"),
            _FakeMetadataStrategy([]), crossref_strategy=_FakeMetadataStrategy(crossref_candidates),
        )
        chapters = sorted(result["chapters"], key=lambda c: c["pdf_start_index"])
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0]["title"], "Introduction")
        self.assertEqual(chapters[0]["pdf_start_index"], 5)
        self.assertEqual(chapters[0]["source"], "crossref")
        self.assertGreater(chapters[0]["confidence"], 0.0)
        self.assertEqual(result["diagnostics"]["strategies_used"], ["crossref"])


class TestAnalyzeAttachmentWithStrategiesZoteroCatalogOnly(unittest.IsolatedAsyncioTestCase):
    async def test_weak_catalog_confidence_pulls_final_confidence_down(self):
        pdf_bytes = _blank_pdf(8)  # no outline
        catalog_candidates = [
            ChapterCandidate(
                title="Introduction", authors=("Jane Author",), printed_page_number=1,
                source="zotero_catalog", metadata_confidence=0.6,
            ),
            ChapterCandidate(
                title="Comparing Citation Styles", authors=("John Smith",), printed_page_number=5,
                source="zotero_catalog", metadata_confidence=0.6,
            ),
        ]
        result = await analyze_attachment_with_strategies(
            _TWO_CHAPTER_PAGES, pdf_bytes, _context(),
            _FakeMetadataStrategy(catalog_candidates), crossref_strategy=None,
        )
        chapters = sorted(result["chapters"], key=lambda c: c["pdf_start_index"])
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0]["source"], "zotero_catalog")
        # match_confidence's own ceiling is 1.0, so 0.6 * match_confidence <= 0.6
        self.assertLessEqual(chapters[0]["confidence"], 0.6)
        self.assertEqual(result["diagnostics"]["strategies_used"], ["zotero_catalog"])


class TestBuildBookContext(unittest.TestCase):
    def test_builds_context_from_book_data(self):
        book_data = {
            "key": "BOOK1", "ISBN": "978-3-031-46637-3", "title": "Some Book",
            "publisher": "Acme Press", "date": "2020-01",
            "creators": [
                {"creatorType": "editor", "firstName": "Jane", "lastName": "Editor"},
                {"creatorType": "author", "firstName": "Not", "lastName": "AnEditor"},
            ],
        }
        context = build_book_context(book_data)
        self.assertEqual(context.item_key, "BOOK1")
        self.assertEqual(context.isbn, "9783031466373")
        self.assertEqual(context.title, "Some Book")
        self.assertEqual(context.publisher, "Acme Press")
        self.assertEqual(context.year, 2020)
        self.assertEqual(context.editors, ("Jane Editor",))

    def test_handles_missing_fields(self):
        context = build_book_context({"key": "BOOK2", "title": "Untitled"})
        self.assertIsNone(context.isbn)
        self.assertIsNone(context.publisher)
        self.assertIsNone(context.year)
        self.assertEqual(context.editors, ())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_segmentation_strategies.py -q`
Expected: FAIL with `ImportError: cannot import name 'analyze_attachment_with_strategies' from 'backend.services.chapter_segmentation'`

- [ ] **Step 3: Add the new imports to `chapter_segmentation.py`**

Add these imports near the top of `backend/services/chapter_segmentation.py`, alongside the existing `backend.services.*` imports:

```python
from backend.services.chapter_evidence.fusion import merge_candidates, merge_metadata_sources
from backend.services.chapter_evidence.outline_strategy import extract_outline_candidates
from backend.services.chapter_evidence.crossref_strategy import normalize_isbn
from backend.services.chapter_evidence.types import BookContext, ChapterCandidate, MetadataStrategy
```

- [ ] **Step 4: Add `build_book_context` and `analyze_attachment_with_strategies`**

Add this code at the end of `chapter_segmentation.py`, after `analyze_attachment_with_llm_fallback` and before `run()`:

```python
_OUTLINE_CONFIDENCE = 0.98  # exceeds chapter_upload.py's calibrated
# confidence_threshold (0.90), so outline-sourced chapters typically route
# straight to commit rather than the review queue. See design spec 2026-08-01
# section 7.


def build_book_context(book_data: dict) -> BookContext:
    """Builds a BookContext from a Zotero `book` item's `data` dict, for
    passing into analyze_attachment_with_strategies. See design spec
    2026-08-01 section 3."""
    editors = tuple(
        f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
        for c in book_data.get("creators", [])
        if c.get("creatorType") == "editor"
    )
    return BookContext(
        item_key=book_data["key"],
        isbn=normalize_isbn(book_data.get("ISBN", "")),
        title=book_data.get("title", ""),
        editors=editors,
        publisher=book_data.get("publisher") or None,
        year=year_from_date(book_data.get("date")),
    )


def _candidate_to_toc_entry(candidate: ChapterCandidate) -> TocEntry:
    return TocEntry(
        title=candidate.title,
        authors=candidate.authors,
        printed_page_number=candidate.printed_page_number if candidate.printed_page_number is not None else -1,
        source_page_index=-1,
    )


async def analyze_attachment_with_strategies(
    pages: list[str],
    file_bytes: bytes,
    context: BookContext,
    zotero_catalog_strategy: MetadataStrategy,
    crossref_strategy: Optional[MetadataStrategy] = None,
    *,
    llm_service: Optional[LLMService] = None,
) -> dict:
    """Runs the PDF-outline read, (if given) the Crossref-by-ISBN strategy,
    and the Zotero-catalog strategy, merges their results (design spec
    2026-08-01 sections 6 and 8). If the merge is non-empty, localizes any
    candidate still missing pdf_page_index via the existing
    locate_chapter_start, builds chapter dicts via the existing
    _chapters_from_located, and overrides confidence per section 7. If the
    merge is empty, delegates to analyze_attachment_with_llm_fallback (when
    llm_service is given) or analyze_attachment -- i.e. today's behavior,
    unchanged, for any book none of the three new strategies cover.
    """
    outline_candidates = extract_outline_candidates(file_bytes)

    crossref_candidates: list[ChapterCandidate] = []
    if crossref_strategy is not None and crossref_strategy.applicable(context):
        crossref_candidates = await crossref_strategy.fetch(context)

    zotero_catalog_candidates: list[ChapterCandidate] = []
    if zotero_catalog_strategy.applicable(context):
        zotero_catalog_candidates = await zotero_catalog_strategy.fetch(context)

    metadata_candidates = merge_metadata_sources([crossref_candidates, zotero_catalog_candidates])
    merged = merge_candidates(outline_candidates, metadata_candidates)

    diagnostics_extra = {
        "outline_candidates_found": len(outline_candidates),
        "crossref_candidates_found": len(crossref_candidates),
        "crossref_isbn_used": context.isbn if crossref_strategy is not None else None,
        "zotero_catalog_candidates_found": len(zotero_catalog_candidates),
    }

    if not merged:
        if llm_service is not None:
            result = await analyze_attachment_with_llm_fallback(pages, llm_service)
        else:
            result = analyze_attachment(pages)
        result["diagnostics"].update(diagnostics_extra)
        result["diagnostics"]["strategies_used"] = []
        return result

    pre_located = [c for c in merged if c.pdf_page_index is not None]
    needs_location = [c for c in merged if c.pdf_page_index is None]

    entry_to_candidate: dict[TocEntry, ChapterCandidate] = {}
    toc_entries: list[TocEntry] = []
    for candidate in needs_location:
        entry = _candidate_to_toc_entry(candidate)
        toc_entries.append(entry)
        entry_to_candidate[entry] = candidate

    exclude_indices = _toc_scan_indices(pages)
    located, _unlocated, non_content_pages = _locate_toc_entries(pages, toc_entries, exclude_indices=exclude_indices)

    for candidate in pre_located:
        entry = _candidate_to_toc_entry(candidate)
        entry_to_candidate[entry] = candidate
        located.append((
            entry,
            ChapterStartMatch(index=candidate.pdf_page_index, score=100.0, margin=_CONFIDENCE_MARGIN_SATURATION),
        ))

    located.sort(key=lambda pair: pair[1].index)
    entry_source = {entry: candidate.source for entry, candidate in entry_to_candidate.items()}
    index_to_confidence = {match.index: entry_to_candidate[entry].metadata_confidence for entry, match in located}

    chapters = _chapters_from_located(pages, located, entry_source=entry_source, non_content_pages=non_content_pages)
    for chapter in chapters:
        if chapter["source"].startswith("outline"):
            chapter["confidence"] = _OUTLINE_CONFIDENCE
        else:
            mc = index_to_confidence.get(chapter["pdf_start_index"], 1.0)
            chapter["confidence"] = round(mc * chapter["confidence"], 2)

    diagnostics_extra["strategies_used"] = sorted({c.source for c in merged})
    return {
        "total_pdf_pages": len(pages),
        "segmentation_confidence": "high" if chapters else "low",
        "chapters": chapters,
        "diagnostics": {
            "toc_pages_scanned": sorted(exclude_indices),
            "toc_matches_found": len(toc_entries) + len(pre_located),
            "toc_matches_located": len(located),
            **diagnostics_extra,
        },
    }
```

Also add `year_from_date` to the `chapter_common` import line added in Task 1 (it currently only imports `_is_back_matter, _is_part_divider, _normalized_title` — extend it):

```python
from backend.services.chapter_common import _is_back_matter, _is_part_divider, _normalized_title, year_from_date
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_segmentation_strategies.py -q`
Expected: PASS (6 tests)

- [ ] **Step 6: Run the full existing chapter-segmentation suite to confirm no regression**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -q`
Expected: PASS, same as baseline (this task only adds new code, `analyze_attachment`/`analyze_attachment_with_llm_fallback` are untouched)

- [ ] **Step 7: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation_strategies.py
git commit -m "feat: add analyze_attachment_with_strategies orchestration function"
```

---

### Task 9: `run()` integration

**Files:**
- Modify: `backend/services/chapter_segmentation.py`
- Modify: `backend/tests/test_chapter_segmentation.py`

**Depends on:** `backend/zotero/library_cache.py` must already exist (see
`docs/superpowers/plans/2026-08-01-zotero-library-sync-cache.md`) before
this task can be implemented — `run()` below is updated to fetch the
library's item list through a `ZoteroLibraryCache` instead of calling
`zotero_client.get_library_items_since(...)` directly, so every `run()`
invocation after the first for a given library does a cheap incremental
sync instead of a full re-download (see
`docs/superpowers/specs/2026-08-01-zotero-library-sync-cache-design.md`).

- [ ] **Step 1: Fix existing fixtures, then write the failing tests**

`ZoteroLibraryCache` reads a top-level `item["key"]` (and, for version
bookkeeping, `item["version"]`) from each fetched item — the real Zotero API
always includes both alongside the nested `item["data"]["key"]` this test
file's fixtures have used so far, since `run()` itself never previously
needed the top-level copies. Once Step 3 wires the cache into `run()`,
`cache.sync()` will raise `KeyError: 'key'` on any fixture missing it, so
fix every existing `TestRun` fixture first. Find them with:

```bash
grep -n '"data": {"key": "BOOK' backend/tests/test_chapter_segmentation.py
```

Replace each of the 9 matching lines exactly as follows (one per existing
test — the rest of each test is unchanged):

```python
# test_skips_already_linked_book -- before:
            {"data": {"key": "BOOK0001", "itemType": "book", "extra": "X-Contains: groups/1:CH01"}},
# after:
            {"key": "BOOK0001", "version": 1, "data": {"key": "BOOK0001", "itemType": "book", "extra": "X-Contains: groups/1:CH01"}},

# test_processes_unlinked_book -- before:
            {"data": {"key": "BOOK0002", "itemType": "book", "extra": ""}},
# after:
            {"key": "BOOK0002", "version": 1, "data": {"key": "BOOK0002", "itemType": "book", "extra": ""}},

# test_needs_ocr_attachment_is_upserted_into_review_queue -- before:
            {"data": {"key": "BOOK9", "itemType": "book", "extra": ""}},
# after:
            {"key": "BOOK9", "version": 1, "data": {"key": "BOOK9", "itemType": "book", "extra": ""}},

# test_uses_llm_fallback_when_llm_service_provided -- before:
            {"data": {"key": "BOOK0003", "itemType": "book", "extra": ""}},
# after:
            {"key": "BOOK0003", "version": 1, "data": {"key": "BOOK0003", "itemType": "book", "extra": ""}},

# test_reads_ocr_cache_when_no_text_layer_and_cache_hit -- before:
            {"data": {"key": "BOOK0004", "itemType": "book", "extra": ""}},
# after:
            {"key": "BOOK0004", "version": 1, "data": {"key": "BOOK0004", "itemType": "book", "extra": ""}},

# test_still_reports_needs_ocr_when_no_cache_dir_given -- before:
            {"data": {"key": "BOOK0005", "itemType": "book", "extra": ""}},
# after:
            {"key": "BOOK0005", "version": 1, "data": {"key": "BOOK0005", "itemType": "book", "extra": ""}},

# test_analysis_cache_hit_skips_download -- before:
            {"data": {"key": "BOOK0006", "itemType": "book", "extra": ""}},
# after:
            {"key": "BOOK0006", "version": 1, "data": {"key": "BOOK0006", "itemType": "book", "extra": ""}},

# test_analysis_cache_miss_saves_result -- before:
            {"data": {"key": "BOOK0007", "itemType": "book", "extra": ""}},
# after:
            {"key": "BOOK0007", "version": 1, "data": {"key": "BOOK0007", "itemType": "book", "extra": ""}},

# test_no_cache_dir_never_touches_analysis_cache -- before:
            {"data": {"key": "BOOK0008", "itemType": "book", "extra": ""}},
# after:
            {"key": "BOOK0008", "version": 1, "data": {"key": "BOOK0008", "itemType": "book", "extra": ""}},
```

Also update `TestRun.setUp` so every test gets its own isolated Zotero item
cache, inside the same per-test temp directory already used for
`review_queue_path` (otherwise every test in the class would share one
on-disk cache under the real default `data/zotero_cache/` path):

```python
class TestRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        get_settings().review_queue_path = _TestPath(self.tmp.name) / "review_queue.json"
        get_settings().zotero_cache_path = _TestPath(self.tmp.name) / "zotero_cache"
```

Now add these test methods to the `TestRun` class (alongside the 9 existing
ones, now fixed above) — each fixture already includes the top-level
`key`/`version` fields `ZoteroLibraryCache` requires:

```python
    def test_uses_outline_strategy_when_present(self):
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK3", "version": 1, "data": {"key": "BOOK3", "itemType": "book", "extra": "", "title": "Some Book"}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT3", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        pdf_bytes = _pdf_with_outline(20, [("Introduction", 5), ("Comparing Citation Styles", 12)])
        zotero_client.get_attachment_file.return_value = pdf_bytes

        with unittest.mock.patch(
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
            return_value=_TWO_CHAPTER_PAGES,
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
                enable_crossref=False,
            ))
        chapters = result["attachments"][0]["chapters"]
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0]["source"], "outline")

    def test_builds_zotero_catalog_index_from_unlinked_book_sections(self):
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK4", "version": 1, "data": {"key": "BOOK4", "itemType": "book", "extra": "", "title": "Some Book"}},
            {"key": "CH1", "version": 1, "data": {
                "key": "CH1", "itemType": "bookSection", "extra": "",
                "title": "Introduction", "bookTitle": "Some Book", "pages": "1-20",
                "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Author"}],
            }},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT4", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        pdf_bytes = _blank_pdf(8)  # no outline -- forces content-search localization
        zotero_client.get_attachment_file.return_value = pdf_bytes

        with unittest.mock.patch(
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
            return_value=_TWO_CHAPTER_PAGES,
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
                enable_crossref=False,
            ))
        chapters = result["attachments"][0]["chapters"]
        titles = {c["title"]: c for c in chapters}
        self.assertIn("Introduction", titles)
        self.assertEqual(titles["Introduction"]["source"], "zotero_catalog")

    def test_already_linked_book_section_is_not_offered_as_candidate(self):
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK5", "version": 1, "data": {"key": "BOOK5", "itemType": "book", "extra": "", "title": "Some Book"}},
            {"key": "CH2", "version": 1, "data": {
                "key": "CH2", "itemType": "bookSection",
                "extra": "X-Contained-By: groups/1:OTHERBOOK",
                "title": "Introduction", "bookTitle": "Some Book", "pages": "1-20",
                "creators": [],
            }},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT5", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        pdf_bytes = _blank_pdf(3)
        zotero_client.get_attachment_file.return_value = pdf_bytes

        with unittest.mock.patch(
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["Just filler prose, nothing chapter-related here at all. " * 5] * 3,
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
                enable_crossref=False,
            ))
        self.assertEqual(result["attachments"][0]["diagnostics"]["strategies_used"], [])

    def test_enable_crossref_false_skips_crossref_entirely(self):
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK6", "version": 1, "data": {"key": "BOOK6", "itemType": "book", "extra": "", "title": "Some Book", "ISBN": "9783031466373"}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT6", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        pdf_bytes = _blank_pdf(3)
        zotero_client.get_attachment_file.return_value = pdf_bytes

        with unittest.mock.patch(
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["Just filler prose, nothing chapter-related here at all. " * 5] * 3,
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
                enable_crossref=False,
            ))
        self.assertIsNone(result["attachments"][0]["diagnostics"]["crossref_isbn_used"])
```

Add these imports/helpers near the top of `backend/tests/test_chapter_segmentation.py` (alongside the existing imports):

```python
import io as _io
from pypdf import PdfWriter as _PdfWriter


def _blank_pdf(num_pages: int) -> bytes:
    writer = _PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    buf = _io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _pdf_with_outline(num_pages: int, entries: list[tuple[str, int]]) -> bytes:
    writer = _PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    for title, page_number in entries:
        writer.add_outline_item(title, page_number)
    buf = _io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


_FILLER = "Unrelated body filler text, nothing chapter-related in this passage at all."

# Same 20-page, indices-5-and-12 fixture as
# test_chapter_segmentation_strategies.py's _TWO_CHAPTER_PAGES (Task 8) --
# kept as a separate copy here since these two test files don't import from
# each other. See that constant's comment for why the chapters must sit
# outside _toc_scan_indices's front/back exclusion zone.
_TWO_CHAPTER_PAGES = [
    _FILLER,  # 0
    _FILLER,  # 1
    _FILLER,  # 2
    _FILLER,  # 3
    _FILLER,  # 4
    "Introduction\nJane Author\n\nBody text opening the chapter.\n\n1",  # 5
    "...continued introduction text with real body content here.\n\n2",  # 6
    "...more continued introduction text with real body content.\n\n3",  # 7
    "...final continued introduction text with real body content.\n\n4",  # 8
    _FILLER,  # 9
    _FILLER,  # 10
    _FILLER,  # 11
    "Comparing Citation Styles\n\nJohn Smith\n\nBody text opening this chapter.\n\n5",  # 12
    "...continued citation styles text with real body content here.\n\n6",  # 13
    "...more continued citation styles text with real body content.\n\n7",  # 14
    "...final continued citation styles text with real body content.\n\n8",  # 15
    _FILLER,  # 16
    _FILLER,  # 17
    _FILLER,  # 18
    _FILLER,  # 19
]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -k "TestRun" -q`
Expected: FAIL — `analyze_run()` does not yet accept `enable_crossref` or `zotero_cache_dir`

- [ ] **Step 3: Update `run()`**

In `backend/services/chapter_segmentation.py`, add these imports near the top:

```python
import httpx

from backend.services.chapter_evidence.crossref_strategy import CrossrefMetadataStrategy
from backend.services.chapter_evidence.zotero_catalog_strategy import ZoteroCatalogMetadataStrategy
from backend.zotero.library_cache import ZoteroLibraryCache
```

Replace the whole `run()` function with:

```python
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
    llm_service: Optional[LLMService] = None,
    ocr_cache_dir: Optional[Path] = None,
    enable_crossref: bool = True,
    crossref_cache_dir: Optional[Path] = None,
    crossref_contact_email: Optional[str] = None,
    zotero_cache_dir: Optional[Path] = None,
) -> dict:
    """Core logic for script 1 (analyze_book_chapters). Scans `book`-type
    items in the library (or the explicit `item_keys` list), skips already-
    linked ones unless `relink`, downloads each PDF attachment, and runs
    analyze_attachment_with_strategies on its page text. See design spec
    2026-07-24 section 5, 2026-08-01 section 9, and
    2026-08-01-zotero-library-sync-cache-design.md for the ZoteroLibraryCache
    used below to fetch the item list.
    """
    if zotero_cache_dir is None:
        zotero_cache_dir = get_settings().zotero_cache_path
    zotero_cache = ZoteroLibraryCache(
        client=zotero_client,
        library_id=library_id,
        library_type=library_type,
        cache_path=zotero_cache_dir,
    )
    try:
        items = await zotero_cache.get_all_items()
    finally:
        zotero_cache.close()
    books = [i for i in items if i["data"].get("itemType") == "book"]
    if item_keys is not None:
        wanted = set(item_keys)
        books = [b for b in books if b["data"]["key"] in wanted]
    if not relink:
        books = [b for b in books if not parse_links(b["data"].get("extra", "")).contains]
    if max_items is not None:
        books = books[:max_items]

    book_sections_by_title: dict[str, list[dict]] = {}
    for item in items:
        if item["data"].get("itemType") != "bookSection":
            continue
        if parse_links(item["data"].get("extra", "")).contained_by:
            continue
        title = item["data"].get("bookTitle", "").strip()
        if title:
            book_sections_by_title.setdefault(title, []).append(item)
    zotero_catalog_strategy = ZoteroCatalogMetadataStrategy(book_sections_by_title)

    if crossref_cache_dir is None:
        crossref_cache_dir = get_settings().crossref_cache_path
    if crossref_contact_email is None:
        crossref_contact_email = get_settings().crossref_contact_email

    attachments_out: list[dict] = []
    total = len(books) or 1
    analysis_mode = "strategies_llm" if llm_service is not None else "strategies"

    async with httpx.AsyncClient() as http_client:
        crossref_strategy = (
            CrossrefMetadataStrategy(http_client, crossref_cache_dir, crossref_contact_email)
            if enable_crossref else None
        )
        for i, book in enumerate(books):
            item_key = book["data"]["key"]
            progress_callback(i / total, f"Analyzing {item_key} ({i + 1}/{total})")

            children = await zotero_client.get_item_children(library_id, item_key, library_type=library_type)
            pdf_attachments = [c for c in children if c["data"].get("contentType") == "application/pdf"]
            if not pdf_attachments:
                continue
            attachment_key = pdf_attachments[0]["data"]["key"]
            attachment_version = pdf_attachments[0]["data"].get("version", 0)

            if ocr_cache_dir is not None:
                cached_entry = load_cached_analysis(ocr_cache_dir, item_key, attachment_key, attachment_version, analysis_mode)
                if cached_entry is not None:
                    attachments_out.append(cached_entry)
                    continue

            file_bytes = await zotero_client.get_attachment_file(library_id, attachment_key, library_type=library_type)
            if not file_bytes:
                continue

            pages = extract_page_texts_from_pdf_bytes(file_bytes)
            has_text_layer = sum(len(p.strip()) for p in pages) > 100

            if not has_text_layer and ocr_cache_dir is not None:
                content_hash = hashlib.sha256(file_bytes).hexdigest()
                cached = load_cached_ocr(ocr_cache_dir, content_hash)
                if cached is not None:
                    pages = cached["pages"]
                    has_text_layer = sum(len(p.strip()) for p in pages) > 100

            if not has_text_layer:
                attachments_out.append({
                    "item_key": item_key,
                    "attachment_key": attachment_key,
                    "has_text_layer": False,
                    "needs_ocr": True,
                })
                continue

            book_context = build_book_context(book["data"])
            analysis = await analyze_attachment_with_strategies(
                pages, file_bytes, book_context, zotero_catalog_strategy,
                crossref_strategy=crossref_strategy, llm_service=llm_service,
            )
            result_entry = {
                "item_key": item_key,
                "attachment_key": attachment_key,
                "has_text_layer": True,
                "needs_ocr": False,
                **analysis,
            }
            if ocr_cache_dir is not None:
                save_analysis_cache(ocr_cache_dir, item_key, attachment_key, attachment_version, analysis_mode, result_entry)
            attachments_out.append(result_entry)

    ocr_entries = [
        {
            "queue_id": f"ocr:{a['attachment_key']}",
            "type": "ocr",
            "bucket": "review",
            "payload": {"book_key": a["item_key"], "attachment_key": a["attachment_key"]},
        }
        for a in attachments_out
        if a.get("needs_ocr")
    ]
    if ocr_entries:
        review_queue_store.upsert_many(get_settings().review_queue_path, slug, ocr_entries)

    progress_callback(1.0, "Done")
    return {"slug": slug, "attachments": attachments_out}
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -k "TestRun" -q`
Expected: PASS (all `TestRun` tests, including the 4 new ones)

- [ ] **Step 5: Run the full chapter-segmentation suite to confirm no regression**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -q`
Expected: PASS, same total as before this task's additions plus 4 new tests

- [ ] **Step 6: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "feat: wire outline/Crossref/Zotero-catalog strategies and ZoteroLibraryCache into run()"
```

---

### Task 10: CLI flags (`scripts/analyze_book_chapters.py`)

**Files:**
- Modify: `scripts/analyze_book_chapters.py`

- [ ] **Step 1: Add the new flags and pass them through**

In `scripts/analyze_book_chapters.py`, add to the argument parser (after the existing `--auto-select-model` block):

```python
    parser.add_argument(
        "--no-crossref",
        action="store_true",
        help="Disable the Crossref-by-ISBN chapter-lookup strategy (on by default -- "
             "free, cached, but makes an external network call per book with an ISBN)",
    )
    parser.add_argument(
        "--crossref-contact-email",
        default=None,
        help="Contact email sent as Crossref's 'mailto' polite-pool parameter (optional)",
    )
```

Update the `analyze_run(...)` call in `_main()` to pass the new flags:

```python
    result = await analyze_run(
        zotero_client=client,
        library_id=library_id,
        library_type=library_type,
        slug=args.library_slug,
        item_keys=item_keys,
        max_items=args.max_items,
        relink=args.relink,
        progress_callback=on_progress,
        llm_service=llm_service,
        ocr_cache_dir=Path(args.cache_dir),
        enable_crossref=not args.no_crossref,
        crossref_contact_email=args.crossref_contact_email,
    )
```

- [ ] **Step 2: Verify the CLI parses the new flags**

Run: `uv run python scripts/analyze_book_chapters.py --help`
Expected: output includes `--no-crossref` and `--crossref-contact-email` with the help text above

- [ ] **Step 3: Commit**

```bash
git add scripts/analyze_book_chapters.py
git commit -m "feat: add --no-crossref/--crossref-contact-email flags to analyze_book_chapters CLI"
```

---

### Task 11: API changes (`backend/api/chapter_linking.py`)

**Files:**
- Modify: `backend/api/chapter_linking.py`
- Modify: `backend/tests/test_chapter_linking_api.py`

- [ ] **Step 1: Write the failing tests**

Add these test methods to the existing `TestAnalyzeEndpoint` class in `backend/tests/test_chapter_linking_api.py`:

```python
    @patch("backend.api.chapter_linking.ZoteroWebAPI")
    @patch("backend.api.chapter_linking.analyze_run", new_callable=AsyncMock)
    def test_enable_crossref_defaults_to_true(self, mock_run, mock_web_api):
        mock_run.return_value = {"slug": "groups/1", "attachments": []}
        response = self.client.post(
            "/api/chapter-linking/analyze",
            json={"library_slug": "groups/1", "api_key": "fake-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(mock_run.call_args.kwargs["enable_crossref"])

    @patch("backend.api.chapter_linking.ZoteroWebAPI")
    @patch("backend.api.chapter_linking.analyze_run", new_callable=AsyncMock)
    def test_enable_crossref_can_be_disabled(self, mock_run, mock_web_api):
        mock_run.return_value = {"slug": "groups/1", "attachments": []}
        response = self.client.post(
            "/api/chapter-linking/analyze",
            json={"library_slug": "groups/1", "api_key": "fake-key", "enable_crossref": False},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(mock_run.call_args.kwargs["enable_crossref"])

    @patch("backend.api.chapter_linking.ZoteroWebAPI")
    @patch("backend.api.chapter_linking.analyze_run", new_callable=AsyncMock)
    def test_crossref_contact_email_passed_through(self, mock_run, mock_web_api):
        mock_run.return_value = {"slug": "groups/1", "attachments": []}
        response = self.client.post(
            "/api/chapter-linking/analyze",
            json={"library_slug": "groups/1", "api_key": "fake-key", "crossref_contact_email": "me@example.com"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(mock_run.call_args.kwargs["crossref_contact_email"], "me@example.com")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py -k "crossref" -q`
Expected: FAIL — `AnalyzeRequest` has no field `enable_crossref` (422 response, not 200)

- [ ] **Step 3: Update `AnalyzeRequest` and the route handler**

In `backend/api/chapter_linking.py`, add two fields to `AnalyzeRequest`:

```python
class AnalyzeRequest(BaseModel):
    library_slug: str
    api_key: str
    item_keys: list[str] | None = None
    relink: bool = False
    max_items: int | None = None
    enable_llm_fallback: bool = False
    auto_select_model: bool = False
    ocr_cache_dir: str = "data/ocr_cache"
    enable_crossref: bool = True
    crossref_contact_email: str | None = None
```

Update the `analyze_run(...)` call inside `start_analyze`'s `_task()`:

```python
            result = await analyze_run(
                zotero_client=client,
                library_id=library_id,
                library_type=library_type,
                slug=request.library_slug,
                item_keys=request.item_keys,
                max_items=request.max_items,
                relink=request.relink,
                progress_callback=lambda p, m: tracker.update(job_id, progress=p, message=m),
                llm_service=llm_service,
                ocr_cache_dir=_Path(request.ocr_cache_dir),
                enable_crossref=request.enable_crossref,
                crossref_contact_email=request.crossref_contact_email,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py -q`
Expected: PASS (all existing tests plus the 3 new ones)

- [ ] **Step 5: Commit**

```bash
git add backend/api/chapter_linking.py backend/tests/test_chapter_linking_api.py
git commit -m "feat: expose enable_crossref/crossref_contact_email on the analyze API endpoint"
```

---

### Task 12: Evaluation script and README update

**Files:**
- Create: `scripts/evaluate_chapter_segmentation_strategies.py`
- Modify: `backend/evaluation/book-segmentation/README.md`

- [ ] **Step 1: Write the evaluation script**

```python
#!/usr/bin/env python3
# scripts/evaluate_chapter_segmentation_strategies.py
"""Runs the chapter-segmentation evaluation set (see backend/evaluation/
book-segmentation/) through analyze_attachment_with_strategies instead of
the pure-heuristic analyze_attachment, and prints the same precision/recall
table format backend/tests/test_chapter_segmentation_accuracy.py already
uses, plus per-book strategies_used diagnostics.

Not a pytest test -- makes real (free, cached) Crossref API calls per book:

    uv run python scripts/evaluate_chapter_segmentation_strategies.py

Pass --no-crossref to disable the Crossref lookup and see outline-only
numbers:

    uv run python scripts/evaluate_chapter_segmentation_strategies.py --no-crossref

See docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md section 12.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services.chapter_evidence.crossref_strategy import CrossrefMetadataStrategy
from backend.services.chapter_evidence.types import BookContext
from backend.services.chapter_evidence.zotero_catalog_strategy import ZoteroCatalogMetadataStrategy
from backend.services.chapter_segmentation import (
    analyze_attachment_with_strategies,
    extract_page_texts_from_pdf_bytes,
)

_EVAL_DIR = Path(__file__).resolve().parent.parent / "backend" / "evaluation" / "book-segmentation"


def _load_manifest_books() -> list[dict]:
    # Mirrors evaluate_chapter_segmentation_llm_fallback.py's identically-named
    # helper -- kept as a separate copy, same rationale (backend/tests/ is not
    # a runtime dependency of anything under scripts/).
    books = json.loads((_EVAL_DIR / "manifest.json").read_text(encoding="utf-8"))["books"]
    local_manifest_path = _EVAL_DIR / "manifest.local.json"
    if local_manifest_path.exists():
        books = books + json.loads(local_manifest_path.read_text(encoding="utf-8"))["books"]
    return books


def _available_books() -> list[tuple[Path, Path, dict]]:
    triples = []
    for book in _load_manifest_books():
        pdf_path = _EVAL_DIR / book["filename"]
        expected_path = _EVAL_DIR / (Path(book["filename"]).stem + ".expected.json")
        if pdf_path.exists() and expected_path.exists():
            triples.append((pdf_path, expected_path, book))
    return triples


async def _main(enable_crossref: bool) -> int:
    triples = _available_books()
    if not triples:
        print("No evaluation PDFs present -- run: uv run python scripts/fetch_evaluation_pdfs.py")
        return 1

    zotero_catalog_strategy = ZoteroCatalogMetadataStrategy({})  # no live library in this script

    async with httpx.AsyncClient() as http_client:
        crossref_strategy = (
            CrossrefMetadataStrategy(http_client, cache_dir=Path("data/crossref_cache"), contact_email=None)
            if enable_crossref else None
        )
        for pdf_path, expected_path, book in triples:
            expected = json.loads(expected_path.read_text(encoding="utf-8"))["chapters"]
            file_bytes = pdf_path.read_bytes()
            pages = extract_page_texts_from_pdf_bytes(file_bytes)
            # The evaluation manifest names each PDF after its own ISBN-13
            # (see backend/evaluation/book-segmentation/README.md), so the
            # filename stem doubles as the ISBN BookContext needs.
            isbn = Path(book["filename"]).stem
            context = BookContext(
                item_key=book["filename"], isbn=isbn, title=book["title"],
                editors=(), publisher=None, year=None,
            )
            result = await analyze_attachment_with_strategies(
                pages, file_bytes, context, zotero_catalog_strategy,
                crossref_strategy=crossref_strategy,
            )

            expected_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in expected}
            found_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in result["chapters"]}
            true_positives = expected_ranges & found_ranges

            precision = len(true_positives) / len(found_ranges) if found_ranges else 0.0
            recall = len(true_positives) / len(expected_ranges) if expected_ranges else 0.0
            diag = result["diagnostics"]
            print(
                f"{pdf_path.name}: precision={precision:.2f} recall={recall:.2f} "
                f"({len(true_positives)}/{len(found_ranges)} found, {len(true_positives)}/{len(expected_ranges)} expected) "
                f"strategies_used={diag.get('strategies_used')}"
            )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-crossref", action="store_true", help="Disable the Crossref lookup strategy")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(enable_crossref=not args.no_crossref)))
```

- [ ] **Step 2: Verify the script runs against local evaluation PDFs (if present)**

Run: `uv run python scripts/evaluate_chapter_segmentation_strategies.py --no-crossref`
Expected: either `No evaluation PDFs present -- run: uv run python scripts/fetch_evaluation_pdfs.py` (exit code 1, if PDFs haven't been fetched locally) or one `precision=... recall=...` line per available evaluation book (exit code 0) — either is an acceptable outcome for this step, since the evaluation PDFs are gitignored and may not be present in every environment.

- [ ] **Step 3: Add a short section to the evaluation README**

In `backend/evaluation/book-segmentation/README.md`, add this new subsection directly after the existing `### LLM-fallback evaluation` subsection (before `## Current results`):

```markdown
### Strategy-pipeline evaluation

`scripts/evaluate_chapter_segmentation_strategies.py` runs the same
evaluation set through `analyze_attachment_with_strategies` (see
`docs/superpowers/specs/2026-08-01-chapter-segmentation-strategy-pipeline-design.md`)
instead of the pure-heuristic `analyze_attachment` -- i.e. with the PDF
outline read and Crossref-by-ISBN lookup strategies active (the evaluation
manifest names each PDF after its own ISBN-13, which doubles as the ISBN
this script passes in). Not a pytest test -- makes real, free, cached
Crossref API calls:

```bash
uv run python scripts/evaluate_chapter_segmentation_strategies.py
```

Prints the same precision/recall table format as the harnesses above, plus
each book's `strategies_used` diagnostic. Run after any change to the
outline/Crossref/fusion logic to check whether the new strategies are
net-helpful on the real evaluation set, the same operational pattern the
LLM-fallback evaluation script above already established.
```

- [ ] **Step 4: Commit**

```bash
git add scripts/evaluate_chapter_segmentation_strategies.py backend/evaluation/book-segmentation/README.md
git commit -m "feat: add strategy-pipeline evaluation script"
```

---

## Final verification

Run the complete chapter-segmentation-related test suite one more time to confirm everything lands together cleanly:

```bash
uv run pytest backend/tests/test_chapter_common.py \
               backend/tests/test_chapter_evidence_types.py \
               backend/tests/test_chapter_evidence_outline.py \
               backend/tests/test_chapter_evidence_crossref.py \
               backend/tests/test_chapter_evidence_zotero_catalog.py \
               backend/tests/test_chapter_evidence_fusion.py \
               backend/tests/test_chapter_segmentation_strategies.py \
               backend/tests/test_chapter_segmentation.py \
               backend/tests/test_chapter_retrofit.py \
               backend/tests/test_chapter_linking_api.py \
               backend/tests/test_settings_crossref.py \
               -q
```

Expected: all PASS. Then run the full default suite once to confirm nothing outside the chapter-segmentation area regressed:

```bash
uv run pytest -q
```

Expected: PASS (same pass/skip counts as before this plan's changes, outside the files this plan touched).
