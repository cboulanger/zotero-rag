> Judged run of `kisski-rerank-mpcdf` (run as a data copy named `kisski-rerank-mpcdf-eval`): KISSKI embeddings and gemma-4-31b-it, re-ranking with BAAI/bge-reranker-v2-m3 (pool 100) as a job on the user's own MPCDF account (vLLM 0.11.2 + FlexAttention), re-ranking on (`run_eval.py --presets kisski-rerank-mpcdf-eval --models gemma-4-31b-it --repeat 3`) on the 16-question gold standard, three repetitions. Its pair: `2026-11-16-reranking-kisski-mpcdf-off.md`. Compare with `2026-11-15-reranking-kisski-on.md` (same answering model, local mMiniLM re-ranker, pool 30).

# RAG quality evaluation

- backend `http://localhost:8119` commit `f3058f8`, gold v3, library 6297749
- started 2026-11-16T07:26:09.183479+00:00, finished 2026-11-16T07:33:36.804875+00:00

## Ranking (composite 0-1: pass >= 0.85, warn >= 0.65; fact recall < 0.5 always fails)

| Preset / model | Composite | Pass/warn/fail | Recall | Easy | Medium | Hard | Errors | p50 s | Stability (stdev) |
|---|---|---|---|---|---|---|---|---|---|
| kisski-rerank-mpcdf-eval / gemma-4-31b-it | 0.92 | 39/6/3 | 80% | 0.97 | 0.87 | 0.90 | 0 | 6.9 | 0.012 |

## Composite and routing by question domain

Routing = share of the expected agents chosen by the router (1 = exact set); `mentions` questions are scored on routing alone (see SKILL.md).

| Preset / model | facts | routing | mentions | metadata | Routing |
|---|---|---|---|---|---|
| kisski-rerank-mpcdf-eval / gemma-4-31b-it | 0.89 | 1.00 | 1.00 | 0.94 | 1.00 |

## Criteria means per preset / model

| Preset / model | routing | fact_recall | expected_doc_recall | source_count | cited_diversity | citation_compliance | grounded_numbers | length | language | hygiene |
|---|---|---|---|---|---|---|---|---|---|---|
| kisski-rerank-mpcdf-eval / gemma-4-31b-it | 1.00 | 0.80 | 0.97 | 1.00 | 1.00 | 0.96 | 1.00 | 1.00 | 0.86 | 1.00 |

## Judged findings (reviewing agent)

| Preset / model | Judged | Recall judged | Recall regex | Citation support | Inference quality | Unsupported claims | Attribution errors | Overall (0-5) |
|---|---|---|---|---|---|---|---|---|
| kisski-rerank-mpcdf-eval / gemma-4-31b-it | 42/48 | 80% | 84% | 97% | 54% | 2 | 4 | 4.1 |

### Recall by reasoning level

verbatim = literal figures/names, semantic = same meaning in other words or another language, inference = conclusion that must be derived by combining sources.

| Preset / model | Verbatim | Semantic | Inference |
|---|---|---|---|
| kisski-rerank-mpcdf-eval / gemma-4-31b-it | 89% | 39% | 67% |

### Regex vs. judge disagreements

False negatives = the judge found a hit the regex missed (paraphrase, translation, inference); false positives = the regex matched but the judge says the fact is wrong, swapped or absent.

| Preset / model | Regex false negatives | Regex false positives |
|---|---|---|
| kisski-rerank-mpcdf-eval / gemma-4-31b-it | 10 | 8 |

## Multilingual behaviour

Fact recall on English questions vs non-English questions (cross-lingual retrieval + answer in the question's language).

| Preset / model | Recall en | Recall non-en | Gap | Answer-language match (non-en) |
|---|---|---|---|---|
| kisski-rerank-mpcdf-eval / gemma-4-31b-it | 81% | 77% | 4% | 80% |

## Pipeline diagnostics

| Preset / model | Avg sources | Avg cited | Uncited answers | Retry rate | Escalation | Facts lost in retrieval | Facts lost in generation | Ungrounded numbers | Avg words |
|---|---|---|---|---|---|---|---|---|---|
| kisski-rerank-mpcdf-eval / gemma-4-31b-it | 4.1 | 2.1 | 6 | 0% | 7% | 21 | 6 | 0 | 76 |

## Structural-guideline violations

- **kisski-rerank-mpcdf-eval / gemma-4-31b-it**: , 6 answer(s) without any citation

## Composite per question

| Preset / model | Q01 | Q02 | Q03 | Q04 | Q05 | Q06 | Q07 | Q08 | Q09 | Q10 | Q11 | Q12 | Q13 | Q14 | Q15 | Q16 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| kisski-rerank-mpcdf-eval / gemma-4-31b-it | 1.00 | 0.97 | 0.96 | 0.95 | 0.73 | 0.82 | 0.88 | 0.91 | 0.82 | 0.87 | 1.00 | 0.91 | 0.91 | 1.00 | 1.00 | 1.00 |

## Change vs baseline

| Preset / model | Baseline | Now | Delta |
|---|---|---|---|
| kisski-rerank-mpcdf-eval / gemma-4-31b-it | - | 0.92 | - |
