> Judged run of a local preset `kisski-rerank-local` (`remote-kisski` plus the bundled local re-ranker: cross-encoder/mmarco-mMiniLMv2-L12-H384-v1, CPU, pool 30) on the 16-question gold standard, KISSKI's gemma-4-31b-it, three repetitions, re-ranking on (`run_eval.py --presets kisski-rerank-local --models first --repeat 3`). Its pair: `2026-11-15-reranking-kisski-off.md`. Issue #108, section 4, with the local re-ranker instead of RunPod's bge-reranker-v2-m3, so it measures what the bundled local presets ship.

# RAG quality evaluation

- backend `http://localhost:8119` commit `9209941`, gold v3, library 6297749
- started 2026-11-15T08:05:37.803442+00:00, finished 2026-11-15T08:14:16.792631+00:00

## Ranking (composite 0-1: pass >= 0.85, warn >= 0.65; fact recall < 0.5 always fails)

| Preset / model | Composite | Pass/warn/fail | Recall | Easy | Medium | Hard | Errors | p50 s | Stability (stdev) |
|---|---|---|---|---|---|---|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 0.90 | 33/8/7 | 74% | 0.94 | 0.87 | 0.87 | 0 | 6.8 | 0.004 |

## Composite and routing by question domain

Routing = share of the expected agents chosen by the router (1 = exact set); `mentions` questions are scored on routing alone (see SKILL.md).

| Preset / model | facts | routing | mentions | metadata | Routing |
|---|---|---|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 0.86 | 1.00 | 1.00 | 0.94 | 1.00 |

## Criteria means per preset / model

| Preset / model | routing | fact_recall | expected_doc_recall | source_count | cited_diversity | citation_compliance | grounded_numbers | length | language | hygiene |
|---|---|---|---|---|---|---|---|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 1.00 | 0.74 | 0.97 | 1.00 | 1.00 | 0.99 | 1.00 | 0.98 | 0.86 | 1.00 |

## Judged findings (reviewing agent)

| Preset / model | Judged | Recall judged | Recall regex | Citation support | Inference quality | Unsupported claims | Attribution errors | Overall (0-5) |
|---|---|---|---|---|---|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 42/48 | 74% | 75% | 91% | 38% | 7 | 3 | 3.6 |

### Recall by reasoning level

verbatim = literal figures/names, semantic = same meaning in other words or another language, inference = conclusion that must be derived by combining sources.

| Preset / model | Verbatim | Semantic | Inference |
|---|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 84% | 29% | 50% |

### Regex vs. judge disagreements

False negatives = the judge found a hit the regex missed (paraphrase, translation, inference); false positives = the regex matched but the judge says the fact is wrong, swapped or absent.

| Preset / model | Regex false negatives | Regex false positives |
|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 16 | 8 |

## Multilingual behaviour

Fact recall on English questions vs non-English questions (cross-lingual retrieval + answer in the question's language).

| Preset / model | Recall en | Recall non-en | Gap | Answer-language match (non-en) |
|---|---|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 73% | 76% | -3% | 80% |

## Pipeline diagnostics

| Preset / model | Avg sources | Avg cited | Uncited answers | Retry rate | Escalation | Facts lost in retrieval | Facts lost in generation | Ungrounded numbers | Avg words |
|---|---|---|---|---|---|---|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 4.4 | 2.0 | 6 | 0% | 7% | 37 | 5 | 0 | 73 |

## Structural-guideline violations

- **kisski-rerank-local / gemma-4-31b-it**: , 6 answer(s) without any citation

## Composite per question

| Preset / model | Q01 | Q02 | Q03 | Q04 | Q05 | Q06 | Q07 | Q08 | Q09 | Q10 | Q11 | Q12 | Q13 | Q14 | Q15 | Q16 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 1.00 | 0.90 | 0.96 | 0.82 | 0.74 | 0.99 | 0.70 | 0.78 | 0.82 | 0.87 | 1.00 | 0.91 | 0.91 | 1.00 | 1.00 | 1.00 |

## Change vs baseline

| Preset / model | Baseline | Now | Delta |
|---|---|---|---|
| kisski-rerank-local / gemma-4-31b-it | 0.86 | 0.90 | +0.038 |

## Reviewer notes

- **Judged effect of re-ranking** (on vs off, 42 judged answers each): judged recall 74% vs 60%, inference quality 38% vs 12%, overall 3.6 vs 3.3, composite 0.90 vs 0.86. The automatic "facts lost in retrieval" (37 vs 36) counts regex misses and does not see this: the judges found 16 regex false negatives with re-ranking and 21 regex false positives without it.
- **Where it helped:** Q02, Q04, Q05, Q06, Q07 got a key chunk only with re-ranking (Basak's tool names, Mendeley's URL/DOI fields, Cai's 58%, Chen's Mendeley 2 GB, Lorenzetti & Ghali's abstract). Without it, those answers say the context lacks the information, or (Q04 r3) contradict the source.
- **Where it cost:** citation support 91% vs 98%, 7 unsupported claims vs 1, 3 attribution errors vs 0. Most of this is Q04: the re-ranked context holds Basak's Table I in garbled form, and the model misreads which fields Zotero imported. Q08 builds a "critical view" from an Anand fragment whose subject is not in the context.
- **Unchanged:** Q01, Q11-Q16 are identical in both modes. Q08-Q10 (hard tier) score the same overall (2.67); their losses are retrieval of single passages (Jin's critical PDF-import passage, Li's "do not provide this data", PyZoBot's chunk display) that neither mode retrieves.
- **Generation was faithful:** nearly every missing fact was absent from the context; gemma-4-31b-it flagged gaps instead of inventing figures.
- **Gold file, for a separate fix:** Q02 `endnote_top`/`refmanager_refworks` patterns require the counts, so a correct ranking without numbers never matches; Q04 `neither` (ISSN + publisher) matches any answer that quotes the "dissimilarities" list; Q09 `zotero_no_usercounts` also matches Chen's "no API ... Zotero"; Q10 `hysemrag_audit` names 99.0% in the fact text but accepts "post-hoc" alone; Q06's `minimum_answer` attributes Mendeley's 2 GB to Jin and Sarkar, while the retrieved sentence is Chen et al. (2018).
