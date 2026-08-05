# Evaluation data for book segmentation

Ground-truth chapter boundaries for each book are hand-verified and committed
alongside this README as `<filename-without-extension>.expected.json` (schema:
see `docs/superpowers/plans/2026-07-24-chapter-segmentation-linking.md` Task 30).
The PDFs themselves are gitignored (`*.pdf`, see `.gitignore` in this directory)
and are not shipped.

This README documents what the evaluation set is and how to run it -- it
changes rarely. For current precision/recall numbers, known gaps, and
investigation findings from the last time each evaluation was actually run,
see **`RESULTS.md`** in this directory instead -- that document is a
snapshot and is expected to be regenerated (or rewritten) whenever the
heuristics, the strategy pipeline, the extraction/OCR path, or the
evaluation set change.

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
finds zero of its known chapters," not a quality bar. A book flagged
`"heuristic_expected_zero": true` in its manifest entry is exempt from this
assertion (see `CLAUDE.md`'s "Step 0" for exactly when that flag applies and
how to re-check it); `RESULTS.md` documents which books currently carry it
and why.

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
real evaluation set -- record what you find in `RESULTS.md`.

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
LLM-fallback evaluation script above already established -- record what you
find in `RESULTS.md`.

## Evaluation set composition

The committed `manifest.json` set (7 books) is small and, per the design
spec's `## 1. Goal` motivation, skews toward well-produced academic books
with a parseable embedded TOC page -- exactly the case the pure-heuristic
pipeline already handles well (6 of 7 have `embedded_toc: true`; the 7th, a
scan, still has a regex-detectable printed TOC).

`manifest.local.json` (gitignored, per `CLAUDE.md`'s "no DOI" rule above)
adds 10 more books sourced directly from a real personal Zotero library,
selected for the opposite profile: `embedded_toc: false`, spanning
1967-2020, native and scanned, German and English, none open access, and
none with a Crossref-registered DOI at all (checked directly against the
API, book- or chapter-level). This set exercises the case the
outline/Crossref/Zotero-catalog strategies were built for, and the case the
layout-mode extraction fallback and evaluation OCR route (see "Running an
evaluation" above) were built for:

| Filename | Title | Year | Extraction |
| --- | --- | --- | --- |
| `9783848736829.pdf` | Politik und Recht | 2017 | native |
| `9783492021234.pdf` | Empirische Rechtssoziologie | 1975 | native |
| `9783789016202.pdf` | Rechtsproduktion und Rechtsbewußtsein | 1988 | native |
| `9783789057366.pdf` | Soziologie des Rechts (Festschrift für Erhard Blankenburg) | 1999 | native |
| `9783899718188.pdf` | Systemtheorie in den Fachwissenschaften | 2011 | native |
| `9780367439712.pdf` | Luhmann and Socio-Legal Research | 2020 | native |
| `9783465016878.pdf` | Historische Soziologie der Rechtswissenschaft | 1986 | scan |
| `9781409403906.pdf` | Central and Eastern Europe After Transition | 2010 | scan |
| `9783848704316.pdf` | Constitutional Jurisprudence | 2016 | scan |
| `dnb-36942798X.pdf` | Studien und Materialien zur Rechtssoziologie | 1967 | scan |

See `RESULTS.md` for how each of these books currently scores and why.

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
