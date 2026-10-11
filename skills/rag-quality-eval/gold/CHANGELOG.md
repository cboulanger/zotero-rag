# Gold standard changelog

Scores from different gold versions are not comparable. Bump `version` in
`questions.json` for any change to a question, a fact, a pattern, a weight or a
word band, and record it here.

## v1 - 2026-10-10

- Initial set: 10 questions over 10 documents of the test-rag-plugin group
  (6 en, 2 de, 1 fr, 1 es), 39 facts (20 verbatim, 16 semantic, 3 inference).
- 36 grounded facts verified against the PDF full text
  (`fetch_gold_corpus.py verify`); 3 judge-only inference facts.
- Not yet calibrated against a live backend (no live run was possible when it
  was written): after the first real run, review facts that every model misses or
  that every model hits and record adjustments here.
