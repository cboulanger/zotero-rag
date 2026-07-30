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
first for the open-access ones, or acquire non-OA books manually (see above).

For each book, the test runs `chapter_segmentation.analyze_attachment` over
the real extracted page text and compares the resulting chapter ranges
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
chapters. The heuristic pipeline's main mechanisms, in the order they run
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
