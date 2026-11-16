> Judged run of `remote-kisski`, the same pipeline as `kisski-rerank-mpcdf` without its re-ranking step: KISSKI embeddings and gemma-4-31b-it, no re-ranking (`run_eval.py --presets remote-kisski --models gemma-4-31b-it --repeat 3`) on the 16-question gold standard, three repetitions. Its pair: `2026-11-16-reranking-kisski-mpcdf-on.md`. Compare with `2026-11-15-reranking-kisski-on.md` (same answering model, local mMiniLM re-ranker, pool 30).

# RAG quality evaluation

- backend `http://localhost:8119` commit `1e15f9b`, gold v3, library 6297749
- started 2026-11-16T06:27:44.911060+00:00, finished 2026-11-16T06:33:25.017731+00:00

## Ranking (composite 0-1: pass >= 0.85, warn >= 0.65; fact recall < 0.5 always fails)

| Preset / model | Composite | Pass/warn/fail | Recall | Easy | Medium | Hard | Errors | p50 s | Stability (stdev) |
|---|---|---|---|---|---|---|---|---|---|
| remote-kisski / gemma-4-31b-it | 0.86 | 27/8/13 | 60% | 0.90 | 0.83 | 0.84 | 0 | 5.4 | 0.004 |

## Composite and routing by question domain

Routing = share of the expected agents chosen by the router (1 = exact set); `mentions` questions are scored on routing alone (see SKILL.md).

| Preset / model | facts | routing | mentions | metadata | Routing |
|---|---|---|---|---|---|
| remote-kisski / gemma-4-31b-it | 0.80 | 1.00 | 1.00 | 0.94 | 1.00 |

## Criteria means per preset / model

| Preset / model | routing | fact_recall | expected_doc_recall | source_count | cited_diversity | citation_compliance | grounded_numbers | length | language | hygiene |
|---|---|---|---|---|---|---|---|---|---|---|
| remote-kisski / gemma-4-31b-it | 1.00 | 0.60 | 1.00 | 1.00 | 1.00 | 0.92 | 1.00 | 0.99 | 0.86 | 1.00 |

## Judged findings (reviewing agent)

| Preset / model | Judged | Recall judged | Recall regex | Citation support | Inference quality | Unsupported claims | Attribution errors | Overall (0-5) |
|---|---|---|---|---|---|---|---|---|
| remote-kisski / gemma-4-31b-it | 42/48 | 60% | 76% | 99% | 12% | 0 | 3 | 3.4 |

### Recall by reasoning level

verbatim = literal figures/names, semantic = same meaning in other words or another language, inference = conclusion that must be derived by combining sources.

| Preset / model | Verbatim | Semantic | Inference |
|---|---|---|---|
| remote-kisski / gemma-4-31b-it | 62% | 33% | 17% |

### Regex vs. judge disagreements

False negatives = the judge found a hit the regex missed (paraphrase, translation, inference); false positives = the regex matched but the judge says the fact is wrong, swapped or absent.

| Preset / model | Regex false negatives | Regex false positives |
|---|---|---|
| remote-kisski / gemma-4-31b-it | 0 | 20 |

## Multilingual behaviour

Fact recall on English questions vs non-English questions (cross-lingual retrieval + answer in the question's language).

| Preset / model | Recall en | Recall non-en | Gap | Answer-language match (non-en) |
|---|---|---|---|---|
| remote-kisski / gemma-4-31b-it | 62% | 58% | 4% | 80% |

## Pipeline diagnostics

| Preset / model | Avg sources | Avg cited | Uncited answers | Retry rate | Escalation | Facts lost in retrieval | Facts lost in generation | Ungrounded numbers | Avg words |
|---|---|---|---|---|---|---|---|---|---|
| remote-kisski / gemma-4-31b-it | 4.9 | 2.5 | 6 | 0% | 7% | 37 | 1 | 0 | 75 |

## Structural-guideline violations

- **remote-kisski / gemma-4-31b-it**: , 6 answer(s) without any citation

## Composite per question

| Preset / model | Q01 | Q02 | Q03 | Q04 | Q05 | Q06 | Q07 | Q08 | Q09 | Q10 | Q11 | Q12 | Q13 | Q14 | Q15 | Q16 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| remote-kisski / gemma-4-31b-it | 1.00 | 0.79 | 0.96 | 0.66 | 0.77 | 0.85 | 0.60 | 0.76 | 0.82 | 0.79 | 1.00 | 0.91 | 0.91 | 1.00 | 1.00 | 1.00 |
