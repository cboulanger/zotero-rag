# Chapter Segmentation: Pluggable Strategy Pipeline — Design Spec

## 1. Goal

`backend/services/chapter_segmentation.py`'s heuristic pipeline (regex TOC
detection + fuzzy content-search localization, with an LLM fallback for the
cases the regex/fuzzy pass can't resolve — see
`docs/superpowers/specs/2026-07-25-llm-chapter-segmentation-fallback-design.md`)
reaches ~91% aggregate precision/recall on the 7-book curated evaluation set
(`backend/evaluation/book-segmentation/README.md`), but has proven unreliable
in live use against real, diverse Zotero libraries — reportedly ~80% combined
false-positive/false-negative rate. The evaluation set is small and skews
toward well-produced academic books with a parseable embedded TOC page; real
libraries range from badly-scanned grey literature with no metadata at all to
PDF-native books with a DOI and per-chapter Crossref records already
available.

The structural problem is that today's pipeline only ever looks *inside* the
PDF's own extracted text, through one fixed sequence of steps. It has no way
to use an authoritative external chapter list when one exists, and no way to
skip the fragile fuzzy-text-search step when a much more reliable
localization mechanism is available for a given document.

This design replaces the single fixed pipeline with a small set of
independently pluggable **strategies** — each either supplying candidate
chapter metadata from an external source, or directly locating chapter
boundaries in the PDF — feeding a bounded fusion step, with the existing
heuristic/LLM pipeline demoted to "the fallback used when nothing more
reliable is available," exactly as it is today. It is deliberately scoped as
a **first, minimal slice**: two new strategies (a zero-cost PDF outline read,
and a Crossref-by-ISBN lookup) plus the fusion logic needed to combine them
with each other and with the existing pipeline. Further strategies and a
richer fusion model are explicitly out of scope for this phase — see
§10.

## 2. Non-goals / explicit scope boundary

- **No OAPEN/DOAB metadata strategy in this phase.** Valuable specifically
  for the open-access/small-press case the research identified as
  Crossref's blind spot, but a second metadata provider is deferred until
  the first (Crossref) has proven the strategy-pipeline shape in practice.
  See §10.
- **No Docling or Kreuzberg structure-extraction strategy in this phase.**
  Both were identified as viable future `StructureStrategy` implementations
  (Docling via a new `docling-serve` sidecar, Kreuzberg via its existing
  `HierarchyConfig`), but neither is built here. See §10.
- **No change to `chapter-retrofit-link`** (`scripts/retrofit_chapter_links.py`
  / `backend/services/chapter_retrofit.py`). That command matches
  already-separately-catalogued `book`/`bookSection` items to each other; it
  does not analyze a book's own PDF for chapter boundaries and is
  unaffected by this design.
- **No general multi-source arbitration/entity-resolution framework.** The
  fusion logic in §6 is a small, bounded algorithm (fuzzy-title alignment
  with an ordering constraint) reusing patterns already validated in
  `_locate_toc_entries`, not a new general-purpose framework for reconciling
  arbitrarily many disagreeing sources.
- **No gap-filling when the new strategies together cover only part of a
  book.** If the PDF has no embedded outline and Crossref only registered a
  subset of the book's chapters (e.g. only the DOI-bearing ones), the
  remaining chapters are not filled in from the heuristic pass in this
  phase — the new strategies are used only when they account for the whole
  book; otherwise the pipeline falls back to today's heuristic+LLM behavior
  unchanged, book-wide. See §6 and §10.
- **No per-source confidence weighting beyond one fixed constant** for
  direct-localization strategies (§7). Crossref-sourced titles are expected
  to *indirectly* raise confidence (better titles/authors improve the
  existing fuzzy content-search hit rate and its author-aware
  disambiguation), not through a new, untested confidence formula.

## 3. Data model

A single dataclass becomes the common currency every strategy and the
fusion step operate on, replacing ad hoc dict/`TocEntry`-shaped intermediate
values for the new code paths (the existing `TocEntry` type is untouched —
see §8 for how the two interoperate):

```python
@dataclass(frozen=True)
class ChapterCandidate:
    title: str
    authors: tuple[str, ...] = ()
    printed_page_number: int | None = None   # from a TOC/metadata source;
    # never assumed equal to pdf_page_index (same invariant as TocEntry)
    pdf_page_index: int | None = None          # set only by a direct-
    # localization strategy (currently: outline). None means "still needs
    # content-search localization."
    chapter_doi: str | None = None             # set only by Crossref
    source: str = "heuristic"                  # "outline" | "crossref" |
    # "outline+crossref" | "heuristic" | "llm" — extends the existing
    # per-chapter "source" field, see §7
```

`BookContext` carries the identifiers a `MetadataStrategy` needs, read once
per book from the Zotero item before any strategy runs:

```python
@dataclass(frozen=True)
class BookContext:
    item_key: str
    isbn: str | None      # from book["data"]["ISBN"], normalized (see §5)
    title: str
    editors: tuple[str, ...]
```

Both types live in a new module, `backend/services/chapter_evidence/types.py`
(new package `backend/services/chapter_evidence/`, see §9 for the full
module layout).

## 4. Strategy interfaces

```python
class MetadataStrategy(Protocol):
    def applicable(self, context: BookContext) -> bool: ...
    async def fetch(self, context: BookContext) -> list[ChapterCandidate]: ...
    # Never raises — any failure (network, parse, rate limit) is caught
    # internally and treated as "found nothing," per §5's error-handling rule.

class StructureStrategy(Protocol):
    def applicable(self, pdf_bytes: bytes) -> bool: ...
    def extract(self, pdf_bytes: bytes) -> list[ChapterCandidate]: ...
    # Synchronous — no I/O beyond parsing bytes already in memory.
```

These are plain `Protocol`s (structural typing, no base class required) so a
future strategy just needs to implement the two methods — this is what makes
the set extensible without touching the orchestrator's dispatch logic beyond
adding the new instance to a list (see §6).

Phase 1 ships exactly one of each:

- `OutlineStructureStrategy` (§5.1) — implements `StructureStrategy`.
- `CrossrefMetadataStrategy` (§5.2) — implements `MetadataStrategy`.

The existing regex-heuristic and LLM-fallback code is **not** wrapped in this
interface in Phase 1 — it continues to be invoked directly, exactly as
today, only reached when both new strategies together produce nothing
usable for a book (§6). Wrapping it as a formal `StructureStrategy` too is a
Phase 2 cleanup (§10), not required for the fusion logic Phase 1 needs.

## 5. The two new strategies

### 5.1 `OutlineStructureStrategy` — PDF `/Outlines` read

```python
def extract_outline_candidates(content: bytes) -> list[ChapterCandidate]:
    """Reads the PDF's embedded outline/bookmark catalog (pypdf's
    PdfReader.outline) and returns one ChapterCandidate per TOP-LEVEL entry
    that survives the existing _is_part_divider/_is_back_matter filters
    (moved to a shared module, see §9) — each with pdf_page_index resolved
    directly via reader.get_destination_page_number(), no content search
    needed. Nested (child) outline entries are not surfaced as separate
    chapters in Phase 1 (see limitation below). Returns [] if the PDF has no
    outline catalog, or if reading it raises (malformed/encrypted PDF) —
    never raises itself.
    """
```

- Uses `pypdf.PdfReader(io.BytesIO(content)).outline` — pypdf is already a
  project dependency (`chapter_segmentation.py` already imports it for
  `extract_page_texts_from_pdf_bytes`).
- Only the outline's **top level** is read. A nested list in pypdf's outline
  representation means "children of the immediately preceding item"; books
  whose top-level bookmarks are Part dividers (with actual chapters nested
  one level below) will not have their chapters surfaced by this strategy in
  Phase 1 — `applicable()` in this case reports `True` (an outline exists)
  but `extract()` returns entries that all get filtered out by
  `_is_part_divider`, correctly yielding `[]` rather than wrong data, so the
  book falls through to the existing pipeline unchanged. Extending this to
  walk one level deeper when the top level is all-dividers is a candidate
  Phase 2 refinement (§10), not required now since it fails safe.
- `applicable(pdf_bytes)`: attempts `PdfReader(...).outline` and returns
  whether it's non-empty — cheap, no separate parse from `extract()`
  (`extract()` itself may simply be called unconditionally and checked for
  an empty result, `applicable()` exists to satisfy the `StructureStrategy`
  protocol uniformly with `CrossrefMetadataStrategy`).
- `chapter_doi` and `printed_page_number` are always `None` for
  outline-sourced candidates — an outline destination has no printed-page
  metadata, only a physical page reference.
- `source="outline"`, confidence handling in §7.
- **Plausibility safeguard**: an outline is supposed to represent the book's
  *complete* structure, so a filtered top-level result that is implausibly
  sparse relative to the document is a signal the outline's real chapters
  are nested one level down (the Part-divider-at-top-level case described
  above), not evidence the book genuinely has that few chapters. Before a
  filtered outline result is accepted as a book's full chapter list (§6),
  it must also pass: at least 2 entries, and average pages-per-entry between
  3 and 150 (`total_pages / len(filtered_entries)`) — the same
  order-of-magnitude plausibility-guard style already used elsewhere in this
  module (e.g. `_TOC_MAX_PAGE_NUMBER_RATIO`, `_TRAILING_BLANK_PAGE_MAX_CHARS`).
  Failing this check makes `extract_outline_candidates` return `[]` for that
  document (logged at `info` level with the rejected count/ratio) rather
  than a too-sparse list, so §6's merge treats it exactly like "no outline"
  and the book falls through to the existing pipeline.

### 5.2 `CrossrefMetadataStrategy` — chapter lookup by ISBN

```python
async def fetch_crossref_chapters(
    isbn: str, http_client: httpx.AsyncClient, cache_dir: Path | None,
    contact_email: str | None,
) -> list[ChapterCandidate]:
    """GET https://api.crossref.org/works?filter=isbn:{isbn}&select=DOI,title,
    author,page,type,container-title&rows=100, keeping only type ==
    "book-chapter" records (excludes the book's own "monograph"/"book"
    record, which the same ISBN filter also returns). Parses each into a
    ChapterCandidate: title from the (list-valued) title field's first
    entry, authors from author[].given+family, printed_page_number from the
    FIRST half of the page field ("85-113" -> 85), chapter_doi from DOI,
    source="crossref". Cached on disk by ISBN (see below); any network,
    HTTP-status, or JSON-shape failure is logged at warning level and
    treated as a cache-worthy empty result, never raised.
    """
```

- **ISBN normalization**: `book["data"].get("ISBN", "")` (same field
  `chapter_upload.py` already reads, per `backend/services/chapter_upload.py`
  line 71) can hold multiple space/semicolon-separated ISBN-10/13 values.
  `BookContext.isbn` is populated by a new small helper that extracts the
  first ISBN-13 (13 digits after stripping hyphens/spaces), falling back to
  the first ISBN-10 if no ISBN-13 is present, or `None` if the field is
  empty/unparseable. `applicable()` returns `False` when `isbn is None` —
  no network call is made for books without a usable ISBN.
- **Caching**: `<cache_dir>/<isbn>.json`, containing `{"isbn", "fetched_at",
  "chapters": [...]}"` — cached even when `chapters` is empty, so a
  known-unregistered book is never re-queried on repeat runs against a
  growing library. Mirrors the existing `chapter_ocr.py` cache-by-key
  pattern (content-hash there, ISBN here) rather than introducing a new
  caching convention.
- **Rate limiting / politeness**: requests include Crossref's documented
  "polite pool" `mailto` query parameter when `contact_email` is configured
  (new `Settings.crossref_contact_email`, optional — see §9); omitted
  entirely otherwise (falls back to the public pool, which is sufficient for
  moderate library sizes). On an HTTP 429, retries up to 3 times honoring
  the `Retry-After` header if present, else a short fixed backoff; after 3
  failures, treated as strategy failure for that ISBN (logged, not cached as
  a permanent miss — a future run may still succeed).
- **Auth**: none — Crossref's REST API is fully anonymous.

## 6. Fusion (per book, before falling back to the existing pipeline)

```python
def merge_outline_and_crossref(
    outline_candidates: list[ChapterCandidate],
    crossref_candidates: list[ChapterCandidate],
) -> list[ChapterCandidate]:
```

1. Filter both lists through the existing `_is_part_divider`/
   `_is_back_matter` title checks (§9 moves these to a shared module so both
   the new strategies and the existing heuristic use the same definitions).
2. If either filtered list is empty, return the other unchanged — nothing to
   merge.
3. Otherwise, align entries by fuzzy title match (`rapidfuzz.fuzz.
   token_sort_ratio(a.title, b.title) >= 70`, `rapidfuzz` is already a
   project dependency) **with a monotonic-order constraint**: once entry *i*
   from one list is matched to entry *j* from the other, no later match may
   pair with an entry before *j* — the same "TOC listing order is book
   order" constraint `_locate_toc_entries` already applies internally, here
   applied between two independently-ordered candidate lists instead of
   between TOC entries and page positions.
4. **Matched pairs** produce one `ChapterCandidate` combining the outline
   entry's `pdf_page_index` (a direct destination is a more reliable
   location than any text match) with the Crossref entry's `title`,
   `authors`, and `chapter_doi` (the publisher's canonical wording and
   author list are ordinarily more complete/accurate than a bookmark
   label) — `source="outline+crossref"`.
5. **Unmatched outline entries** are kept as-is (`source="outline"`,
   `pdf_page_index` already set, no localization needed).
6. **Unmatched Crossref entries** are kept as-is (`source="crossref"`,
   `pdf_page_index` still `None` — these still need content-search
   localization, exactly like an LLM-extracted `TocEntry` does today; see
   §8).
7. Result is sorted by `pdf_page_index` where known, else left in Crossref's
   original (book) order, matching `_locate_toc_entries`'s existing
   page-index sort convention for the final chapter list.

**Book-level trigger**: this merged list is only used for a book when it is
**non-empty**. An empty result (neither strategy applicable, both failing
the plausibility safeguard in §5.1, or both applicable but returning
nothing — e.g. no outline and an unregistered ISBN) means the book falls
through to today's `analyze_attachment` / `analyze_attachment_with_llm_fallback`
pipeline entirely unchanged.

This book-level (not chapter-level) fallback trigger is the Phase-1
simplification called out in §2 and §10. It guarantees the new strategies
never *crash or corrupt* a result relative to today's baseline (every
failure mode is caught and degrades to the existing pipeline, per §11) —
but it does **not** guarantee equal-or-better *coverage*: if Crossref alone
returns a genuine but partial chapter list (e.g. 8 of a 12-chapter book,
because only 8 were DOI-registered) and no outline exists to reveal the
other 4, this design accepts that book at 8 chapters rather than falling
back to let the heuristic pass attempt all 12. This is the specific,
intentional Phase-1 trade-off named in §2 ("no gap-filling when the new
strategies together cover only part of a book") — reconciling partial
results from multiple sources is deferred to Phase 2 (§10), where a second
metadata strategy (OAPEN/DOAB) makes the added complexity worth it.

## 7. Confidence and the chapter dict `source` field

The existing `match_confidence(score, margin)` function (unchanged) is
still used for any candidate that needed content-search localization
(Crossref-sourced entries lacking `pdf_page_index`, and today's
heuristic/LLM entries). For candidates that already carry a
`pdf_page_index` from the outline (whether standalone or merged with
Crossref), no content search happens, so a fixed constant is used instead:

```python
_OUTLINE_CONFIDENCE = 0.98  # exceeds chapter_upload.py's calibrated
# confidence_threshold (0.90), so outline-sourced chapters typically route
# straight to commit rather than the review queue.
```

Chosen below `1.0` deliberately — it is a strong but not absolute signal
(an outline entry could still be mis-titled or point at a part divider that
slipped past the filters), leaving room for a future recalibration rather
than hard-coding certainty.

The per-chapter dict's existing `"source"` field (`"heuristic"` | `"llm"`,
per the LLM-fallback design) gains two new values: `"outline"` and
`"crossref"` (and the merged case, `"outline+crossref"`) — a straightforward
extension, not a schema-breaking change; every existing consumer
(`chapter_upload.py`'s `confidence_threshold` gate, `/admin/review`) already
treats `source` as an opaque observability string, not something it
branches on.

`diagnostics` gains:

```python
{
    ...,  # existing fields unchanged
    "outline_candidates_found": int,
    "crossref_candidates_found": int,
    "crossref_isbn_used": str | None,
    "strategies_used": list[str],  # e.g. ["outline", "crossref"], [] if the
    # book fell through to the existing pipeline entirely
}
```

## 8. Orchestration

**New function** in `chapter_segmentation.py`, sitting alongside (not
replacing) `analyze_attachment` and `analyze_attachment_with_llm_fallback`:

```python
async def analyze_attachment_with_strategies(
    pages: list[str],
    file_bytes: bytes,
    context: BookContext,
    *,
    llm_service: LLMService | None = None,
    http_client: httpx.AsyncClient | None = None,
    crossref_cache_dir: Path | None = None,
    crossref_contact_email: str | None = None,
    enable_crossref: bool = True,
) -> dict:
    """Runs OutlineStructureStrategy and (if enable_crossref and
    context.isbn) CrossrefMetadataStrategy, merges their results (§6). If
    the merge is non-empty: any merged candidate still missing
    pdf_page_index is localized via the EXISTING locate_chapter_start (same
    function, same author-aware disambiguation, called with the
    candidate's title/authors -- mirrors exactly how llm_extract_toc_entries
    results are localized today, see design spec
    2026-07-25-llm-chapter-segmentation-fallback-design.md §4), then built
    into chapter dicts via the existing _chapters_from_located, with
    confidence per §7. If the merge is empty, delegates to
    analyze_attachment_with_llm_fallback(pages, llm_service) when
    llm_service is given, else analyze_attachment(pages) -- i.e. today's
    behavior, byte-for-byte, for any book neither new strategy covers.
    """
```

`ChapterCandidate` entries lacking `pdf_page_index` are converted to
`TocEntry` (via a small adapter, `source_page_index=-1` sentinel — same
convention `llm_extract_toc_entries` already established for
non-page-anchored entries) purely to reuse `_locate_toc_entries` without
duplicating its clustering/ordering-disambiguation logic. This is an
internal implementation detail, not a new public type relationship.

`analyze_attachment` and `analyze_attachment_with_llm_fallback` are **not**
modified — the default evaluation harness (`test_chapter_segmentation_accuracy.py`)
and the existing LLM-fallback evaluation script keep behaving exactly as
today, unaffected by this project, per the same non-regression principle
the LLM-fallback design established for its own predecessor.

## 9. Module layout, settings, and call surface

**New package** `backend/services/chapter_evidence/`:

- `types.py` — `ChapterCandidate`, `BookContext`.
- `outline_strategy.py` — `extract_outline_candidates` (§5.1).
- `crossref_strategy.py` — `fetch_crossref_chapters`, ISBN normalization
  helper, on-disk cache read/write (§5.2).
- `fusion.py` — `merge_outline_and_crossref` (§6).

`_is_part_divider`, `_is_back_matter`, and `_normalized_title` move from
`chapter_segmentation.py` into a new `backend/services/chapter_common.py`
(pure refactor, re-exported from `chapter_segmentation.py` for the existing
call sites so nothing else needs to change) so `chapter_evidence/` can use
them without a circular import.

**`Settings`** (`backend/config/settings.py`) gains two fields, following
the existing `data_path`-relative-default pattern (e.g. `review_queue_path`):

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

**`chapter_segmentation.run()`** gains:

```python
async def run(
    *,
    ...,  # all existing parameters, unchanged
    enable_crossref: bool = True,
    crossref_cache_dir: Optional[Path] = None,  # defaults to Settings' path when None
    crossref_contact_email: Optional[str] = None,
) -> dict:
```

When called, `run()` builds a `BookContext` per book from the Zotero item
data already fetched, constructs one shared `httpx.AsyncClient` for the
whole run (not per-book), and calls
`analyze_attachment_with_strategies` instead of `analyze_attachment` /
`analyze_attachment_with_llm_fallback` directly — those two remain reachable
only via direct import (tests, evaluation scripts), not through `run()`.

- **CLI** (`scripts/analyze_book_chapters.py`): new `--no-crossref` flag
  (Crossref on by default — see rationale below) and `--crossref-contact-email`
  (defaults to `Settings().crossref_contact_email`). The outline strategy
  needs no flag; it is pure local computation with no external dependency or
  cost, so it is always on.
- **API** (`POST /api/chapter-linking/analyze`): `AnalyzeRequest` gains
  `enable_crossref: bool = True`.

**Why Crossref defaults to *on*** (unlike `--llm-fallback`, which defaults
off): it costs nothing, is cached, and is fast — but it is a new external
network dependency and sends a library's ISBNs to a third-party public API,
which some deployments (offline/air-gapped, or privacy-sensitive libraries)
may want to disable — hence the explicit opt-out rather than opt-in.

## 10. Deferred to later phases

Documented here so the scope boundary in §2 has a concrete roadmap rather
than an open-ended "later":

- **Phase 2 — OAPEN/DOAB metadata strategy.** Implements `MetadataStrategy`
  the same way `CrossrefMetadataStrategy` does, targeting exactly the
  open-access/small-press/non-DOI'd-elsewhere case the research identified
  as Crossref's blind spot (German/French OA university presses in
  particular). Also the natural point to revisit the book-level
  all-or-nothing fallback trigger from §6 — with two metadata strategies
  available, a proper interval-based gap-filling reconciliation (extending
  `_locate_toc_entries`'s existing "prune candidates to the interval between
  already-located neighbors" logic to work across differently-sourced,
  partially-overlapping TOC lists) becomes worth the added complexity, where
  it is not yet for a single metadata source.
- **Phase 3 — structure-extraction strategies.** A `docling-serve`-backed
  `StructureStrategy` (a new lightweight sidecar, comparable in operational
  cost to the existing Kreuzberg sidecar) for native and already-OCR'd
  documents lacking an embedded outline, giving a richer structured tree
  than plain-text heuristics; and a Kreuzberg `HierarchyConfig`-backed
  strategy layered on top of the existing OCR pipeline, particularly
  valuable for badly-scanned grey literature where neither the outline nor
  any metadata provider has anything to offer.
- **Phase 4 — exploratory.** Applying the same metadata-strategy pattern to
  `chapter-retrofit-link`'s book/chapter matching; VIAF-based author-name
  normalization to strengthen author-aware disambiguation across
  German/French/English name-variant spellings; revisiting the outline
  strategy's top-level-only limitation (§5.1) to walk into nested
  Part-then-Chapter outline structures if real-library data shows this
  matters; a formal per-source confidence-weighting model if evaluation
  data shows the fixed `_OUTLINE_CONFIDENCE` constant (§7) is miscalibrated.

## 11. Error handling

- Every strategy method (`applicable`, `fetch`/`extract`) catches its own
  failures internally and returns `False`/`[]` — a malformed PDF outline, a
  Crossref network error, an unparseable ISBN, or a JSON-shape surprise in
  Crossref's response is never allowed to raise out of
  `analyze_attachment_with_strategies`. This mirrors the LLM-fallback
  design's §11 "never raise into the caller" rule, extended to the two new
  strategies — a *crash* is always avoided this way, though (per §6) a
  partial-but-non-empty result from a single metadata source is still
  accepted as final rather than triggering the existing pipeline as a
  supplement, which is a distinct, intentional coverage trade-off, not an
  error condition.
- A book where both new strategies fail, don't apply, or fail the §5.1
  plausibility safeguard falls through to exactly today's pipeline (§6, §8)
  — this is the primary safety net, not just a per-call try/except.
- Crossref 429s are retried with backoff (§5.2) before being treated as a
  transient failure for that run; they are not cached as a permanent miss,
  unlike a confirmed "ISBN not registered" empty response.

## 12. Testing

- Unit tests for `extract_outline_candidates`: a fixture PDF with a
  populated outline (build with `pypdf.PdfWriter.add_outline_item` in the
  test itself rather than committing a binary fixture — mirrors how
  `backend/tests/test_chapter_segmentation.py` already builds small
  in-memory fixtures instead of relying on the (gitignored) evaluation
  PDFs); confirms top-level-only extraction, confirms `_is_part_divider`/
  `_is_back_matter` filtering, confirms `[]` on a PDF with no outline.
- Unit tests for `fetch_crossref_chapters` using `patch("httpx.AsyncClient")`
  (the existing convention in `backend/tests/test_kreuzberg_extractor.py`,
  not a new mocking approach) with canned JSON responses: confirms
  `type == "book-chapter"` filtering, ISBN-not-registered (empty) handling,
  cache-hit short-circuiting (no HTTP call made), and that a network
  exception is swallowed rather than raised.
- Unit tests for `merge_outline_and_crossref`: confirms pass-through when
  one list is empty, confirms the ordering-constrained fuzzy match aligns
  correctly-ordered lists with different entry counts (e.g. Crossref
  missing a front-matter chapter the outline has), confirms it does not
  mis-align out-of-order titles across a monotonicity violation.
- Unit tests for `analyze_attachment_with_strategies`: confirms the
  book-level fallback trigger (§6) — both strategies empty → identical
  output to calling `analyze_attachment` directly on the same fixture pages;
  confirms `source`/`diagnostics` fields are populated correctly for each of
  the three strategy-derived cases (outline-only, crossref-only,
  outline+crossref merged).
- **New evaluation script** `scripts/evaluate_chapter_segmentation_strategies.py`,
  following the existing `evaluate_chapter_segmentation_llm_fallback.py`
  pattern (not a pytest test — costs real Crossref calls, though free and
  fast, and needs each evaluation book's real ISBN from `manifest.json`):
  runs the same evaluation-book manifest through
  `analyze_attachment_with_strategies` and prints the same precision/recall
  table format, plus per-book `strategies_used` diagnostics, so
  `backend/evaluation/book-segmentation/README.md`'s "Current results"
  section can add a third comparison column (heuristic-only /
  heuristic+LLM / heuristic+LLM+strategies) once this has been run at least
  once. Given the curated evaluation books are real published academic
  titles, several likely already have Crossref-registered chapters and/or
  embedded PDF outlines — this is expected to be the first concrete
  precision/recall evidence for whether the new strategies help in
  practice, separate from the qualitative "~80% false positive/negative on
  a real library" report that motivated this design.
