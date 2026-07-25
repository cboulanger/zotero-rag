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

The harness lives at `tests/test_chapter_segmentation_accuracy.py` (repo root
`tests/`, deliberately outside `backend/tests/` and `pyproject.toml`'s
`testpaths`, so it never runs as part of the default `uv run pytest`). Run it
directly:

```bash
uv run pytest tests/test_chapter_segmentation_accuracy.py -q -s
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

**First real run of `scripts/evaluate_chapter_segmentation_llm_fallback.py`**
(against the KISSKI-backed `apple-silicon-kisski` preset): precision/recall
came back **identical to the pure-heuristic baseline** below for all 7 books
— the LLM fallback did not change a single result on this run, for two
distinct reasons observed directly in the logs:

- **The configured default preset model returned repeated `500`
  (`InternalServerError`) responses** from the KISSKI Chat-AI endpoint for
  every LLM call attempted — a remote-service issue, not a bug in this
  project's code (the fallback still degraded gracefully every time, per
  its design). Re-running against a smaller, working model
  (`meta-llama-3.1-8b-instruct`, confirmed reachable via
  `scripts/test_kisski_api.py`) eliminated the `500`s.
- With that smaller model actually responding, **`llm_disambiguate_chapter_start`
  fired 11 times across the set but every single response failed to parse**
  — instead of the requested `{"chosen_candidate": ...}` JSON, the model
  repeatedly hallucinated a fictitious tool call (e.g. `"I'll use the
  book_chapter_finder tool to find the correct page... tool call:
  book_chapter_finder(CANDIDATE 1, CANDIDATE 2, ...)"`). Each failure was
  caught and logged exactly as designed, falling back to the heuristic
  result rather than crashing or corrupting output — but the fallback also
  provided zero net benefit in this run.
- `llm_extract_toc_entries` fired once (for `9782375460122.pdf`, the book
  already at 0% heuristic recall below) and also failed to parse for the
  same reason, so it likewise contributed nothing this run.

**Takeaway:** the orchestration/error-handling contract (spec §11 — never
worse than the heuristic baseline) held up empirically, but disambiguation
prompt-following was unreliable on the smaller model tried here. Before
relying on this fallback for real gains, try a model from `KISSKI_RAG_MODELS`
better known for instruction-following on structured-output tasks (e.g.
`llama-3.3-70b-instruct` per `scripts/test_kisski_api.py`), or once the
default `mistral-large-3-675b-instruct-2512` model is confirmed reachable
again. Re-run this script any time the prompt, model choice, or heuristic
changes to check whether the fallback has become net-helpful.

Snapshot from running the harness above, one row per evaluation book
(regenerate anytime — these numbers shift as the heuristics evolve, so treat
this table as a snapshot to compare future runs against, not a guarantee):

| Book (title / filename) | Language | Type | Precision | Recall | Found / Expected |
| --- | --- | --- | --- | --- | --- |
| Transformations of European Welfare States and Social Rights (`9783031466373.pdf`) | en | native | 0.78 | 0.64 | 7/9 found, 7/11 expected |
| Violence, Imagination, and Resistance (`9781771993661.pdf`) | en | native | 0.90 | 0.90 | 9/10 found, 9/10 expected |
| 20 ans de transparence à Genève (`9783907297339.pdf`) | fr | native | 0.71 | 0.45 | 5/7 found, 5/11 expected |
| Accueillir des publics migrants et immigrés (`9782375460122.pdf`) | fr | native | 0.00 | 0.00 | 0/0 found, 0/16 expected |
| Recht in der Krise — APARIUZ XXIII (`9783907297285.pdf`) | de | native | 0.50 | 0.54 | 7/14 found, 7/13 expected |
| Recht umkämpft (`9783847432364.pdf`) | de | native | 0.89 | 0.76 | 16/18 found, 16/21 expected |
| Jahrbuch für Rechtssoziologie und Rechtstheorie IV (`9783322969828.pdf`) | de | scan | 0.63 | 0.71 | 17/27 found, 17/24 expected |

`Accueillir des publics migrants et immigrés` is the one book currently at
0% recall — a known, unfixed limitation of `locate_chapter_start`'s
fuzzy-matching heuristic (no signal available to disambiguate its specific
chapter titles from surrounding text), tracked as an accepted gap rather than
a regression. It now finds zero candidates rather than many wrong ones
(`find_toc_candidates`'s noise filters correctly reject its front-matter
listing pages), which is a smaller failure mode but doesn't fix the
underlying recall gap.

**What changed since the table above first went in** (see git history on
`chapter_segmentation.py` for the full sequence): `find_toc_candidates` used
to accept *any* line anywhere in the front/back scan region that loosely
matched "title ... page number" — on one book this pulled 143 candidate
"chapters" out of a back-of-book alphabetical index and a book's own
per-chapter sub-outlines, for 11 real chapters. It now requires a page to
have several such lines before trusting any of them (a real listing looks
like a listing), keeps only the first such page-cluster in the document (a
book has one table of contents, not several), and drops individual lines
that are a URL/DOI or have an implausible page number (a copyright year,
a law-citation reference). Separately, `analyze_attachment` now trims a
chapter's computed end boundary past trailing blank/divider pages, fixing a
systematic off-by-one on books that force chapters to start on a recto page
— the same fix `scripts/ground_truth_helper.py` already needed when this
evaluation set's own ground truth was built by hand.

Combined effect across the 7-book set: aggregate precision went from ~28%
to ~72%, aggregate recall from ~31% to ~58%, with no book's recall newly
regressing to zero. `chapter_upload.py`'s `confidence_threshold` default
was re-calibrated afterward (0.98 → 0.96) since the previous calibration
was against the old, much noisier candidate set — the new default lifts
precision among chapters that clear the bar to ~82% while keeping 90% of
genuinely correct chapters. **Re-run the calibration sweep in this file's
git history (or re-derive it against `analyze_attachment`'s output) any
time `find_toc_candidates`, `locate_chapter_start`, or `match_confidence`
change, or the evaluation set grows** — these numbers are a snapshot tied
to the current heuristics, not a permanent constant.
