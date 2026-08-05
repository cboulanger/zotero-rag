# Evaluation Corpus Redaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a redaction pipeline that turns each evaluation book's real
page text into a git-trackable "public cache" — real navigational text
(table of contents, secondary listings, running headers, chapter headings)
kept verbatim, chapter prose replaced with random real words in the book's
own language — plus a generation tool, harness support, and a parity test,
so `backend/tests/test_chapter_segmentation_accuracy.py`'s accuracy signal
can be reproduced without any of the source PDFs.

**Architecture:** Implements
`docs/superpowers/specs/2026-08-05-evaluation-corpus-redaction-design.md`
in full. Three new pure-logic modules under `scripts/evaluation_redaction/`
(region classification → preserve mask → word substitution), a CLI
generation tool in `scripts/`, two new functions in
`backend/evaluation/harness.py`, and a new `integration`-marked pytest test.
The redaction modules deliberately import "private" helpers directly from
`backend/services/chapter_segmentation.py` rather than reimplementing
equivalent logic, so the preserved regions are exactly what the real
algorithm reads.

**Tech Stack:** Python, `rapidfuzz` (already a dependency), `Faker` (new,
dev-only dependency for locale-specific real-word pools).

---

## File structure

- `scripts/evaluation_redaction/region_classification.py` (new) —
  `RegionMap` dataclass + `classify_regions(pages)`: which pages/spans are
  navigational text vs. chapter prose, derived by running the real
  production TOC/locate logic once.
- `scripts/evaluation_redaction/wordlists.py` (new) — locale-specific,
  length-bucketed real-word pools sourced from `Faker`'s lorem providers.
- `scripts/evaluation_redaction/redact.py` (new) — `build_preserve_mask`,
  `redact_page`, `redact_book`: turns a `RegionMap` into an actual redacted
  page list.
- `scripts/generate_public_evaluation_cache.py` (new) — CLI tool a
  maintainer with the real books runs to produce and verify
  `backend/evaluation/book-segmentation/public-cache/*.pages.json`.
- `backend/evaluation/harness.py` (modified) — adds `PUBLIC_CACHE_DIR`,
  `available_public_books()`, `public_pages_for()`.
- `backend/tests/test_evaluation_harness.py` (modified) — tests for the two
  new harness functions.
- `backend/tests/test_evaluation_redaction.py` (new) — TDD unit tests for
  all three `scripts/evaluation_redaction/` modules. Pure logic, no
  PDFs/network, so it runs in the default `uv run pytest` suite.
- `backend/tests/test_public_evaluation_cache_parity.py` (new) —
  `integration`-marked aggregate parity test against the committed
  `public-cache/` directory.
- `pyproject.toml` (modified) — adds `faker` to `[dependency-groups] dev`.
- `backend/evaluation/book-segmentation/CLAUDE.md` (modified) — documents
  `public-cache/` as a fourth artifact and its regeneration trigger.

---

### Task 1: Region classification — TOC/secondary-listing pages

**Files:**
- Create: `scripts/evaluation_redaction/region_classification.py`
- Create: `backend/tests/test_evaluation_redaction.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for scripts/evaluation_redaction/ -- the redaction pipeline
that turns real evaluation-book page text into a corpus safe to commit (see
docs/superpowers/specs/2026-08-05-evaluation-corpus-redaction-design.md).
Pure logic, no PDFs/network/OCR involved, so these run in the default
suite."""

import unittest

from scripts.evaluation_redaction.region_classification import classify_regions

# Same shape as backend/tests/test_chapter_segmentation.py's
# TestAnalyzeAttachment._fake_book_pages() -- a proven-working minimal book
# fixture (TOC page + two located chapters). Kept as a separate copy since
# these two test files don't import from each other.
_FAKE_BOOK_PAGES = [
    "CONTENTS\n"
    "Introduction ..... 1\n"
    "Comparing Citation Styles ..... 3\n"
    "Appendix ..... 5\n",
    "Introduction\nJane Author\n\nThis book explores reference management.\n\n1",
    "...continued text follows here, with enough body content on this "
    "page that it clearly reads as a real continuation of the "
    "chapter rather than a blank divider page between sections.\n\n2",
    "Comparing Citation Styles\n\nJohn Smith\n\nThis chapter examines APA and MLA.\n\n3",
    "...continued chapter text, with enough body content on this "
    "final page that it clearly reads as a real continuation of "
    "the chapter rather than a blank divider page.\n\n4",
]


class TestClassifyRegionsFullPages(unittest.TestCase):
    def test_toc_page_is_a_full_preserved_page(self):
        regions = classify_regions(_FAKE_BOOK_PAGES)
        self.assertEqual(regions.full_pages, frozenset({0}))

    def test_chapter_body_pages_are_not_full_preserved_pages(self):
        regions = classify_regions(_FAKE_BOOK_PAGES)
        self.assertNotIn(1, regions.full_pages)
        self.assertNotIn(3, regions.full_pages)

    def test_header_lines_empty_when_book_has_no_running_header(self):
        regions = classify_regions(_FAKE_BOOK_PAGES)
        self.assertEqual(regions.header_lines, frozenset())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_evaluation_redaction.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.evaluation_redaction'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Identifies which spans of a book's page text are navigational/
bibliographic material (table of contents, secondary listings, running
headers, chapter headings) that backend/services/chapter_segmentation.py's
heuristics actually key off -- see docs/superpowers/specs/
2026-08-05-evaluation-corpus-redaction-design.md sections 3-4.

Deliberately imports "private" helpers directly from chapter_segmentation.py
rather than reimplementing equivalent logic, so the preserved regions are
exactly what the real algorithm reads, by construction.
"""

from dataclasses import dataclass

from backend.services.chapter_segmentation import (
    _locate_toc_entries,
    _running_header_lines,
    find_toc_candidates,
)


@dataclass(frozen=True)
class RegionMap:
    full_pages: frozenset[int]  # pages kept 100% verbatim: the TOC and any
    # secondary listing page (a part divider, a repeated chapter list, ...)
    header_lines: frozenset[str]  # normalized running-header line forms
    # (chapter_segmentation._normalize_header_line) kept verbatim wherever
    # they recur, on any page
    heading_windows: dict[int, tuple[int, int]]  # page_index -> (start, end)
    # character span kept verbatim: the chapter-heading text a title was
    # actually fuzzy-matched against. Filled in by Task 2; empty for now.


def classify_regions(pages: list[str]) -> RegionMap:
    """Region classification driven by the real production TOC/listing
    detection -- see module docstring."""
    toc_entries = find_toc_candidates(pages)
    toc_page_indices = {e.source_page_index for e in toc_entries}
    _located, _unlocated, non_content_pages = _locate_toc_entries(
        pages, toc_entries, exclude_indices=toc_page_indices,
    )
    header_lines = _running_header_lines(tuple(pages))
    return RegionMap(
        full_pages=frozenset(non_content_pages),
        header_lines=header_lines,
        heading_windows={},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_evaluation_redaction.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/evaluation_redaction/region_classification.py backend/tests/test_evaluation_redaction.py
git commit -m "feat: identify TOC/secondary-listing pages for evaluation corpus redaction"
```

---

### Task 2: Region classification — chapter heading windows

Extends Task 1's module with the per-page rescoring that finds every page a
chapter's title actually scores against (not just the single page
`locate_chapter_start` returns), covering the documented case where a
chapter's title re-scores on more than one of its own pages.

**Files:**
- Modify: `scripts/evaluation_redaction/region_classification.py`
- Modify: `backend/tests/test_evaluation_redaction.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_evaluation_redaction.py`:

```python
class TestClassifyRegionsHeadingWindows(unittest.TestCase):
    def test_chapter_start_pages_get_a_heading_window(self):
        regions = classify_regions(_FAKE_BOOK_PAGES)
        self.assertIn(1, regions.heading_windows)
        self.assertIn(3, regions.heading_windows)

    def test_heading_window_covers_the_chapter_title(self):
        regions = classify_regions(_FAKE_BOOK_PAGES)
        start, end = regions.heading_windows[1]
        self.assertIn("Introduction", _FAKE_BOOK_PAGES[1][start:end])

    def test_continuation_pages_get_no_heading_window(self):
        regions = classify_regions(_FAKE_BOOK_PAGES)
        self.assertNotIn(2, regions.heading_windows)
        self.assertNotIn(4, regions.heading_windows)

    def test_toc_page_gets_no_heading_window(self):
        regions = classify_regions(_FAKE_BOOK_PAGES)
        self.assertNotIn(0, regions.heading_windows)

    def test_heading_window_matches_what_locate_chapter_start_actually_scores(self):
        # Consistency guard for the literal 200-char window mirrored from
        # chapter_segmentation.locate_chapter_start_candidates -- if that
        # literal ever changes, this test catches the drift.
        from backend.services.chapter_segmentation import (
            _running_header_lines,
            _strip_running_headers,
        )
        regions = classify_regions(_FAKE_BOOK_PAGES)
        header_lines = _running_header_lines(tuple(_FAKE_BOOK_PAGES))
        for index in (1, 3):
            start, end = regions.heading_windows[index]
            production_head = _strip_running_headers(_FAKE_BOOK_PAGES[index], header_lines)[:200]
            self.assertEqual(_FAKE_BOOK_PAGES[index][start:end], production_head)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_evaluation_redaction.py -v -k HeadingWindows`
Expected: FAIL — `heading_windows` is `{}` for every page (Task 1's stub).

- [ ] **Step 3: Implement the heading-window computation**

Replace `region_classification.py`'s contents with:

```python
"""Identifies which spans of a book's page text are navigational/
bibliographic material (table of contents, secondary listings, running
headers, chapter headings) that backend/services/chapter_segmentation.py's
heuristics actually key off -- see docs/superpowers/specs/
2026-08-05-evaluation-corpus-redaction-design.md sections 3-4.

Deliberately imports "private" helpers directly from chapter_segmentation.py
rather than reimplementing equivalent logic, so the preserved regions are
exactly what the real algorithm reads, by construction.
"""

from dataclasses import dataclass

from rapidfuzz import fuzz

from backend.services.chapter_segmentation import (
    _LOCATE_MIN_HEAD_CHARS,
    _LOCATE_SCORE_THRESHOLD,
    _RUNNING_HEADER_SCAN_LINES,
    _locate_toc_entries,
    _normalize_header_line,
    _running_header_lines,
    find_toc_candidates,
)

# Mirrors the literal `[:200]` head-window slice in chapter_segmentation's
# locate_chapter_start_candidates -- not a named constant there, so this
# comment plus TestClassifyRegionsHeadingWindows's consistency test are what
# keep the two in sync if that literal ever changes.
_HEAD_WINDOW_CHARS = 200


@dataclass(frozen=True)
class RegionMap:
    full_pages: frozenset[int]  # pages kept 100% verbatim: the TOC and any
    # secondary listing page (a part divider, a repeated chapter list, ...)
    header_lines: frozenset[str]  # normalized running-header line forms
    # (chapter_segmentation._normalize_header_line) kept verbatim wherever
    # they recur, on any page
    heading_windows: dict[int, tuple[int, int]]  # page_index -> (start, end)
    # character span kept verbatim: the chapter-heading text a title was
    # actually fuzzy-matched against.


def _header_stripped_offset(text: str, header_lines: frozenset[str]) -> int:
    """Character offset in `text` where content begins after skipping
    leading running-header/blank lines -- mirrors
    chapter_segmentation._strip_running_headers's line-skip decision
    exactly, but returns a position in `text`'s own coordinates instead of
    the trimmed string, so a verbatim span can be sliced directly out of
    the original page."""
    if not header_lines:
        return 0
    offset = 0
    for line in text.splitlines(keepends=True)[:_RUNNING_HEADER_SCAN_LINES]:
        norm = _normalize_header_line(line.rstrip("\r\n"))
        if norm in header_lines or len(norm) == 0:
            offset += len(line)
        else:
            break
    return offset


def classify_regions(pages: list[str]) -> RegionMap:
    """Region classification driven by the real production TOC/listing
    detection -- see module docstring."""
    toc_entries = find_toc_candidates(pages)
    toc_page_indices = {e.source_page_index for e in toc_entries}
    located, _unlocated, non_content_pages = _locate_toc_entries(
        pages, toc_entries, exclude_indices=toc_page_indices,
    )
    header_lines = _running_header_lines(tuple(pages))

    # Every winning title's variant, pooled together: a page qualifies for a
    # heading window if it scores above threshold against ANY of them, not
    # just the specific entry it was originally located for -- deliberately
    # more inclusive than strictly necessary (preserving one extra page's
    # heading text is harmless), and it naturally covers the case where a
    # chapter's title legitimately re-scores on more than one of its own
    # pages without needing to reimplement locate_chapter_start_candidates's
    # internal clustering.
    titles = {variant for entry, _match in located for variant in (entry.title, *entry.title_variants)}

    heading_windows: dict[int, tuple[int, int]] = {}
    for index, text in enumerate(pages):
        if index in non_content_pages:
            continue
        offset = _header_stripped_offset(text, header_lines)
        head = text[offset:offset + _HEAD_WINDOW_CHARS]
        if len(head.strip()) < _LOCATE_MIN_HEAD_CHARS:
            continue
        head_lower = head.lower()
        if any(fuzz.partial_ratio(title.lower(), head_lower) >= _LOCATE_SCORE_THRESHOLD for title in titles):
            heading_windows[index] = (offset, offset + len(head))

    return RegionMap(
        full_pages=frozenset(non_content_pages),
        header_lines=header_lines,
        heading_windows=heading_windows,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_evaluation_redaction.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/evaluation_redaction/region_classification.py backend/tests/test_evaluation_redaction.py
git commit -m "feat: derive chapter-heading verbatim windows for evaluation corpus redaction"
```

---

### Task 3: Preserve mask

Turns a `RegionMap` plus one page's text into a per-character
preserve/redact boolean mask.

**Files:**
- Create: `scripts/evaluation_redaction/redact.py`
- Modify: `backend/tests/test_evaluation_redaction.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_evaluation_redaction.py`:

```python
from scripts.evaluation_redaction.redact import build_preserve_mask
from scripts.evaluation_redaction.region_classification import RegionMap


class TestBuildPreserveMask(unittest.TestCase):
    def test_full_page_is_entirely_preserved(self):
        regions = RegionMap(full_pages=frozenset({0}), header_lines=frozenset(), heading_windows={})
        text = "Contents\nIntroduction ..... 1\n"
        mask = build_preserve_mask(text, 0, regions)
        self.assertTrue(all(mask))

    def test_heading_window_is_preserved_rest_is_not(self):
        regions = RegionMap(full_pages=frozenset(), header_lines=frozenset(), heading_windows={1: (0, 12)})
        text = "Introduction\n\nBody prose continues here well past the window."
        mask = build_preserve_mask(text, 1, regions)
        self.assertTrue(all(mask[:12]))
        self.assertFalse(any(mask[12:]))

    def test_running_header_line_preserved_wherever_it_recurs(self):
        regions = RegionMap(
            full_pages=frozenset(), heading_windows={},
            header_lines=frozenset({"my book title"}),
        )
        text = "Body text first.\nMy Book Title\nMore body text after.\n"
        mask = build_preserve_mask(text, 2, regions)
        header_start = text.index("My Book Title")
        header_end = header_start + len("My Book Title\n")
        self.assertTrue(all(mask[header_start:header_end]))
        self.assertFalse(mask[0])

    def test_ordinary_body_page_with_no_regions_is_entirely_unpreserved(self):
        regions = RegionMap(full_pages=frozenset(), header_lines=frozenset(), heading_windows={})
        mask = build_preserve_mask("Just ordinary body prose.\n", 5, regions)
        self.assertFalse(any(mask))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_evaluation_redaction.py -v -k PreserveMask`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.evaluation_redaction.redact'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Turns a RegionMap (scripts/evaluation_redaction/region_classification.py)
into per-character preserve/redact decisions and applies word-level
redaction -- see docs/superpowers/specs/
2026-08-05-evaluation-corpus-redaction-design.md sections 3.1, 5."""

from backend.services.chapter_segmentation import _normalize_header_line
from scripts.evaluation_redaction.region_classification import RegionMap


def build_preserve_mask(text: str, page_index: int, regions: RegionMap) -> list[bool]:
    """True at every character index in `text` that must survive redaction
    unchanged: this page's chapter-heading window (if any) and every line
    on this page whose normalized form is a recognized running header."""
    if page_index in regions.full_pages:
        return [True] * len(text)
    mask = [False] * len(text)
    window = regions.heading_windows.get(page_index)
    if window is not None:
        start, end = window
        for i in range(max(start, 0), min(end, len(text))):
            mask[i] = True
    pos = 0
    for line in text.splitlines(keepends=True):
        if _normalize_header_line(line.rstrip("\r\n")) in regions.header_lines:
            for i in range(pos, pos + len(line)):
                mask[i] = True
        pos += len(line)
    return mask
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_evaluation_redaction.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/evaluation_redaction/redact.py backend/tests/test_evaluation_redaction.py
git commit -m "feat: add per-character preserve mask for evaluation corpus redaction"
```

---

### Task 4: Locale word pools

Adds `Faker` as a dev-only dependency and builds length-bucketed real-word
pools per language.

**Files:**
- Modify: `pyproject.toml`
- Create: `scripts/evaluation_redaction/wordlists.py`
- Modify: `backend/tests/test_evaluation_redaction.py`

- [ ] **Step 1: Add the dependency**

```bash
uv add --group dev "faker>=33.0.0"
```

This adds `faker` to `pyproject.toml`'s `[dependency-groups] dev` list —
**not** the core `dependencies` list, since the only place it's imported is
`scripts/evaluation_redaction/`, which nothing under `backend/` imports and
which is never copied into the production Docker image.

- [ ] **Step 2: Write the failing tests**

Add to `backend/tests/test_evaluation_redaction.py`:

```python
from scripts.evaluation_redaction.wordlists import build_word_pool, locale_for_detected_language, pick_word


class TestLocaleForDetectedLanguage(unittest.TestCase):
    def test_maps_known_language_codes(self):
        self.assertEqual(locale_for_detected_language("deu"), "de_DE")
        self.assertEqual(locale_for_detected_language("fra"), "fr_FR")
        self.assertEqual(locale_for_detected_language("spa"), "es_ES")
        self.assertEqual(locale_for_detected_language("eng"), "en_US")

    def test_falls_back_to_english_for_the_combined_default(self):
        self.assertEqual(locale_for_detected_language("eng+deu+fra+spa"), "en_US")


class TestBuildWordPool(unittest.TestCase):
    def test_pool_has_real_words_bucketed_by_length(self):
        pool = build_word_pool("deu")
        self.assertIn(5, pool)
        for word in pool[5]:
            self.assertEqual(len(word), 5)

    def test_unknown_language_falls_back_to_english_pool(self):
        pool = build_word_pool("xyz")
        self.assertTrue(pool)


class TestPickWord(unittest.TestCase):
    def test_picks_a_word_of_the_exact_length_when_available(self):
        pool = {4: ["word"], 5: ["words"]}
        self.assertEqual(len(pick_word(pool, 4, seed=0)), 4)

    def test_falls_back_to_nearest_length_when_no_exact_match(self):
        pool = {4: ["word"]}
        self.assertEqual(pick_word(pool, 9, seed=0), "word")

    def test_deterministic_for_the_same_seed(self):
        pool = {5: ["reala", "realb", "realc"]}
        self.assertEqual(pick_word(pool, 5, seed=7), pick_word(pool, 5, seed=7))
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_evaluation_redaction.py -v -k "LocaleFor or BuildWordPool or PickWord"`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.evaluation_redaction.wordlists'`

- [ ] **Step 4: Write minimal implementation**

```python
"""Locale-appropriate real-word pools for scripts/evaluation_redaction/redact.py,
bucketed by character length so a redacted token can be replaced by a
same-length (or nearest-length) real word in the book's own language -- see
docs/superpowers/specs/2026-08-05-evaluation-corpus-redaction-design.md
section 6."""

from collections import defaultdict

from faker.providers.lorem import Provider as EnProvider
from faker.providers.lorem.de_DE import Provider as DeProvider
from faker.providers.lorem.es_ES import Provider as EsProvider
from faker.providers.lorem.fr_FR import Provider as FrProvider

# backend.services.chapter_ocr.detect_language returns one of these, or the
# combined-default "eng+deu+fra+spa" when detection is ambiguous -- mapped
# to English below, since an ambiguous-language book is the exception, not
# most of the evaluation set.
_LOCALE_NAMES = {"deu": "de_DE", "fra": "fr_FR", "spa": "es_ES", "eng": "en_US"}
_LOCALE_PROVIDERS = {"deu": DeProvider, "fra": FrProvider, "spa": EsProvider, "eng": EnProvider}


def locale_for_detected_language(detected: str) -> str:
    return _LOCALE_NAMES.get(detected, "en_US")


def build_word_pool(detected_language: str) -> dict[int, list[str]]:
    """{word_length: [real dictionary words of that length, this language]},
    built once per book from Faker's locale lorem provider."""
    provider_cls = _LOCALE_PROVIDERS.get(detected_language, EnProvider)
    pool: dict[int, list[str]] = defaultdict(list)
    for word in provider_cls.word_list:
        pool[len(word)].append(word)
    return dict(pool)


def pick_word(pool: dict[int, list[str]], length: int, seed: int) -> str:
    """Deterministic pick from `pool`'s bucket closest to `length` (exact
    match preferred; falls back to the nearest length that has any words at
    all, so a length with no dictionary entry doesn't crash)."""
    if not pool:
        return "x" * max(length, 1)
    candidates = pool.get(length)
    if not candidates:
        nearest_length = min(pool, key=lambda available: abs(available - length))
        candidates = pool[nearest_length]
    return candidates[seed % len(candidates)]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_evaluation_redaction.py -v`
Expected: 18 passed

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock scripts/evaluation_redaction/wordlists.py backend/tests/test_evaluation_redaction.py
git commit -m "feat: add locale word pools for evaluation corpus redaction"
```

---

### Task 5: Word-level redaction

Tokenizes a page and replaces every non-preserved, non-digit, non-roman-
numeral word with a same-length real word from the book's language pool.

**Files:**
- Modify: `scripts/evaluation_redaction/redact.py`
- Modify: `backend/tests/test_evaluation_redaction.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_evaluation_redaction.py`:

```python
from scripts.evaluation_redaction.redact import redact_page


class TestRedactPage(unittest.TestCase):
    _POOL = {4: ["real"], 5: ["reals"], 6: ["length"]}

    def test_preserved_span_survives_unchanged(self):
        regions = RegionMap(full_pages=frozenset({0}), header_lines=frozenset(), heading_windows={})
        text = "Contents\nIntroduction ..... 1\n"
        out = redact_page(text, 0, regions, self._POOL, book_salt="book1")
        self.assertEqual(out, text)

    def test_digits_and_whitespace_survive_unchanged(self):
        regions = RegionMap(full_pages=frozenset(), header_lines=frozenset(), heading_windows={})
        text = "words 42\nwords\n"
        out = redact_page(text, 1, regions, self._POOL, book_salt="book1")
        self.assertIn("42", out)
        self.assertEqual(out.count("\n"), text.count("\n"))

    def test_body_word_is_replaced_with_a_same_length_real_word(self):
        regions = RegionMap(full_pages=frozenset(), header_lines=frozenset(), heading_windows={})
        out = redact_page("words", 1, regions, {5: ["reals"]}, book_salt="book1")
        self.assertEqual(out, "reals")

    def test_same_input_produces_same_output(self):
        regions = RegionMap(full_pages=frozenset(), header_lines=frozenset(), heading_windows={})
        text = "words are here"
        out1 = redact_page(text, 1, regions, self._POOL, book_salt="book1")
        out2 = redact_page(text, 1, regions, self._POOL, book_salt="book1")
        self.assertEqual(out1, out2)

    def test_different_book_salt_changes_output(self):
        regions = RegionMap(full_pages=frozenset(), header_lines=frozenset(), heading_windows={})
        pool = {5: ["reala", "realb", "realc", "reald"]}
        out1 = redact_page("words", 1, regions, pool, book_salt="book1")
        out2 = redact_page("words", 1, regions, pool, book_salt="book2")
        self.assertNotEqual(out1, out2)

    def test_roman_numeral_page_number_survives_unchanged(self):
        regions = RegionMap(full_pages=frozenset(), header_lines=frozenset(), heading_windows={})
        out = redact_page("xiv", 1, regions, self._POOL, book_salt="book1")
        self.assertEqual(out, "xiv")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_evaluation_redaction.py -v -k RedactPage`
Expected: FAIL — `redact_page` does not exist yet.

- [ ] **Step 3: Implement `redact_page`**

Add to `scripts/evaluation_redaction/redact.py` (keep `build_preserve_mask`
from Task 3 above it):

```python
import hashlib
import re

from backend.services.chapter_segmentation import _PAGE_NUMBER_TOKEN_RE, _normalize_header_line
from scripts.evaluation_redaction.region_classification import RegionMap
from scripts.evaluation_redaction.wordlists import pick_word

_TOKEN_RE = re.compile(r"(?P<word>\w+)|(?P<other>\W+)", re.UNICODE)


def build_preserve_mask(text: str, page_index: int, regions: RegionMap) -> list[bool]:
    ...  # unchanged from Task 3


def _match_case(word: str, original: str) -> str:
    if original.isupper() and len(original) > 1:
        return word.upper()
    if original[:1].isupper():
        return word[:1].upper() + word[1:]
    return word


def redact_page(text: str, page_index: int, regions: RegionMap, pool: dict[int, list[str]], book_salt: str) -> str:
    """Replaces every word token outside `regions`' preserved spans with a
    deterministic, same-length (or nearest-length) real word from `pool`;
    digits, roman-numeral-shaped tokens, and preserved spans pass through
    unchanged."""
    mask = build_preserve_mask(text, page_index, regions)
    out: list[str] = []
    pos = 0
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0)
        start = pos
        pos += len(token)
        if match.group("word") is None:
            out.append(token)  # whitespace/punctuation run
            continue
        if all(mask[start:pos]):
            out.append(token)  # inside a preserved span
            continue
        if token.isdigit() or _PAGE_NUMBER_TOKEN_RE.match(token):
            out.append(token)  # page-number-shaped token, never prose
            continue
        seed = int.from_bytes(
            hashlib.sha256(f"{book_salt}:{page_index}:{start}:{token.casefold()}".encode("utf-8")).digest()[:8],
            "big",
        )
        out.append(_match_case(pick_word(pool, len(token), seed), token))
    return "".join(out)
```

(Leave `build_preserve_mask`'s actual body exactly as Task 3 wrote it — the
`...` above is just marking "unchanged", not literal code to write.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_evaluation_redaction.py -v`
Expected: 24 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/evaluation_redaction/redact.py backend/tests/test_evaluation_redaction.py
git commit -m "feat: redact chapter prose with same-length real words"
```

---

### Task 6: Orchestration — `redact_book`

Ties region classification, word pools, and per-page redaction together,
and proves the core claim: redaction does not change detected chapter
boundaries on a real (fixture) book.

**Files:**
- Modify: `scripts/evaluation_redaction/redact.py`
- Modify: `backend/tests/test_evaluation_redaction.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_evaluation_redaction.py`:

```python
from scripts.evaluation_redaction.redact import redact_book


class TestRedactBook(unittest.TestCase):
    def test_toc_page_survives_verbatim_chapter_prose_does_not(self):
        redacted = redact_book(_FAKE_BOOK_PAGES, detected_language="eng", book_salt="9999999")
        self.assertEqual(redacted[0], _FAKE_BOOK_PAGES[0])
        self.assertNotEqual(redacted[1], _FAKE_BOOK_PAGES[1])
        self.assertIn("Introduction", redacted[1])

    def test_boundary_detection_is_unchanged_by_redaction(self):
        from backend.services.chapter_segmentation import analyze_attachment
        redacted = redact_book(_FAKE_BOOK_PAGES, detected_language="eng", book_salt="9999999")
        real_result = analyze_attachment(_FAKE_BOOK_PAGES)
        redacted_result = analyze_attachment(redacted)
        real_boundaries = {(c["pdf_start_index"], c["pdf_end_index"]) for c in real_result["chapters"]}
        redacted_boundaries = {(c["pdf_start_index"], c["pdf_end_index"]) for c in redacted_result["chapters"]}
        self.assertEqual(real_boundaries, redacted_boundaries)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_evaluation_redaction.py -v -k RedactBook`
Expected: FAIL — `redact_book` does not exist yet.

- [ ] **Step 3: Implement `redact_book`**

Add to the bottom of `scripts/evaluation_redaction/redact.py`:

```python
from scripts.evaluation_redaction.region_classification import classify_regions
from scripts.evaluation_redaction.wordlists import build_word_pool


def redact_book(pages: list[str], detected_language: str, book_salt: str) -> list[str]:
    """Full per-book redaction pipeline: classify regions once, build the
    language's word pool once, then redact every page."""
    regions = classify_regions(pages)
    pool = build_word_pool(detected_language)
    return [redact_page(text, index, regions, pool, book_salt) for index, text in enumerate(pages)]
```

(Add the two new imports at the top of the file alongside the existing
ones, not inline as shown here.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_evaluation_redaction.py -v`
Expected: 26 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/evaluation_redaction/redact.py backend/tests/test_evaluation_redaction.py
git commit -m "feat: add redact_book orchestration for evaluation corpus redaction"
```

---

### Task 7: Harness support for the public cache

**Files:**
- Modify: `backend/evaluation/harness.py`
- Modify: `backend/tests/test_evaluation_harness.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_evaluation_harness.py` (add `import json`,
`import tempfile`, `from pathlib import Path` to its existing imports):

```python
import json
import tempfile
from pathlib import Path

from backend.evaluation.harness import available_public_books, public_pages_for


class TestAvailablePublicBooks(unittest.TestCase):
    def test_yields_books_with_a_cache_entry_and_expected_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            eval_dir = Path(tmp)
            public_cache_dir = eval_dir / "public-cache"
            public_cache_dir.mkdir()
            (eval_dir / "9999999.expected.json").write_text("{}", encoding="utf-8")
            (public_cache_dir / "9999999.pages.json").write_text(
                json.dumps({"pages": ["a"]}), encoding="utf-8",
            )
            book = {"filename": "9999999.pdf", "title": "Test Book"}
            with patch("backend.evaluation.harness.EVAL_DIR", eval_dir), \
                 patch("backend.evaluation.harness.PUBLIC_CACHE_DIR", public_cache_dir), \
                 patch("backend.evaluation.harness.load_manifest_books", return_value=[book]):
                results = available_public_books()
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][0], "9999999")

    def test_skips_books_with_no_cache_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            eval_dir = Path(tmp)
            public_cache_dir = eval_dir / "public-cache"
            public_cache_dir.mkdir()
            (eval_dir / "9999999.expected.json").write_text("{}", encoding="utf-8")
            book = {"filename": "9999999.pdf", "title": "Test Book"}
            with patch("backend.evaluation.harness.EVAL_DIR", eval_dir), \
                 patch("backend.evaluation.harness.PUBLIC_CACHE_DIR", public_cache_dir), \
                 patch("backend.evaluation.harness.load_manifest_books", return_value=[book]):
                results = available_public_books()
            self.assertEqual(results, [])


class TestPublicPagesFor(unittest.TestCase):
    def test_returns_pages_for_existing_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            public_cache_dir = Path(tmp) / "public-cache"
            public_cache_dir.mkdir()
            (public_cache_dir / "9999999.pages.json").write_text(
                json.dumps({"pages": ["redacted page text"]}), encoding="utf-8",
            )
            with patch("backend.evaluation.harness.PUBLIC_CACHE_DIR", public_cache_dir):
                pages = public_pages_for("9999999")
            self.assertEqual(pages, ["redacted page text"])

    def test_returns_none_for_missing_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            public_cache_dir = Path(tmp) / "public-cache"
            public_cache_dir.mkdir()
            with patch("backend.evaluation.harness.PUBLIC_CACHE_DIR", public_cache_dir):
                self.assertIsNone(public_pages_for("9999999"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_evaluation_harness.py -v`
Expected: FAIL — `available_public_books`/`public_pages_for` do not exist.

- [ ] **Step 3: Add the two functions**

Add to `backend/evaluation/harness.py`, after the existing
`OCR_CACHE_DIR = EVAL_DIR / ".ocr-cache"` line:

```python
PUBLIC_CACHE_DIR = EVAL_DIR / "public-cache"
```

Add after `available_books()`:

```python
def available_public_books() -> list[tuple[str, Path, dict]]:
    """(manifest_key, expected_json_path, manifest_entry) for every manifest
    book with a public-cache entry -- no PDF or .ocr-cache required."""
    triples = []
    for book in load_manifest_books():
        manifest_key = Path(book["filename"]).stem
        expected_path = EVAL_DIR / f"{manifest_key}.expected.json"
        cache_path = PUBLIC_CACHE_DIR / f"{manifest_key}.pages.json"
        if cache_path.exists() and expected_path.exists():
            triples.append((manifest_key, expected_path, book))
    return triples


def public_pages_for(manifest_key: str) -> Optional[list[str]]:
    """Redacted pages for one book from the committed public-cache, or None
    if no entry exists yet for this key."""
    cache_path = PUBLIC_CACHE_DIR / f"{manifest_key}.pages.json"
    if not cache_path.exists():
        return None
    return json.loads(cache_path.read_text(encoding="utf-8"))["pages"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_evaluation_harness.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add backend/evaluation/harness.py backend/tests/test_evaluation_harness.py
git commit -m "feat: add public-cache lookup functions to the evaluation harness"
```

---

### Task 8: Generation tool

**Files:**
- Create: `scripts/generate_public_evaluation_cache.py`

No TDD step here — this is a thin CLI orchestrating already-tested pieces
(Tasks 1-7), same category as `scripts/ocr_evaluation_pdfs.py`, which has no
unit tests either. Verified manually in Step 3 below instead.

- [ ] **Step 1: Write the tool**

```python
#!/usr/bin/env python3
"""Generate backend/evaluation/book-segmentation/public-cache/ -- a
redacted, git-trackable corpus safe to commit and distribute (real
navigational/bibliographic text kept verbatim, chapter prose replaced with
random real words in the book's own language) -- see docs/superpowers/specs/
2026-08-05-evaluation-corpus-redaction-design.md.

Run by a maintainer who has the real books locally; not something a
contributor without PDFs needs to run.

    uv run python scripts/generate_public_evaluation_cache.py [--book <manifest-key>] [--no-verify]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.evaluation.harness import PUBLIC_CACHE_DIR, analysis_pages_for, available_books
from backend.services.chapter_ocr import detect_language
from backend.services.chapter_segmentation import (
    analyze_attachment,
    extract_page_texts_for_analysis,
    pages_need_ocr,
)
from scripts.evaluation_redaction.redact import redact_book

CIPHER_VERSION = 1


def _verify(real_pages: list[str], redacted_pages: list[str]) -> list[str]:
    """Human-readable diff lines, empty when the two page sets produce
    identical detected chapter boundaries."""
    real_result = analyze_attachment(real_pages)
    redacted_result = analyze_attachment(redacted_pages)
    real_boundaries = {(c["pdf_start_index"], c["pdf_end_index"]) for c in real_result["chapters"]}
    redacted_boundaries = {(c["pdf_start_index"], c["pdf_end_index"]) for c in redacted_result["chapters"]}
    if real_boundaries == redacted_boundaries:
        return []
    return [
        f"  real boundaries:     {sorted(real_boundaries)}",
        f"  redacted boundaries: {sorted(redacted_boundaries)}",
    ]


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book", help="Only regenerate this manifest key (filename stem)")
    parser.add_argument("--no-verify", action="store_true", help="Skip the exact-boundary-match check")
    args = parser.parse_args()

    PUBLIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    failures = 0
    for pdf_path, _expected_path, book in available_books():
        manifest_key = Path(book["filename"]).stem
        if args.book and manifest_key != args.book:
            continue
        file_bytes = pdf_path.read_bytes()
        real_pages = analysis_pages_for(file_bytes)
        if real_pages is None:
            print(f"{manifest_key}: SKIPPED (needs OCR -- run scripts/ocr_evaluation_pdfs.py first)")
            continue
        try:
            raw_pages, _layout_used = extract_page_texts_for_analysis(file_bytes)
            source = "ocr" if pages_need_ocr(raw_pages) else "extracted"
            language = detect_language(book.get("language"), book.get("title", ""))
            redacted_pages = redact_book(real_pages, detected_language=language, book_salt=manifest_key)
            if not args.no_verify:
                diff = _verify(real_pages, redacted_pages)
                if diff:
                    print(f"{manifest_key}: VERIFY FAILED -- redaction changed detected chapter boundaries")
                    print("\n".join(diff))
                    failures += 1
                    continue
        except Exception as exc:
            # One book's failure must not strand the rest of the batch --
            # same catch-log-continue shape as scripts/ocr_evaluation_pdfs.py.
            print(f"{manifest_key}: FAILED ({exc}) -- skipping")
            failures += 1
            continue
        cache_path = PUBLIC_CACHE_DIR / f"{manifest_key}.pages.json"
        cache_path.write_text(
            json.dumps(
                {"cipher_version": CIPHER_VERSION, "source": source, "pages": redacted_pages},
                indent=2, ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"{manifest_key}: OK, wrote {cache_path}")
    if failures:
        print(f"{failures} book(s) failed -- see above")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
```

- [ ] **Step 2: Make it executable and runnable**

```bash
chmod +x scripts/generate_public_evaluation_cache.py
```

- [ ] **Step 3: Verify manually against whatever real evaluation books are
  present locally**

Run: `uv run python scripts/generate_public_evaluation_cache.py`
Expected: One `OK, wrote ...` line per book with a local PDF and a usable
text layer or OCR cache entry (others print `SKIPPED`); exit code 0. If any
book prints `VERIFY FAILED`, stop and investigate before proceeding to Task
9 — see spec §10.1/§10.3 for what that would mean.

- [ ] **Step 4: Commit the tool (not its output yet)**

```bash
git add scripts/generate_public_evaluation_cache.py
git commit -m "feat: add generation tool for the redacted public evaluation cache"
```

---

### Task 9: Aggregate parity test

**Files:**
- Create: `backend/tests/test_public_evaluation_cache_parity.py`

- [ ] **Step 1: Write the test**

```python
"""Aggregate precision/recall parity check for the redacted public-cache
corpus (backend/evaluation/book-segmentation/public-cache/) -- see
docs/superpowers/specs/2026-08-05-evaluation-corpus-redaction-design.md
section 9.

Needs no PDFs or .ocr-cache/ -- only the committed public-cache/ directory,
so this is what CI and contributors without the source books actually run.

Marked "integration" so it's excluded from the default `uv run pytest` run
(see pyproject.toml's addopts) -- reported, not gated, same as
test_chapter_segmentation_accuracy.py.

    uv run pytest backend/tests/test_public_evaluation_cache_parity.py -q -s
"""

import json
import unittest

import pytest

from backend.evaluation.harness import available_public_books, public_pages_for
from backend.services.chapter_segmentation import analyze_attachment

pytestmark = pytest.mark.integration


@unittest.skipUnless(
    available_public_books(),
    "No public-cache entries present -- run: "
    "uv run python scripts/generate_public_evaluation_cache.py",
)
class TestPublicEvaluationCacheParity(unittest.TestCase):
    def test_boundary_precision_recall_per_book(self):
        for manifest_key, expected_path, _book in available_public_books():
            with self.subTest(book=manifest_key):
                expected = json.loads(expected_path.read_text(encoding="utf-8"))["chapters"]
                pages = public_pages_for(manifest_key)
                result = analyze_attachment(pages)

                expected_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in expected}
                found_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in result["chapters"]}
                true_positives = expected_ranges & found_ranges

                precision = len(true_positives) / len(found_ranges) if found_ranges else 0.0
                recall = len(true_positives) / len(expected_ranges) if expected_ranges else 0.0
                print(f"{manifest_key}: precision={precision:.2f} recall={recall:.2f} "
                      f"({len(true_positives)}/{len(found_ranges)} found, "
                      f"{len(true_positives)}/{len(expected_ranges)} expected)")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it against whatever `public-cache/` entries Task 8
  produced**

Run: `uv run pytest backend/tests/test_public_evaluation_cache_parity.py -q -s -m integration`
Expected: One line per book with a `public-cache/` entry, all passing
(the test only prints — it has no assertion to fail on low recall, mirroring
`test_chapter_segmentation_accuracy.py`'s "reported, not gated" policy,
except this one has no non-zero-recall guard either, since Task 8's
`--verify` already guarantees per-book boundary parity with the real
corpus before a `public-cache/` entry is ever committed).

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_public_evaluation_cache_parity.py
git commit -m "test: add aggregate parity check for the redacted public evaluation cache"
```

---

### Task 10: Document the new artifact and its regeneration trigger

**Files:**
- Modify: `backend/evaluation/book-segmentation/CLAUDE.md`

- [ ] **Step 1: Add `public-cache/` to the document-organization convention**

Read the file's existing "## Document organization in this directory"
section first (added in prior work on this evaluation set), then add a
fourth bullet describing `public-cache/` alongside the existing
README.md/RESULTS.md/CLAUDE.md bullets:

```markdown
- **`public-cache/`** — a redacted, git-tracked snapshot of each book's
  page text (real navigational/bibliographic material verbatim, chapter
  prose replaced with random real words) — see
  `docs/superpowers/specs/2026-08-05-evaluation-corpus-redaction-design.md`.
  Regenerate it with `uv run python scripts/generate_public_evaluation_cache.py`
  whenever `chapter_segmentation.py` or `chapter_common.py` changes in a way
  that touches text-matching logic (a new heuristic could read page text
  outside what the redaction pipeline currently preserves) -- the tool's
  `--verify` step will refuse to write a stale/incorrect entry, so a clean
  run is the confirmation that a change didn't need any redaction-pipeline
  updates.
```

- [ ] **Step 2: Commit**

```bash
git add backend/evaluation/book-segmentation/CLAUDE.md
git commit -m "docs: document public-cache/ regeneration trigger in book-segmentation CLAUDE.md"
```

---

## After all tasks

Run the full default suite once more to confirm nothing regressed:

```bash
uv run pytest -q
```

Expected: same pass count as before this plan, plus the ~26 new tests from
`test_evaluation_redaction.py` and the 4 new tests in
`test_evaluation_harness.py`.

If real evaluation PDFs were available locally during Task 8, also run the
new parity test against the generated `public-cache/` (Task 9, Step 2) one
more time as a final sanity check before considering `public-cache/` ready
to commit for real. Per spec §11, treat actually publishing that directory
as a separate decision needing human sign-off — this plan only builds and
locally verifies the pipeline.
