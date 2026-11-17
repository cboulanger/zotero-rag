> Judged run of an experimental stand-in preset `mpcdf-gemma-runpod` (not bundled): multilingual-e5-large-instruct embeddings and bge-reranker-v2-m3 re-ranking (pool 100) on the user's own MPCDF account, answers by Google's 4-bit QAT checkpoint `google/gemma-4-31B-it-qat-w4a16-ct` on the user's own RunPod serverless endpoint (RTX A6000 48 GB, worker-v1-vllm v2.30.3, MAX_MODEL_LEN 16384, idle timeout 60 s), query translation on (`run_eval.py --presets active --models all --repeat 3`). Compare with the KISSKI run of the same code (gemma-4-31b-it on KISSKI, query translation on): judged recall 83% vs 82% here, overall 4.3 vs 4.2, composite 0.94 vs 0.87; the composite gap comes mostly from routing (Q16: the 4-bit router chose passage search instead of the mentions agent in all three repetitions). Median latency 15.5 s vs 8.3 s. RunPod cost of the 48 queries, including one cold start and the idle tail: $0.49 (balance $20.22 -> $19.73); about $0.005 per warm question, about $0.07 per cold start (about 3.5 min to load the model), about $0.02 per idle minute. Questions after more than 60 s idle wait for a cold start; the 10 s query-translation timeout then skips the translation.

# RAG quality evaluation

- backend `http://localhost:8119` commit `3b037aa`, gold v3, library 6297749
- started 2026-11-16T13:24:34.871128+00:00, finished 2026-11-16T13:45:09.266309+00:00

## Ranking (composite 0-1: pass >= 0.85, warn >= 0.65; fact recall < 0.5 always fails)

| Preset / model | Composite | Pass/warn/fail | Recall | Easy | Medium | Hard | Errors | p50 s | Stability (stdev) |
|---|---|---|---|---|---|---|---|---|---|
| mpcdf-gemma-runpod / google/gemma-4-31B-it-qat-w4a16-ct | 0.87 | 35/7/6 | 82% | 0.97 | 0.92 | 0.62 | 0 | 15.5 | 0.009 |

## Composite and routing by question domain

Routing = share of the expected agents chosen by the router (1 = exact set); `mentions` questions are scored on routing alone (see SKILL.md).

| Preset / model | facts | routing | mentions | metadata | Routing |
|---|---|---|---|---|---|
| mpcdf-gemma-runpod / google/gemma-4-31B-it-qat-w4a16-ct | 0.91 | 1.00 | 0.50 | 0.94 | 0.94 |

## Criteria means per preset / model

| Preset / model | routing | fact_recall | expected_doc_recall | source_count | cited_diversity | citation_compliance | grounded_numbers | length | language | hygiene |
|---|---|---|---|---|---|---|---|---|---|---|
| mpcdf-gemma-runpod / google/gemma-4-31B-it-qat-w4a16-ct | 0.94 | 0.82 | 0.97 | 1.00 | 1.00 | 0.98 | 1.00 | 0.97 | 0.86 | 1.00 |

## Judged findings (reviewing agent)

| Preset / model | Judged | Recall judged | Recall regex | Citation support | Inference quality | Unsupported claims | Attribution errors | Overall (0-5) |
|---|---|---|---|---|---|---|---|---|
| mpcdf-gemma-runpod / google/gemma-4-31B-it-qat-w4a16-ct | 42/48 | 82% | 87% | 97% | 62% | 1 | 2 | 4.2 |

### Recall by reasoning level

verbatim = literal figures/names, semantic = same meaning in other words or another language, inference = conclusion that must be derived by combining sources.

| Preset / model | Verbatim | Semantic | Inference |
|---|---|---|---|
| mpcdf-gemma-runpod / google/gemma-4-31B-it-qat-w4a16-ct | 92% | 50% | 56% |

### Regex vs. judge disagreements

False negatives = the judge found a hit the regex missed (paraphrase, translation, inference); false positives = the regex matched but the judge says the fact is wrong, swapped or absent.

| Preset / model | Regex false negatives | Regex false positives |
|---|---|---|
| mpcdf-gemma-runpod / google/gemma-4-31B-it-qat-w4a16-ct | 4 | 6 |

## Multilingual behaviour

Fact recall on English questions vs non-English questions (cross-lingual retrieval + answer in the question's language).

| Preset / model | Recall en | Recall non-en | Gap | Answer-language match (non-en) |
|---|---|---|---|---|
| mpcdf-gemma-runpod / google/gemma-4-31B-it-qat-w4a16-ct | 79% | 88% | -10% | 80% |

## Pipeline diagnostics

| Preset / model | Avg sources | Avg cited | Uncited answers | Retry rate | Escalation | Facts lost in retrieval | Facts lost in generation | Ungrounded numbers | Avg words |
|---|---|---|---|---|---|---|---|---|---|
| mpcdf-gemma-runpod / google/gemma-4-31B-it-qat-w4a16-ct | 4.1 | 1.9 | 6 | 0% | 7% | 18 | 5 | 0 | 75 |

## Structural-guideline violations

- **mpcdf-gemma-runpod / google/gemma-4-31B-it-qat-w4a16-ct**: , 6 answer(s) without any citation

## Composite per question

| Preset / model | Q01 | Q02 | Q03 | Q04 | Q05 | Q06 | Q07 | Q08 | Q09 | Q10 | Q11 | Q12 | Q13 | Q14 | Q15 | Q16 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| mpcdf-gemma-runpod / google/gemma-4-31B-it-qat-w4a16-ct | 1.00 | 0.97 | 0.96 | 0.97 | 0.73 | 0.99 | 0.97 | 0.82 | 0.81 | 0.85 | 1.00 | 0.91 | 0.91 | 1.00 | 1.00 | 0.00 |
