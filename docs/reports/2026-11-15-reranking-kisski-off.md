> The same run as `2026-11-15-reranking-kisski-on.md` without re-ranking (`--no-rerank`): same preset, model, index and repetitions; only the re-ranking step is skipped.

# RAG quality evaluation

- backend `http://localhost:8119` commit `e73daea`, gold v3, library 6297749
- started 2026-11-15T08:14:21.655006+00:00, finished 2026-11-15T08:21:16.074011+00:00

## Ranking (composite 0-1: pass >= 0.85, warn >= 0.65; fact recall < 0.5 always fails)

| Preset / model | Composite | Pass/warn/fail | Recall | Easy | Medium | Hard | Errors | p50 s | Stability (stdev) |
|---|---|---|---|---|---|---|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 0.86 | 25/9/14 | 60% | 0.90 | 0.82 | 0.84 | 0 | 6.1 | 0.006 |

## Composite and routing by question domain

Routing = share of the expected agents chosen by the router (1 = exact set); `mentions` questions are scored on routing alone (see SKILL.md).

| Preset / model | facts | routing | mentions | metadata | Routing |
|---|---|---|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 0.80 | 1.00 | 1.00 | 0.94 | 1.00 |

## Criteria means per preset / model

| Preset / model | routing | fact_recall | expected_doc_recall | source_count | cited_diversity | citation_compliance | grounded_numbers | length | language | hygiene |
|---|---|---|---|---|---|---|---|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 1.00 | 0.60 | 1.00 | 1.00 | 1.00 | 0.94 | 1.00 | 0.98 | 0.81 | 1.00 |

## Judged findings (reviewing agent)

| Preset / model | Judged | Recall judged | Recall regex | Citation support | Inference quality | Unsupported claims | Attribution errors | Overall (0-5) |
|---|---|---|---|---|---|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 42/48 | 60% | 76% | 98% | 12% | 1 | 0 | 3.3 |

### Recall by reasoning level

verbatim = literal figures/names, semantic = same meaning in other words or another language, inference = conclusion that must be derived by combining sources.

| Preset / model | Verbatim | Semantic | Inference |
|---|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 62% | 34% | 17% |

### Regex vs. judge disagreements

False negatives = the judge found a hit the regex missed (paraphrase, translation, inference); false positives = the regex matched but the judge says the fact is wrong, swapped or absent.

| Preset / model | Regex false negatives | Regex false positives |
|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 0 | 21 |

## Multilingual behaviour

Fact recall on English questions vs non-English questions (cross-lingual retrieval + answer in the question's language).

| Preset / model | Recall en | Recall non-en | Gap | Answer-language match (non-en) |
|---|---|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 61% | 60% | 1% | 67% |

## Pipeline diagnostics

| Preset / model | Avg sources | Avg cited | Uncited answers | Retry rate | Escalation | Facts lost in retrieval | Facts lost in generation | Ungrounded numbers | Avg words |
|---|---|---|---|---|---|---|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 4.9 | 2.6 | 6 | 0% | 7% | 36 | 2 | 0 | 74 |

## Structural-guideline violations

- **kisski-rerank-local / gemma-4-31b-it**: , 6 answer(s) without any citation

## Composite per question

| Preset / model | Q01 | Q02 | Q03 | Q04 | Q05 | Q06 | Q07 | Q08 | Q09 | Q10 | Q11 | Q12 | Q13 | Q14 | Q15 | Q16 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 1.00 | 0.80 | 0.96 | 0.66 | 0.77 | 0.80 | 0.60 | 0.74 | 0.82 | 0.81 | 1.00 | 0.91 | 0.91 | 1.00 | 1.00 | 1.00 |
