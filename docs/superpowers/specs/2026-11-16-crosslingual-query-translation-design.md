# Cross-lingual retrieval: search and re-rank with an English translation

Short design (small change), 2026-11-16.

## Intent

A question in German (or French, Spanish, ...) should find English-language works. Today it
loses them twice:

- **Vector search:** in a spot check on the user's library `u39226` (2.15M chunks), all 5
  German-question -> English-work cases had the target outside the top 500 vector hits; the
  same questions in English found every target at rank 1-4 and in the final top 10.
- **Re-ranking:** bge-reranker-v2-m3 scores an English passage near 0 against a German question
  (an on-topic abstract: 0.004 vs 0.40 against the English question), so English passages that
  do reach the pool are thrown out.

## Goals, non-goals

- Goal: non-English questions retrieve and keep relevant English passages, without making
  English questions slower or different.
- Non-goal: translating English questions into other languages (would need to know which
  languages a library contains; the one English->German spot-check case was found anyway).
- Non-goal: changing query decomposition (splits questions into parts; evaluated and left off).
- Anti-goal: losing passages in the question's own language (German works for German questions).

## Approaches

1. **Translate non-English questions into English** (chosen by the user): one extra short LLM
   call for non-English questions only; search with both; re-rank each passage against both and
   keep the higher score.
2. Translate in both directions: needs per-library language knowledge that nothing records;
   deferred.

Reusing query decomposition was ruled out: its prompt asks for part-queries, which lowered
recall on the gold set; a translation keeps the question whole.

## Design

- **Trigger:** `_looks_like_non_english(question)` (existing lenient heuristic in
  `rag_engine.py`). A translation identical to the question (case-insensitive) is ignored.
- **Translation:** new module `backend/services/query_translation.py`: prompt "translate this
  search question into English; output only the translation", `llm_service.generate`
  (temperature 0, short `max_tokens`), parse the first non-empty line, length-guarded like
  `parse_sub_queries`. Any failure falls back to the original question only (never fatal).
- **Search:** in `RAGEngine.query`, after the original search, search with the translation
  (same limit, filters, threshold) and fuse both ranked lists with the existing
  `fuse_ranked_lists` (RRF). Applied in the escalation and retry searches too, reusing the one
  translation per query. Runs before `_search_with_sub_queries` so the two can combine.
- **Re-ranking:** `rerank_pool` takes the list of question variants; each passage gets the
  maximum score over them (both scores come from the same model in [0, 1]). The per-query score
  cache keeps working (keyed by chunk; the variants are fixed per query). Cost: one extra
  scoring pass, only for translated questions.
- **Setting:** `RETRIEVAL_QUERY_TRANSLATION` (bool, settings.py), default to be decided by the
  evaluation below; on if it helps.
- **Trace:** `retrieval.translated_query` records the translation (or null).
- **Generation:** unchanged; the answer stays in the question's language.

## Evaluation

- Offline/live spot check: the 17 known-answer questions on `u39226` (5 German -> English).
- Gold set: the 5 non-English questions (Q03 fr, Q06 de, Q07 es, Q10 de, Q13 de) must not get
  worse; full judged run with gemma-4-31b-it x 3 against the current baseline
  (`data/logs/rag_eval/rerank-kmixed-on` and the pool run).

## Left to the builder

Exact prompt wording, `max_tokens`, where the fusion helper is called from, test layout.
