# Gold standard changelog

Scores from different gold versions are not comparable. Bump `version` in
`questions.json` for any change to a question, a fact, a pattern, a weight or a
word band, and record it here.

## v2 - 2026-10-11

- Investigated #83 (Q04's `neither` fact looked like it might contradict its
  own source table, per a judging subagent's read of a garbled retrieved
  chunk). Fetched the clean Zotero full-text API text directly: the table
  reads `Publisher 0 0 ISSN no. 0 0 URL 0 1 DOI 0 1` (1=imported) — the gold
  fact is correct, neither Zotero nor Mendeley imported Publisher/ISSN. The
  apparent contradiction was caused by a retrieval defect (the RAG-retrieved
  chunk for this question drops the table's numeric values and the Fig. 2
  "Zotero did not import Publisher, ISSN, URL, DOI" sentence), tracked under
  #80, not a gold defect.
- Added explicit `source_patterns` to the `neither` fact (`"Publisher 0 0"`,
  `"ISSN no\\. 0 0"`) so `fetch_gold_corpus.py verify` would catch a future
  swapped attribution automatically — previously it fell back to
  `answer_patterns`, which only checks the words "ISSN"/"publisher" appear,
  not which program they're attributed to.

## v1 - 2026-10-10

- Initial set: 10 questions over 10 documents of the test-rag-plugin group
  (6 en, 2 de, 1 fr, 1 es), 39 facts (20 verbatim, 16 semantic, 3 inference).
- 36 grounded facts verified against the PDF full text
  (`fetch_gold_corpus.py verify`); 3 judge-only inference facts.
- Not yet calibrated against a live backend (no live run was possible when it
  was written): after the first real run, review facts that every model misses or
  that every model hits and record adjustments here.
