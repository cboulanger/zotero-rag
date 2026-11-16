> Judged run of `remote-mpcdf` as fixed in #112 (embedding and re-ranking on `rocm/vllm:rocm7.0.0_vllm_0.11.2_20251210` with FlexAttention; run as a data copy named `remote-mpcdf-pr112`) on the 16-question gold standard, the user's own MPCDF endpoints, gpt-oss-120b, three repetitions, re-ranking on (`run_eval.py --presets remote-mpcdf-pr112 --models first --repeat 3`). Re-ranking: BAAI/bge-reranker-v2-m3, pool 100. Its pair: `2026-11-16-reranking-mpcdf-off.md`. Issue #111.

# RAG quality evaluation

- backend `http://localhost:8119` commit `1e15f9b`, gold v3, library 6297749
- started 2026-11-15T22:26:48.786398+00:00, finished 2026-11-15T23:27:10.591338+00:00

## Ranking (composite 0-1: pass >= 0.85, warn >= 0.65; fact recall < 0.5 always fails)

| Preset / model | Composite | Pass/warn/fail | Recall | Easy | Medium | Hard | Errors | p50 s | Stability (stdev) |
|---|---|---|---|---|---|---|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 0.64 | 12/14/22 | 62% | 0.77 | 0.55 | 0.53 | 0 | 68.9 | 0.033 |

## Composite and routing by question domain

Routing = share of the expected agents chosen by the router (1 = exact set); `mentions` questions are scored on routing alone (see SKILL.md).

| Preset / model | facts | routing | mentions | metadata | Routing |
|---|---|---|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 0.78 | 1.00 | 0.00 | 0.48 | 0.69 |

## Criteria means per preset / model

| Preset / model | routing | fact_recall | expected_doc_recall | source_count | cited_diversity | citation_compliance | grounded_numbers | length | language | hygiene |
|---|---|---|---|---|---|---|---|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 0.69 | 0.62 | 0.97 | 1.00 | 0.62 | 0.43 | 0.94 | 0.99 | 0.90 | 0.99 |

## Judged findings (reviewing agent)

| Preset / model | Judged | Recall judged | Recall regex | Citation support | Inference quality | Unsupported claims | Attribution errors | Overall (0-5) |
|---|---|---|---|---|---|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 42/48 | 62% | 63% | 83% | 33% | 32 | 15 | 3.0 |

### Recall by reasoning level

verbatim = literal figures/names, semantic = same meaning in other words or another language, inference = conclusion that must be derived by combining sources.

| Preset / model | Verbatim | Semantic | Inference |
|---|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 69% | 47% | 39% |

### Regex vs. judge disagreements

False negatives = the judge found a hit the regex missed (paraphrase, translation, inference); false positives = the regex matched but the judge says the fact is wrong, swapped or absent.

| Preset / model | Regex false negatives | Regex false positives |
|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 9 | 5 |

## Multilingual behaviour

Fact recall on English questions vs non-English questions (cross-lingual retrieval + answer in the question's language).

| Preset / model | Recall en | Recall non-en | Gap | Answer-language match (non-en) |
|---|---|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 61% | 65% | -4% | 73% |

## Pipeline diagnostics

| Preset / model | Avg sources | Avg cited | Uncited answers | Retry rate | Escalation | Facts lost in retrieval | Facts lost in generation | Ungrounded numbers | Avg words |
|---|---|---|---|---|---|---|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 5.5 | 1.3 | 22 | 0% | 0% | 22 | 12 | 6 | 97 |

## Structural-guideline violations

- **remote-mpcdf-pr112 / openai/gpt-oss-120b**: bare_sn x38, refusal x1, 22 answer(s) without any citation

## Composite per question

| Preset / model | Q01 | Q02 | Q03 | Q04 | Q05 | Q06 | Q07 | Q08 | Q09 | Q10 | Q11 | Q12 | Q13 | Q14 | Q15 | Q16 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 0.86 | 0.84 | 0.79 | 0.98 | 0.68 | 0.78 | 0.78 | 0.65 | 0.70 | 0.77 | 0.57 | 0.37 | 0.50 | 1.00 | 0.00 | 0.00 |

## Change vs baseline

| Preset / model | Baseline | Now | Delta |
|---|---|---|---|
| remote-mpcdf-pr112 / openai/gpt-oss-120b | 0.62 | 0.64 | +0.026 |
