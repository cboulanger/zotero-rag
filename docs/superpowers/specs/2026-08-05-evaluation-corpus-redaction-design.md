# Chapter-Segmentation Evaluation Corpus Redaction — Design Spec

## 1. Goal

`backend/evaluation/book-segmentation/` evaluates the chapter-boundary
heuristics (`backend/services/chapter_segmentation.py`) against real,
in-copyright books. The books themselves (`*.pdf`, gitignored) and their OCR
output (`.ocr-cache/`, gitignored — see `backend/services/chapter_ocr.py`)
are never committed, which means nobody without a personal copy of each book
can reproduce a run, and CI cannot exercise the accuracy suite at all.

This spec defines a **redaction pipeline** that turns the real per-page text
each book's evaluation run sees (`backend.evaluation.harness.analysis_pages_for`'s
output) into a corpus safe to commit: the navigational/bibliographic
material the heuristics key off — the table of contents, chapter headings,
running headers, page numbers — is kept byte-for-byte as scanned, because
that material is routinely published as public bibliographic metadata by
library catalogs (e.g. the German National Library scans and republishes
book TOCs) and carries no meaningful copyright exposure on its own. Only the
actual chapter **prose** — the part that is genuinely the author's
copyrighted expression, and the part the heuristics never need to read for
meaning — is replaced, page by page, with random real words in the book's
own language.

The output is small enough, and free enough of the book's actual protected
content, to commit to the repository — letting anyone run the accuracy
suite (`backend/tests/test_chapter_segmentation_accuracy.py`) without ever
downloading a PDF.

## 2. Non-goals / explicit scope boundary

- **No change to production code paths.** `chapter_segmentation.py` and
  `chapter_common.py` are read-only inputs to this design (their functions
  and constants are imported and, in the generation tool, directly called —
  never modified) — this is evaluation tooling, not a runtime feature.
- **No redaction of the PDFs themselves.** `*.pdf` files stay gitignored and
  un-distributed; this spec only concerns the *extracted/OCR'd text* derived
  from them.
- **No change to the already-committed `*.expected.json` ground-truth
  files.** These contain short chapter titles and page ranges — the same
  category of bibliographic material this spec treats as safe to keep
  verbatim (§3.1) — so they need no separate treatment here.
- **No general-purpose text-anonymization library.** The redaction is scoped
  exactly to what §3 shows `chapter_segmentation.py` actually reads — not a
  reusable PII/redaction tool for other parts of the codebase.
- **No claim of legal sufficiency.** See §11. This reduces risk; it is not a
  substitute for judgment about what's safe to publish.
- **No guarantee that `extract_authors_near`'s spaCy-NER output survives
  redaction.** See §3.2 — that function reads further into the page than
  what's preserved, and its output doesn't affect the evaluation's measured
  metric, so this is explicitly not attempted.

## 3. Background: what the heuristics actually depend on

Every matching/scoring function in `chapter_segmentation.py` and
`chapter_common.py` was audited (`grep` for every `re.compile`, `fuzz.*`
call, and literal-string comparison, plus reading `_locate_toc_entries`,
`locate_chapter_start_candidates`, and `analyze_attachment` in full) to
determine exactly which spans of page text the algorithm's decisions
actually depend on.

### 3.1 What must stay verbatim (navigational/bibliographic — not a copyright concern)

- **The table of contents page(s).** `analyze_attachment` computes
  `toc_entries = find_toc_candidates(pages)` and
  `toc_page_indices = {e.source_page_index for e in toc_entries}` — this is
  exactly "the pages a library catalog would scan as the book's TOC." Kept
  100% verbatim.
- **Secondary listing pages** (`_secondary_listing_pages` — part-divider
  pages listing that part's chapters, a series/half-title page repeating the
  whole book's chapter list, etc.). These are themselves tables of contents
  in substance, just not the book's primary one. `_locate_toc_entries`
  already computes and returns their indices as part of its
  `non_content_pages` result. Kept 100% verbatim, same as the TOC.
- **Running header/footer lines** (`_running_header_lines`) — the
  book/chapter-title-plus-page-number line stamped on most pages. Pure page
  metadata. Kept verbatim on every page it occurs on, front matter through
  back matter.
- **Each chapter's heading window** — the leading ~200 characters of the
  page(s) `locate_chapter_start_candidates` actually scored the chapter
  title against (see §4 for exactly which pages). This is the chapter
  title/subtitle/author byline — a heading, not chapter content — kept
  verbatim; everything past that window on the same page is chapter prose
  and is redacted like any other body text (§3.3).

None of this needs a cipher or consistency trick: it is real text, kept as
real text, because §1's premise is that this specific material isn't the
copyright-sensitive part of the book.

### 3.2 What's read but not part of the measured metric (left unaddressed)

`extract_authors_near(pages[start_index])` runs spaCy NER (`en_core_web_sm`)
over the first 500 characters of a chapter's start page — further than
§3.1's 200-character heading window — to populate the output chapter dict's
`authors` field. It only runs *after* `pdf_start_index`/`pdf_end_index` are
already resolved, and `test_chapter_segmentation_accuracy.py` scores
precision/recall purely from `(pdf_start_index, pdf_end_index)` pairs — this
function's output is not part of the measured metric. Its behavior against
partially-redacted text (real heading, redacted prose starting somewhere
inside its 500-character window) is therefore explicitly left unverified;
see §2.

### 3.3 What gets redacted (chapter prose — the actual copyright concern, and structurally irrelevant to the heuristics)

Every character of page text *not* covered by §3.1 is chapter body prose.
The heuristics never read it for meaning — the entire matching pipeline is
regex shape-matching and fuzzy string comparison against the §3.1 spans
(confirmed by the audit: no hardcoded keyword list, no semantic parsing
anywhere in `chapter_segmentation.py`/`chapter_common.py` operates outside
those spans). This is exactly the text this spec replaces, page by page,
with random real words in the book's own language (§4).

Because §3.1 already keeps the table of contents, secondary listings, and
every chapter heading verbatim, the entries that needed a keyword allowlist
in an earlier draft of this design — `_TOC_AUTHOR_MARKER_RE`'s "par"/"by"
markers, `_PART_DIVIDER_RE`'s "teil"/"part"/"partie"/"section"/"abschnitt",
and `chapter_common.py`'s `_BACK_MATTER_TITLES` set — turn out not to need
one at all: every place those are evaluated (`_chapters_from_located` on
`entry.title`, `_is_back_matter` on TOC-entry-derived titles) operates
exclusively on text sourced from §3.1's verbatim spans. No separate
allowlist mechanism is needed.

## 4. Region classification: identifying what to preserve

The generation tool derives the verbatim regions by **running the real
production analysis once, against the real page text**, and reusing its own
internal decisions — not by reimplementing a separate classifier that could
drift from what `analyze_attachment` actually does:

```python
from backend.services.chapter_segmentation import (
    find_toc_candidates, _locate_toc_entries, _running_header_lines,
    _strip_running_headers, _LOCATE_SCORE_THRESHOLD,
)
from rapidfuzz import fuzz

toc_entries = find_toc_candidates(pages)
toc_page_indices = {e.source_page_index for e in toc_entries}
located, _unlocated, non_content_pages = _locate_toc_entries(
    pages, toc_entries, exclude_indices=toc_page_indices,
)
header_lines = _running_header_lines(tuple(pages))
```

- `non_content_pages` (returned by `_locate_toc_entries`) is already the
  union of the TOC pages and every secondary listing page — §3.1's first
  two bullets, in one call. Preserve these pages in full.
- `header_lines` — preserve any line on any page whose
  `_normalize_header_line` form is a member, wherever it recurs.
- For each `(entry, match)` pair in `located`: `match.index` is the page
  `locate_chapter_start` returned, but a single chapter's title can
  legitimately re-score above threshold on more than one of its own pages
  (documented in `chapter_segmentation.py`: a real evaluation book repeats
  its title as a running header every 2 pages across a 21-page chapter) —
  and `ChapterStartCandidate`/`ChapterStartMatch` don't expose the full
  merged-cluster member list, only its first index. The generation tool
  therefore re-derives the complete set of qualifying pages per entry by
  running the same per-page scoring `locate_chapter_start_candidates` does
  internally — head window, header-stripped, scored with
  `fuzz.partial_ratio(title.lower(), head.lower())` — and keeps every page
  that clears `_LOCATE_SCORE_THRESHOLD`, not just `match.index`. On each
  such page, the verbatim span is the same head window the score was
  computed from: `_strip_running_headers(text, header_lines)[:200]`
  (mapped back to its original position in the untouched text, since
  header-stripping only affects which characters get scored, not what's in
  the page).

This intentionally imports "private" (underscore-prefixed) helpers directly
from `chapter_segmentation.py` rather than re-deriving equivalent logic.
That's a deliberate coupling, not an oversight: it guarantees the preserved
regions are *exactly* what the real algorithm reads, by construction, and
any future change to that internal logic is caught by §9's parity check
rather than silently drifting out of sync with a hand-maintained
reimplementation.

## 5. Redacting body text: random real-language words

Everything not covered by §4 is chapter prose. For each such span:

1. **Tokenize** with `re.finditer(r"\w+|\W+", text, re.UNICODE)`, alternating
   word-runs and non-word-runs. Non-word runs (whitespace, punctuation,
   newlines) pass through **completely unchanged** — this is what keeps
   per-page character counts and line/newline positions close to the
   original, which matters if anything downstream (e.g. a future re-run of
   `pages_need_ocr` against the public cache) inspects those statistics.
2. For each word-run token:
   - If it is purely digits, or matches `_STRICT_ROMAN_RE` /
     `_PAGE_NUMBER_TOKEN_RE` (imported from `chapter_segmentation`, never
     re-implemented) → pass through unchanged. These never carry copyright-
     relevant content and stripping them would only cost fidelity for no
     benefit.
   - Otherwise → replace with a **real word, in the book's language**,
     chosen from a locale word list bucketed by character length so the
     replacement is the same length as the original token where a same-
     length word exists in that language, or the nearest available length
     otherwise (§6.2).
3. Concatenate back into the page string.

Unlike an earlier draft of this design, there is **no cross-occurrence
consistency requirement** for body-text substitution: because §3.1's
headings/TOC/listings/running-headers are kept verbatim rather than
ciphered, nothing downstream ever needs two *different* redacted spans to
fuzzy-match each other (the one case that would have required
that — TOC-entry-title vs. heading-page-text matching — no longer touches
redacted text on either side). Substitution can therefore pick an
independent random word per token; the only reason to still make token→word
selection deterministic (seeded by page/token position and a per-book salt,
same mechanism, simpler purpose) is so re-running the generation tool
produces a byte-identical `public-cache/` and a clean, empty `git diff`
when nothing about the source book or the redaction logic changed.

## 6. Language selection and the word-list dependency

### 6.1 Locale selection

Reuse `backend.services.chapter_ocr.detect_language(book.get("language"), book.get("title", ""))`
— the same call `scripts/ocr_evaluation_pdfs.py` already makes — which
returns one of `"deu"`/`"fra"`/`"spa"`/`"eng"`, or the combined-default
`"eng+deu+fra+spa"` when detection is ambiguous. Map to a word-list locale:

```python
_LOCALE_MAP = {"deu": "de_DE", "fra": "fr_FR", "spa": "es_ES", "eng": "en_US"}
locale = _LOCALE_MAP.get(detected, "en_US")  # combined-default falls back to en_US
```

### 6.2 Word source: `Faker`, added as a dev-only dependency

Add `faker` (MIT licensed) to `pyproject.toml`'s `[dependency-groups] dev`
group — **not** the core `dependencies` list, since the only place it's
imported is `scripts/generate_public_evaluation_cache.py`, which (per this
project's `bin/` vs `scripts/` convention) is never copied into the
production Docker image and never imported by anything under `backend/`.

`Faker`'s locale-specific `lorem` providers (`de_DE`, `fr_FR`, `es_ES`,
`en_US` all ship one) hold real dictionary words for that language, not
Latin lorem-ipsum filler. The generation tool builds a length-bucketed pool
once per locale from that provider's word list, then picks deterministically
(§5) within the bucket matching a token's length, falling back to the
nearest available length when no exact match exists in that language's
pool. (The exact attribute used to reach the raw word list depends on the
installed Faker version and should be confirmed against `pyproject.toml`'s
pinned version at implementation time — a `Faker(locale).word()` call
sampled many times and deduplicated is an acceptable fallback if the
provider's internal word list isn't cleanly accessible.)

## 7. Public cache artifact format & module layout

New directory, **tracked in git** (unlike `.ocr-cache/`, which stays
gitignored real-text cache):

```text
backend/evaluation/book-segmentation/public-cache/<manifest-key>.pages.json
```

`<manifest-key>` is the book's filename stem (e.g. `9780367439712`, matching
the existing `*.expected.json` naming), so it's stable and independent of
whether the source PDF is present on disk.

File contents:

```json
{
  "cipher_version": 1,
  "source": "ocr",
  "layout_mode_used": false,
  "pages": ["<redacted page 1 text>", "<redacted page 2 text>", ...]
}
```

- `cipher_version`: bumped whenever §4/§5's algorithm changes in a way that
  alters output — lets a regeneration script detect stale entries.
- `source`: `"ocr"` or `"extracted"`, mirroring which path
  `analysis_pages_for` took for this book (informational, matches
  `run()`'s existing `layout_mode_used` diagnostic field).

New harness function, `backend/evaluation/harness.py`:

```python
PUBLIC_CACHE_DIR = EVAL_DIR / "public-cache"

def available_public_books() -> list[tuple[str, Path, dict]]:
    """Like available_books(), but yields (manifest_key, expected_path, book)
    for every book with a public-cache entry -- no PDF or .ocr-cache required."""

def public_pages_for(manifest_key: str) -> Optional[list[str]]:
    """Loads and returns the redacted pages for one book, or None if no
    public-cache entry exists yet."""
```

`test_chapter_segmentation_accuracy.py` is **not** modified to *require*
this — it keeps using `available_books()`/`analysis_pages_for` against real
PDFs as today. A parallel, explicitly-named test path is added instead (§9)
so both real-corpus and public-corpus runs stay independently inspectable,
and a contributor without any PDFs can still get a signal.

## 8. Generation tool

`scripts/generate_public_evaluation_cache.py` (in `scripts/`, not `bin/` —
nothing in `backend/` imports this at runtime; it's a human-run dev/admin
tool per the project's `bin/` vs `scripts/` convention):

```bash
uv run python scripts/generate_public_evaluation_cache.py [--book <manifest-key>] [--verify]
```

For each book in `available_books()` (i.e. every book with a local PDF —
this tool is what a maintainer with the real books runs to *produce* the
committed public-cache, not what a contributor without PDFs runs):

1. Compute `analysis_pages_for(pdf_bytes)` — the exact same pages production
   and the real-corpus evaluation already see.
2. Run §4's region classification, then §5's redaction over each page.
3. Write `public-cache/<manifest-key>.pages.json`.
4. If `--verify` is passed (default on): re-run `analyze_attachment` on both
   the real pages and the redacted pages and assert the two runs produce the
   **identical** `{(pdf_start_index, pdf_end_index)}` set — not just similar
   precision/recall, but byte-for-byte identical detected boundaries. This
   is the strongest test available and the direct answer to "does redaction
   change the evaluation's outcome": if the sets ever diverge, the tool
   prints a per-page diff and exits non-zero rather than writing a
   silently-degraded cache entry.

## 9. Verification protocol

Two levels, both required before a public-cache entry is committed:

1. **Per-book exact-match** (§8 step 4, run automatically by the generation
   tool) — the strong guarantee, checked at generation time.
2. **Aggregate accuracy-suite parity** — a new, `integration`-marked test,
   `backend/tests/test_public_evaluation_cache_parity.py`, that runs
   `analyze_attachment` over every `public-cache/*.pages.json` entry and
   asserts the same aggregate precision/recall reported in `RESULTS.md` for
   the real corpus. This is what CI (or a contributor without PDFs) actually
   runs day to day — it doesn't need the real PDFs or `.ocr-cache/` at all,
   only the committed `public-cache/` directory.

If §8's per-book check passes for every book, §9's aggregate check is
mechanically guaranteed to match too (same per-book boundary sets sum to the
same aggregate) — it's kept as a separate test anyway because it's the one
that actually runs in the absence of source material, and a future
production-code change could alter `analyze_attachment` without anyone
re-running the generation tool, at which point this is what would catch the
drift.

## 10. Known limitations / residual risk

### 10.1 Region-classification coverage is a snapshot

§4's preserved regions depend entirely on the audit in §3 being complete. A
future heuristic added to `chapter_segmentation.py`/`chapter_common.py` that
reads page text outside §3.1's spans in a new way would silently degrade
without §8's per-book `--verify` catching it *at generation time* — but it
would surface at the next `--verify` regeneration or in §9's parity test,
since both compare against the real corpus/real metric. **Action:** re-run
`generate_public_evaluation_cache.py --verify` for the whole set whenever
`chapter_segmentation.py` or `chapter_common.py` changes in a way that
touches text-matching logic — this belongs in
`backend/evaluation/book-segmentation/CLAUDE.md`'s document-organization
convention as a fourth trigger for updating `public-cache/`.

### 10.2 `extract_authors_near` fidelity is explicitly unverified

As noted in §2/§3.2, this is a deliberate scope cut, not an oversight: its
output isn't part of the measured metric, so no claim is made about it.

### 10.3 An incidental fuzzy-match false positive is theoretically possible

Redacted body text on an unrelated page could, by chance, score above
`_LOCATE_SCORE_THRESHOLD` (80) against some chapter's title after
substitution — extremely unlikely given the threshold and the length-bucket
substitution's use of unrelated real words, but not mathematically
impossible. §8's per-book `--verify` step is exactly the check that would
catch this: it would show up as an extra or shifted boundary in the
redacted-vs-real comparison, not silently pass.

## 11. Legal caveat

This pipeline is a **technical risk-reduction measure**, not a legal
determination. §1's premise — that a book's table of contents, chapter
headings, and running headers carry materially lower copyright exposure
than its flowing prose, evidenced by library catalogs routinely publishing
exactly this material as bibliographic metadata — is a reasonable starting
point, not a substitute for judgment about a specific publication decision.
Treat committing `public-cache/` to a public repository as a decision that
benefits from a human, copyright-aware sign-off before it happens — this
spec makes the technical approach reviewable, it doesn't pre-clear it.

## 12. Error handling

- Generation tool: a single book's redact/verify failure (§8 step 4) must
  not abort the whole batch — print the failing book's diff, skip writing
  its cache entry, continue to the next book, and exit non-zero at the end
  if any book failed (same catch-log-continue shape as
  `scripts/ocr_evaluation_pdfs.py`'s existing per-book resilience).
- `public_pages_for`: a missing or `cipher_version`-mismatched entry returns
  `None` (mirrors `analysis_pages_for`'s existing "needs work, not present
  yet" contract) rather than raising — callers already have a
  skip-with-message convention for this (§7, `available_public_books`).

## 13. Testing

- **Region-classification unit tests**: given a small synthetic `pages`
  fixture with a known TOC page, a known secondary listing page, and a known
  running header, assert §4 correctly identifies each and that a chapter's
  head window covers every page that independently scores above threshold
  (the multi-page-repeat case), not just the first.
- **Redaction unit tests** (`scripts/evaluation_redaction/`, TDD per project
  convention): non-word-run exact preservation, digit/roman-numeral
  passthrough, length-bucket selection (exact-length match and nearest-
  length fallback), locale selection from `detect_language`'s output,
  reproducibility (same input + same book → same output across runs).
- **Per-book `--verify`** (§8): run as part of generating each committed
  `public-cache/*.pages.json` — not a pytest test, but the generation tool's
  own gate, documented in `CLAUDE.md`.
- **Aggregate parity test** (§9): `integration`-marked, runs against
  `public-cache/` only, no PDFs/OCR cache required — this is the one meant
  to run in CI and by contributors without source material.

## 14. Deferred to later phases

- A `--diff-only` mode for the generation tool to show exactly which pages
  changed when regenerating after a `cipher_version` bump, instead of a full
  silent rewrite.
- Automating the §10.1 re-verification trigger (e.g. a pre-commit hook that
  notices `chapter_segmentation.py`/`chapter_common.py` changed and prompts
  a `public-cache/` regeneration) rather than relying on the `CLAUDE.md`
  documentation convention alone.
