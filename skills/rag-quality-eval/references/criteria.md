# Evaluation criteria

Every query yields these measurements. "Auto" = computed by `scoring.py` from the
API response and its debug trace; "Judge" = written by the reviewing agent
(`references/judging.md`). Once a run is judged, the judged values replace the
auto proxies in the composite (`scoring.WEIGHTS_JUDGED`).

## Criteria requested

| Criterion | Measured by | How |
|---|---|---|
| Answer length | Auto | `length`: 1.0 inside the question's `min_words..max_words` band, decaying outside; `words` reported. Bands grow with difficulty (25-200 easy, 60-300 medium, 100-500 hard). |
| Answer quality | Auto floor + Judge | `fact_recall` (weighted share of gold facts), split by reasoning level (verbatim / semantic / inference); judge verdicts per fact, attribution, inference quality, overall 0-5. |
| Enough sources found and processed | Auto | `expected_doc_recall` (gold documents among returned sources), `source_count` (>= `min_sources`), `retrieved_doc_recall` (gold documents among the retrieved chunks in the trace), `cited_diversity` (distinct `[SN]` cited >= `min_distinct_cited`), `citation_precision` (share of cited sources that are gold documents). Duplicated library items (two copies of one paper) count as one document. |
| Compliance with the prompt's structural guidelines | Auto | `citation_compliance` = claim-sentence coverage x format validity, following `_build_generation_prompt`: every factual sentence ends with `[SN]`/`[SN:P]`/`[SN,SM]`; no `[S1:p.3]`, no page ranges, no literal `[S1:P]`, no bare `[1]`, no "Source 1"/bare `S1`, no dangling `[S9]` beyond the returned sources. `hygiene`: no process narration at the start, no tool-call syntax, no leaked `<think>` block, no visible `###CONTEXT_INSUFFICIENT###` marker, no refusal on an answerable question. `language`: answer in the question's language. |

## Additional criteria (proposed and implemented)

| Criterion | Why it matters | How |
|---|---|---|
| **Multilingual behaviour** | Users ask in their own language over English papers (cross-lingual retrieval) and the prompt demands an answer in the question's language. | Gold has de/fr/es questions. Reported: answer-language match, recall en vs non-en and their gap. A low non-en recall with a good en recall points at the embedding model; a wrong answer language at the LLM/prompt. |
| **Retrieval vs generation diagnosis** | Tells you which component to fix. | A missed fact is "lost in retrieval" if it is not in the context the LLM received (`context_text` in the trace) and "lost in generation" if it was there. Also `retrieved_doc_recall`, `documents_grouped`, `escalated_retrieval`. |
| **Groundedness** | Hallucination is the main RAG failure. | Auto proxy: numbers in the answer that never occur in the context (`ungrounded_numbers`). Judge: unsupported claims against `context.txt`. |
| **Citation support** | A well-formed `[S2]` that points at the wrong source is worse than none. | Judge spot-checks each cited sentence against its source (0..1). |
| **Attribution correctness** | Right fact, wrong document/tool is a factual error. | Judge `attribution_ok` per fact; removes credit. |
| **Inference quality** | The hardest questions need a conclusion nobody wrote down. | Judge 0 / 0.5 / 1 against the question's `inference` chain; judge-only inference facts. |
| **Semantic recall vs regex recall** | Shows how much a model's quality is invisible to string matching. | Report lists regex false negatives (paraphrase/translation/inference the regex missed) and false positives. |
| **Stability** | Router and LLM run at temperature 0.7; one run proves little. | `--repeat N`; stdev of the composite per question. |
| **Latency and cost** | A better answer that takes 90 s or needs 5x the tokens is a trade-off. | Wall time p50/p95 (warm-up excluded), approximate prompt/completion tokens from trace character counts, LLM calls per query. |
| **Pipeline self-correction rate** | The engine retries when a citation guard fires. | `generation_retries` (more than one generation call), fallback and escalation flags. High rates show the model needs the guard to be acceptable. |
| **Robustness / error rate** | 503/504/429 under load are part of quality. | HTTP errors and retries per preset/model. |

## Ideas not yet implemented

- **Negative controls**: unanswerable questions (the library has no answer) to
  check the model says what is missing and emits `###CONTEXT_INSUFFICIENT###`
  instead of inventing; add 2-3 to the gold file with an empty `facts` list and
  a `must_abstain: true` flag.
- **Conciseness / redundancy**: repeated sentences, restating the question.
- **Follow-up conversation quality** (`conversation_history`, the continuation agent).
- **Metadata and mentions agents** (counting/filtering questions, citation
  evidence round trip): need their own gold questions; the current set exercises
  the RAG agent (the router may still route elsewhere; `agents` is recorded).
- **Rank-based retrieval metrics** (MRR/nDCG) over chunk-level relevance labels.

## Verdict thresholds

`composite >= 0.85` pass, `>= 0.65` warn, otherwise fail; `fact_recall < 0.5`
fails regardless; an HTTP error or an empty answer fails. Tune in
`scoring.py` (`PASS_THRESHOLD`, `WARN_THRESHOLD`, `FACT_RECALL_FLOOR`), and note
that changing a weight changes all historical comparisons: re-run `report.py`
on the old run directories to re-score them (raw responses are kept).
