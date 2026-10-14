# Extractor evaluation: Docling vs Kreuzberg (2026-10-14)

Question: is the Docling extractor (`kisski-docling` preset, added in #97) equivalent to Kreuzberg
(`remote-kisski`) for the RAG pipeline?

**Answer: equivalent at the retrieval depth the pipeline uses (10 chunks), with cleaner text from Docling;
no end-to-end answer-quality difference is detectable at this sample size.**

All data, scripts, per-document and per-question results are in the Hugging Face evaluation store:

- Study folder (full write-up, question set, metrics, Kreuzberg snapshots built for it, scripts):
  <https://huggingface.co/datasets/cmboulanger/zotero-rag-gold/tree/main/evaluations/extractor-comparison-20261014>
- Judged eval runs (archive only, `reports/latest` untouched):
  [`20261014-extractor-cmp-docling`](https://huggingface.co/datasets/cmboulanger/zotero-rag-gold/tree/main/reports/20261014-extractor-cmp-docling),
  [`20261014-extractor-cmp-kfresh`](https://huggingface.co/datasets/cmboulanger/zotero-rag-gold/tree/main/reports/20261014-extractor-cmp-kfresh)

## Setup

Corpus: the 39 gold PDFs of `groups/6297749` (38 distinct documents). Same embedding model
(multilingual-e5-large-instruct) and chunking in every arm. Arms: the store's Docling snapshot
`docling-20261013-02f00c6` (4527 chunks); Kreuzberg rebuilt from the same PDFs with the production
configuration (4497 chunks); the published Kreuzberg `latest` snapshot (12019 chunks); Kreuzberg with
`force_ocr` off as an ablation (4051 chunks). Three levels:

1. Extraction quality against a `pdftotext` reference, paired Wilcoxon over documents.
2. Dense retrieval on 178 hand-written questions (en/de/fr/es, body text and tables) with verbatim evidence
   spans; McNemar and a cluster bootstrap over documents.
3. End-to-end: `run_eval.py` on gold Q01-Q10 and Q14, two KISSKI models x 3 repetitions, routing off, all 132
   answers judged with `references/judging.md`; bootstrap over questions and Wilcoxon on per-question means.

## Results

| Level | Docling | Kreuzberg (fresh) | Difference |
|---|---|---|---|
| Shingle precision vs reference | 0.910 | 0.860 | p<0.0001 |
| Page label exactly right | 97% | 58% | |
| Running headers per 100 chunks | 2.7 | 23.7 | |
| Retrieval P@10 (178 q.) | 0.674 | 0.669 | +0.6 pts, 90% CI [-4.6, +6.0] |
| Retrieval P@1 | 0.320 | 0.404 | -8.4 pts, p=0.03 (sibling-chunk near-ties) |
| Judged fact recall (66 pairs) | 0.562 | 0.580 | -0.018, 95% CI [-0.24, +0.21], p=0.98 |
| Judged overall (0-5) | 3.02 | 3.21 | -0.20, p=0.65 |

End-to-end per-question swings go both ways (Docling better on 4 questions, Kreuzberg on 5) and trace to which
chunks ranked into the context; only one (Q03) is caused by an extraction defect.

## Findings to act on

- **Kreuzberg forces OCR on every page**: `backend/services/extraction/kreuzberg.py` sends
  `"force_ocr": ocr_enabled`, default true. This drops the text layer's page mapping (page labels missing on
  about 58% of chunks), reads German as English (diacritic recall 0.01) and keeps hyphenation breaks and running
  headers. Turning it off alone costs 9.6 points of P@10 (worse reading order), so the fix is OCR only for pages
  without text, not no OCR.
- **Docling emits unmapped ligature glyph names** (`signi/uniFB01cant`, `/T_he`, `Wissenscha/f_t`) in 7 of 38
  documents; a normalisation step in the Docling extractor would remove them.
- **Docling can detach table captions from their tables** (Li et al. 2012: the "820 Science articles" caption
  lands on another table), which caused the one extraction-related wrong answer.
- **The published Kreuzberg `latest` snapshot is not a clean baseline**: 5463 exact duplicate chunks and mixed
  512/800-character chunks. Docling beats it at P@20 by 10.7 points (p=0.007). Rebuild it before using it for
  comparisons.

## Limits

End-to-end power is low (11 independent contexts per arm), so equivalence rests on the retrieval benchmark.
Answers were judged by one judge who could see the arm. The Kreuzberg arm was rebuilt with Kreuzberg 4.9.5's
Python binding in a cloud session (production chunks matched at 0.999 similarity). The corpus is small and
mostly born-digital; scanned documents are not covered. The Docling snapshot was not promoted to `latest`.
