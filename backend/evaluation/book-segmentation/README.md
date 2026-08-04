# Evaluation data for book segmentation

Ground-truth chapter boundaries for each book are hand-verified and committed
alongside this README as `<filename-without-extension>.expected.json` (schema:
see `docs/superpowers/plans/2026-07-24-chapter-segmentation-linking.md` Task 30).
The PDFs themselves are gitignored (`*.pdf`, see `.gitignore` in this directory)
and are not shipped.

`manifest.json` is the source for this evaluation set. Each entry has:

- `filename` — matches a `<name>.pdf` / `<name>.expected.json` pair here
- `title`, `language`, `extraction_type` (`native` or `scan`), `embedded_toc`
- `oa` — whether the book can be legally auto-downloaded
- `doi` — the book's DOI (used both as metadata and, for non-OA books, as
  the pointer a human follows to acquire it)
- `download_url` — direct PDF URL, only meaningful when `oa: true`; `null`
  otherwise

**Fetching the PDFs:**

```bash
uv run python scripts/fetch_evaluation_pdfs.py
```

Downloads every `oa: true` entry that isn't already present. Non-OA entries
are never touched by this script — if one is missing, it prints the DOI and
the exact path to save the file to. Get that book through your institution's
legal access (library subscription, interlibrary loan, etc.), save it there,
and re-run the tests.

**Adding a new evaluation book** (e.g. a "difficult" PDF the segmentation
heuristics scored low-confidence on during live testing against a real
Zotero library) — see `CLAUDE.md` in this directory for the full step-by-step
workflow, including the `scripts/ground_truth_helper.py` draft-then-verify
process and known failure modes. Short version:

1. Has a DOI? Add an entry to the committed `manifest.json` (`"oa": false,
   "download_url": null` if it can't be freely redistributed — that's fully
   supported, it just means `fetch_evaluation_pdfs.py` won't auto-download
   it, only print the DOI for manual acquisition). No DOI, or can't be
   identified/shared at all? Add it to `manifest.local.json` instead (same
   schema, gitignored, never committed — see `CLAUDE.md`) so it's still
   exercised by your own local test runs.
2. Place (or fetch) the PDF at `<filename>` here.
3. Build `<name>.expected.json` by actually inspecting the real PDF — never
   guessed or extrapolated from the TOC alone (`CLAUDE.md` explains why).

## Running an evaluation

The harness lives at `backend/tests/test_chapter_segmentation_accuracy.py`,
marked `@pytest.mark.integration` so it never runs as part of the default
`uv run pytest` / `npm test` (see `pyproject.toml`'s `addopts`). Run it
directly:

```bash
uv run pytest backend/tests/test_chapter_segmentation_accuracy.py -q -s
```

`-s` is required to see the per-book summary lines (`pytest` swallows `print`
output by default). A book is silently skipped, not failed, if its PDF isn't
present locally yet — run `uv run python scripts/fetch_evaluation_pdfs.py`
first for the open-access ones, or acquire non-OA books manually (see above)
— or if it needs OCR and the evaluation OCR cache hasn't been populated yet
(see below).

Pages are loaded via `backend/evaluation/harness.py`'s `analysis_pages_for`,
the same way production's `chapter_segmentation.run()` loads them: default
pypdf extraction, falling back to pypdf's `layout` extraction mode when the
default mode finds no table of contents (recovers books whose two-column
TOC the default mode scrambles), and finally the evaluation OCR cache
(`backend/evaluation/book-segmentation/.ocr-cache/`, gitignored,
content-hash keyed) for books whose text layer is absent or degenerate. A
book that needs OCR and has no cache entry yet prints `SKIPPED (needs OCR
...)` and is excluded from that run rather than scored 0.00 -- populate the
cache first with:

```bash
uv run python scripts/ocr_evaluation_pdfs.py
```

This requires the Kreuzberg sidecar container running (see root `CLAUDE.md`'s
Live Server section). It OCRs every evaluation book whose text layer needs
it and caches the result by content hash, so a first run over several full
scanned books can take 1-3 hours but every later run (unchanged PDFs) is
near-instant, cache-hit-only.

For each book, the test runs `chapter_segmentation.analyze_attachment` over
the loaded page text and compares the resulting chapter ranges
against `<name>.expected.json`:

- **Precision** = correctly-found ranges ÷ all ranges the algorithm returned
  (how much of what it found was right)
- **Recall** = correctly-found ranges ÷ all ranges in the ground truth (how
  much of the real content it found)

A "correct" range requires an **exact** `(pdf_start_index, pdf_end_index)`
match — a one-page-off boundary counts as a miss, not partial credit.

This harness is **reported, not gated** (design spec §12): the heuristics are
probabilistic and not expected to hit 100% on real, messy PDFs, so precision/
recall are not asserted against a required minimum. The only hard assertion
is `recall > 0` per book — a regression guard that catches "this book now
finds zero of its known chapters," not a quality bar. A book already at 0%
recall (a known, accepted heuristic limitation — see `CLAUDE.md`'s "Known
failure modes") will keep failing this assertion until someone improves the
underlying heuristic; that failure is expected, not a sign something else is
broken.

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

## Current results

Snapshot from running the harness above, one row per evaluation book
(regenerate anytime — these numbers shift as the heuristics evolve, so treat
this table as a snapshot to compare future runs against, not a guarantee):

| Book (title / filename) | Language | Type | Precision | Recall | Found / Expected |
| --- | --- | --- | --- | --- | --- |
| Transformations of European Welfare States and Social Rights (`9783031466373.pdf`) | en | native | 1.00 | 1.00 | 11/11 found, 11/11 expected |
| Violence, Imagination, and Resistance (`9781771993661.pdf`) | en | native | 1.00 | 1.00 | 10/10 found, 10/10 expected |
| 20 ans de transparence à Genève (`9783907297339.pdf`) | fr | native | 1.00 | 1.00 | 11/11 found, 11/11 expected |
| Accueillir des publics migrants et immigrés (`9782375460122.pdf`) | fr | native | 0.78 | 0.82 | 14/18 found, 14/17 expected |
| Recht in der Krise — APARIUZ XXIII (`9783907297285.pdf`) | de | native | 0.69 | 0.69 | 9/13 found, 9/13 expected |
| Recht umkämpft (`9783847432364.pdf`) | de | native | 0.95 | 0.95 | 20/21 found, 20/21 expected |
| Jahrbuch für Rechtssoziologie und Rechtstheorie IV (`9783322969828.pdf`) | de | scan | 0.96 | 0.92 | 22/23 found, 22/24 expected |

Aggregate (micro): **precision 0.91, recall 0.91** across 107 expected
chapters. These 7 books' numbers are unaffected by the layout-mode
extraction fallback and evaluation OCR cache described in "Diverse
real-library evaluation set" below -- their default-mode pypdf extraction
already finds a usable TOC, so neither addition ever fires for them.

The heuristic pipeline's main mechanisms, in the order they run
(see `chapter_segmentation.py` for the full details on each):

- `find_toc_candidates` counts only lines that survive the content filters
  (URL/DOI, implausible page numbers) toward the "does this page look like
  a listing" density test, so an imprint/metadata page can't shadow the
  real TOC; it accepts roman-numeral page fields for front-matter entries
  ("Foreword vii"), merges wrapped multi-line titles into alternative
  "variant" readings (the page number sits on the last physical line only),
  adopts the preceding line's text when the page number sits on a bare
  dot-leader line of its own, extends the TOC cluster onto a following page
  holding just the listing's last couple of entries, and recognizes the
  author-line convention ("MOTS ET CHIFFRES ... / par Mustapha Harzoune
  .... 16") where only `par`/`by`-marked entries are chapter-level.
- `locate_chapter_start` strips repeated running headers (detected
  digit-insensitively across the book) before scoring a page's head, so a
  book that stamps every page with a long header doesn't hide its titles.
- `_locate_toc_entries` picks, per entry, whichever variant reading locates
  most trustworthily (ranked by `min(score, margin)` — certainty, not raw
  score), excludes "secondary listing" pages (part-divider pages and
  cover/blurb pages that quote several chapter titles) and everything on or
  before the TOC itself (except for roman-paginated front-matter entries),
  and finally resolves per-entry ambiguity with TOC-order constraints: an
  entry's candidates are pruned to the interval between its already-located
  neighbors, which cleanly separates Introduction/Conclusion pairs sharing
  the book's own title as a suffix.
- `_chapters_from_located` keeps part dividers and standard back-matter
  sections (Index, Contributors, Sommaire, ...) as located *boundaries*
  without emitting them as chapters, and trims chapter ends past trailing
  blank pages and known non-content (TOC/listing) pages.

Known remaining misses, all understood and accepted for now:

- `9782375460122.pdf`: three chapters end on image-only pages that pypdf
  extracts as empty text — the text heuristic cannot distinguish them from
  blank divider pages, so those ends are one page short.
- `9783907297285.pdf`: this book's hand-verified ground truth attaches each
  trailing part-divider page ("Teil 2: ...") to the preceding chapter,
  while `9783322969828.pdf`'s ground truth excludes divider pages from
  chapters — the two conventions are mutually exclusive for a single
  heuristic, and the current behavior matches the latter (more common)
  book. Its first chapter also opens with a per-chapter half-title page two
  pages before the body, which the locate step returns instead of the
  ground truth's body page.
- `9783322969828.pdf` (scan): one chapter's TOC line is OCR-garbled beyond
  what the fuzzy matcher recovers.

`chapter_upload.py`'s `confidence_threshold` default was re-calibrated
against this snapshot (now `0.90`): with ~91% of all proposed chapters
already exactly correct, the sweep shows the threshold no longer buys
precision (0.91 → 0.93 across the whole range) while anything above ~0.94
sharply cuts how many correct chapters survive — the remaining errors are
end-boundary quirks that the start-match confidence cannot see. **Re-run
the calibration sweep (or re-derive it against `analyze_attachment`'s
output) any time `find_toc_candidates`, `locate_chapter_start`, or
`match_confidence` change, or the evaluation set grows** — these numbers
are a snapshot tied to the current heuristics, not a permanent constant.

### Strategy-pipeline status

Snapshot from `uv run python scripts/evaluate_chapter_segmentation_strategies.py`
(regenerate anytime -- these numbers move with the strategies/fusion logic):

| Book (filename) | Precision | Recall | Found / Expected | Strategies used |
| --- | --- | --- | --- | --- |
| `9783031466373.pdf` | 0.83 | 0.91 | 10/12 found, 10/11 expected | crossref, outline, outline+crossref |
| `9781771993661.pdf` | 1.00 | 1.00 | 10/10 found, 10/10 expected | (falls back to heuristic) |
| `9783907297339.pdf` | 0.82 | 0.82 | 9/11 found, 9/11 expected | outline |
| `9782375460122.pdf` | 0.78 | 0.82 | 14/18 found, 14/17 expected | (falls back to heuristic) |
| `9783907297285.pdf` | 0.64 | 0.69 | 9/14 found, 9/13 expected | outline |
| `9783847432364.pdf` | 0.95 | 0.95 | 20/21 found, 20/21 expected | crossref |
| `9783322969828.pdf` | 0.96 | 0.96 | 23/24 found, 23/24 expected | crossref |

Aggregate (micro): **precision 0.86, recall 0.89** across 107 expected
chapters -- still below the pure-heuristic baseline's 0.91/0.91 above, so on
this evaluation set the strategies remain a net *regression* when they fire,
not an improvement, though a much smaller one than the previous snapshots.
`analyze_attachment_with_strategies` only falls back to the pure-heuristic
pipeline when a book's merged candidate list is completely empty (two books
above), never when it's merely wrong -- so a confidently-incomplete or
imprecise outline/Crossref result is trusted over what the heuristic
pipeline would have found instead. Root causes found and fixed so far (see
`extract_outline_candidates` in
`backend/services/chapter_evidence/outline_strategy.py`,
`_is_non_chapter_structural_title` in `backend/services/chapter_common.py`,
`analyze_attachment_with_strategies`'s `exclude_indices` computation and
`merge_metadata_sources`'s single-source sort in
`backend/services/chapter_segmentation.py`/`fusion.py`, and
`_parse_crossref_item`'s subtitle handling in
`backend/services/chapter_evidence/crossref_strategy.py` for the code,
`backend/tests/test_chapter_evidence_outline.py` /
`test_chapter_segmentation_strategies.py` /
`test_chapter_evidence_fusion.py` / `test_chapter_evidence_crossref.py` for
regression coverage):

- **Real chapters nested one level under unlabeled "Part I/II/III" outline
  nodes.** The top-level-only read used to silently accept whatever
  front/back matter survived around the part dividers as "the chapter
  list" (a sparse-but-non-empty result, so the empty-merge fallback never
  triggered) -- now rejected outright the moment a part-divider entry is
  found to have nested children, deferring correctly to the heuristic
  fallback. Recovered both books that hit this to their pre-regression
  1.00/1.00 and 0.78/0.82 scores.
- **PDF-outline-specific bookmark vocabulary** (Cover, Half Title, Title,
  Copyright, Backcover, Impressum, German "...verzeichnis" compound nouns,
  and the book's own title used as its own bookmark) was leaking through as
  false-positive chapters -- none of it appears in a printed table of
  contents, which is what the back-matter title list was originally built
  from.
- **Trailing-page trim over-reach for outline-sourced chapters.** The
  generic "front matter through the printed TOC" exclusion region (built to
  constrain where a *not-yet-located* chapter may start) was also being
  used to trim chapter *ends* -- collapsing a chapter's real content down
  to a single page whenever its true end fell inside that broad region
  (e.g. a Foreword that legitimately lives in the front matter).
- **Crossref/Zotero-catalog candidates were localized against a blind
  15%-of-total-pages front exclusion zone, not the book's actual printed
  TOC.** `analyze_attachment_with_strategies` fed metadata-sourced
  candidates needing content-search localization through
  `_toc_scan_indices(pages)` -- a fixed front-15%/back-5% page-fraction
  region meant only as a *search window* for `find_toc_candidates`, never
  as a "definitely non-chapter-content" guarantee. On books whose front
  matter is much shorter than 15% of total length (all three
  `crossref`-touched books above run 226-419 pages with a 1-2 page printed
  TOC), this blindly excluded real early chapter starts from the candidate
  pool entirely (silently dropped as unlocated) and pushed others onto a
  later page sharing the same running header. Fixed by deriving
  `exclude_indices` from `find_toc_candidates(pages)`'s own detected TOC
  page(s) instead -- the same source `analyze_attachment`/
  `analyze_attachment_with_llm_fallback` already use -- falling back to the
  blind fraction only when no real TOC page is found at all. Recovered
  `9783322969828.pdf` to near-parity with the heuristic baseline (0.95/0.75
  -> 0.96/0.96) and substantially improved the other two Crossref-touched
  books.
- **A single metadata source's candidate list is not guaranteed to be in
  book order.** `merge_metadata_sources` only sorted by
  `printed_page_number` when actually merging TWO non-empty sources
  (`_merge_two_metadata_lists`'s own final sort); with a single source --
  the common case for a Crossref-only book -- the list passed straight
  through in whatever order the Crossref API happened to return it.
  `_locate_toc_entries`'s second-pass "TOC order is book order" ambiguity
  disambiguation relies on list POSITION (not `printed_page_number` value)
  mirroring book order, so an out-of-order single source silently broke
  its `lower`/`upper` bound computation and made a genuinely locatable
  chapter (e.g. a short, ambiguous title with real page-order neighbors
  that should have pinned it down) fall through as unresolved instead.
  Fixed by sorting a single source the same way a merged pair already is.
- **Crossref splits a chapter's real printed heading into separate
  title/subtitle fields, and `_parse_crossref_item` was discarding the
  subtitle.** A truncated title (e.g. "Commons." instead of the real
  "Commons. Was wir brauchen und was uns gemeinsam ist") is a much weaker,
  more ambiguous content-search target than the full heading a PDF-outline
  bookmark supplies for the same chapter -- it can tie or even out-score
  its own true opening page against a brief in-body mention of the same
  short phrase in a nearby, unrelated chapter, and because
  `locate_chapter_start_candidates` transitively chains same-title matches
  within a small page gap (deliberately, to keep a long chapter's own
  repeating running header as one cluster -- see its docstring), a
  spurious short mention 2-3 pages before the real opening page can chain
  onto it and report the earlier, wrong page as the location. Fixed by
  requesting Crossref's `subtitle` field and appending it to `title[0]`
  when present, restoring the full heading. Recovered `9783847432364.pdf`
  from 0.80/0.76 to 0.95/0.95 (20/21 chapters now exactly correct).

Known remaining gaps, not yet fixed:

- A book-specific structural section label with no generic vocabulary
  match (e.g. "Gastbeiträge: ..." -- German for "guest contributions",
  grouping several chapters that follow it) still leaks through as a false
  chapter on `9783907297285.pdf`. The same class of gap causes "Preface" (a
  real outline bookmark, not counted as a ground-truth chapter) to leak
  through as a false positive on `9783031466373.pdf`.
- **The last located chapter in a book has no "next entry" to bound its
  end**, so `_chapters_from_located` defaults to the last PDF page and
  trims backward only past pages its blank-page/non-content checks
  recognize. A publisher's back-cover blurb or colophon page (ISBN,
  website, marketing copy -- real, non-blank text, so the blank-page check
  doesn't fire) is not detected as non-chapter content, over-extending the
  final chapter by a few pages on both `9783847432364.pdf` (350-364 found
  vs. 350-361 expected) and `9783322969828.pdf` (405-418 found vs. 405-416
  expected). Crossref's own "Back Matter" candidate (present in both
  books' raw results, see `crossref_candidates_found` in diagnostics)
  would be the right boundary if it could be located, but "Back Matter" is
  a generic schema label, not literal text printed anywhere in either
  book, so it never resolves to a page and can't serve as one; not yet
  root-caused further.
- Even where the located start page is correct, `_chapters_from_located`'s
  blank-page/trim heuristics still occasionally over- or under-shoot a
  chapter's end by a few pages for outline- and Crossref-sourced chapters
  alike -- the same class of imprecision the pure-heuristic pipeline
  already has (see "Known remaining misses" above).

**Given this net regression, `analyze_book_chapters.py`/the `/api/analyze`
endpoint should not be pointed at the strategy pipeline for production use
until the remaining gaps above are closed** -- re-run this evaluation after
any further change to the outline/Crossref/fusion logic.

### Diverse real-library evaluation set

The 7-book set above is small and, per the design spec's `## 1. Goal`
motivation, skews toward well-produced academic books with a parseable
embedded TOC page -- exactly the case the pure-heuristic pipeline already
handles well (6 of 7 have `embedded_toc: true`; the 7th, a scan, still has a
regex-detectable printed TOC). It does not exercise the case the
outline/Crossref/Zotero-catalog strategies were actually built for: books
from real, diverse personal libraries with no embedded TOC bookmark, no
Crossref/DOI record, and (for most) no *regex-detectable* printed TOC in
the raw extracted text -- though, as the investigation below found, several
of them do have a real printed TOC that only needed a different extraction
approach to become detectable.

`manifest.local.json` (gitignored, per `CLAUDE.md`'s "no DOI" rule below)
adds 10 more books sourced directly from a real personal Zotero library,
selected for the opposite profile of the 7 above: `embedded_toc: false`,
spanning 1967-2020, native and scanned, German and English, none open
access, and none with a Crossref-registered DOI at all (checked directly
against the API, book- or chapter-level):

| Filename | Title | Year | Extraction | Precision | Recall | Found/Expected | Recovery route |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `9783848736829.pdf` | Politik und Recht | 2017 | native | 1.00 | 1.00 | 23/23, 23/23 | layout-mode fallback |
| `9783492021234.pdf` | Empirische Rechtssoziologie | 1975 | native | 0.27 | 0.29 | 4/15, 4/14 | layout-mode fallback |
| `9783789016202.pdf` | Rechtsproduktion und Rechtsbewußtsein | 1988 | native | 0.46 | 0.50 | 6/13, 6/12 | layout-mode fallback |
| `9783899718188.pdf` | Systemtheorie in den Fachwissenschaften | 2011 | native | 0.27 | 0.30 | 3/11, 3/10 | layout-mode fallback |
| `9780367439712.pdf` | Luhmann and Socio-Legal Research | 2020 | native | 0.36 | 0.42 | 5/14, 5/12 | OCR (degenerate text layer) |
| `9783789057366.pdf` | Soziologie des Rechts (Festschrift für Erhard Blankenburg) | 1999 | native | 0.00 | 0.00 | 0/2, 0/56 | OCR (degenerate text layer) -- still 0 |
| `9783465016878.pdf` | Historische Soziologie der Rechtswissenschaft | 1986 | scan | 0.40 | 0.15 | 2/5, 2/13 | OCR (no text layer) |
| `9781409403906.pdf` | Central and Eastern Europe After Transition | 2010 | scan | 0.10 | 0.08 | 1/10, 1/12 | OCR (no text layer) |
| `9783848704316.pdf` | Constitutional Jurisprudence | 2016 | scan | 0.00 | 0.00 | 0/0, 0/15 | OCR (no text layer) -- still 0 |
| `dnb-36942798X.pdf` | Studien und Materialien zur Rechtssoziologie | 1967 | scan | 0.00 | 0.00 | 0/1, 0/18 | OCR (no text layer) -- still 0 |

Aggregate (micro, these 10 books alone): **precision 0.47, recall 0.24**
(44/94 found correctly, 44/185 expected chapters). These are the same
numbers whether run through the pure heuristic (`analyze_attachment`,
`test_chapter_segmentation_accuracy.py`) or the full strategy pipeline
(`analyze_attachment_with_strategies`, `strategies_used: []` on all 10) --
none of the 10 has a Crossref record or a usable PDF outline, so the
strategy pipeline always falls straight back to the same heuristic result
on this set; the strategies genuinely add nothing here, for better or
worse.

This is a large change from an earlier, incorrect reading of this same
sample: all 10 books used to be reported as scoring 0.00/0.00 with
`toc_matches_found: 0`, taken as evidence that "none of the three
currently-implemented strategies have any signal to work with on this
sample at all." That conclusion was wrong -- it measured a text-extraction
artifact, not an absence of signal:

- **6 of the 10 books have a real, intact printed table of contents.**
  pypdf's *default* text-extraction mode destroys a two-column TOC layout
  (page numbers land on their own line, separated from titles, or glue onto
  the next title: `'7Vorwort'`, `'123III. Recht zwischen den
  Professionen'`), so `find_toc_candidates`'s `<title> ... <page number>`
  regex never matches a single physical line and reports zero candidates.
  `extract_page_texts_for_analysis` (`chapter_segmentation.py`) now retries
  with pypdf's `layout` extraction mode -- which preserves column
  alignment -- whenever default-mode extraction finds no TOC, and adopts
  the layout-mode pages only if THAT finds one. This is a pure fallback:
  none of the original 7 committed books' scores changed, since their
  default-mode extraction already found a TOC and the layout attempt never
  fires for them. Four of the ten books recovered this way -- one to a
  perfect 1.00/1.00, the other three to partial (0.29-0.50) scores limited
  by ordinary heuristic imprecision (messy TOC layouts: inline author
  names, a left-column page-number convention), not a missing signal.
- **2 of the 10 books have a *degenerate* text layer**
  (`9783789057366.pdf`, `9780367439712.pdf`): every page extracts as one
  giant line with essentially no newlines, in *both* default and
  layout-mode pypdf extraction (pypdf itself warns "Rotated text
  discovered" while reading them) -- no line-oriented heuristic can work on
  that shape of text regardless of extraction mode. `pages_need_ocr`
  (`chapter_segmentation.py`) now detects this case (alongside an
  absent/near-absent text layer) and routes both books through OCR instead
  -- the same per-page Kreuzberg OCR path production uses for scans
  (`chapter_ocr.ocr_pdf_pages`), cached by content hash in the gitignored
  `backend/evaluation/book-segmentation/.ocr-cache/` and populated by
  `uv run python scripts/ocr_evaluation_pdfs.py` (run once; re-runs are
  instant cache hits). OCR recovers usable line structure for one of the
  two (`9780367439712.pdf`, 0.00 -> 0.42); the other
  (`9783789057366.pdf`) still scores 0.00 -- see "still 0" below.
- **The 4 true scans** (`9783465016878.pdf`, `9781409403906.pdf`,
  `9783848704316.pdf`, `dnb-36942798X.pdf`) had no text layer at all and
  were never exercised by any evaluation script before this change (the
  pytest harness and both `scripts/evaluate_chapter_segmentation_*.py`
  scripts fed raw pypdf text straight into analysis, with no OCR step --
  unlike production's `run()`, which already had one). They now go through
  the same OCR-cache route as the two degenerate-text books above. 2 of the
  4 recover partial signal (0.08-0.15); 2 remain at 0.00 -- see below.

**The 3 books still at 0.00 recall after OCR, with actual root causes**
(traced by hand -- inspecting the cached OCR text and `find_toc_candidates`'
raw output directly, not guessed):

- `9783789057366.pdf`: OCR *does* locate the real front-matter TOC page
  (`find_toc_candidates` finds it), but Tesseract's OCR of the dot-leader
  lines is badly garbled on this particular scan -- a line that should read
  something like `"Zueignung .......... 6"` instead OCRs as
  `"ZUEISNUNG .n...onannnnnennenenennsnnennnnmn nen nn nenn nennen rennen"`.
  The dot-leader run of characters is essentially noise, and several
  titles are corrupted too, so almost no entry survives cleanly enough for
  the content-search localization step to find its real opening page. This
  is an OCR-quality problem on a specific page layout convention (dense
  dot leaders), not a missing-TOC problem.
- `dnb-36942798X.pdf`: `find_toc_candidates` finds 6 "candidates," but they
  are OCR'd bibliography/citation-index lines from the back of the book
  (`"Law, Criminology, and Police Science ... 55"`,
  `"... American Journal of Sociology ... 70"`) -- journal citations that
  structurally resemble a TOC line ("title ... number") but aren't one.
  The book's real front-matter TOC apparently didn't survive OCR at a high
  enough line-density to win the "first qualifying cluster" contest against
  this back-matter citation list. This is the same "secondary listing wins
  over the real TOC" failure mode already documented for
  `9783322969828.pdf` in "Known remaining misses" above, here triggered by
  OCR degrading the real TOC's density rather than a structural ambiguity.
- `9783848704316.pdf`: `find_toc_candidates` returns **zero** candidates
  anywhere in the OCR'd text's front/back scan window -- no page has three
  or more lines matching the TOC-line pattern at all, even after OCR. Not
  yet root-caused further (worth checking by hand whether this book's
  printed TOC uses a structurally different convention the regex can't
  match at all, versus OCR quality issues specific to its front matter).

These three keep `"heuristic_expected_zero": true` in `manifest.local.json`
(a known, currently-accepted limitation, re-checked and re-justified with
real evidence rather than the earlier blanket claim); the other seven of
the ten are now `false` and are held to the same `recall > 0` regression
guard as the rest of the evaluation set.

Ground truth for these 10 books was built directly from the PDFs via
multimodal reading (visually locating each chapter's opening/closing page
and transcribing its table of contents) rather than `CLAUDE.md`'s
Crossref-page-range shortcut for OA books, since none of the 10 has any
Crossref record to shortcut from. Because none has a DOI, all 10 entries
(and their PDFs) live only in the gitignored `manifest.local.json`/`*.pdf`
-- but the 10 `.expected.json` ground-truth files themselves (titles,
authors, and page indices only, no copyrighted text) are committed here, so
anyone who acquires the same PDFs by ISBN (see filenames) can reuse them
directly without rebuilding ground truth from scratch.

Two other personal-library PDFs were found and rejected as candidates
before landing on this set of 10: both turned out to be heavily abridged
excerpts of much larger books (missing whole chapters, or missing
everything past a certain page, likely a photocopied subset rather than a
full digitization) rather than complete books -- a real, if narrower,
failure mode of sourcing evaluation data this way, distinct from either
`embedded_toc` value and worth checking for (compare the file's actual page
count against its own printed page numbers near the front and back) before
investing time in transcribing ground truth for a new candidate.

### LLM-fallback status

With the heuristics above, a full run of
`scripts/evaluate_chapter_segmentation_llm_fallback.py --auto-select-model`
(KISSKI-backed preset) reports **identical numbers to the pure-heuristic
harness, with neither fallback path firing on any book**: TOC extraction
never triggers because the regex path now finds a usable listing everywhere,
and per-entry ambiguity is resolved heuristically by the TOC-order
constraints before the LLM would be consulted. The disambiguation fallback
still exists for the case ordering genuinely cannot solve (all of an
ambiguous entry's candidates conflicting with its located neighbors — a
disordered TOC), and TOC extraction still covers books whose listing the
regex can't parse at all; neither case occurs in the current evaluation
set. Re-run after any prompt or heuristic change to check whether that is
still true — if the heuristic path regresses, the fallback is the safety
net that should catch it.
