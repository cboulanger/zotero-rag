# Evaluation data for book segmentation

Ground-truth chapter boundaries for each OA book are hand-verified and committed
alongside this README as `<filename-without-extension>.expected.json` (schema:
see `docs/superpowers/plans/2026-07-24-chapter-segmentation-linking.md` Task 30).
The PDFs themselves are gitignored (`*.pdf`, see `.gitignore` in this directory)
and are not shipped — `scripts/fetch_evaluation_pdfs.py` downloads the `OA: Yes`
ones on demand from `manifest.json`. To add a new evaluation book: add a row
below **and** a matching entry in `manifest.json`, then build its
`<name>.expected.json` (a PDF-reader spot-check of the real file, not a guess).

| File name | Language | Type | Embedded TOC | OA | Download URL
| --- | --- | --- | --- | --- | ---
| 9783031466373.pdf | English | PDF-native | Yes | Yes | https://library.oapen.org/bitstream/handle/20.500.12657/86934/978-3-031-46637-3.pdf?sequence=1
| 9781771993661.pdf | English | PDF-native  | Yes | Yes | https://www.aupress.ca/app/uploads/120313_Alam_et_al_2023-Violence_Imagination_and_Resistance.pdf
| 9783907297339.pdf| French | PDF-native | Yes | Yes | https://library.oapen.org/bitstream/handle/20.500.12657/61692/oa_pdf-033-1675100892.pdf?sequence=1
| 9782375460122.pdf | French | PDF-native | Yes | Yes | https://books.openedition.org/pressesenssib/pdf/7527
| 9783907297285.pdf | German | PDF-native| Yes | Yes | https://library.oapen.org/bitstream/handle/20.500.12657/58534/oa_pdf-028-1-1663335379.pdf?sequence=1
| 9783847432364.pdf | German | PDF-native | Yes | Yes | https://library.oapen.org/bitstream/handle/20.500.12657/101141/UTF-89783847432364.pdf?sequence=1
| 9783322969828.pdf | German | Scan + OCR | No | No | https://link.springer.com/book/10.1007/978-3-322-96982-8

**Notes:**

- `9782375460122.pdf`'s "Embedded TOC" is corrected to **Yes** above: it has a
  real, machine-readable "Sommaire" page (PDF page index 5) despite the
  original entry saying no — verified directly while building its ground truth.
- `9783322969828.pdf` (`OA: No`) is intentionally **not** in the fetch
  script's scope and has no `.expected.json` yet — it can't be legally
  auto-downloaded, and its scanned/OCR'd text (from a 1976 Springer volume)
  needs spot-checking against the real OCR pipeline (script 2) rather than
  plain `pypdf` extraction before ground truth is built for it. It remains a
  useful manually-supplied fixture for testing the low-text-quality path once
  someone places a copy here.
- A `Lautmann1970.pdf` file also exists in this directory but has no row here
  and no documented source/OA status — it was left out of the evaluation set
  built in this pass; add a row (and decide OA/download-URL) before using it.
