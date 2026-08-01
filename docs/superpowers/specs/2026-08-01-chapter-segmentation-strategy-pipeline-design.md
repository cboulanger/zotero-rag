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
chapter metadata from an external (or already-local) source, or directly
locating chapter boundaries in the PDF — feeding a bounded fusion step, with
the existing heuristic/LLM pipeline demoted to "the fallback used when
nothing more reliable is available," exactly as it is today. It is
deliberately scoped as a **first, minimal slice**: three new strategies (a
zero-cost PDF outline read, a Crossref-by-ISBN lookup, and a zero-network
lookup of already-catalogued chapters within the same Zotero library) plus
the fusion logic needed to combine them with each other and with the
existing pipeline. Further strategies and a richer fusion model are
explicitly out of scope for this phase — see §10.

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
  unaffected by this design. The new §5.3 strategy below is a distinct,
  narrower operation — it feeds chapter *analysis*, not linking — and
  deliberately does not call into or replace `chapter_retrofit.py`'s own
  fuzzy book-matching (see §5.3 for why the two use different match
  strictness for different reasons).
- **No cross-library search.** Zotero's public Web API is scoped per-library
  (`/users/<id>/...` or `/groups/<id>/...`, gated by an API key with access
  to that specific library) — there is no supported endpoint to search
  arbitrary other users' or groups' libraries. §5.3's strategy is therefore
  necessarily scoped to the same library currently being processed, not a
  global catalog lookup across all of zotero.org.
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
    chapter_doi: str | None = None             # set by Crossref, or by the
    # §5.3 catalog strategy when the matched bookSection item has its own
    # populated DOI field
    source: str = "heuristic"                  # "outline" | "crossref" |
    # "zotero_catalog" | "outline+crossref" | "outline+zotero_catalog" |
    # "heuristic" | "llm" — extends the existing per-chapter "source"
    # field, see §7
    metadata_confidence: float = 1.0            # how certain the SOURCE
    # (not the eventual PDF location) is that this candidate is the right
    # chapter of the right book — fixed at 1.0 for outline/Crossref
    # candidates (unchanged Phase-1 behavior, see §7), graduated for §5.3
    # candidates (see §5.3's scoring function)
```

`BookContext` carries the identifiers a `MetadataStrategy` needs, read once
per book from the Zotero item before any strategy runs:

```python
@dataclass(frozen=True)
class BookContext:
    item_key: str
    isbn: str | None      # from book["data"]["ISBN"], normalized (see §5.2)
    title: str
    editors: tuple[str, ...]
    publisher: str | None   # from book["data"]["publisher"], used only by
    # §5.3's scoring function
    year: int | None        # from book["data"]["date"], parsed via the
    # existing _year_from_date helper (backend/services/chapter_retrofit.py
    # — imported, not duplicated), used only by §5.3's scoring function
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

Phase 1 ships one `StructureStrategy` and two `MetadataStrategy`
implementations:

- `OutlineStructureStrategy` (§5.1) — implements `StructureStrategy`.
- `CrossrefMetadataStrategy` (§5.2) — implements `MetadataStrategy`.
- `ZoteroCatalogMetadataStrategy` (§5.3) — implements `MetadataStrategy`.

The existing regex-heuristic and LLM-fallback code is **not** wrapped in this
interface in Phase 1 — it continues to be invoked directly, exactly as
today, only reached when all three new strategies together produce nothing
usable for a book (§6). Wrapping it as a formal `StructureStrategy` too is a
Phase 2 cleanup (§10), not required for the fusion logic Phase 1 needs.

## 5. The three new strategies

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

### 5.3 `ZoteroCatalogMetadataStrategy` — same-library exact `bookTitle` lookup

The cheapest of the three strategies: it makes **zero additional network
calls**. `run()` fetches the library's full item list through a
`ZoteroLibraryCache` (see
`docs/superpowers/specs/2026-08-01-zotero-library-sync-cache-design.md`)
instead of a raw `zotero_client.get_library_items_since(...)` call — the
first `run()` invocation for a given library does a full fetch and every
later one does a cheap version-based incremental sync instead of
re-downloading the whole library. This matters because `run()` is invoked far
more often than "once per library": every `POST /chapter-linking/analyze`
request triggers a fresh `run()` call, and so does every `"ocr"`-type entry
processed in a `/chapter-linking/review/execute` batch (one `run()` call per
queue entry) — today, each of those independently re-downloads the entire
library just to filter it down to `books`/`bookSection` items, which is
exactly the wasteful pattern this cache module exists to remove (see §9).
This strategy just also looks at that same cached item list's `bookSection`
items, which is a discovery use of the same idea `chapter-retrofit-link`
already applies as a *linking* operation (see the non-goal in §2 explaining
why the two are kept separate).

```python
def find_zotero_catalog_candidates(
    context: BookContext, book_sections_by_title: dict[str, list[dict]],
) -> list[ChapterCandidate]:
    """Looks up book_sections_by_title[context.title.strip()] -- an exact,
    non-fuzzy string match on the bookSection item's own `bookTitle` field
    against the book's `title` field, PER THE EXPLICIT REQUIREMENT that this
    strategy never fuzzy-matches (unlike chapter_retrofit.py's
    find_best_book_match, which fuzzy-matches because it has no better
    signal available for its own, harder problem -- see §2). No match key
    -> [], no network call either way. Each matching bookSection item
    becomes one ChapterCandidate: title/authors from the item's own
    title/creators (creatorType=="author"), printed_page_number from the
    FIRST half of its `pages` field (same "85-113" -> 85 parsing
    fetch_crossref_chapters uses -- factored into one shared helper, see
    §9), chapter_doi from the item's own DOI field if populated,
    metadata_confidence from score_zotero_catalog_candidate (below),
    source="zotero_catalog". Candidates are sorted by printed_page_number
    (unknowns last) to approximate book order, then deduplicated by title
    -- if two matching items share a title (e.g. an accidental duplicate
    import), only the higher-metadata_confidence one is kept.
    """


def score_zotero_catalog_candidate(book_section_data: dict, context: BookContext) -> float:
    """0.0-1.0. An exact match on the candidate's own (book-inherited)
    ISBN field against context.isbn short-circuits to 1.0 -- the same
    identifier the book itself carries, so this is as certain as ISBN
    matching gets. Otherwise, starts from a base score for the bookTitle
    match already required to reach this function at all, then adds one
    bonus per additional corroborating field, capped at 1.0:

        _BASE_SCORE = 0.6           # exact bookTitle match alone
        _YEAR_BONUS = 0.15          # candidate's parsed year == context.year
        _PUBLISHER_BONUS = 0.15     # case-insensitive, trimmed string equality
        _EDITOR_OVERLAP_BONUS = 0.2 # any last-name overlap between the
                                     # candidate's editor-type creators and
                                     # context.editors

    These four constants are initial estimates, not empirically calibrated
    (no evaluation-set data exists yet for this strategy) -- flagged for
    recalibration once scripts/evaluate_chapter_segmentation_strategies.py
    (§12) has real precision/recall numbers, the same "snapshot, not a
    guarantee" caveat the existing confidence_threshold calibration
    already carries.
    """
```

- **Exact-DOI corroboration is handled in fusion, not here** (§6): if this
  strategy's candidate and a Crossref candidate align to the same chapter
  and both carry a non-empty, equal `chapter_doi`, that is stronger
  evidence than any single-source field comparison this function could make
  alone — two independently-maintained sources agreeing on the literal same
  DOI. Baking that into `score_zotero_catalog_candidate` itself would
  require it to know about Crossref's results, which breaks the
  strategy-independence this design is built around; it belongs in the
  fusion step, which already exists to reconcile multiple sources.
- **Only unlinked chapters are candidates.** `book_sections_by_title` is
  built once per `run()` call from `bookSection` items that do **not**
  already have an `X-Contained-By` link (`not
  parse_links(c["data"].get("extra", "")).contained_by` — the identical
  filter `chapter_retrofit.py`'s `find_matches` already applies, imported
  rather than re-implemented), so this strategy never proposes a chapter
  that's already linked to some other book.
- **Construction**: `ZoteroCatalogMetadataStrategy` is built once per
  `run()` invocation with `book_sections_by_title` closed over (dependency
  injection at construction time, the same pattern `CrossrefMetadataStrategy`
  uses for its `http_client`/`cache_dir`) — its `fetch(context)` method
  still matches the plain `MetadataStrategy` protocol from the caller's
  point of view.
- `applicable(context)`: `context.title.strip() in book_sections_by_title`.

## 6. Fusion (per book, before falling back to the existing pipeline)

Fusion is now two stages: first consolidate the (possibly two) metadata
sources into one list, then merge that with the outline exactly as the
single-metadata-source version of this design originally did.

### 6.1 Stage A — consolidating metadata sources

```python
def merge_metadata_sources(
    strategy_results: list[list[ChapterCandidate]],  # priority order:
    # [crossref_candidates, zotero_catalog_candidates]
) -> list[ChapterCandidate]:
```

Folds the results left-to-right using the same ordering-constrained
fuzzy-title alignment §6.2 uses (factored into one shared helper, `_align`,
used by both stages — see §9): when two strategies' candidates align to the
same chapter, the entry with the higher `metadata_confidence` wins outright
(its `title`/`authors`/`printed_page_number` are kept as-is; the loser's
data is discarded rather than blended field-by-field, keeping this bounded
per §2's "no general arbitration framework" non-goal); a tie prefers
Crossref (a Crossref record already implies a formally-registered DOI, a
stronger provenance signal than a same-scoring catalog entry). **Exact-DOI
corroboration**: when two aligned candidates both carry a non-empty,
*equal* `chapter_doi`, the survivor's `metadata_confidence` is forced to
`1.0` regardless of what either source's own scoring produced — two
independently-maintained sources agreeing on the literal same DOI is
stronger evidence than either alone (this is where §5.3's deferred
"exact DOI matching" case is actually resolved). Entries only one strategy
found are kept unchanged, unmodified confidence.

With only Crossref active (as before this addition), `merge_metadata_sources`
is called with a single-element list and returns it unchanged — this stage
is a strict superset of the original single-source behavior, not a
replacement of it.

### 6.2 Stage B — merging with the outline

```python
def merge_candidates(
    outline_candidates: list[ChapterCandidate],
    metadata_candidates: list[ChapterCandidate],  # Stage A's output
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
   location than any text match) with the metadata entry's `title`,
   `authors`, `chapter_doi`, and `metadata_confidence` (the publisher's/
   cataloguer's canonical wording and author list are ordinarily more
   complete/accurate than a bookmark label) —
   `source="outline+crossref"` or `"outline+zotero_catalog"`, matching
   whichever the metadata entry's own `source` was.
5. **Unmatched outline entries** are kept as-is (`source="outline"`,
   `pdf_page_index` already set, no localization needed).
6. **Unmatched metadata entries** are kept as-is (`source="crossref"` or
   `"zotero_catalog"`, `pdf_page_index` still `None` — these still need
   content-search localization, exactly like an LLM-extracted `TocEntry`
   does today; see §8).
7. Result is sorted by `pdf_page_index` where known, else left in the
   metadata list's original (book) order, matching `_locate_toc_entries`'s
   existing page-index sort convention for the final chapter list.

**Book-level trigger**: Stage B's merged list is only used for a book when
it is **non-empty**. An empty result (no strategy applicable, all failing
the plausibility safeguard in §5.1, or all applicable but returning
nothing — e.g. no outline, an unregistered ISBN, and no exact `bookTitle`
match in the library) means the book falls through to today's
`analyze_attachment` / `analyze_attachment_with_llm_fallback` pipeline
entirely unchanged.

This book-level (not chapter-level) fallback trigger is the Phase-1
simplification called out in §2 and §10. It guarantees the new strategies
never *crash or corrupt* a result relative to today's baseline (every
failure mode is caught and degrades to the existing pipeline, per §11) —
but it does **not** guarantee equal-or-better *coverage*: if the consolidated
metadata list (§6.1) alone returns a genuine but partial chapter list (e.g.
8 of a 12-chapter book, because only 8 were DOI-registered and only those
same 8 happen to already be catalogued as separate `bookSection` items) and
no outline exists to reveal the other 4, this design accepts that book at 8
chapters rather than falling back to let the heuristic pass attempt all 12.
This is the specific, intentional Phase-1 trade-off named in §2 ("no
gap-filling when the new strategies together cover only part of a book") —
reconciling partial results into a true union is deferred to Phase 2 (§10),
where a third metadata strategy (OAPEN/DOAB) makes the added complexity
worth it.

## 7. Confidence and the chapter dict `source` field

The existing `match_confidence(score, margin)` function (unchanged) is
still used to score the localization itself for any candidate that needed
content-search (a metadata-only entry lacking `pdf_page_index`, and today's
heuristic/LLM entries). A candidate's final `confidence` is that
localization score multiplied by its own `metadata_confidence`:

```python
final_confidence = candidate.metadata_confidence * match_confidence(score, margin)
```

For Crossref-sourced candidates, `metadata_confidence` stays fixed at
`1.0` (unchanged from the original single-source design — Crossref data is
already DOI-verified by construction), so `final_confidence` reduces to
exactly `match_confidence(score, margin)`, byte-for-byte the same number
this design produced before §5.3 existed. For `zotero_catalog`-sourced
candidates, `metadata_confidence` is the graduated score from §5.3 —
multiplying it in means a weakly-corroborated catalog match (bookTitle
alone, no ISBN/year/publisher/editor agreement) pulls the final confidence
down even when localization itself succeeds cleanly, correctly routing it
toward the review queue rather than auto-commit.

For candidates that already carry a `pdf_page_index` from the outline
(whether standalone or merged with a metadata source in §6.2), no content
search happens, so `match_confidence` doesn't apply. A fixed constant is
used instead, **not** multiplied by `metadata_confidence` — an outline
destination is a location-certainty signal, independent of how well the
title/authors happened to be corroborated by whichever metadata source
merged into it:

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
per the LLM-fallback design) gains four new values: `"outline"`,
`"crossref"`, `"zotero_catalog"`, and the two merged cases
(`"outline+crossref"`, `"outline+zotero_catalog"`) — a straightforward
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
    "zotero_catalog_candidates_found": int,
    "strategies_used": list[str],  # e.g. ["outline", "crossref"] or
    # ["zotero_catalog"], [] if the book fell through to the existing
    # pipeline entirely
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
    zotero_catalog_strategy: ZoteroCatalogMetadataStrategy,
    *,
    llm_service: LLMService | None = None,
    http_client: httpx.AsyncClient | None = None,
    crossref_cache_dir: Path | None = None,
    crossref_contact_email: str | None = None,
    enable_crossref: bool = True,
) -> dict:
    """Runs OutlineStructureStrategy, (if enable_crossref and context.isbn)
    CrossrefMetadataStrategy, and zotero_catalog_strategy (always -- no
    network cost, see §5.3) concurrently, consolidates the two metadata
    results via merge_metadata_sources (§6.1), then merges that with the
    outline result via merge_candidates (§6.2). If the merge is non-empty:
    any merged candidate still missing pdf_page_index is localized via the
    EXISTING locate_chapter_start (same function, same author-aware
    disambiguation, called with the candidate's title/authors -- mirrors
    exactly how llm_extract_toc_entries results are localized today, see
    design spec 2026-07-25-llm-chapter-segmentation-fallback-design.md §4),
    then built into chapter dicts via the existing _chapters_from_located,
    with confidence per §7. If the merge is empty, delegates to
    analyze_attachment_with_llm_fallback(pages, llm_service) when
    llm_service is given, else analyze_attachment(pages) -- i.e. today's
    behavior, byte-for-byte, for any book none of the three new strategies
    cover.
    """
```

`zotero_catalog_strategy` is passed in rather than constructed inside this
function because it's built once per `run()` call from the whole library's
already-fetched item list (§5.3, §9) — this function operates on one
attachment at a time and has no access to that broader context itself.

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

- `types.py` — `ChapterCandidate`, `BookContext`, and a shared
  `_first_page_number(range_str: str) -> int | None` helper (parses
  `"85-113"` -> `85`) used by both `crossref_strategy.py` and
  `zotero_catalog_strategy.py` rather than duplicated.
- `outline_strategy.py` — `extract_outline_candidates` (§5.1).
- `crossref_strategy.py` — `fetch_crossref_chapters`, ISBN normalization
  helper, on-disk cache read/write (§5.2).
- `zotero_catalog_strategy.py` — `find_zotero_catalog_candidates`,
  `score_zotero_catalog_candidate`, `ZoteroCatalogMetadataStrategy` (§5.3).
- `fusion.py` — `merge_metadata_sources` (§6.1), `merge_candidates` (§6.2),
  and the shared ordering-constrained fuzzy-alignment helper both use.

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
    zotero_cache_dir: Optional[Path] = None,  # defaults to Settings().zotero_cache_path when None
) -> dict:
```

`zotero_cache_dir` has no CLI/API opt-out flag (unlike `enable_crossref`): it
has no external-network or privacy surface of its own — it only changes how
the library's own already-authorized data is fetched from Zotero — so unlike
Crossref there is no reason a deployment would want to disable it.

When called, `run()` constructs one `ZoteroLibraryCache` for the whole run —
wrapping the already-injected `zotero_client` rather than a new client, per
`docs/superpowers/specs/2026-08-01-zotero-library-sync-cache-design.md` — and
gets the library's item list via `await cache.get_all_items()` instead of
calling `zotero_client.get_library_items_since(...)` directly. It then builds
a `BookContext` per book from that same item data, constructs one shared
`httpx.AsyncClient` for the whole run (not per-book), builds
`book_sections_by_title` once from the same cached `items` list (§5.3) and
constructs one `ZoteroCatalogMetadataStrategy` from it for the whole run, and
calls `analyze_attachment_with_strategies` instead of `analyze_attachment` /
`analyze_attachment_with_llm_fallback` directly — those two remain reachable
only via direct import (tests, evaluation scripts), not through `run()`.
This integration depends on `backend/zotero/library_cache.py` already
existing (implemented by the companion spec's own plan), not on any new code
introduced here.

- **CLI** (`scripts/analyze_book_chapters.py`): new `--no-crossref` flag
  (Crossref on by default — see rationale below) and `--crossref-contact-email`
  (defaults to `Settings().crossref_contact_email`). Neither the outline nor
  the Zotero-catalog strategy gets a flag; both are pure local computation
  (no external dependency, no added cost beyond what `run()` already fetches
  for the library), so both are always on.
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
  data shows the fixed `_OUTLINE_CONFIDENCE` constant (§7) is miscalibrated;
  recalibrating §5.3's `score_zotero_catalog_candidate` constants once real
  evaluation-set matches exist (they are currently estimates, see §5.3); and
  an opt-in fuzzy-`bookTitle` mode for §5.3 (mirroring
  `chapter_retrofit.py`'s existing `find_best_book_match` threshold/margin
  approach) for libraries where a cataloguer's `bookTitle` entry has a minor
  typo or abbreviation relative to the book's own `title` field — deliberately
  not in Phase 1, which requires an exact match only, per the explicit
  no-fuzzy-matching requirement for this strategy; and migrating
  `chapter_retrofit.py`'s own independent full-library `bookSection` fetch to
  the same `ZoteroLibraryCache` §9 wires into `chapter_segmentation.run()` —
  today it performs a second, entirely separate full download of the
  library, duplicating the exact inefficiency this design's cache
  integration just removed for `run()`. Deferred rather than done now
  because §2 explicitly keeps `chapter-retrofit-link` out of this design's
  scope; worth revisiting once the cache has proven itself at its one
  adopted call site.

## 11. Error handling

- Every strategy method (`applicable`, `fetch`/`extract`) catches its own
  failures internally and returns `False`/`[]` — a malformed PDF outline, a
  Crossref network error, an unparseable ISBN, or a JSON-shape surprise in
  Crossref's response is never allowed to raise out of
  `analyze_attachment_with_strategies`. This mirrors the LLM-fallback
  design's §11 "never raise into the caller" rule, extended to all three new
  strategies — a *crash* is always avoided this way, though (per §6) a
  partial-but-non-empty result from a single metadata source is still
  accepted as final rather than triggering the existing pipeline as a
  supplement, which is a distinct, intentional coverage trade-off, not an
  error condition.
- A book where all three new strategies fail, don't apply, or fail the §5.1
  plausibility safeguard falls through to exactly today's pipeline (§6, §8)
  — this is the primary safety net, not just a per-call try/except.
- Crossref 429s are retried with backoff (§5.2) before being treated as a
  transient failure for that run; they are not cached as a permanent miss,
  unlike a confirmed "ISBN not registered" empty response.
- The §5.3 strategy has no network surface at all — its only failure modes
  are malformed `date`/`pages` field text (handled the same permissive way
  `_year_from_date`/Crossref's page-range parser already do: unparseable
  input yields `None`, never an exception) and are correspondingly lower
  risk than the other two strategies.

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
- Unit tests for `find_zotero_catalog_candidates` / `score_zotero_catalog_candidate`:
  confirms an exact `bookTitle` match is required (a near-miss with a typo
  or different capitalization does **not** match — the explicit no-fuzzy
  requirement); confirms unlinked-only filtering (an already-`X-Contained-By`-linked
  bookSection is never returned); confirms ISBN-match short-circuits to
  `metadata_confidence == 1.0`; confirms the additive year/publisher/editor
  bonuses and the 1.0 cap; confirms duplicate-title dedup keeps the
  higher-scoring candidate.
- Unit tests for `merge_metadata_sources` (§6.1): confirms single-source
  pass-through (list of one) is unchanged from calling `merge_candidates`
  directly with that one source; confirms a higher-`metadata_confidence`
  candidate wins an aligned pair; confirms the Crossref tie-break; confirms
  exact-DOI corroboration between an aligned Crossref/zotero_catalog pair
  forces `metadata_confidence` to `1.0`.
- Unit tests for `merge_candidates` (§6.2, the renamed/generalized former
  `merge_outline_and_crossref`): confirms pass-through when one list is
  empty, confirms the ordering-constrained fuzzy match aligns
  correctly-ordered lists with different entry counts (e.g. the metadata
  list missing a front-matter chapter the outline has), confirms it does
  not mis-align out-of-order titles across a monotonicity violation.
- Unit tests for `analyze_attachment_with_strategies`: confirms the
  book-level fallback trigger (§6.2) — all three strategies empty →
  identical output to calling `analyze_attachment` directly on the same
  fixture pages; confirms `source`/`diagnostics` fields are populated
  correctly for each strategy-derived case (outline-only, crossref-only,
  zotero_catalog-only, and both merged combinations).
- `run()`-level tests exercise a real `ZoteroLibraryCache` backed by a
  temp-directory SQLite file (via `Settings().zotero_cache_path` pointed at
  a `TemporaryDirectory` in test setup) rather than mocking the cache itself
  — only the underlying `ZoteroWebAPI`-shaped `zotero_client` is mocked
  (`get_library_items_since`/`get_deleted_item_keys`), the same network
  boundary every existing `run()` test already mocks. This matches how
  `ocr_cache_dir`/`crossref_cache_dir` tests already exercise the real
  filesystem rather than mocking those caching layers.
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
