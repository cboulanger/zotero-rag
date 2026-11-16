# Cross-lingual Query Translation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Non-English questions also search and re-rank with an English translation, so English-language works are found.

**Architecture:** A new `query_translation` module asks the answering LLM for an English translation when `_looks_like_non_english(question)` holds. `RAGEngine.query` searches with both question embeddings and fuses the lists by RRF (`fuse_ranked_lists`), in the first search and in the escalation and retry searches. `rerank_pool` scores each passage against every question variant and keeps the maximum.

**Tech Stack:** Python 3.12, FastAPI backend, Qdrant, unittest/pytest (`uv run pytest`).

**Spec:** `docs/superpowers/specs/2026-11-16-crosslingual-query-translation-design.md`

## Global Constraints

- English questions (where `_looks_like_non_english` is false) must behave exactly as today: no LLM call, no extra search, no extra re-ranking pass.
- Translation failure of any kind falls back to the original question only; never fails a query.
- A translation equal to the question (case-insensitive, stripped) is ignored.
- Setting `retrieval_query_translation: bool` in `backend/config/settings.py` (env `RETRIEVAL_QUERY_TRANSLATION`); default `False` until Task 4's evaluation decides.
- Trace: `RetrievalTrace.translated_query: Optional[str] = None`.
- User-facing docs describe current behaviour only (CLAUDE.md rule).

## Review Focus

- Degenerate LLM output (empty, a repeated token for hundreds of characters, a preamble like "Translation:"): ignored or cleaned, never searched. → Task 1 tests.
- LLM raises or times out: original question only, query still answers. → Task 3 test.
- Re-ranking disabled or not ready while a translation exists: fused vector order is used, no error. → Task 2 test (`disabled` path unchanged with extra questions).
- A short German question that the heuristic misses ("Rechtsdogmatik?"): no translation, same as today; acceptable, documented. → no test (heuristic unchanged).
- Escalation retry for a translated question re-uses the one translation (no second LLM call). → Task 3 test counts `llm.generate` calls.

---

### Task 1: Translation module and setting

**Files:**
- Create: `backend/services/query_translation.py`
- Modify: `backend/config/settings.py` (next to `retrieval_query_decomposition`)
- Test: `backend/tests/test_query_translation.py`

**Interfaces:**
- Produces: `build_translation_prompt(question: str) -> str`; `parse_translation(text: str, question: str) -> Optional[str]`; `async translate_question(llm_service, question: str) -> Optional[str]` (calls `llm_service.generate(prompt=..., max_tokens=200, temperature=0.0)`; returns None on any exception or unusable output).

- [ ] **Step 1: Write the failing tests**

```python
class ParseTranslationTest(unittest.TestCase):
    def test_first_nonempty_line_stripped_of_quotes_and_label(self):
        q = "Welche Werke analysieren die Rechtsdogmatik?"
        self.assertEqual(parse_translation('\n"Which works analyse legal doctrine?"\n', q),
                         "Which works analyse legal doctrine?")
        self.assertEqual(parse_translation("Translation: Which works analyse legal doctrine?", q),
                         "Which works analyse legal doctrine?")
    def test_identical_or_degenerate_output_is_ignored(self):
        q = "What is Zotero?"
        self.assertIsNone(parse_translation("what is zotero?", q))
        self.assertIsNone(parse_translation("", q))
        self.assertIsNone(parse_translation("a" * 400, q))   # over _MAX_TRANSLATION_CHARS = 300

class TranslateQuestionTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_translation(self):  # llm.generate AsyncMock -> "Which works ...?"
    async def test_llm_error_returns_none(self):  # generate raises RuntimeError -> None
```

- [ ] **Step 2: Run** `uv run pytest backend/tests/test_query_translation.py -q` — expected: collection error (module missing).
- [ ] **Step 3: Implement** the module (prompt: translate the search question into English, keep names/terms, output only the translation; strip a leading `Translation:` label, quotes, list markers) and add the setting `retrieval_query_translation: bool = Field(default=False, description=...)`.
- [ ] **Step 4: Run** the tests — expected: PASS.
- [ ] **Step 5: Commit** `feat(retrieval): translate non-English questions into English (module, off by default)`.

### Task 2: Re-rank against several question variants

**Files:**
- Modify: `backend/services/rerank.py` (`rerank_pool`)
- Test: `backend/tests/test_rerank.py` (`RerankPoolTest`)

**Interfaces:**
- Produces: `rerank_pool(question, results, preset, reranker=None, cache=None, pool_size=None, extra_questions: Sequence[str] = ())` — each passage's score is `max` over `[question, *extra_questions]`; cache stores that max per chunk id.

- [ ] **Step 1: Write the failing tests**

```python
async def test_extra_question_keeps_the_higher_score(self):
    # FakeScorer-like scorer returning 0.9 for ("en q", "b") and 0.1 otherwise
    out, outcome = await rerank_pool("de q", self.pool, _preset({"model_name": "x"}), scorer, extra_questions=["en q"])
    self.assertEqual(_keys(out)[0], "B")
    self.assertEqual(outcome.scored, 3)          # passages, not passage x question pairs
async def test_disabled_with_extra_questions_keeps_order(self):  # RerankRequest(enabled=False) -> status "disabled", input unchanged
```

- [ ] **Step 2: Run** `uv run pytest backend/tests/test_rerank.py -q` — expected: the two new tests fail (unexpected keyword).
- [ ] **Step 3: Implement**: score `todo` texts once per question (sequential `scorer.score` calls under the same `asyncio.wait_for` budget), take element-wise max.
- [ ] **Step 4: Run** the test file — expected: PASS.
- [ ] **Step 5: Commit** `feat(rerank): score passages against every question variant`.

### Task 3: Engine integration and trace

**Files:**
- Modify: `backend/services/rag_engine.py` (`query`, `_prepare_pool`), `backend/models/trace.py` (`RetrievalTrace.translated_query`)
- Test: `backend/tests/test_query_translation.py` (`EngineTranslationTest`, reusing the mock-engine pattern of `test_rerank.EngineRerankTest._engine`)

**Interfaces:**
- Consumes: `translate_question` (Task 1), `rerank_pool(..., extra_questions=...)` (Task 2), `fuse_ranked_lists` (existing, `query_decomposition.py`).
- Produces: `RAGEngine._search_variants(vectors: list[list[float]], **search_kwargs) -> list[SearchResult]` (one search per vector, fused when more than one); `_prepare_pool(..., extra_questions: Sequence[str] = ())`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_non_english_question_searches_twice_and_fuses(self):
    # settings.retrieval_query_translation = True; llm.generate returns "Which works ...?" for the translation prompt
    result = await engine.query("Welche Werke analysieren die Rechtsdogmatik?", ["1"], top_k=1, search_candidate_pool_size=2)
    self.assertEqual(engine.vector_store.search.call_count, 2)
    self.assertEqual(engine.embedding_service.embed_query.await_args_list[1].args[0], "Which works ...?")
async def test_english_question_is_not_translated(self):        # one search, llm.generate only for the answer
async def test_setting_off_means_no_translation(self):          # retrieval_query_translation = False -> one search
async def test_translation_failure_falls_back(self):            # translation call raises -> one search, answer returned
async def test_translated_query_in_trace(self):                 # TraceCollector -> retrieval.translated_query == "Which works ...?"
async def test_escalation_reuses_the_translation(self):         # diversity_floor high -> escalation; exactly one translation LLM call
```

- [ ] **Step 2: Run** `uv run pytest backend/tests/test_query_translation.py -q` — expected: the engine tests fail.
- [ ] **Step 3: Implement**: in `query`, after `query_embedding`, `translation = await translate_question(...)` only when the setting is on and `_looks_like_non_english(question)`; embed it; replace the three `vector_store.search` calls (first, escalation, retry) with `_search_variants`; pass `extra_questions=[translation]` (or `()`) to every `_prepare_pool`; set `translated_query` on `RetrievalTrace`.
- [ ] **Step 4: Run** `uv run pytest backend/tests -q` — expected: all pass.
- [ ] **Step 5: Commit** `feat(retrieval): search and re-rank with the English translation of non-English questions`.

### Task 4: Evaluate, choose the default, document

**Files:**
- Modify: `backend/config/settings.py` (default), `.env.dist`, `docs/query-routing.md` (new "Query translation" section next to "Query decomposition")

- [ ] **Step 1: Spot check live** with `RETRIEVAL_QUERY_TRANSLATION=true` on the dev backend: the 17 questions of the 2026-11-16 spot check (`u39226`); record target-in-top-10 per question vs the earlier 9/17. Pass: the 5 German→English targets improve, no German→German target is lost.
- [ ] **Step 2: Gold run**: `run_eval.py --presets <kisski-rerank-mpcdf copy> --models gemma-4-31b-it --repeat 3`, judge all packets, compare with `data/logs/rag_eval/rerank-mpcdf-pool300` and `rerank-kmixed-on`. Pass: non-English questions (Q03, Q06, Q07, Q10, Q13) not worse, English questions unchanged.
- [ ] **Step 3: Set the default** to `True` if both pass (else leave `False` and document why), add the env line to `.env.dist`, write the docs section.
- [ ] **Step 4: Run** `uv run pytest backend/tests -q` — expected: all pass.
- [ ] **Step 5: Commit** `feat(retrieval): enable query translation by default` (or `docs:` if left off), with the evaluation numbers in the message.
