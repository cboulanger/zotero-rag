# Evaluation data for book segmentation

Ground-truth chapter boundaries for each book are hand-verified and committed
alongside this README as `<filename-without-extension>.expected.json` (schema:
see `docs/superpowers/plans/2026-07-24-chapter-segmentation-linking.md` Task 30).
The PDFs themselves are gitignored (`*.pdf`, see `.gitignore` in this directory)
and are not shipped.

`manifest.json` is the **single source of truth** for this evaluation set —
there is no README table to keep in sync with it. Each entry has:

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

**Adding a new evaluation book:**

1. Add an entry to `manifest.json` (set `"oa": false, "download_url": null`
   if it can't be freely redistributed — that's fully supported, it just
   means `fetch_evaluation_pdfs.py` won't auto-download it).
2. Place (or fetch) the PDF at `<filename>` here.
3. Build `<name>.expected.json` by actually inspecting the real PDF (open it,
   cross-reference its table of contents against the true chapter-start
   pages, verify the PDF-index↔printed-page relationship directly — several
   books in this set have non-constant offsets from blank filler pages or
   part-divider pages that consume a printed page number without their own
   PDF page) — never guessed or extrapolated from the TOC alone.
