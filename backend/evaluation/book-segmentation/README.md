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

**LLM-fallback evaluation, first meaningful run** (KISSKI-backed
`apple-silicon-kisski` preset). Two attempts were needed to get a usable
signal:

1. **Naive first attempt** — just calling `make_llm_service()` with the
   preset's configured default model failed on every single LLM call with a
   `500 InternalServerError`. Cross-checking against KISSKI's live
   `/v1/models` endpoint (via this project's own
   `backend.utils.kisski.fetch_kisski_rag_models()`) showed the configured
   default, `mistral-large-3-675b-instruct-2512`, **is not currently in
   KISSKI's live model list at all** — it's likely been renamed or retired
   upstream. This is a preset-configuration staleness issue, not a bug in
   the fallback code itself (worth updating `apple-silicon-kisski`'s
   `KISSKI_RAG_MODELS` default separately from this feature). The fallback's
   own error handling degraded gracefully every time, as designed.
2. **Systematic retry across live, available models** — using
   `fetch_kisski_rag_models()` to get every text-generation-suitable model
   or the endpoint currently reports (already filters out code-only models),
   excluding any at `"very busy"`, ordered most-available-first, and retrying
   each LLM call against the next candidate model whenever a call raised an
   error *or* returned a response that failed to parse as the expected JSON
   shape (this happened once, for one book, purely due to one model's
   context-window limit — the retry moved on to the next model and
   succeeded). This is real signal, not a fluke of one lucky model choice.

Aggregate result across the 7-book set, LLM fallback vs. the pure-heuristic
baseline below: **precision essentially unchanged (71.8% → 71.7%), recall up
~9.5 points (57.5% → 67.0%)** — the fallback found real chapters the
heuristic missed without meaningfully increasing false positives. Per book:

| Book (filename) | Heuristic P/R | LLM-fallback P/R | Fallback paths fired |
| --- | --- | --- | --- |
| `9783031466373.pdf` | 0.78 / 0.64 | 0.67 / 0.73 | disambiguation ×3 |
| `9781771993661.pdf` | 0.90 / 0.90 | 0.90 / 0.90 | none (heuristic already unambiguous) |
| `9783907297339.pdf` | 0.71 / 0.45 | 0.88 / 0.64 | disambiguation ×1 |
| `9782375460122.pdf` | 0.00 / 0.00 | 0.25 / 0.06 | TOC extraction |
| `9783907297285.pdf` | 0.50 / 0.54 | 0.47 / 0.54 | disambiguation ×1 |
| `9783847432364.pdf` | 0.89 / 0.76 | 0.85 / 0.81 | disambiguation ×3 |
| `9783322969828.pdf` | 0.63 / 0.71 | 0.73 / 0.92 | disambiguation ×3 |

`9782375460122.pdf` (the persistent 0%-recall book, see below) improved from
0/16 to a still-poor 1/16 via TOC extraction — a marginal gain, not a fix;
this book's underlying difficulty remains open. `9783031466373.pdf` and
`9783907297285.pdf` show the fallback isn't free of trade-offs either: the
former traded some precision for a real recall gain, and the latter's
disambiguation pick didn't change the outcome (a resolved "ambiguous" match
that scored the same as what the heuristic guard had already rejected).

**Takeaway:** with a model that's actually live and responds in the expected
JSON shape, the fallback is net-positive on this evaluation set — mainly by
resolving genuine start-page ambiguities the heuristic correctly refused to
guess at. The retry-across-models approach prototyped for this run has
since been promoted into the general `LLMService` abstraction
(`AutoSelectLLMService`, `backend/services/llm.py`) and is no longer ad
hoc: pass `--auto-select-model` to
`scripts/evaluate_chapter_segmentation_llm_fallback.py` (or the
`--llm-fallback` CLI/`enable_llm_fallback` API paths) to retry across the
active preset's live, non-"very busy" models automatically, never a
hardcoded model name. The preset's stale default model
(`mistral-large-3-675b-instruct-2512`, not currently in KISSKI's live
list) is still worth fixing separately. Re-run whenever the prompt, model
choice, or heuristic changes to check whether the fallback is still
net-helpful.

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
