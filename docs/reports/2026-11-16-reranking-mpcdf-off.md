> Judged run of `remote-mpcdf` as fixed in #112 (embedding and re-ranking on `rocm/vllm:rocm7.0.0_vllm_0.11.2_20251210` with FlexAttention; run as a data copy named `remote-mpcdf-pr112`) on the 16-question gold standard, the user's own MPCDF endpoints, gpt-oss-120b, three repetitions, re-ranking off (`run_eval.py --presets remote-mpcdf-pr112 --models first --repeat 3 --no-rerank`). Re-ranking: BAAI/bge-reranker-v2-m3, pool 100. Its pair: `2026-11-16-reranking-mpcdf-on.md`. Issue #111.

# RAG quality evaluation

- backend `http://localhost:8119` commit `1e15f9b`, gold v3, library 6297749
- started 2026-11-15T23:27:14.364129+00:00, finished 2026-11-16T00:55:27.089543+00:00

## Ranking (composite 0-1: pass >= 0.85, warn >= 0.65; fact recall < 0.5 always fails)

| Preset / model | Composite | Pass/warn/fail | Recall | Easy | Medium | Hard | Errors | p50 s | Stability (stdev) |
|---|---|---|---|---|---|---|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 0.62 | 7/13/28 | 44% | 0.74 | 0.49 | 0.55 | 0 | 73.0 | 0.036 |

## Composite and routing by question domain

Routing = share of the expected agents chosen by the router (1 = exact set); `mentions` questions are scored on routing alone (see SKILL.md).

| Preset / model | facts | routing | mentions | metadata | Routing |
|---|---|---|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 0.74 | 0.95 | 0.00 | 0.48 | 0.69 |

## Criteria means per preset / model

| Preset / model | routing | fact_recall | expected_doc_recall | source_count | cited_diversity | citation_compliance | grounded_numbers | length | language | hygiene |
|---|---|---|---|---|---|---|---|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 0.69 | 0.44 | 1.00 | 1.00 | 0.71 | 0.42 | 0.97 | 0.99 | 1.00 | 1.00 |

## Judged findings (reviewing agent)

| Preset / model | Judged | Recall judged | Recall regex | Citation support | Inference quality | Unsupported claims | Attribution errors | Overall (0-5) |
|---|---|---|---|---|---|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 42/48 | 44% | 53% | 85% | 33% | 14 | 5 | 2.5 |

### Recall by reasoning level

verbatim = literal figures/names, semantic = same meaning in other words or another language, inference = conclusion that must be derived by combining sources.

| Preset / model | Verbatim | Semantic | Inference |
|---|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 41% | 34% | 44% |

### Regex vs. judge disagreements

False negatives = the judge found a hit the regex missed (paraphrase, translation, inference); false positives = the regex matched but the judge says the fact is wrong, swapped or absent.

| Preset / model | Regex false negatives | Regex false positives |
|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 11 | 16 |

## Multilingual behaviour

Fact recall on English questions vs non-English questions (cross-lingual retrieval + answer in the question's language).

| Preset / model | Recall en | Recall non-en | Gap | Answer-language match (non-en) |
|---|---|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 46% | 40% | 6% | 100% |

## Pipeline diagnostics

| Preset / model | Avg sources | Avg cited | Uncited answers | Retry rate | Escalation | Facts lost in retrieval | Facts lost in generation | Ungrounded numbers | Avg words |
|---|---|---|---|---|---|---|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 5.9 | 1.5 | 18 | 0% | 0% | 35 | 10 | 4 | 96 |

## Structural-guideline violations

- **remote-mpcdf-pr112 / openai/gpt-oss-120b**: bare_sn x22, 18 answer(s) without any citation

## Composite per question

| Preset / model | Q01 | Q02 | Q03 | Q04 | Q05 | Q06 | Q07 | Q08 | Q09 | Q10 | Q11 | Q12 | Q13 | Q14 | Q15 | Q16 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 0.98 | 0.73 | 0.82 | 0.61 | 0.77 | 0.82 | 0.48 | 0.66 | 0.78 | 0.78 | 0.65 | 0.42 | 0.39 | 0.95 | 0.00 | 0.00 |
