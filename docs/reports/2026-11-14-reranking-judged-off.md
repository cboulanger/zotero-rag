> Judged run of the bundled `runpod` preset (Qwen/Qwen2.5-7B-Instruct, bge-reranker-v2-m3 with a pool of 100) on the 16-question gold standard, one repetition, re-ranking off (`run_eval.py --presets runpod --models first --no-rerank`). Its pair: `2026-11-14-reranking-judged-on.md`; offline numbers: `2026-11-14-reranking-offline.md`.

# RAG quality evaluation

- backend `http://localhost:8119` commit `a25970c`, gold v3, library 6297749
- started 2026-11-14T20:01:22.849889+00:00, finished 2026-11-14T20:04:49.848090+00:00

## Ranking (composite 0-1: pass >= 0.85, warn >= 0.65; fact recall < 0.5 always fails)

| Preset / model | Composite | Pass/warn/fail | Recall | Easy | Medium | Hard | Errors | p50 s | Stability (stdev) |
|---|---|---|---|---|---|---|---|---|---|
| runpod / Qwen/Qwen2.5-7B-Instruct | 0.76 | 9/0/7 | 52% | 0.87 | 0.64 | 0.74 | 0 | 10.1 | - |

## Composite and routing by question domain

Routing = share of the expected agents chosen by the router (1 = exact set); `mentions` questions are scored on routing alone (see SKILL.md).

| Preset / model | facts | routing | mentions | metadata | Routing |
|---|---|---|---|---|---|
| runpod / Qwen/Qwen2.5-7B-Instruct | 0.72 | 1.00 | 1.00 | 0.67 | 0.88 |

## Criteria means per preset / model

| Preset / model | routing | fact_recall | expected_doc_recall | source_count | cited_diversity | citation_compliance | grounded_numbers | length | language | hygiene |
|---|---|---|---|---|---|---|---|---|---|---|
| runpod / Qwen/Qwen2.5-7B-Instruct | 0.88 | 0.52 | 1.00 | 1.00 | 0.93 | 0.73 | 1.00 | 0.89 | 0.79 | 1.00 |

## Judged findings (reviewing agent)

| Preset / model | Judged | Recall judged | Recall regex | Citation support | Inference quality | Unsupported claims | Attribution errors | Overall (0-5) |
|---|---|---|---|---|---|---|---|---|
| runpod / Qwen/Qwen2.5-7B-Instruct | 14/16 | 52% | 56% | 71% | 0% | 9 | 5 | 2.6 |

### Recall by reasoning level

verbatim = literal figures/names, semantic = same meaning in other words or another language, inference = conclusion that must be derived by combining sources.

| Preset / model | Verbatim | Semantic | Inference |
|---|---|---|---|
| runpod / Qwen/Qwen2.5-7B-Instruct | 56% | 24% | 0% |

### Regex vs. judge disagreements

False negatives = the judge found a hit the regex missed (paraphrase, translation, inference); false positives = the regex matched but the judge says the fact is wrong, swapped or absent.

| Preset / model | Regex false negatives | Regex false positives |
|---|---|---|
| runpod / Qwen/Qwen2.5-7B-Instruct | 4 | 4 |

## Multilingual behaviour

Fact recall on English questions vs non-English questions (cross-lingual retrieval + answer in the question's language).

| Preset / model | Recall en | Recall non-en | Gap | Answer-language match (non-en) |
|---|---|---|---|---|
| runpod / Qwen/Qwen2.5-7B-Instruct | 61% | 36% | 25% | 60% |

## Pipeline diagnostics

| Preset / model | Avg sources | Avg cited | Uncited answers | Retry rate | Escalation | Facts lost in retrieval | Facts lost in generation | Ungrounded numbers | Avg words |
|---|---|---|---|---|---|---|---|---|---|
| runpod / Qwen/Qwen2.5-7B-Instruct | 4.9 | 1.8 | 3 | 0% | 21% | 15 | 4 | 0 | 52 |

## Structural-guideline violations

- **runpod / Qwen/Qwen2.5-7B-Instruct**: , 3 answer(s) without any citation

## Composite per question

| Preset / model | Q01 | Q02 | Q03 | Q04 | Q05 | Q06 | Q07 | Q08 | Q09 | Q10 | Q11 | Q12 | Q13 | Q14 | Q15 | Q16 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| runpod / Qwen/Qwen2.5-7B-Instruct | 0.94 | 0.90 | 0.88 | 0.54 | 0.70 | 0.85 | 0.43 | 0.67 | 0.63 | 0.68 | 0.89 | 0.91 | 0.20 | 1.00 | 1.00 | 1.00 |
