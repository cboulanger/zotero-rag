# LLM Fallback for Chapter Recognition — Design Spec

## 1. Goal

`backend/services/chapter_segmentation.py`'s heuristic pipeline (regex TOC
detection + fuzzy content-search localization) is fast, free, and reported in
`backend/evaluation/book-segmentation/README.md` at ~72% aggregate precision
/ ~58% aggregate recall across the 7-book evaluation set — but it has two
documented, structural limits it cannot get past by further threshold
tuning:

1. **TOC-extraction failure.** `find_toc_candidates`'s regex
   (`<title> <dots/spaces> <page number>`) only recognizes one physical TOC
   layout. A book whose listing uses different typography (no dot leaders,
   multi-column, non-Latin punctuation conventions) produces zero structural
   matches. `Accueillir des publics migrants et immigrés` sits at 0%
   recall (0/16) for exactly this reason — the front-matter listing page
   exists but doesn't match the pattern.
2. **Start-localization ambiguity.** `locate_chapter_start`'s fuzzy match
   against page-head text sometimes finds multiple competing candidate
   pages (a generic title, a running header repeating on several pages) and
   correctly refuses to guess (`margin < _LOCATE_MARGIN_REQUIRED` → `None`)
   rather than risk a wrong boundary. This is the right default behavior,
   but it means real chapters are silently dropped whenever a rival page
   happens to look almost as plausible.

This project adds an **opt-in, LLM-backed fallback path** that targets both
failure modes, reusing the app's existing LLM plumbing
(`backend/services/llm.py`'s `LLMService`/`create_llm_service`, already used
by `query_router.py` for structured JSON extraction) rather than introducing
new model/provider config. It is strictly additive: the existing heuristic
`analyze_attachment(pages)` function, its output schema, and the current
evaluation harness are unchanged. The fallback is slower and costs a paid
API call, so it only runs when explicitly enabled by the caller, and even
then only fires for the specific books/chapters the heuristic pass already
flagged as failed or ambiguous — not on every book.

## 2. Non-goals / explicit scope boundary

- **Not handled by the LLM in v1:** the case where `locate_chapter_start`
  finds *zero* candidate pages above the score threshold anywhere in the
  scanned range (as opposed to finding several ambiguous ones). Fixing that
  would require giving the LLM the full book's text rather than a handful of
  short candidate snippets, which is a materially larger cost/latency
  jump. This remains a known, accepted gap, tracked the same way the
  README already tracks the 0%-recall book today.
- **Not a verification/correction pass.** The LLM never overrides a
  heuristic result the heuristic was confident about. It only fills gaps
  the heuristic pass explicitly reports as empty or ambiguous.
- **No new model/provider configuration.** The fallback uses whatever
  `create_llm_service(settings)` already resolves to for the rest of the
  app (same preset, same API key handling). Swapping in a different model
  for this task specifically is not part of this design.

## 3. Shared plumbing: JSON extraction helper

`query_router.py` already has a private `_parse_json(text: str) -> dict`
(strip markdown code fences, find the outermost `{...}`, `json.loads`) used
to parse the router's structured LLM output. The new chapter-segmentation
LLM calls need the same thing for both a chapter-list array and a
disambiguation object, so this helper is promoted (unchanged logic) to a new
shared module:

**New module** `backend/utils/llm_json.py`:

```python
def parse_json_object(text: str) -> dict: ...   # existing _parse_json body, moved
def parse_json_array(text: str) -> list: ...     # same fence-stripping, then finds outermost [ ... ]
```

`query_router.py` is updated to import `parse_json_object` from here instead
of defining its own copy (behavior-preserving refactor, not a functional
change to routing).

## 4. TOC-extraction fallback

**Trigger** (checked once per attachment, after the heuristic
`analyze_attachment(pages)` pass has already run): `len(toc_entries) == 0`
**or** `len(chapters) == 0` — i.e. the regex found no listing-shaped page at
all, or found entries but every single one then failed to localize. This is
a strict "heuristic produced nothing usable" gate, deliberately without a
partial/fuzzy threshold, so the fallback never has to reconcile a partial
heuristic result with a partial LLM one — it's a full replacement of the TOC
list only in the case where the heuristic list was already empty in effect.

**New function** in `chapter_segmentation.py`:

```python
async def llm_extract_toc_entries(pages: list[str], llm_service: LLMService) -> list[TocEntry]:
    """Reads the same front/back-matter page range find_toc_candidates
    already scans (max_front_fraction/max_back_fraction), sends their raw
    text verbatim to the LLM, and asks it to return the book's chapter
    listing as it actually appears -- for layouts too irregular for the
    regex (no dot leaders, multi-column, unconventional spacing/punctuation)
    but readable by inspection. Never asks the LLM for a physical page
    index -- only title/authors/printed_page_number, exactly like a
    regex-found TocEntry, so the result flows into the SAME
    locate_chapter_start content-search step used for every other TOC
    entry. This preserves the existing invariant (design spec §5 of the
    parent chapter-segmentation design) that PDF index and printed page
    number are never assumed equal -- the LLM only answers "what chapters
    exist," never "where are they physically."
    """
```

Prompt shape (mirrors `query_router.py`'s pattern: numbered/labeled input
blocks, a fixed JSON-array schema, "return ONLY JSON"):

```text
You are reading the front and back matter of a scanned/extracted book to
find its table of contents.

[PAGE 3]
<page text>

[PAGE 4]
<page text>
...

Return ONLY a JSON array, one entry per real chapter (skip acknowledgements,
bibliography, index, part-divider pages):
[{"title": "...", "authors": ["First Last", ...], "printed_page_number": 12}, ...]

If a chapter's printed page number is not visible in this text, use null.
```

`TocEntry` gains a new field so these authors survive into localization
(§5): `authors: tuple[str, ...] = ()` (default empty — regex-found entries
never populate it, since a TOC listing line has no author info; this is a
backward-compatible dataclass change, existing heuristic call sites
unaffected).

The returned entries are fed into the *exact same* clustering/localization
loop `analyze_attachment` already runs for regex-found entries — no
duplicate code path for "how do LLM-sourced chapters become chapter dicts."

## 5. Author-aware localization (small heuristic upgrade, both paths)

Since `llm_extract_toc_entries` naturally recovers author names (something
the regex TOC-line pattern structurally cannot — a TOC line is just "title
… number"), `locate_chapter_start` is extended to use them when available,
mirroring the disambiguation approach `scripts/ground_truth_helper.py`
already validated by hand while building this evaluation set: require an
author's last name to also appear near the top of the candidate page,
distinguishing the chapter's true opening page from a mere continuation page
carrying the same running-header title.

```python
def locate_chapter_start(
    pages: list[str],
    title: str,
    exclude_indices: set[int],
    authors: tuple[str, ...] = (),
) -> ChapterStartMatch | None:
```

- Default `authors=()` → byte-for-byte identical behavior to today. This is
  purely additive; the 7-book evaluation baseline for regex-sourced entries
  does not change.
- When `authors` is non-empty: for each candidate page already clearing
  `_LOCATE_SCORE_THRESHOLD`, check whether any author's last name
  (lowercased) appears in that page's head text. Record this as
  `author_confirmed: bool` on the winning cluster.
- `ChapterStartMatch` gains `author_confirmed: bool = False`.
- Effect on the ambiguity guard: an author-confirmed cluster is treated as
  having its margin boosted by a fixed bonus (reusing the existing
  `_LOCATE_MARGIN_REQUIRED`/`_CONFIDENCE_MARGIN_SATURATION` constants,
  bonus chosen so a merely-plausible confirmed match reliably clears
  `_LOCATE_MARGIN_REQUIRED` even against a same-scoring unconfirmed rival,
  without being so large it overrides a rival that scores far higher on
  title match alone). If two candidates are both author-confirmed, normal
  score/margin ranking still decides between them.
- `match_confidence` takes the same `score`/`margin` inputs unchanged — an
  author-confirmed match simply tends to reach a higher effective margin,
  which the existing formula already turns into higher confidence. No new
  confidence formula is needed.

This is scoped narrowly: it changes ranking/tie-breaking among candidates
`locate_chapter_start` already finds, not what counts as a candidate in the
first place, so it cannot introduce new false positives on its own — only
help pick correctly among ones the fuzzy matcher already surfaced.

## 6. Start-localization fallback (LLM disambiguation)

**Trigger**: per TOC entry (regardless of whether it came from regex or
§4's LLM extraction), when `locate_chapter_start` returns `None` **and** the
reason is ambiguity — i.e. at least one candidate cluster existed but the
top one's margin over the runner-up fell short of
`_LOCATE_MARGIN_REQUIRED`. Not triggered when zero candidates existed at all
(see §2, out of scope).

To expose this, `locate_chapter_start`'s internal cluster list is surfaced
instead of being discarded on rejection:

```python
@dataclass(frozen=True)
class ChapterStartCandidate:
    index: int
    score: float
    author_confirmed: bool

def locate_chapter_start(
    ...,
) -> ChapterStartMatch | None: ...
    # unchanged public behavior/signature otherwise

def locate_chapter_start_candidates(
    pages: list[str], title: str, exclude_indices: set[int], authors: tuple[str, ...] = (),
) -> list[ChapterStartCandidate]:
    """The same candidate-gathering + clustering logic locate_chapter_start
    uses internally, exposed directly so callers can inspect an ambiguous
    result's competing clusters instead of only getting None."""
```

`locate_chapter_start` is refactored to call this shared function
internally rather than duplicating the clustering logic — pure internal
reuse, no behavior change.

**New function**:

```python
async def llm_disambiguate_chapter_start(
    pages: list[str],
    title: str,
    authors: tuple[str, ...],
    candidates: list[ChapterStartCandidate],
    llm_service: LLMService,
) -> ChapterStartMatch | None:
    """Shows the LLM each competing candidate's page index + a short
    snippet (page head, ~200-300 chars, same window locate_chapter_start
    itself scores against) and asks it to pick which one is the chapter's
    true opening page, or none. Small, bounded prompt -- a handful of short
    snippets, never whole-book text -- since the heuristic pass has already
    narrowed the field to a few real contenders.
    """
```

Prompt shape: numbered candidate blocks (`[CANDIDATE 1] page 42: "..."`),
fixed JSON schema (`{"chosen_candidate": 1}` or `{"chosen_candidate": null}`
if none look right), "return ONLY JSON."

The winning candidate becomes a `ChapterStartMatch` using **that
candidate's own** `score`, and `margin` set to exactly
`_LOCATE_MARGIN_REQUIRED` (i.e. treated as just barely clearing the
ambiguity guard, never higher — the LLM resolved *which* candidate is right,
it did not make the underlying textual match itself any less genuinely
contested). This keeps `match_confidence` meaningful and comparable to
heuristic-only matches without inventing a new confidence scale, while
still conservatively reflecting that this chapter needed a tie-break.

## 7. Chapter dict schema change

Every chapter dict (from `analyze_attachment`, and from the new
LLM-augmented orchestration in §8) gains one field:

```python
{
    ...,  # existing fields unchanged
    "source": "heuristic" | "llm",   # "llm" if EITHER the TOC entry or the
                                      # start-page match came from an LLM call
}
```

`chapter_upload.py`'s `confidence_threshold` gate is unchanged — it already
just compares `chapter["confidence"] >= confidence_threshold` regardless of
where the chapter came from, and continues to work unmodified. `source` is
carried through purely for observability (evaluation reporting, future
per-source threshold tuning) and is not itself gated on in v1.

## 8. Orchestration: `analyze_attachment_with_llm_fallback`

`analyze_attachment(pages)` itself is **not** touched — it stays pure,
synchronous, and exactly as fast/free as today, since the default
evaluation harness and any caller that doesn't opt in must see identical
behavior to before this project.

Its internal "given a list of `TocEntry` + `pages`, produce clustered
chapter dicts" logic is extracted into a reusable helper (pure refactor,
same behavior):

```python
def _build_chapters_from_toc_entries(pages: list[str], toc_entries: list[TocEntry]) -> list[dict]:
    """The clustering/trailing-blank-trim/citation-page/confidence logic
    currently inline in analyze_attachment, minus TOC discovery itself."""
```

**New async function**:

```python
async def analyze_attachment_with_llm_fallback(pages: list[str], llm_service: LLMService) -> dict:
    """Runs analyze_attachment(pages) first (unchanged heuristic pass).
    If its result qualifies for the §4 trigger, replaces toc_entries with
    llm_extract_toc_entries(pages, llm_service)'s result and rebuilds via
    _build_chapters_from_toc_entries. Then, for any TOC entry (from either
    source) that still lacks a chapter in the result due to ambiguity (§6
    trigger), calls llm_disambiguate_chapter_start and folds a successful
    result into the chapter list. Returns the same dict shape
    analyze_attachment returns, with "source" populated on every chapter
    and diagnostics extended (see below).
    """
```

`diagnostics` gains two counters: `"llm_toc_extraction_used": bool` and
`"llm_disambiguation_used": int` (count of chapters resolved this way) —
cheap, useful for the evaluation report in §10 without needing to re-derive
it from `source` on every chapter.

## 9. Call surface (CLI / API)

`chapter_segmentation.run()` gains one new optional parameter:

```python
async def run(
    *,
    ...,  # all existing parameters, unchanged
    llm_service: Optional[LLMService] = None,
) -> dict:
```

`None` (default) = today's behavior exactly, calling `analyze_attachment`
per attachment as now. When an `LLMService` is supplied, `run()` calls
`analyze_attachment_with_llm_fallback` instead — the confidence-gating
inside that function (§4, §6) decides per-book/per-chapter whether an LLM
call actually happens, so passing a service does not mean every book incurs
one.

- **CLI** (`scripts/analyze_book_chapters.py`): new `--llm-fallback` flag,
  off by default. When passed, constructs an `LLMService` via
  `create_llm_service(Settings())` — the same call `dependencies.py` already
  makes for the rest of the app, no new credential plumbing — and passes it
  to `run()`.
- **API** (`POST /api/chapter-linking/analyze`): new optional request field
  `enable_llm_fallback: bool = False`, same default-off behavior, same
  `create_llm_service` construction inside the route handler.

## 10. Evaluation harness

`tests/test_chapter_segmentation_accuracy.py` (the existing, default-path
harness) is **unchanged** — still heuristic-only, deterministic, free, no
network access required, still runs `analyze_attachment` directly.

**New script** `scripts/evaluate_chapter_segmentation_llm_fallback.py`,
deliberately not a pytest test (matches the existing precedent that this
harness is "reported, not gated" — see README §"Running an evaluation"):
runs the same evaluation-book manifest through
`analyze_attachment_with_llm_fallback` (requires a configured, working
`LLMService` — reads real settings/API keys, costs real money per run) and
prints the same precision/recall/found-vs-expected table format the README
already uses, plus per-book counts of how often each fallback path fired
(from the new `diagnostics` counters). Not run in CI; intended for manual
runs when tuning the fallback prompts or re-validating after a heuristic
change, same operational pattern as the existing harness's own "re-run the
calibration sweep" guidance.

`backend/evaluation/book-segmentation/README.md`'s "Current results"
section gets a second table (same book rows) once this script has been run
at least once, showing the heuristic-only numbers next to
heuristic+LLM-fallback numbers, with the same "snapshot, not a guarantee"
framing the existing table already uses.

## 11. Error handling

- Any exception from an LLM call (`llm_extract_toc_entries`,
  `llm_disambiguate_chapter_start`) — network failure, malformed JSON,
  provider error — is caught at the call site and treated as "fallback
  unavailable for this book/chapter," falling back to the heuristic result
  exactly as if `llm_service` had been `None`. This mirrors
  `query_router.route()`'s existing "fall back to RAG-only plan on any
  failure" pattern. A book is never left worse off than the heuristic-only
  baseline by enabling the flag.
- Malformed LLM JSON (extra prose, wrong shape, out-of-range
  `chosen_candidate` index) is treated the same way — logged at `warning`
  level, function returns empty/`None`, orchestration proceeds without that
  fallback result.

## 12. Testing

- Unit tests for `locate_chapter_start`'s new `authors` parameter (§5):
  confirms default-empty-tuple behavior is unchanged, confirms an
  author-confirmed candidate can win against a same-scoring unconfirmed
  rival that would otherwise fail the ambiguity guard.
- Unit tests for `locate_chapter_start_candidates` (§6): confirms it
  returns the same clusters `locate_chapter_start` computes internally,
  using fixture pages already present in `backend/tests/test_chapter_segmentation.py`.
- Unit tests for `llm_extract_toc_entries` / `llm_disambiguate_chapter_start`
  / `analyze_attachment_with_llm_fallback` using a fake `LLMService` (a
  canned-JSON stub, same style as `MockLLMService` in `llm.py`) — no real
  network/API key needed for these, keeping them part of the default
  `uv run pytest` run. Covers: trigger conditions fire/don't-fire correctly,
  malformed-JSON error handling, and that `source` is populated correctly
  on the resulting chapter dicts.
- The real-LLM evaluation script (§10) is exercised manually, not part of
  the automated test suite, consistent with how the existing evaluation
  harness itself is already excluded from `pyproject.toml`'s `testpaths`.
