# Gold standard changelog

Scores from different gold versions are not comparable. Bump `version` in
`questions.json` for any change to a question, a fact, a pattern, a weight or a
word band, and record it here.

## v3 - 2026-10-13

- Added `categories` (`difficulty:1-5`, `domain:`, `language:`, `scope:`) to every
  question and `expected_agents` (scored as the new `routing` criterion). The
  `difficulty` field is now on a 1-5 scale (was 1-10, one question per step);
  Q01-Q10 keep their text and facts: Q01 1, Q02 1, Q03 2, Q04 2, Q05 3, Q06 3,
  Q07 3, Q08 4, Q09 5, Q10 4. The separate `tier` field is gone: reports derive
  easy/medium/hard from `difficulty` (1-2 easy, 3 medium, 4-5 hard), which moves Q04
  from medium to easy. All carry `expected_agents: ["rag"]`.
- Added Q11-Q13 (`domain:metadata`, catalog questions for the metadata agent: items by
  an author, books, German preprints of 2025), Q14 (`domain:routing`, an author name
  in a content question must stay on the rag agent) and Q15-Q16 (`domain:mentions`,
  scored on routing only with `expected_citation_targets`). Facts for Q11-Q13 come
  from the library catalog in `corpus.md`, restricted to items with an indexed PDF (the metadata agent
  lists the vector store's items only; Q12 therefore expects the 2 books with a PDF, not all 5); Q14 reuses Q01's verified facts.

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
