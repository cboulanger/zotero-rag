# LLM Fallback for Chapter Recognition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, confidence-gated LLM fallback to `backend/services/chapter_segmentation.py` that extracts a chapter listing when the regex TOC detector finds nothing usable, and disambiguates a chapter's start page when the fuzzy-match localizer finds a genuine, unresolved tie — reusing the app's existing `LLMService` plumbing.

**Architecture:** Two new async functions (`llm_extract_toc_entries`, `llm_disambiguate_chapter_start`) plug into the existing heuristic pipeline only at the exact points it already reports failure/ambiguity, via a new orchestrator (`analyze_attachment_with_llm_fallback`) that wraps the untouched, pure `analyze_attachment`. A small, behavior-preserving refactor exposes `locate_chapter_start`'s internal candidate clusters and adds author-name-aware disambiguation (since LLM-extracted TOC entries carry author names the regex path never had). Full design: `docs/superpowers/specs/2026-07-25-llm-chapter-segmentation-fallback-design.md`.

**Tech Stack:** Python 3.12, `backend.services.llm.LLMService` (existing), `rapidfuzz`, `pytest`/`unittest`, FastAPI/Pydantic.

---

## Task 1: Shared JSON-extraction helper module

**Files:**
- Create: `backend/utils/llm_json.py`
- Test: `backend/tests/test_llm_json.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for backend.utils.llm_json."""

import unittest

from backend.utils.llm_json import parse_json_array, parse_json_object


class TestParseJsonObject(unittest.TestCase):
    def test_plain_json(self):
        data = parse_json_object('{"agents": ["rag"], "year_min": null}')
        self.assertEqual(data["agents"], ["rag"])

    def test_strips_markdown_fence(self):
        raw = '```json\n{"agents": ["metadata"]}\n```'
        data = parse_json_object(raw)
        self.assertEqual(data["agents"], ["metadata"])

    def test_strips_plain_code_fence(self):
        raw = '```\n{"agents": ["rag", "metadata"]}\n```'
        data = parse_json_object(raw)
        self.assertEqual(data["agents"], ["rag", "metadata"])

    def test_json_with_leading_text(self):
        raw = 'Here is the answer: {"agents": ["rag"]}'
        data = parse_json_object(raw)
        self.assertEqual(data["agents"], ["rag"])

    def test_raises_on_no_braces(self):
        with self.assertRaises(ValueError):
            parse_json_object("no json here at all")

    def test_raises_on_invalid_json(self):
        with self.assertRaises(Exception):
            parse_json_object("{bad json}")


class TestParseJsonArray(unittest.TestCase):
    def test_plain_array(self):
        data = parse_json_array('[{"title": "Intro"}, {"title": "Conclusion"}]')
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["title"], "Intro")

    def test_strips_markdown_fence(self):
        raw = '```json\n[{"title": "Intro"}]\n```'
        data = parse_json_array(raw)
        self.assertEqual(data[0]["title"], "Intro")

    def test_json_with_leading_text(self):
        raw = 'Here is the chapter list: [{"title": "Intro"}]'
        data = parse_json_array(raw)
        self.assertEqual(data[0]["title"], "Intro")

    def test_raises_on_no_brackets(self):
        with self.assertRaises(ValueError):
            parse_json_array("no json here at all")

    def test_raises_on_invalid_json(self):
        with self.assertRaises(Exception):
            parse_json_array("[bad json]")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_llm_json.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.utils.llm_json'`

- [ ] **Step 3: Write the implementation**

```python
"""Shared JSON-extraction helpers for parsing structured LLM output.

LLMs are asked to "return ONLY JSON" but the real world routinely adds
markdown code fences or a sentence of leading prose anyway -- both helpers
strip that off before parsing. Originally a private helper in
query_router.py; promoted here once chapter_segmentation.py's LLM fallback
needed the identical logic (see docs/superpowers/specs/
2026-07-25-llm-chapter-segmentation-fallback-design.md §3).
"""

import json


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(line for line in lines if not line.startswith("```")).strip()
    return text


def parse_json_object(text: str) -> dict:
    """Extract and parse the first JSON object ({...}) found in *text*."""
    text = _strip_code_fence(text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in LLM response: {text!r}")
    return json.loads(text[start: end + 1])


def parse_json_array(text: str) -> list:
    """Extract and parse the first JSON array ([...]) found in *text*."""
    text = _strip_code_fence(text)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON array found in LLM response: {text!r}")
    return json.loads(text[start: end + 1])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_llm_json.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/utils/llm_json.py backend/tests/test_llm_json.py
git commit -m "feat: add shared JSON-extraction helpers for LLM responses"
```

---

## Task 2: Refactor `query_router.py` to use the shared helper

**Files:**
- Modify: `backend/services/query_router.py`
- Modify: `backend/tests/test_query_router.py`

- [ ] **Step 1: Update `test_query_router.py`** — drop the now-duplicated `TestParseJson` class and its import (this is the "test" side of a behavior-preserving refactor: the same assertions already live in `test_llm_json.py`, written in Task 1)

In `backend/tests/test_query_router.py`, change the import on line 15 from:

```python
from backend.services.query_router import QueryRouter, _parse_json
```

to:

```python
from backend.services.query_router import QueryRouter
```

Then delete the entire `TestParseJson` class (originally lines 41–68, the block starting `class TestParseJson(unittest.TestCase):` up to — but not including — the `# ---...` divider comment before `TestQueryRouterRoute`).

- [ ] **Step 2: Run the router test file to confirm it still collects (it will still pass, since `_parse_json` isn't touched yet)**

Run: `uv run pytest backend/tests/test_query_router.py -v`
Expected: PASS (all remaining tests, `TestParseJson` gone)

- [ ] **Step 3: Refactor `query_router.py` to use `parse_json_object`**

In `backend/services/query_router.py`, remove the `import json` line (line 11) — it's only used by `_parse_json`, which is being deleted — and add the new import alongside the existing ones:

```python
from backend.utils.llm_json import parse_json_object
```

Change the call site (originally line 156):

```python
            data = _parse_json(raw)
```

to:

```python
            data = parse_json_object(raw)
```

Delete the `_parse_json` function definition entirely (originally lines 227–243, the block from `def _parse_json(text: str) -> dict:` to the end of the file).

- [ ] **Step 4: Run both test files to verify everything still passes**

Run: `uv run pytest backend/tests/test_query_router.py backend/tests/test_llm_json.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/services/query_router.py backend/tests/test_query_router.py
git commit -m "refactor: use shared llm_json helper in query_router"
```

---

## Task 3: `TocEntry.authors` field

**Files:**
- Modify: `backend/services/chapter_segmentation.py:78-82`
- Test: `backend/tests/test_chapter_segmentation.py`

- [ ] **Step 1: Write the failing test**

In `backend/tests/test_chapter_segmentation.py`, add to `TestFindTocCandidates` (after `test_finds_dotted_leader_entries`):

```python
    def test_entries_default_to_empty_authors(self):
        pages = [
            "CONTENTS\n"
            "Introduction to Reference Management ..... 1\n"
            "Comparing Citation Styles ..... 45\n"
            "Zotero in Practice ..... 89\n",
        ] + self._FILLER_PAGES
        entries = find_toc_candidates(pages)
        self.assertEqual(entries[0].authors, ())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py::TestFindTocCandidates::test_entries_default_to_empty_authors -v`
Expected: FAIL — `AttributeError: 'TocEntry' object has no attribute 'authors'`

- [ ] **Step 3: Add the field**

In `backend/services/chapter_segmentation.py`, change:

```python
@dataclass(frozen=True)
class TocEntry:
    title: str
    printed_page_number: int
    source_page_index: int  # which page (0-based) the TOC entry itself was found on
```

to:

```python
@dataclass(frozen=True)
class TocEntry:
    title: str
    printed_page_number: int
    source_page_index: int  # which page (0-based) the TOC entry itself was found on
    authors: tuple[str, ...] = ()  # populated only by llm_extract_toc_entries (see
    # docs/superpowers/specs/2026-07-25-llm-chapter-segmentation-fallback-design.md §4)
    # -- a regex-found TOC line has no author info, so heuristic-found entries always
    # leave this empty. Feeds locate_chapter_start's author-aware disambiguation (§5).
```

- [ ] **Step 4: Run the full chapter_segmentation test file to verify the new test passes and nothing else broke**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -v`
Expected: PASS (all tests, including the existing `TocEntry(...)` equality assertions in `test_finds_dotted_leader_entries` — they still pass because both sides default `authors` to `()`)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "feat: add authors field to TocEntry"
```

---

## Task 4: Extract `_toc_scan_indices` helper

**Files:**
- Modify: `backend/services/chapter_segmentation.py:94-120`
- Test: `backend/tests/test_chapter_segmentation.py`

- [ ] **Step 1: Write the failing test**

In `backend/tests/test_chapter_segmentation.py`, add its import to the existing multi-line import block from `backend.services.chapter_segmentation` (add `_toc_scan_indices` to the first import group), and add a new test class after `TestFindTocCandidates`:

```python
class TestTocScanIndices(unittest.TestCase):
    def test_scans_front_and_back_fractions(self):
        pages = ["x"] * 100
        indices = _toc_scan_indices(pages, max_front_fraction=0.1, max_back_fraction=0.05)
        self.assertIn(0, indices)
        self.assertIn(9, indices)  # last front-matter page (10% of 100)
        self.assertNotIn(10, indices)
        self.assertIn(99, indices)  # last back-matter page is always included
        self.assertNotIn(50, indices)  # a middle page is never scanned

    def test_empty_pages_returns_empty_set(self):
        self.assertEqual(_toc_scan_indices([]), set())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py::TestTocScanIndices -v`
Expected: FAIL — `ImportError: cannot import name '_toc_scan_indices'`

- [ ] **Step 3: Extract the helper and use it in `find_toc_candidates`**

In `backend/services/chapter_segmentation.py`, add this function directly above `find_toc_candidates`:

```python
def _toc_scan_indices(pages: list[str], max_front_fraction: float = 0.15, max_back_fraction: float = 0.05) -> set[int]:
    """The front/back-matter page-index range both find_toc_candidates
    (regex) and llm_extract_toc_entries (LLM fallback design spec §4) scan
    for a table-of-contents listing."""
    total = len(pages)
    if total == 0:
        return set()
    front_count = max(1, int(total * max_front_fraction))
    back_count = max(1, int(total * max_back_fraction))
    return set(range(min(front_count, total))) | set(range(max(0, total - back_count), total))
```

Then, inside `find_toc_candidates`, replace:

```python
    total = len(pages)
    if total == 0:
        return []
    front_count = max(1, int(total * max_front_fraction))
    back_count = max(1, int(total * max_back_fraction))
    scan_indices = sorted(set(range(min(front_count, total))) | set(range(max(0, total - back_count), total)))
    max_plausible_page_number = total * _TOC_MAX_PAGE_NUMBER_RATIO
```

with:

```python
    total = len(pages)
    if total == 0:
        return []
    scan_indices = sorted(_toc_scan_indices(pages, max_front_fraction, max_back_fraction))
    max_plausible_page_number = total * _TOC_MAX_PAGE_NUMBER_RATIO
```

- [ ] **Step 4: Run the full chapter_segmentation test file to verify the new tests pass and nothing broke**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -v`
Expected: PASS (all tests — `find_toc_candidates`'s existing tests are unaffected since the scan region computation is unchanged, just moved)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "refactor: extract _toc_scan_indices helper from find_toc_candidates"
```

---

## Task 5: Expose candidate clusters + author-aware disambiguation

**Files:**
- Modify: `backend/services/chapter_segmentation.py:154-243`
- Test: `backend/tests/test_chapter_segmentation.py`

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_chapter_segmentation.py`, add `ChapterStartCandidate` and `locate_chapter_start_candidates` to the existing import block from `backend.services.chapter_segmentation`. Then add these methods to `TestLocateChapterStart` (after `test_treats_nearby_repeated_header_as_one_location`):

```python
    def test_locate_chapter_start_candidates_exposes_competing_clusters(self):
        pages = [
            "Comparing Citation Styles\n\nBy Jane Author\n\nThis chapter examines APA style only.",
            "Unrelated filler page one.",
            "Unrelated filler page two.",
            "Unrelated filler page three.",
            "Unrelated filler page four.",
            "Comparing Citation Style\n\nBy John Smith\n\nAnother chapter about MLA style.",
        ]
        candidates = locate_chapter_start_candidates(pages, "Comparing Citation Styles", exclude_indices=set())
        self.assertEqual(len(candidates), 2)
        self.assertEqual({c.index for c in candidates}, {0, 5})
        # Sorted best-first.
        self.assertGreaterEqual(candidates[0].score, candidates[1].score)

    def test_author_confirmation_resolves_ambiguous_tie(self):
        # Same near-tie shape as test_rejects_ambiguous_tie, but the true
        # chapter's page also has its author's last name near the top --
        # locate_chapter_start_candidates flags that cluster author_confirmed,
        # and the bonus lets it beat an equally-scoring, unconfirmed rival
        # that would otherwise make the match ambiguous.
        pages = [
            "Comparing Citation Styles\n\nBy Jane Doe\n\nThis chapter examines APA style only.",
            "Unrelated filler page one.",
            "Unrelated filler page two.",
            "Unrelated filler page three.",
            "Unrelated filler page four.",
            "Comparing Citation Style\n\nBy John Smith\n\nAnother chapter about MLA style.",
        ]
        match = locate_chapter_start(pages, "Comparing Citation Styles", exclude_indices=set(), authors=("Jane Doe",))
        self.assertIsNotNone(match)
        self.assertEqual(match.index, 0)
        self.assertTrue(match.author_confirmed)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py::TestLocateChapterStart -v`
Expected: FAIL — `ImportError: cannot import name 'ChapterStartCandidate'` (and, once that import is added, `TypeError: locate_chapter_start() got an unexpected keyword argument 'authors'`)

- [ ] **Step 3: Implement the refactor**

In `backend/services/chapter_segmentation.py`, replace the whole block from the `_PAGE_NUMBER_TOKEN_RE` constant through the end of `locate_chapter_start` (originally lines 154–243) with:

```python
_PAGE_NUMBER_TOKEN_RE = re.compile(r"^[0-9]{1,4}$|^[ivxlcdm]{1,7}$", re.IGNORECASE)
_LOCATE_SCORE_THRESHOLD = 80.0  # rapidfuzz partial_ratio, 0-100
# rapidfuzz partial_ratio is unreliable on very short strings (a near-blank
# page's head can trivially "perfectly align" with a short substring of the
# title, scoring 100 despite being meaningless) -- require this many
# stripped characters before a page is even considered a candidate.
_LOCATE_MIN_HEAD_CHARS = 20
# Top candidate cluster must beat the runner-up cluster by this much -- a
# single qualifying cluster is never rejected on margin grounds alone.
_LOCATE_MARGIN_REQUIRED = 8.0
# A running header often repeats a chapter's title on several of its OWN
# pages (e.g. the title appears on both the opening page and a later page of
# the same chapter, with a gap where an intervening page didn't score highly
# -- empirically observed as a real 2-page gap in an evaluation book). Pages
# this close together are treated as one location, not competing candidates,
# so the chapter's own repeated header is never mistaken for ambiguity.
_LOCATE_CLUSTER_GAP = 3
# An author's last name appearing near the top of a candidate page is a much
# stronger opening-page signal than title text alone (a running header can
# repeat the title on every page of a chapter, but rarely repeats the
# author's name on non-opening pages) -- see design spec §5. Chosen so a
# confirmed candidate reliably beats a same-scoring unconfirmed rival
# (15 > _LOCATE_MARGIN_REQUIRED's 8, with room to spare) without being large
# enough to override a rival that scores far higher on title match alone.
_AUTHOR_CONFIRMED_BONUS = 15.0


@dataclass(frozen=True)
class ChapterStartMatch:
    """Result of a successful locate_chapter_start lookup, carrying the raw
    signal needed to derive a real per-chapter confidence (see
    match_confidence) rather than a flat constant.
    """

    index: int
    score: float  # winning cluster's best rapidfuzz partial_ratio, 0-100
    margin: float  # score minus the runner-up cluster's score; equals score
    # itself when there was no competing cluster at all (uncontested match)
    author_confirmed: bool = False


@dataclass(frozen=True)
class ChapterStartCandidate:
    """One clustered candidate location from locate_chapter_start_candidates
    -- exposes what locate_chapter_start only used internally before, so an
    ambiguous result's competing clusters can be inspected (e.g. for LLM
    disambiguation, see llm_disambiguate_chapter_start)."""

    index: int
    score: float
    author_confirmed: bool


def _candidate_ranking_key(candidate: ChapterStartCandidate) -> float:
    return candidate.score + (_AUTHOR_CONFIRMED_BONUS if candidate.author_confirmed else 0.0)


def locate_chapter_start_candidates(
    pages: list[str], title: str, exclude_indices: set[int], authors: tuple[str, ...] = (),
) -> list[ChapterStartCandidate]:
    """Find every plausible page location for `title`'s chapter opening,
    clustered exactly as locate_chapter_start does internally (see its
    docstring for the clustering rationale), sorted best-first (the
    author-confirmed bonus is already applied to the ranking). Returns an
    empty list when nothing clears the score threshold at all.

    When `authors` is non-empty, a candidate page is additionally checked
    for whether any given author's last name (lowercased) appears in the
    same head-of-page text the title is scored against -- mirrors the
    disambiguation approach scripts/ground_truth_helper.py already
    validated by hand while building this project's evaluation set.
    """
    last_names = tuple(a.split()[-1].lower() for a in authors if a.strip())
    raw_candidates: list[tuple[int, float, bool]] = []
    for index, text in enumerate(pages):
        if index in exclude_indices:
            continue
        head = text[:200]
        if len(head.strip()) < _LOCATE_MIN_HEAD_CHARS:
            continue
        head_lower = head.lower()
        score = fuzz.partial_ratio(title.lower(), head_lower)
        if score >= _LOCATE_SCORE_THRESHOLD:
            confirmed = bool(last_names) and any(name in head_lower for name in last_names)
            raw_candidates.append((index, score, confirmed))
    if not raw_candidates:
        return []

    raw_candidates.sort()
    clusters: list[ChapterStartCandidate] = []
    cluster_start, cluster_max, cluster_confirmed = raw_candidates[0]
    prev_index = raw_candidates[0][0]
    for index, score, confirmed in raw_candidates[1:]:
        if index - prev_index <= _LOCATE_CLUSTER_GAP:
            cluster_max = max(cluster_max, score)
            cluster_confirmed = cluster_confirmed or confirmed
        else:
            clusters.append(ChapterStartCandidate(index=cluster_start, score=cluster_max, author_confirmed=cluster_confirmed))
            cluster_start, cluster_max, cluster_confirmed = index, score, confirmed
        prev_index = index
    clusters.append(ChapterStartCandidate(index=cluster_start, score=cluster_max, author_confirmed=cluster_confirmed))

    clusters.sort(key=_candidate_ranking_key, reverse=True)
    return clusters


def locate_chapter_start(
    pages: list[str], title: str, exclude_indices: set[int], authors: tuple[str, ...] = (),
) -> ChapterStartMatch | None:
    """Find the PDF page index whose text most plausibly begins with `title`.

    This is a content lookup, not an index computation: it never assumes the
    TOC's printed page number corresponds to this page's index. Delegates
    candidate-gathering and clustering to locate_chapter_start_candidates,
    then applies the ambiguity guard: returns the earliest page of the
    best-scoring cluster, or None if no page qualifies or the top cluster
    doesn't beat the runner-up by _LOCATE_MARGIN_REQUIRED. Default
    `authors=()` reproduces the exact behavior before author-aware
    disambiguation was added -- see locate_chapter_start_candidates.
    """
    candidates = locate_chapter_start_candidates(pages, title, exclude_indices, authors)
    if not candidates:
        return None

    best = candidates[0]
    runner_up_key = _candidate_ranking_key(candidates[1]) if len(candidates) > 1 else 0.0
    margin = _candidate_ranking_key(best) - runner_up_key
    if len(candidates) > 1 and margin < _LOCATE_MARGIN_REQUIRED:
        return None
    return ChapterStartMatch(index=best.index, score=best.score, margin=margin, author_confirmed=best.author_confirmed)
```

- [ ] **Step 4: Run the full chapter_segmentation test file to verify everything passes**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -v`
Expected: PASS (all tests — every existing `TestLocateChapterStart`/`TestMatchConfidence`/`TestAnalyzeAttachment` test is unaffected since `authors=()` reproduces identical scores/margins/None-behavior, plus the 2 new tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "feat: expose locate_chapter_start candidate clusters, add author-aware disambiguation"
```

---

## Task 6: Extract `_locate_toc_entries`/`_chapters_from_located`, add `source` field

**Files:**
- Modify: `backend/services/chapter_segmentation.py:326-401` (post-Task-5 line numbers will differ; locate by content — the `analyze_attachment` function and the `_TRAILING_BLANK_PAGE_MAX_CHARS` constant above it)
- Test: `backend/tests/test_chapter_segmentation.py`

- [ ] **Step 1: Write the failing test**

In `backend/tests/test_chapter_segmentation.py`, add to `TestAnalyzeAttachment` (after `test_authors_attached_to_chapters`):

```python
    def test_chapters_default_to_heuristic_source(self):
        result = analyze_attachment(self._fake_book_pages())
        self.assertTrue(all(c["source"] == "heuristic" for c in result["chapters"]))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py::TestAnalyzeAttachment::test_chapters_default_to_heuristic_source -v`
Expected: FAIL — `KeyError: 'source'`

- [ ] **Step 3: Extract the two helpers and add the `source` field**

In `backend/services/chapter_segmentation.py`, replace the entire `analyze_attachment` function (from `def analyze_attachment(pages: list[str]) -> dict:` through its closing `}` and `return` block, keeping the `_TRAILING_BLANK_PAGE_MAX_CHARS` constant above it unchanged) with:

```python
def _locate_toc_entries(
    pages: list[str], toc_entries: list[TocEntry], exclude_indices: set[int]
) -> tuple[list[tuple[TocEntry, ChapterStartMatch]], list[TocEntry]]:
    """Attempts locate_chapter_start for every entry. Returns (located pairs
    sorted by match index, entries that failed to locate at all -- either
    zero candidates cleared the score threshold, or a genuine unresolved
    ambiguity). Passes each entry's own authors through (empty for
    regex-found entries) for author-aware disambiguation."""
    located: list[tuple[TocEntry, ChapterStartMatch]] = []
    unlocated: list[TocEntry] = []
    for entry in toc_entries:
        match = locate_chapter_start(pages, entry.title, exclude_indices=exclude_indices, authors=entry.authors)
        if match is not None:
            located.append((entry, match))
        else:
            unlocated.append(entry)
    located.sort(key=lambda pair: pair[1].index)
    return located, unlocated


def _chapters_from_located(
    pages: list[str],
    located: list[tuple[TocEntry, ChapterStartMatch]],
    entry_source: dict[TocEntry, str] | None = None,
) -> list[dict]:
    """Turns located (entry, match) pairs into the final chapter dict list:
    clusters each entry's page range against its neighbor, trims trailing
    blank/divider pages, extracts citation_pages and confidence. entry_source
    marks which TocEntry objects were LLM-sourced (default "heuristic" for
    any entry not present in the mapping) -- see design spec §7.
    """
    entry_source = entry_source or {}
    total_pages = len(pages)
    chapters: list[dict] = []
    for i, (entry, match) in enumerate(located):
        start_index = match.index
        end_index = (located[i + 1][1].index - 1) if i + 1 < len(located) else (total_pages - 1)
        if end_index < start_index:
            continue  # degenerate/ambiguous overlap — skip rather than guess

        # Back off past trailing blank/divider pages (e.g. a blank page
        # forcing the next chapter to start on a recto page) -- these
        # belong to neither neighboring chapter. Mirrors the proven
        # build_draft logic in scripts/ground_truth_helper.py, which needed
        # the identical fix when building ground truth by hand.
        while end_index > start_index and len(pages[end_index].strip()) < _TRAILING_BLANK_PAGE_MAX_CHARS:
            end_index -= 1

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
            "confidence": match_confidence(match.score, match.margin),
            "page_mapping_confidence": page_mapping_confidence,
            "source": entry_source.get(entry, "heuristic"),
        })
    return chapters


def analyze_attachment(pages: list[str]) -> dict:
    """Orchestrate TOC detection, content-based localization, printed-page
    extraction, and author NER into the per-attachment output described in
    design spec §5. Pure and synchronous -- unchanged behavior/speed from
    before the LLM fallback project; every chapter now also carries
    "source": "heuristic" (see design spec §7).
    """
    total_pages = len(pages)
    toc_entries = find_toc_candidates(pages)
    toc_page_indices = {e.source_page_index for e in toc_entries}

    located, _unlocated = _locate_toc_entries(pages, toc_entries, exclude_indices=toc_page_indices)
    chapters = _chapters_from_located(pages, located)

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

- [ ] **Step 4: Run the full chapter_segmentation test file to verify everything passes**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -v`
Expected: PASS (all tests — the extraction is behavior-preserving; the new `source` key doesn't affect any existing assertion, since none of them check full-dict equality)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "refactor: extract TOC-entry location/clustering helpers, tag chapter source"
```

---

## Task 7: `llm_extract_toc_entries`

**Files:**
- Modify: `backend/services/chapter_segmentation.py` (imports at top, new function after `find_toc_candidates`)
- Test: `backend/tests/test_chapter_segmentation.py`

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_chapter_segmentation.py`, add `llm_extract_toc_entries` to the import block from `backend.services.chapter_segmentation`, and add a new test class after `TestFindTocCandidates`:

```python
class TestLlmExtractTocEntries(unittest.IsolatedAsyncioTestCase):
    def _fake_llm(self, response: str):
        llm = MagicMock()
        llm.generate = AsyncMock(return_value=response)
        return llm

    async def test_parses_chapter_list_from_llm_response(self):
        response = (
            '[{"title": "Introduction", "authors": ["Jane Author"], "printed_page_number": 1}, '
            '{"title": "Comparing Citation Styles", "authors": [], "printed_page_number": null}]'
        )
        llm = self._fake_llm(response)
        pages = ["Front matter page with an irregularly formatted chapter listing."] * 5
        entries = await llm_extract_toc_entries(pages, llm)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].title, "Introduction")
        self.assertEqual(entries[0].authors, ("Jane Author",))
        self.assertEqual(entries[0].printed_page_number, 1)
        self.assertEqual(entries[1].printed_page_number, -1)  # null -> sentinel, unused downstream

    async def test_returns_empty_list_on_malformed_response(self):
        llm = self._fake_llm("not json at all")
        entries = await llm_extract_toc_entries(["some front matter"] * 5, llm)
        self.assertEqual(entries, [])

    async def test_skips_entries_with_too_short_title(self):
        llm = self._fake_llm('[{"title": "Hi", "authors": [], "printed_page_number": 1}]')
        entries = await llm_extract_toc_entries(["front matter"] * 5, llm)
        self.assertEqual(entries, [])

    async def test_returns_empty_list_for_empty_pages(self):
        llm = self._fake_llm("[]")
        entries = await llm_extract_toc_entries([], llm)
        self.assertEqual(entries, [])
        llm.generate.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py::TestLlmExtractTocEntries -v`
Expected: FAIL — `ImportError: cannot import name 'llm_extract_toc_entries'`

- [ ] **Step 3: Add imports and implement the function**

At the top of `backend/services/chapter_segmentation.py`, add (alongside the existing imports):

```python
import logging

from backend.services.llm import LLMService
from backend.utils.llm_json import parse_json_array, parse_json_object
```

and, right after the imports, before the module docstring's constants begin:

```python
logger = logging.getLogger(__name__)
```

Then add this constant and function directly after `find_toc_candidates` (before the `_PAGE_NUMBER_TOKEN_RE` section):

```python
_LLM_TOC_EXTRACTION_PROMPT = """\
You are reading the front and back matter of a scanned/extracted book to \
find its table of contents. Some layouts don't use simple dotted leaders \
(e.g. "Title ..... 12") -- read the text directly rather than pattern-matching.

{page_blocks}

Return ONLY a JSON array, one entry per real chapter -- skip \
acknowledgements, bibliography, index, and part-divider pages:
[{{"title": "...", "authors": ["First Last", ...], "printed_page_number": 12}}]

If a chapter's printed page number is not visible in this text, use null \
for printed_page_number. If authors are not identifiable, use an empty list."""


async def llm_extract_toc_entries(pages: list[str], llm_service: LLMService) -> list[TocEntry]:
    """Reads the same front/back-matter page range find_toc_candidates
    already scans (_toc_scan_indices), sends their raw text verbatim to the
    LLM, and asks it to return the book's chapter listing as it actually
    appears -- for layouts too irregular for the regex (no dot leaders,
    multi-column, unconventional spacing/punctuation) but readable by
    inspection. Never asks the LLM for a physical page index -- only
    title/authors/printed_page_number, exactly like a regex-found TocEntry,
    so the result flows into the SAME locate_chapter_start content-search
    step used for every other TOC entry (design spec §4).
    """
    scan_indices = sorted(_toc_scan_indices(pages))
    if not scan_indices:
        return []
    page_blocks = "\n\n".join(f"[PAGE {i}]\n{pages[i]}" for i in scan_indices)
    prompt = _LLM_TOC_EXTRACTION_PROMPT.format(page_blocks=page_blocks)

    try:
        raw = await llm_service.generate(prompt=prompt, max_tokens=1024, temperature=0.0)
        items = parse_json_array(raw)
    except Exception:
        logger.warning("llm_extract_toc_entries: LLM call or JSON parse failed", exc_info=True)
        return []

    entries: list[TocEntry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if len(title) < 3:
            continue
        authors = tuple(str(a) for a in (item.get("authors") or []) if str(a).strip())
        printed = item.get("printed_page_number")
        printed_page_number = int(printed) if isinstance(printed, (int, float)) else -1
        # source_page_index is a sentinel here -- unlike a regex-found entry,
        # an LLM-extracted entry has no single "the TOC line was on this
        # page" origin; the orchestration layer excludes the whole scanned
        # front/back-matter range instead (see _toc_scan_indices).
        entries.append(TocEntry(title=title, printed_page_number=printed_page_number, source_page_index=-1, authors=authors))
    return entries
```

- [ ] **Step 4: Run the full chapter_segmentation test file to verify everything passes**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "feat: add llm_extract_toc_entries for irregular TOC layouts"
```

---

## Task 8: `llm_disambiguate_chapter_start`

**Files:**
- Modify: `backend/services/chapter_segmentation.py` (new function after `locate_chapter_start`)
- Test: `backend/tests/test_chapter_segmentation.py`

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_chapter_segmentation.py`, add `llm_disambiguate_chapter_start` and `_LOCATE_MARGIN_REQUIRED` to the import block from `backend.services.chapter_segmentation`, and add a new test class after `TestLlmExtractTocEntries`:

```python
class TestLlmDisambiguateChapterStart(unittest.IsolatedAsyncioTestCase):
    def _fake_llm(self, response: str):
        llm = MagicMock()
        llm.generate = AsyncMock(return_value=response)
        return llm

    async def test_picks_chosen_candidate(self):
        candidates = [
            ChapterStartCandidate(index=0, score=95.0, author_confirmed=False),
            ChapterStartCandidate(index=5, score=93.0, author_confirmed=False),
        ]
        pages = ["Comparing Citation Styles by Jane Doe..."] * 6
        llm = self._fake_llm('{"chosen_candidate": 2}')
        match = await llm_disambiguate_chapter_start(pages, "Comparing Citation Styles", (), candidates, llm)
        self.assertIsNotNone(match)
        self.assertEqual(match.index, 5)
        self.assertEqual(match.score, 93.0)
        self.assertEqual(match.margin, _LOCATE_MARGIN_REQUIRED)

    async def test_returns_none_when_llm_picks_none(self):
        candidates = [ChapterStartCandidate(index=0, score=95.0, author_confirmed=False)]
        pages = ["some text"] * 2
        llm = self._fake_llm('{"chosen_candidate": null}')
        match = await llm_disambiguate_chapter_start(pages, "Title", (), candidates, llm)
        self.assertIsNone(match)

    async def test_returns_none_on_out_of_range_choice(self):
        candidates = [ChapterStartCandidate(index=0, score=95.0, author_confirmed=False)]
        pages = ["some text"] * 2
        llm = self._fake_llm('{"chosen_candidate": 5}')
        match = await llm_disambiguate_chapter_start(pages, "Title", (), candidates, llm)
        self.assertIsNone(match)

    async def test_returns_none_on_malformed_response(self):
        candidates = [ChapterStartCandidate(index=0, score=95.0, author_confirmed=False)]
        pages = ["some text"] * 2
        llm = self._fake_llm("not json")
        match = await llm_disambiguate_chapter_start(pages, "Title", (), candidates, llm)
        self.assertIsNone(match)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py::TestLlmDisambiguateChapterStart -v`
Expected: FAIL — `ImportError: cannot import name 'llm_disambiguate_chapter_start'`

- [ ] **Step 3: Implement the function**

Add this constant and function directly after `locate_chapter_start`:

```python
_LLM_DISAMBIGUATION_PROMPT = """\
A chapter titled "{title}"{author_clause} could not be confidently located \
because more than one page in this book plausibly matches. Below are the \
competing candidate pages -- pick which one is the chapter's TRUE opening \
page (its title page, not a continuation page repeating the same running \
header).

{candidate_blocks}

Return ONLY JSON: {{"chosen_candidate": <candidate number>}} or \
{{"chosen_candidate": null}} if none of them are actually the right page."""


async def llm_disambiguate_chapter_start(
    pages: list[str],
    title: str,
    authors: tuple[str, ...],
    candidates: list[ChapterStartCandidate],
    llm_service: LLMService,
) -> ChapterStartMatch | None:
    """Shows the LLM each competing candidate's page index + a short
    snippet (page head, matching the ~200-char window locate_chapter_start
    itself scores against) and asks it to pick which one is the chapter's
    true opening page, or none. A small, bounded prompt -- a handful of
    short snippets, never whole-book text -- since locate_chapter_start_candidates
    has already narrowed the field to the real contenders (design spec §6).
    """
    author_clause = f" by {', '.join(authors)}" if authors else ""
    candidate_blocks = "\n\n".join(
        f"[CANDIDATE {n}] page {c.index}:\n{pages[c.index][:300]}"
        for n, c in enumerate(candidates, start=1)
    )
    prompt = _LLM_DISAMBIGUATION_PROMPT.format(title=title, author_clause=author_clause, candidate_blocks=candidate_blocks)

    try:
        raw = await llm_service.generate(prompt=prompt, max_tokens=64, temperature=0.0)
        data = parse_json_object(raw)
    except Exception:
        logger.warning("llm_disambiguate_chapter_start: LLM call or JSON parse failed", exc_info=True)
        return None

    chosen = data.get("chosen_candidate")
    if not isinstance(chosen, int) or not (1 <= chosen <= len(candidates)):
        return None
    winner = candidates[chosen - 1]
    # Deliberately conservative: margin is pinned at exactly the minimum
    # required to clear locate_chapter_start's own ambiguity guard, never
    # higher -- the LLM resolved WHICH candidate is right, it did not make
    # the underlying textual match itself any less genuinely contested
    # (design spec §6).
    return ChapterStartMatch(index=winner.index, score=winner.score, margin=_LOCATE_MARGIN_REQUIRED, author_confirmed=winner.author_confirmed)
```

- [ ] **Step 4: Run the full chapter_segmentation test file to verify everything passes**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "feat: add llm_disambiguate_chapter_start for ambiguous matches"
```

---

## Task 9: `analyze_attachment_with_llm_fallback` orchestration

**Files:**
- Modify: `backend/services/chapter_segmentation.py` (new function, placed after `_chapters_from_located`/`analyze_attachment`, requires `llm_extract_toc_entries`/`llm_disambiguate_chapter_start` from Tasks 7–8 to already exist above it — or simply appended anywhere after them, Python doesn't care about definition order across module-level functions as long as all exist before first call at runtime)
- Test: `backend/tests/test_chapter_segmentation.py`

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_chapter_segmentation.py`, add `analyze_attachment_with_llm_fallback` to the import block from `backend.services.chapter_segmentation`, and add a new test class after `TestAnalyzeAttachment`:

```python
class TestAnalyzeAttachmentWithLlmFallback(unittest.IsolatedAsyncioTestCase):
    def _fake_llm(self, toc_response: str | None = None, disambiguation_response: str | None = None):
        llm = MagicMock()
        responses = [r for r in (toc_response, disambiguation_response) if r is not None]
        llm.generate = AsyncMock(side_effect=responses)
        return llm

    async def test_llm_toc_extraction_fires_when_heuristic_finds_nothing(self):
        # 20 pages total so the front/back scan zones (15%/5% of the page
        # count, same fractions find_toc_candidates and llm_extract_toc_entries
        # both use) don't accidentally overlap the two real chapters' opening
        # pages, which are placed well inside the body.
        filler = "Unrelated body filler text, nothing chapter-related in this passage at all."
        pages = [
            "Front matter with an irregular listing the regex can't parse: "
            "Introduction (Jane Author) ... Comparing Citation Styles (John Smith)",
            "Filler front-matter page, nothing chapter-like here at all.",
            "Filler front-matter page, nothing chapter-like here at all.",
            *([filler] * 7),  # indices 3-9
            "Introduction\n\nJane Author\n\nThis book explores reference management in depth.",  # index 10
            *([filler] * 4),  # indices 11-14
            "Comparing Citation Styles\n\nJohn Smith\n\nThis chapter examines APA and MLA styles.",  # index 15
            *([filler] * 3),  # indices 16-18
            "Back matter index page, nothing chapter-related here.",  # index 19
        ]
        self.assertEqual(len(pages), 20)
        response = (
            '[{"title": "Introduction", "authors": ["Jane Author"], "printed_page_number": 1}, '
            '{"title": "Comparing Citation Styles", "authors": ["John Smith"], "printed_page_number": 15}]'
        )
        llm = self._fake_llm(toc_response=response)
        result = await analyze_attachment_with_llm_fallback(pages, llm)
        self.assertTrue(result["diagnostics"]["llm_toc_extraction_used"])
        self.assertEqual(len(result["chapters"]), 2)
        self.assertTrue(all(c["source"] == "llm" for c in result["chapters"]))
        self.assertEqual(result["chapters"][0]["pdf_start_index"], 10)
        self.assertEqual(result["chapters"][1]["pdf_start_index"], 15)

    async def test_does_not_call_llm_when_heuristic_already_succeeds(self):
        pages = [
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
        llm = MagicMock()
        llm.generate = AsyncMock(side_effect=AssertionError("LLM should not be called"))
        result = await analyze_attachment_with_llm_fallback(pages, llm)
        self.assertFalse(result["diagnostics"]["llm_toc_extraction_used"])
        self.assertEqual(result["diagnostics"]["llm_disambiguation_used"], 0)
        llm.generate.assert_not_called()

    async def test_llm_disambiguation_resolves_ambiguous_chapter(self):
        filler = ["Unrelated filler page.", "Unrelated filler page.", "Unrelated filler page.", "Unrelated filler page."]
        pages = [
            "CONTENTS\n"
            "Introduction ..... 1\n"
            "Comparing Citation Styles ..... 10\n"
            "Appendix ..... 20\n",
            "Introduction\nJane Author\n\nThis book explores reference management.\n\n1",
            "Comparing Citation Styles\n\nBy Jane Doe\n\nThis chapter examines APA style only.",
            *filler,
            "Comparing Citation Style\n\nBy John Smith\n\nAnother chapter about MLA style.",
        ]
        llm = self._fake_llm(disambiguation_response='{"chosen_candidate": 1}')
        result = await analyze_attachment_with_llm_fallback(pages, llm)
        self.assertFalse(result["diagnostics"]["llm_toc_extraction_used"])
        self.assertEqual(result["diagnostics"]["llm_disambiguation_used"], 1)
        sources = {c["title"]: c["source"] for c in result["chapters"]}
        self.assertEqual(sources.get("Comparing Citation Styles"), "llm")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py::TestAnalyzeAttachmentWithLlmFallback -v`
Expected: FAIL — `ImportError: cannot import name 'analyze_attachment_with_llm_fallback'`

- [ ] **Step 3: Implement the orchestration function**

Add this function directly after `analyze_attachment`:

```python
async def analyze_attachment_with_llm_fallback(pages: list[str], llm_service: LLMService) -> dict:
    """Runs the heuristic TOC/locate pipeline first, then applies the two
    LLM fallback paths (design spec §4/§6) only where the heuristic pass
    reports a failure: TOC extraction when it found nothing usable at all,
    and per-chapter start disambiguation for any entry left genuinely
    ambiguous (as opposed to zero candidates -- out of scope, see spec §2).
    Any LLM failure (network, malformed JSON) is swallowed and treated as
    "fallback unavailable" -- never leaves the result worse than the pure
    heuristic pass would have (spec §11).
    """
    toc_entries = find_toc_candidates(pages)
    toc_page_indices = {e.source_page_index for e in toc_entries}
    located, unlocated = _locate_toc_entries(pages, toc_entries, exclude_indices=toc_page_indices)
    heuristic_chapters = _chapters_from_located(pages, located)

    entry_source: dict[TocEntry, str] = {}
    llm_toc_extraction_used = False
    if len(toc_entries) == 0 or len(heuristic_chapters) == 0:
        try:
            llm_entries = await llm_extract_toc_entries(pages, llm_service)
        except Exception:
            logger.warning("analyze_attachment_with_llm_fallback: TOC extraction failed", exc_info=True)
            llm_entries = []
        if llm_entries:
            toc_entries = llm_entries
            toc_page_indices = _toc_scan_indices(pages)
            entry_source = {e: "llm" for e in toc_entries}
            located, unlocated = _locate_toc_entries(pages, toc_entries, exclude_indices=toc_page_indices)
            llm_toc_extraction_used = True

    disambiguation_count = 0
    for entry in unlocated:
        candidates = locate_chapter_start_candidates(pages, entry.title, exclude_indices=toc_page_indices, authors=entry.authors)
        if len(candidates) <= 1:
            continue  # zero-candidate case is out of scope for v1 (design spec §2)
        try:
            resolved = await llm_disambiguate_chapter_start(pages, entry.title, entry.authors, candidates, llm_service)
        except Exception:
            logger.warning("analyze_attachment_with_llm_fallback: disambiguation failed for %r", entry.title, exc_info=True)
            continue
        if resolved is not None:
            located.append((entry, resolved))
            entry_source[entry] = "llm"
            disambiguation_count += 1

    located.sort(key=lambda pair: pair[1].index)
    chapters = _chapters_from_located(pages, located, entry_source=entry_source)

    return {
        "total_pdf_pages": len(pages),
        "segmentation_confidence": "high" if chapters else "low",
        "chapters": chapters,
        "diagnostics": {
            "toc_pages_scanned": sorted(toc_page_indices),
            "toc_matches_found": len(toc_entries),
            "toc_matches_located": len(located),
            "llm_toc_extraction_used": llm_toc_extraction_used,
            "llm_disambiguation_used": disambiguation_count,
        },
    }
```

- [ ] **Step 4: Run the full chapter_segmentation test file to verify everything passes**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "feat: add analyze_attachment_with_llm_fallback orchestrator"
```

---

## Task 10: Thread `llm_service` through `run()`

**Files:**
- Modify: `backend/services/chapter_segmentation.py:404-468` (post-Task-9 line numbers will differ; locate by content — the `run()` function)
- Test: `backend/tests/test_chapter_segmentation.py`

- [ ] **Step 1: Write the failing test**

In `backend/tests/test_chapter_segmentation.py`, add to `TestRun` (after `test_processes_unlinked_book`):

```python
    def test_uses_llm_fallback_when_llm_service_provided(self):
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"data": {"key": "BOOK0003", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0002", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"
        fake_llm = MagicMock()

        with unittest.mock.patch(
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["Just filler prose, no TOC pattern here at all. " * 3],
        ), unittest.mock.patch(
            "backend.services.chapter_segmentation.analyze_attachment_with_llm_fallback",
            new_callable=AsyncMock,
            return_value={"total_pdf_pages": 1, "segmentation_confidence": "low", "chapters": [], "diagnostics": {}},
        ) as mock_fallback:
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
                llm_service=fake_llm,
            ))
        mock_fallback.assert_called_once()
        self.assertEqual(result["attachments"][0]["item_key"], "BOOK0003")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py::TestRun::test_uses_llm_fallback_when_llm_service_provided -v`
Expected: FAIL — `TypeError: run() got an unexpected keyword argument 'llm_service'`

- [ ] **Step 3: Add the parameter and branch**

In `backend/services/chapter_segmentation.py`, change the `run()` signature from:

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
) -> dict:
```

to:

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
) -> dict:
```

Then change the block:

```python
        analysis = analyze_attachment(pages)
        attachments_out.append({
            "item_key": item_key,
            "attachment_key": attachment_key,
            "has_text_layer": True,
            "needs_ocr": False,
            **analysis,
        })
```

to:

```python
        if llm_service is not None:
            analysis = await analyze_attachment_with_llm_fallback(pages, llm_service)
        else:
            analysis = analyze_attachment(pages)
        attachments_out.append({
            "item_key": item_key,
            "attachment_key": attachment_key,
            "has_text_layer": True,
            "needs_ocr": False,
            **analysis,
        })
```

- [ ] **Step 4: Run the full chapter_segmentation test file to verify everything passes**

Run: `uv run pytest backend/tests/test_chapter_segmentation.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/chapter_segmentation.py backend/tests/test_chapter_segmentation.py
git commit -m "feat: thread optional llm_service through chapter_segmentation.run"
```

---

## Task 11: CLI `--llm-fallback` flag

**Files:**
- Modify: `scripts/analyze_book_chapters.py`

- [ ] **Step 1: Add the flag and wire it up**

In `scripts/analyze_book_chapters.py`, change `_main` from:

```python
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
```

to:

```python
async def _main(args: argparse.Namespace) -> int:
    library_type, _numeric_id, library_id = parse_library_slug(args.library_slug)
    client = ZoteroWebAPI(api_key=args.api_key)

    item_keys = args.item_keys.split(",") if args.item_keys else None

    llm_service = None
    if args.llm_fallback:
        from backend.dependencies import make_llm_service
        llm_service = make_llm_service()

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
        llm_service=llm_service,
    )
    bar.close()
```

And add the argument in `main()`, after the existing `--relink` argument:

```python
    parser.add_argument(
        "--llm-fallback",
        action="store_true",
        help="Enable the LLM-based fallback for chapters the heuristic pass finds "
             "nothing or is ambiguous about (slower, calls a configured LLM API)",
    )
```

- [ ] **Step 2: Verify the CLI still parses correctly**

Run: `uv run python scripts/analyze_book_chapters.py --help`
Expected: Help text lists `--llm-fallback` alongside the existing flags, exit code 0

- [ ] **Step 3: Commit**

```bash
git add scripts/analyze_book_chapters.py
git commit -m "feat: add --llm-fallback flag to analyze_book_chapters CLI"
```

---

## Task 12: API `enable_llm_fallback` field

**Files:**
- Modify: `backend/api/chapter_linking.py`
- Test: `backend/tests/test_chapter_linking_api.py`

- [ ] **Step 1: Write the failing tests**

In `backend/tests/test_chapter_linking_api.py`, add to `TestAnalyzeEndpoint` (after `test_returns_job_id_immediately`):

```python
    @patch("backend.api.chapter_linking.make_llm_service")
    @patch("backend.api.chapter_linking.ZoteroWebAPI")
    @patch("backend.api.chapter_linking.analyze_run", new_callable=AsyncMock)
    def test_passes_llm_service_when_fallback_enabled(self, mock_run, mock_web_api, mock_make_llm_service):
        mock_run.return_value = {"slug": "groups/1", "attachments": []}
        fake_llm = object()
        mock_make_llm_service.return_value = fake_llm
        response = self.client.post(
            "/api/chapter-linking/analyze",
            json={"library_slug": "groups/1", "api_key": "fake-key", "enable_llm_fallback": True},
        )
        self.assertEqual(response.status_code, 200)
        mock_make_llm_service.assert_called_once()
        self.assertIs(mock_run.call_args.kwargs["llm_service"], fake_llm)

    @patch("backend.api.chapter_linking.make_llm_service")
    @patch("backend.api.chapter_linking.ZoteroWebAPI")
    @patch("backend.api.chapter_linking.analyze_run", new_callable=AsyncMock)
    def test_skips_llm_service_by_default(self, mock_run, mock_web_api, mock_make_llm_service):
        mock_run.return_value = {"slug": "groups/1", "attachments": []}
        response = self.client.post(
            "/api/chapter-linking/analyze",
            json={"library_slug": "groups/1", "api_key": "fake-key"},
        )
        self.assertEqual(response.status_code, 200)
        mock_make_llm_service.assert_not_called()
        self.assertIsNone(mock_run.call_args.kwargs["llm_service"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py -v -k llm`
Expected: FAIL — `AttributeError: <module 'backend.api.chapter_linking'> does not have the attribute 'make_llm_service'` (patch target doesn't exist yet)

- [ ] **Step 3: Add the field and wire it up**

In `backend/api/chapter_linking.py`, add to the existing imports:

```python
from backend.dependencies import make_llm_service
```

Change `AnalyzeRequest`:

```python
class AnalyzeRequest(BaseModel):
    library_slug: str
    api_key: str
    item_keys: list[str] | None = None
    relink: bool = False
    max_items: int | None = None
    enable_llm_fallback: bool = False
```

Change `start_analyze`:

```python
@router.post("/chapter-linking/analyze", response_model=JobIdResponse, summary="Analyze book PDFs for chapter-segmentation candidates")
async def start_analyze(request: AnalyzeRequest) -> JobIdResponse:
    library_type, _numeric_id, library_id = parse_library_slug(request.library_slug)
    client = ZoteroWebAPI(api_key=request.api_key)
    llm_service = make_llm_service() if request.enable_llm_fallback else None
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
                llm_service=llm_service,
            )
            tracker.update(job_id, result=result)
        except Exception as exc:  # noqa: BLE001 — surfaced via job status, not re-raised
            logger.exception("chapter-linking analyze job %s failed", job_id)
            tracker.update(job_id, error=str(exc))

    asyncio.create_task(_task())
    return JobIdResponse(job_id=job_id)
```

- [ ] **Step 4: Run the full API test file to verify everything passes**

Run: `uv run pytest backend/tests/test_chapter_linking_api.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add backend/api/chapter_linking.py backend/tests/test_chapter_linking_api.py
git commit -m "feat: add enable_llm_fallback field to the chapter-linking analyze endpoint"
```

---

## Task 13: Evaluation script for the LLM fallback

**Files:**
- Create: `scripts/evaluate_chapter_segmentation_llm_fallback.py`

- [ ] **Step 1: Write the script**

```python
#!/usr/bin/env python3
"""Runs the chapter-segmentation evaluation set (see backend/evaluation/
book-segmentation/) through analyze_attachment_with_llm_fallback instead of
the pure-heuristic analyze_attachment, and prints the same precision/recall
table format tests/test_chapter_segmentation_accuracy.py already uses, plus
per-book fallback-usage counts.

Requires a real, working LLM (reads normal app settings/API keys) and costs
a paid API call per book -- not a pytest test, run manually:

    uv run python scripts/evaluate_chapter_segmentation_llm_fallback.py

See docs/superpowers/specs/2026-07-25-llm-chapter-segmentation-fallback-design.md §10.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.dependencies import make_llm_service
from backend.services.chapter_segmentation import (
    analyze_attachment_with_llm_fallback,
    extract_page_texts_from_pdf_bytes,
)

_EVAL_DIR = Path(__file__).resolve().parent.parent / "backend" / "evaluation" / "book-segmentation"


def _load_manifest_books() -> list[dict]:
    # Mirrors tests/test_chapter_segmentation_accuracy.py's identically-named
    # helper -- kept as a separate copy rather than importing across the
    # tests/scripts boundary (tests/ is deliberately not a runtime dependency
    # of anything under scripts/).
    books = json.loads((_EVAL_DIR / "manifest.json").read_text(encoding="utf-8"))["books"]
    local_manifest_path = _EVAL_DIR / "manifest.local.json"
    if local_manifest_path.exists():
        books = books + json.loads(local_manifest_path.read_text(encoding="utf-8"))["books"]
    return books


def _available_books() -> list[tuple[Path, Path]]:
    pairs = []
    for book in _load_manifest_books():
        pdf_path = _EVAL_DIR / book["filename"]
        expected_path = _EVAL_DIR / (Path(book["filename"]).stem + ".expected.json")
        if pdf_path.exists() and expected_path.exists():
            pairs.append((pdf_path, expected_path))
    return pairs


async def _main() -> int:
    pairs = _available_books()
    if not pairs:
        print("No evaluation PDFs present -- run: uv run python scripts/fetch_evaluation_pdfs.py")
        return 1

    llm_service = make_llm_service()

    for pdf_path, expected_path in pairs:
        expected = json.loads(expected_path.read_text(encoding="utf-8"))["chapters"]
        pages = extract_page_texts_from_pdf_bytes(pdf_path.read_bytes())
        result = await analyze_attachment_with_llm_fallback(pages, llm_service)

        expected_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in expected}
        found_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in result["chapters"]}
        true_positives = expected_ranges & found_ranges

        precision = len(true_positives) / len(found_ranges) if found_ranges else 0.0
        recall = len(true_positives) / len(expected_ranges) if expected_ranges else 0.0
        diag = result["diagnostics"]
        print(
            f"{pdf_path.name}: precision={precision:.2f} recall={recall:.2f} "
            f"({len(true_positives)}/{len(found_ranges)} found, {len(true_positives)}/{len(expected_ranges)} expected) "
            f"llm_toc_extraction_used={diag.get('llm_toc_extraction_used')} "
            f"llm_disambiguation_used={diag.get('llm_disambiguation_used')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
```

- [ ] **Step 2: Verify it at least imports and parses cleanly**

Run: `uv run python -m py_compile scripts/evaluate_chapter_segmentation_llm_fallback.py`
Expected: No output, exit code 0

Run: `uv run python scripts/evaluate_chapter_segmentation_llm_fallback.py`
Expected (in this environment, no evaluation PDFs present): prints `No evaluation PDFs present -- run: uv run python scripts/fetch_evaluation_pdfs.py` and exits with code 1 — this confirms the script's imports and manifest-loading logic work end-to-end without needing real PDFs or a real LLM key

- [ ] **Step 3: Commit**

```bash
git add scripts/evaluate_chapter_segmentation_llm_fallback.py
git commit -m "feat: add manual evaluation script for the LLM chapter-segmentation fallback"
```

---

## Task 14: Document the fallback in the evaluation README

**Files:**
- Modify: `backend/evaluation/book-segmentation/README.md`

- [ ] **Step 1: Add the new subsection**

In `backend/evaluation/book-segmentation/README.md`, insert this new subsection immediately before the `## Current results` heading:

```markdown
### LLM-fallback evaluation

`scripts/evaluate_chapter_segmentation_llm_fallback.py` runs the same
evaluation set through `analyze_attachment_with_llm_fallback` instead of
the pure-heuristic `analyze_attachment` (see
`docs/superpowers/specs/2026-07-25-llm-chapter-segmentation-fallback-design.md`).
Unlike the harness above, this requires a real, working LLM (reads normal
app settings/API keys) and costs a paid API call per book, so it's a
manual script, not a pytest test:

```bash
uv run python scripts/evaluate_chapter_segmentation_llm_fallback.py
```

It prints the same precision/recall table format as the harness above, plus
per-book counts of how often each fallback path (`llm_toc_extraction_used`,
`llm_disambiguation_used`) actually fired. Run it after any prompt or
heuristic change to check whether the fallback is still net-helpful on the
real evaluation set.

```

Then, immediately under the existing `## Current results` heading (before its first paragraph), add:

```markdown
A heuristic-vs-LLM-fallback comparison table will be added here after the
first real run of `scripts/evaluate_chapter_segmentation_llm_fallback.py`
(see "LLM-fallback evaluation" above).

```

- [ ] **Step 2: Commit**

```bash
git add backend/evaluation/book-segmentation/README.md
git commit -m "docs: document the LLM chapter-segmentation fallback evaluation script"
```

---

## Final verification

- [ ] **Run the full default test suite**

Run: `uv run pytest -q`
Expected: PASS, no new failures (the LLM-fallback unit tests all use fake `LLMService` mocks, so they run in the default suite with no network/API key needed; `tests/test_chapter_segmentation_accuracy.py` stays outside `testpaths` and is unaffected)

- [ ] **Run the touched test files individually with verbose output, to eyeball the new test names**

Run: `uv run pytest backend/tests/test_llm_json.py backend/tests/test_query_router.py backend/tests/test_chapter_segmentation.py backend/tests/test_chapter_linking_api.py -v`
Expected: PASS, all new tests from Tasks 1–12 visible and green

- [ ] **Confirm no leftover references to the old private `_parse_json` name**

Run: `grep -rn "_parse_json" backend --include="*.py" | grep -v __pycache__`
Expected: no output (fully replaced by `parse_json_object`/`parse_json_array` in Tasks 1–2)

- [ ] **Confirm the CLI and evaluation scripts still parse**

Run: `uv run python -m py_compile scripts/analyze_book_chapters.py scripts/evaluate_chapter_segmentation_llm_fallback.py`
Expected: No output, exit code 0
