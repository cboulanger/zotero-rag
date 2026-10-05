# Routing & Retrieval Quality Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Branch/worktree:** This plan is executed directly on `devel` in the main
> checkout at `/Users/cboulanger/Code/zotero-rag` — **no git worktree**. Every
> subagent must `cd /Users/cboulanger/Code/zotero-rag`, run `git branch
> --show-current` to confirm it prints `devel`, and make all edits, test runs,
> and commits from there.

**Goal:** Fix two retrieval-pipeline bugs found via a debug trace (the query
router inventing author filters not present in the question; duplicate chunks
from multi-attachment items wasting retrieval slots), then add an optional,
off-by-default self-review step that escalates retrieval when an answer's
citation coverage looks thin.

**Architecture:** Three independent phases against the existing RAG pipeline
(`query_router.py` → `query_orchestrator.py` → `rag_agent.py` → `rag_engine.py`
→ `vector_store.py`). Phase 1 (router) and Phase 2 (dedup) are unconditional
bug fixes with no new flags. Phase 3 (self-review) adds one new boolean flag
threaded end-to-end through the same kwarg-forwarding pattern already used by
`diversity_floor`/`max_chunks_per_document` etc., default `False`.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, pytest/unittest
(`unittest.IsolatedAsyncioTestCase` + `unittest.mock`), Qdrant.

**Spec:** `docs/superpowers/specs/2026-10-05-routing-retrieval-quality-design.md`

---

## Phase 1 — Router must not invent entities absent from the question

### Task 1: Add `dropped_filters` to `QueryPlan`

**Files:**
- Modify: `backend/services/base_agent.py:37-44`

- [ ] **Step 1: Add the field**

Current (`backend/services/base_agent.py:37-44`):

```python
class QueryPlan(BaseModel):
    """Routing decision produced by the QueryRouter."""

    agents_to_use: list[str] = ["rag"]    # names of agents to invoke (must match registered names)
    filters: MetadataFilters = MetadataFilters()
    routing_description: Optional[str] = None   # LLM's brief reasoning, used in synthesis
    clarification_needed: bool = False          # router judged the question too broad, pre-execution
    clarification_question: Optional[str] = None
```

Change to:

```python
class QueryPlan(BaseModel):
    """Routing decision produced by the QueryRouter."""

    agents_to_use: list[str] = ["rag"]    # names of agents to invoke (must match registered names)
    filters: MetadataFilters = MetadataFilters()
    routing_description: Optional[str] = None   # LLM's brief reasoning, used in synthesis
    clarification_needed: bool = False          # router judged the question too broad, pre-execution
    clarification_question: Optional[str] = None
    dropped_filters: Optional[dict[str, list[str]]] = None
    # Entries the router LLM returned that were dropped because they don't
    # literally occur in the question text (see QueryRouter._filter_mentioned).
    # Keys are "authors" | "title_keywords" | "citation_targets.author".
```

- [ ] **Step 2: Commit**

```bash
cd /Users/cboulanger/Code/zotero-rag
git add backend/services/base_agent.py
git commit -m "feat(router): add dropped_filters field to QueryPlan

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

No test needed for this step alone — it's exercised by Task 2's tests.

---

### Task 2: Deterministic post-check — drop entities not mentioned in the question

**Files:**
- Modify: `backend/services/query_router.py`
- Test: `backend/tests/test_query_router.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_query_router.py`, as a new test class (after
`TestQueryRouterRoute`, before `TestConversationHistoryInPrompt`):

```python
class TestEntityMentionValidation(unittest.IsolatedAsyncioTestCase):
    """The router LLM can hallucinate authors/title_keywords/citation_targets
    that sound plausible for the topic but were never written in the
    question — observed live: a question with no named author ("Welche
    Beziehung besteht zwischen der systemtheoretischen Rechtssoziologie und
    der Rechtsdogmatik?") still got authors=["teubner", "wiethölter"]
    because those scholars are commonly associated with the topic. Any
    candidate not literally present in the question (allowing for
    diacritics/case/German inflection) must be dropped before it becomes a
    hard Qdrant filter."""

    async def test_drops_author_not_mentioned_in_question(self):
        router = _make_router('{"agents": ["rag"], "authors": ["teubner", "wiethölter"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route(
            "Welche Beziehung besteht zwischen der systemtheoretischen "
            "Rechtssoziologie und der Rechtsdogmatik?", agents,
        )
        self.assertEqual(plan.filters.authors, [])
        self.assertEqual(
            sorted(plan.dropped_filters["authors"]), ["teubner", "wiethölter"]
        )

    async def test_keeps_author_mentioned_in_question(self):
        router = _make_router('{"agents": ["rag"], "authors": ["luhmann"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("What does Luhmann say about autopoiesis?", agents)
        self.assertEqual(plan.filters.authors, ["luhmann"])
        self.assertIsNone(plan.dropped_filters)

    async def test_keeps_author_mentioned_with_diacritic_variant(self):
        """The question may spell a name without its diacritic even though
        the router returns the canonical spelling, or vice versa."""
        router = _make_router('{"agents": ["rag"], "authors": ["wiethölter"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("What did Wietholter argue?", agents)
        self.assertEqual(plan.filters.authors, ["wiethölter"])

    async def test_keeps_author_mentioned_in_inflected_german_form(self):
        """German genitive ("Wiethölters") must still count as a mention of
        "wiethölter" — substring containment after normalization handles
        this without a stemmer."""
        router = _make_router('{"agents": ["rag"], "authors": ["wiethölter"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("Was ist Wiethölters Hauptargument?", agents)
        self.assertEqual(plan.filters.authors, ["wiethölter"])

    async def test_drops_title_keyword_not_mentioned_in_question(self):
        router = _make_router('{"agents": ["rag"], "title_keywords": ["bukowina"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("What is systems theory?", agents)
        self.assertEqual(plan.filters.title_keywords, [])
        self.assertEqual(plan.dropped_filters["title_keywords"], ["bukowina"])

    async def test_keeps_title_keyword_mentioned_in_question(self):
        router = _make_router('{"agents": ["rag"], "title_keywords": ["bukowina"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("Find the paper called 'Globale Bukowina'", agents)
        self.assertEqual(plan.filters.title_keywords, ["bukowina"])

    async def test_drops_citation_target_author_not_mentioned(self):
        router = _make_router(
            '{"agents": ["mentions"], '
            '"citation_targets": [{"author": "wiethölter", "year": 1975, "title_keywords": []}]}'
        )
        agents = [_make_agent("mentions", "citation search")]
        plan = await router.route("Which publications discuss legal sociology?", agents)
        self.assertEqual(plan.filters.citation_targets, [])
        self.assertEqual(plan.dropped_filters["citation_targets.author"], ["wiethölter"])

    async def test_keeps_citation_target_author_mentioned(self):
        router = _make_router(
            '{"agents": ["mentions"], '
            '"citation_targets": [{"author": "wiethölter", "year": 1975, "title_keywords": []}]}'
        )
        agents = [_make_agent("mentions", "citation search")]
        plan = await router.route("Which publications cite Wiethölter's 1975 article?", agents)
        self.assertEqual(len(plan.filters.citation_targets), 1)
        self.assertEqual(plan.filters.citation_targets[0].author, "wiethölter")

    async def test_dropped_filters_is_none_when_nothing_dropped(self):
        router = _make_router('{"agents": ["rag"], "authors": ["luhmann"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("What does Luhmann argue?", agents)
        self.assertIsNone(plan.dropped_filters)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/cboulanger/Code/zotero-rag
uv run pytest backend/tests/test_query_router.py -k TestEntityMentionValidation -v
```

Expected: FAIL — `plan.dropped_filters` doesn't exist as a populated behavior
yet (authors/title_keywords/citation_targets pass through unfiltered).

- [ ] **Step 3: Implement the validation helpers**

In `backend/services/query_router.py`, add `import unicodedata` to the
imports at the top (after `import time`), then add these module-level
functions after `_ROUTING_GUIDANCE` (after line 58, before `_PROMPT_TEMPLATE`):

```python
_GERMAN_DIACRITIC_EXPANSIONS = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}


def _normalize_forms(text: str) -> set[str]:
    """Return both a diacritic-stripped and a German-transliterated,
    casefolded form of `text`, so an entity written either way (e.g.
    "Wiethölter", "Wietholter", or "Wiethoelter") matches consistently
    whichever form appears on the candidate side vs. the question side."""
    folded = text.casefold()
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", folded) if not unicodedata.combining(c)
    )
    translit = folded
    for umlaut, expansion in _GERMAN_DIACRITIC_EXPANSIONS.items():
        translit = translit.replace(umlaut, expansion)
    return {stripped, translit}


def _mentioned_in_question(candidate: str, question_forms: set[str]) -> bool:
    """True if `candidate` (in any normalized form) is a substring of the
    question (in any normalized form). Substring containment — not exact
    token equality — so German inflection (e.g. genitive "Wiethölters")
    matches for free without needing a stemmer."""
    if not candidate:
        return False
    return any(
        cand_form in q_form
        for cand_form in _normalize_forms(candidate)
        for q_form in question_forms
    )


def _filter_mentioned(candidates: list[str], question_forms: set[str]) -> tuple[list[str], list[str]]:
    """Split `candidates` into (kept, dropped) by whether each is mentioned
    in the question."""
    kept: list[str] = []
    dropped: list[str] = []
    for candidate in candidates:
        (kept if _mentioned_in_question(candidate, question_forms) else dropped).append(candidate)
    return kept, dropped
```

- [ ] **Step 4: Wire the helpers into `route()`**

In `backend/services/query_router.py`, replace this block inside `route()`
(current lines 162-184):

```python
            citation_targets = []
            for ct in data.get("citation_targets") or []:
                if isinstance(ct, dict) and ct.get("author"):
                    citation_targets.append(CitationTarget(
                        author=str(ct["author"]),
                        year=ct.get("year"),
                        title_keywords=ct.get("title_keywords") or [],
                    ))

            plan = QueryPlan(
                agents_to_use=selected,
                filters=MetadataFilters(
                    year_min=data.get("year_min"),
                    year_max=data.get("year_max"),
                    authors=data.get("authors") or [],
                    item_types=data.get("item_types") or [],
                    title_keywords=data.get("title_keywords") or [],
                    citation_targets=citation_targets,
                ),
                routing_description=data.get("routing_description"),
                clarification_needed=bool(data.get("clarification_needed", False)),
                clarification_question=data.get("clarification_question"),
            )
```

with:

```python
            question_forms = _normalize_forms(question)

            authors, dropped_authors = _filter_mentioned(data.get("authors") or [], question_forms)
            title_keywords, dropped_title_keywords = _filter_mentioned(
                data.get("title_keywords") or [], question_forms
            )

            citation_targets = []
            dropped_citation_authors = []
            for ct in data.get("citation_targets") or []:
                if isinstance(ct, dict) and ct.get("author"):
                    author = str(ct["author"])
                    if _mentioned_in_question(author, question_forms):
                        citation_targets.append(CitationTarget(
                            author=author,
                            year=ct.get("year"),
                            title_keywords=ct.get("title_keywords") or [],
                        ))
                    else:
                        dropped_citation_authors.append(author)

            dropped_filters: dict[str, list[str]] = {}
            if dropped_authors:
                dropped_filters["authors"] = dropped_authors
            if dropped_title_keywords:
                dropped_filters["title_keywords"] = dropped_title_keywords
            if dropped_citation_authors:
                dropped_filters["citation_targets.author"] = dropped_citation_authors
            if dropped_filters:
                logger.warning(
                    "QueryRouter: dropped filter entries not found in question text: %s",
                    dropped_filters,
                )

            plan = QueryPlan(
                agents_to_use=selected,
                filters=MetadataFilters(
                    year_min=data.get("year_min"),
                    year_max=data.get("year_max"),
                    authors=authors,
                    item_types=data.get("item_types") or [],
                    title_keywords=title_keywords,
                    citation_targets=citation_targets,
                ),
                routing_description=data.get("routing_description"),
                clarification_needed=bool(data.get("clarification_needed", False)),
                clarification_question=data.get("clarification_question"),
                dropped_filters=dropped_filters or None,
            )
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /Users/cboulanger/Code/zotero-rag
uv run pytest backend/tests/test_query_router.py -v
```

Expected: PASS, all tests including the pre-existing ones in this file (the
existing `test_extracts_authors` test uses `authors=["luhmann", "habermas"]`
with question `"Luhmann vs Habermas"` — both names are literally in the
question, so this must still pass unchanged).

- [ ] **Step 6: Commit**

```bash
cd /Users/cboulanger/Code/zotero-rag
git add backend/services/query_router.py backend/tests/test_query_router.py
git commit -m "fix(router): drop author/title/citation filters not mentioned in the question

The router LLM could populate authors/title_keywords/citation_targets with
entities that sound plausible for the topic but were never written in the
question (e.g. a conceptual question about systems-theory jurisprudence got
authors=[teubner, wiethölter] despite naming neither). Those became hard
Qdrant filters, starving retrieval. Drop any candidate not literally present
in the question (diacritic/case/German-inflection tolerant).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: Tighten the routing prompt itself

**Files:**
- Modify: `backend/services/query_router.py:28-58` (`_ROUTING_GUIDANCE`)
- Test: `backend/tests/test_query_router.py`

- [ ] **Step 1: Write the failing test**

Add to `TestQueryRouterRoute` in `backend/tests/test_query_router.py` (near
`test_prompt_warns_against_treating_tool_names_as_authors`):

```python
    async def test_prompt_warns_against_inferring_authors_from_topic(self):
        """The prompt must explicitly warn against inferring a plausible
        author from the topic rather than extracting one from the question
        text — the exact failure mode behind dropped_filters (Task 2) is a
        safety net; the prompt itself should discourage it first."""
        llm = MagicMock()
        llm.generate = AsyncMock(return_value='{"agents": ["rag"]}')
        router = QueryRouter(llm)
        agents = [_make_agent("rag", "semantic")]
        await router.route(
            "Welche Beziehung besteht zwischen der systemtheoretischen "
            "Rechtssoziologie und der Rechtsdogmatik?", agents,
        )
        prompt = llm.generate.call_args.kwargs["prompt"].lower()
        self.assertIn("never infer", prompt)
        self.assertIn("luhmann", prompt)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/cboulanger/Code/zotero-rag
uv run pytest backend/tests/test_query_router.py -k test_prompt_warns_against_inferring_authors_from_topic -v
```

Expected: FAIL — `"never infer"` not in the current prompt.

- [ ] **Step 3: Add the guidance**

In `backend/services/query_router.py`, in `_ROUTING_GUIDANCE` (lines 28-58),
insert this new bullet immediately after the existing "mentions" example
block and before `- "mentions" is expensive...` (i.e. right after the closing
`].` of the citation_targets example, which currently ends the paragraph
right before line 54's `- "mentions" is expensive`):

```python
- Only include a name in "authors", "title_keywords", or "citation_targets"
  if that name (or an inflected form of it) is actually written in the
  Question. Never infer a plausible author from the topic — a question about
  a concept or school of thought (e.g. systems-theory jurisprudence) must NOT
  populate authors with scholars commonly associated with that topic (e.g.
  Luhmann, Teubner, Wiethölter) unless one of those names literally appears
  in the Question text.
  Example: "Welche Beziehung besteht zwischen der systemtheoretischen
  Rechtssoziologie und der Rechtsdogmatik?" -> agents: ["rag"], authors: []
  (NOT ["luhmann", "teubner", "wiethölter"] — none of those names appear in
  the question; it names a concept, not a person).
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/cboulanger/Code/zotero-rag
uv run pytest backend/tests/test_query_router.py -v
```

Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
cd /Users/cboulanger/Code/zotero-rag
git add backend/services/query_router.py backend/tests/test_query_router.py
git commit -m "fix(router): instruct the routing LLM not to infer authors from topic

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Phase 2 — Chunk-text dedup before per-document truncation

### Task 4: Make the test fixture give each chunk unique text (prerequisite)

The shared `_make_chunk` test helper in `test_rag_engine.py` currently gives
every chunk of a document identical text (`f"Content from {title}."`,
regardless of `chunk_index`). This happened to not matter before — nothing
compared chunk text — but once Task 5 adds text-based dedup, chunks built by
this helper would wrongly collapse into one in the diversity/escalation
tests that create multiple chunks per document. Fix the helper first, before
touching production code, so Task 5's dedup logic can't accidentally "pass"
by deduping away a legitimate test fixture.

**Files:**
- Modify: `backend/tests/test_rag_engine.py:484-498` (`_make_chunk`)

- [ ] **Step 1: Update the helper**

Current (`backend/tests/test_rag_engine.py:484-498`):

```python
    def _make_chunk(self, item_key, attachment_key, title, score, chunk_index=0):
        chunk = DocumentChunk(
            text=f"Content from {title}.",
            metadata=ChunkMetadata(
                chunk_id=f"{item_key}-{chunk_index}",
                document_metadata=DocumentMetadata(
                    library_id="12345", item_key=item_key, attachment_key=attachment_key,
                    title=title, authors=["Author, A."], year=2024,
                    item_type="journalArticle",
                ),
                page_number=1, text_preview=title, chunk_index=chunk_index,
                content_hash=f"hash-{item_key}-{chunk_index}",
            ),
        )
        return SearchResult(chunk=chunk, score=score)
```

Change to:

```python
    def _make_chunk(self, item_key, attachment_key, title, score, chunk_index=0, text=None):
        if text is None:
            text = f"Content from {title}, chunk {chunk_index}."
        chunk = DocumentChunk(
            text=text,
            metadata=ChunkMetadata(
                chunk_id=f"{item_key}-{chunk_index}",
                document_metadata=DocumentMetadata(
                    library_id="12345", item_key=item_key, attachment_key=attachment_key,
                    title=title, authors=["Author, A."], year=2024,
                    item_type="journalArticle",
                ),
                page_number=1, text_preview=title, chunk_index=chunk_index,
                content_hash=f"hash-{item_key}-{chunk_index}",
            ),
        )
        return SearchResult(chunk=chunk, score=score)
```

The new optional `text` parameter lets Task 5's dedup tests construct two
chunks with deliberately identical text; every existing call site keeps
working unchanged since it omits `text` and gets the new per-`chunk_index`
default instead of the old all-identical default. All existing assertions
that check `prompt.count("Content from Dominant Doc")` remain valid — that
substring still appears once per surviving chunk regardless of the
`", chunk N."` suffix.

- [ ] **Step 2: Run the full existing test file to confirm no regressions**

```bash
cd /Users/cboulanger/Code/zotero-rag
uv run pytest backend/tests/test_rag_engine.py -v
```

Expected: PASS, all tests (this step changes only test fixture defaults, not
production code — nothing should break).

- [ ] **Step 3: Commit**

```bash
cd /Users/cboulanger/Code/zotero-rag
git add backend/tests/test_rag_engine.py
git commit -m "test(rag-engine): give each _make_chunk fixture chunk unique text

Prerequisite for the chunk-text dedup logic in the next commit — the
fixture previously gave every chunk of one document identical text
regardless of chunk_index, which would make text-based dedup wrongly
collapse legitimate multi-chunk test fixtures.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 5: Dedup identical-text chunks within a document before truncation

This task also extracts the existing grouping/context-assembly code into
named helper functions (`_group_chunks_by_document`, `_assemble_context`),
a pure refactor with no behavior change on its own — done here because Task
8 (Phase 3) needs to re-run this same logic on an escalated result set, and
because the dedup call has to live inside it anyway.

**Files:**
- Modify: `backend/services/rag_engine.py`
- Test: `backend/tests/test_rag_engine.py`

- [ ] **Step 1: Write the failing tests**

Add to `TestRAGEngine` in `backend/tests/test_rag_engine.py`, after
`test_query_custom_max_chunks_per_document_overrides_default`:

```python
    async def test_query_dedupes_identical_chunk_text_within_one_document(self):
        """Observed live: the same passage can be indexed twice for one
        Zotero item (e.g. extracted from two attachment_keys, or re-chunked
        during a second indexing pass), producing two SearchResults with
        identical text. These must not both consume a context slot — keep
        the higher-scoring copy so a genuinely different chunk can fill the
        slot that would otherwise be wasted on the duplicate."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        duplicate_text = "Identical passage repeated across two attachments."
        chunk_a = self._make_chunk("DOC1", "ATT1", "Doc", 0.9, chunk_index=0, text=duplicate_text)
        chunk_b = self._make_chunk("DOC1", "ATT2", "Doc", 0.85, chunk_index=1, text=duplicate_text)
        unique_chunk = self._make_chunk("DOC1", "ATT3", "Doc", 0.8, chunk_index=2)

        self.mock_vector_store.search = Mock(return_value=[chunk_a, chunk_b, unique_chunk])
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        await self.rag_engine.query(question, library_ids, top_k=3, max_chunks_per_document=2)

        prompt = self.mock_llm_service.generate.call_args.kwargs["prompt"]
        self.assertEqual(prompt.count(duplicate_text), 1)
        self.assertIn(unique_chunk.chunk.text, prompt)

    async def test_query_dedup_keeps_higher_scoring_duplicate(self):
        """When two chunks have identical text, the surviving copy's score
        must be the higher of the two, so it isn't treated as a
        lower-quality hit downstream (e.g. in SourceInfo.score)."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        duplicate_text = "Identical passage."
        low_score_first = self._make_chunk("DOC1", "ATT1", "Doc", 0.7, chunk_index=0, text=duplicate_text)
        high_score_second = self._make_chunk("DOC1", "ATT2", "Doc", 0.95, chunk_index=1, text=duplicate_text)

        self.mock_vector_store.search = Mock(return_value=[low_score_first, high_score_second])
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        result = await self.rag_engine.query(question, library_ids, top_k=2)

        self.assertEqual(result.sources[0].score, 0.95)

    async def test_query_dedup_is_case_and_whitespace_insensitive(self):
        """Observed live: the same passage extracted twice differed only by
        a capitalization/whitespace artifact from re-extraction — this must
        still be recognized as a duplicate."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        chunk_a = self._make_chunk(
            "DOC1", "ATT1", "Doc", 0.9, chunk_index=0,
            text="RechtsVergleichung  und   Rechtsdogmatik",
        )
        chunk_b = self._make_chunk(
            "DOC1", "ATT2", "Doc", 0.85, chunk_index=1,
            text="Rechtsvergleichung und Rechtsdogmatik",
        )

        self.mock_vector_store.search = Mock(return_value=[chunk_a, chunk_b])
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        await self.rag_engine.query(question, library_ids, top_k=2)

        prompt = self.mock_llm_service.generate.call_args.kwargs["prompt"]
        self.assertIn(chunk_a.chunk.text, prompt)
        self.assertNotIn(chunk_b.chunk.text, prompt)

    async def test_query_dedup_does_not_collapse_distinct_chunks(self):
        """No regression: chunks with genuinely different text must not be
        affected by dedup, even within the same document."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        results = [self._make_chunk("DOC1", "ATT1", "Dominant Doc", 0.9 - i * 0.01, i) for i in range(3)]
        self.mock_vector_store.search = Mock(return_value=results)
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        await self.rag_engine.query(question, library_ids, top_k=3, max_chunks_per_document=10)

        prompt = self.mock_llm_service.generate.call_args.kwargs["prompt"]
        self.assertEqual(prompt.count("Content from Dominant Doc"), 3)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/cboulanger/Code/zotero-rag
uv run pytest backend/tests/test_rag_engine.py -k "dedup" -v
```

Expected: FAIL on the first three (no dedup exists yet); the fourth
(`does_not_collapse_distinct_chunks`) already passes today — that's fine,
it's there to catch a regression in the next step, not to prove new
behavior.

- [ ] **Step 3: Implement dedup + extract the grouping/context-assembly helpers**

In `backend/services/rag_engine.py`, add these two functions right after
`_format_authors` (after line 151, before `class SourceInfo`):

```python
def _normalize_for_dedup(text: str) -> str:
    """Normalize chunk text for duplicate detection: collapse whitespace and
    casefold, so near-identical chunks differing only by whitespace/case
    (e.g. the same passage re-extracted from two attachment_keys of one
    item) are recognized as duplicates."""
    return " ".join(text.split()).casefold()


def _dedup_chunks_by_text(results: list) -> list:
    """Within one document's chunks, drop exact-after-normalization text
    duplicates, keeping the highest-scoring copy of each. Observed live: the
    same page re-extracted from two attachment_keys of one Zotero item (or
    chunked twice during indexing) produces two SearchResults with identical
    text, wasting a context slot that could hold a genuinely different
    passage."""
    best_by_text: dict[str, object] = {}
    for result in results:
        key = _normalize_for_dedup(result.chunk.text)
        existing = best_by_text.get(key)
        if existing is None or result.score > existing.score:
            best_by_text[key] = result
    return list(best_by_text.values())


def _group_chunks_by_document(search_results: list) -> tuple[dict[str, list], list[str]]:
    """Group search results by item_key (a Zotero item can have several
    indexed attachments, which must still count as one document), returning
    the grouping plus document keys sorted by each document's best chunk
    score (most relevant document first)."""
    doc_chunks: dict[str, list] = {}
    doc_best_score: dict[str, float] = {}
    for result in search_results:
        key = result.chunk.metadata.document_metadata.item_key
        if key not in doc_chunks:
            doc_chunks[key] = []
            doc_best_score[key] = result.score
        doc_chunks[key].append(result)
        if result.score > doc_best_score[key]:
            doc_best_score[key] = result.score
    sorted_doc_keys = sorted(doc_chunks.keys(), key=lambda k: doc_best_score[k], reverse=True)
    return doc_chunks, sorted_doc_keys


def _assemble_context(
    doc_chunks: dict[str, list], sorted_doc_keys: list[str], max_chunks_per_document: int
) -> tuple[str, list]:
    """Build the numbered [S1]/[S2]/... context string, one source per
    document. Within each document: dedup identical-text chunks first (see
    _dedup_chunks_by_text), then cap to max_chunks_per_document, keeping the
    highest-scoring survivors. Returns (context, doc_representatives) —
    doc_representatives is the best-scoring chunk per document, in [SN]
    order, used for SourceInfo citations."""
    context_parts = []
    doc_representatives: list = []
    for i, doc_key in enumerate(sorted_doc_keys, 1):
        results_for_doc = _dedup_chunks_by_text(doc_chunks[doc_key])
        if len(results_for_doc) > max_chunks_per_document:
            results_for_doc = sorted(results_for_doc, key=lambda r: r.score, reverse=True)[:max_chunks_per_document]
        results_for_doc.sort(key=lambda r: (
            r.chunk.metadata.page_number or 0,
            r.chunk.metadata.chunk_index or 0,
        ))
        best_result = max(results_for_doc, key=lambda r: r.score)
        doc_representatives.append(best_result)

        doc_meta = results_for_doc[0].chunk.metadata.document_metadata
        authors_str = _format_authors(doc_meta.authors or [])
        year_str = f" ({doc_meta.year})" if doc_meta.year else ""
        attribution = f"{authors_str}{year_str} — " if authors_str or year_str else ""
        header = f"[S{i}: {attribution}{doc_meta.title or 'Unknown'}]"
        passages = []
        for result in results_for_doc:
            metadata = result.chunk.metadata
            page_label = f"[p. {metadata.page_number}] " if metadata.page_number else ""
            passages.append(f"{page_label}{result.chunk.text}")
        context_parts.append(f"{header}\n" + "\n\n".join(passages))

    context = "\n\n".join(context_parts)
    return context, doc_representatives
```

Now replace the grouping + context-assembly block inside `query()` (current
lines 300-349):

```python
        # Group chunks by document (item_key — a Zotero item can have several indexed
        # attachments, e.g. multiple PDF versions of the same paper, which must still
        # count as one document), preserving all relevant passages. This gives the LLM
        # real content (not just the highest-scoring chunk, which is often a
        # bibliography/reference section) while still assigning one source number per
        # document so citations are not repetitively labelled [1], [2], [3] for the
        # same paper.
        doc_chunks: dict[str, list] = {}
        doc_best_score: dict[str, float] = {}
        for result in search_results:
            key = result.chunk.metadata.document_metadata.item_key
            if key not in doc_chunks:
                doc_chunks[key] = []
                doc_best_score[key] = result.score
            doc_chunks[key].append(result)
            if result.score > doc_best_score[key]:
                doc_best_score[key] = result.score

        # Sort documents by their best chunk score (most relevant document first)
        sorted_doc_keys = sorted(doc_chunks.keys(), key=lambda k: doc_best_score[k], reverse=True)
        logger.info(f"Grouped into {len(sorted_doc_keys)} unique documents for context")

        # Step 3: Assemble context — one numbered source per document, all its chunks listed
        context_parts = []
        doc_representatives: list = []  # best-scoring chunk per doc for SourceInfo
        for i, doc_key in enumerate(sorted_doc_keys, 1):
            results_for_doc = doc_chunks[doc_key]
            if len(results_for_doc) > max_chunks_per_document:
                results_for_doc = sorted(results_for_doc, key=lambda r: r.score, reverse=True)[:max_chunks_per_document]
            # Sort chunks within document by page number, then chunk index
            results_for_doc.sort(key=lambda r: (
                r.chunk.metadata.page_number or 0,
                r.chunk.metadata.chunk_index or 0,
            ))
            best_result = max(results_for_doc, key=lambda r: r.score)
            doc_representatives.append(best_result)

            doc_meta = results_for_doc[0].chunk.metadata.document_metadata
            authors_str = _format_authors(doc_meta.authors or [])
            year_str = f" ({doc_meta.year})" if doc_meta.year else ""
            attribution = f"{authors_str}{year_str} — " if authors_str or year_str else ""
            header = f"[S{i}: {attribution}{doc_meta.title or 'Unknown'}]"
            passages = []
            for result in results_for_doc:
                metadata = result.chunk.metadata
                page_label = f"[p. {metadata.page_number}] " if metadata.page_number else ""
                passages.append(f"{page_label}{result.chunk.text}")
            context_parts.append(f"{header}\n" + "\n\n".join(passages))

        context = "\n\n".join(context_parts)
```

with:

```python
        # Group chunks by document (item_key — a Zotero item can have several indexed
        # attachments, e.g. multiple PDF versions of the same paper, which must still
        # count as one document), preserving all relevant passages. This gives the LLM
        # real content (not just the highest-scoring chunk, which is often a
        # bibliography/reference section) while still assigning one source number per
        # document so citations are not repetitively labelled [1], [2], [3] for the
        # same paper.
        doc_chunks, sorted_doc_keys = _group_chunks_by_document(search_results)
        logger.info(f"Grouped into {len(sorted_doc_keys)} unique documents for context")

        # Step 3: Assemble context — one numbered source per document, all its chunks
        # listed (deduped by text, then capped per document — see _assemble_context).
        context, doc_representatives = _assemble_context(doc_chunks, sorted_doc_keys, max_chunks_per_document)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/cboulanger/Code/zotero-rag
uv run pytest backend/tests/test_rag_engine.py -v
```

Expected: PASS, all tests (both the new dedup tests and every pre-existing
test in the file — the refactor must not change observable behavior for
non-duplicate inputs).

- [ ] **Step 5: Commit**

```bash
cd /Users/cboulanger/Code/zotero-rag
git add backend/services/rag_engine.py backend/tests/test_rag_engine.py
git commit -m "fix(rag-engine): dedup identical-text chunks before per-document truncation

4 of 10 chunks in a real debug trace were exact (or case-only-different)
duplicates of text already present elsewhere in the same document —
from two attachment_keys of one item, or re-chunking — wasting top-k
slots that could have held genuinely distinct passages. Also extracts
the grouping/context-assembly logic into named helpers, needed by the
Phase 3 self-review retry to re-run it on an escalated result set.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Phase 3 (optional, off by default) — Thin-context self-review

### Task 6: Thread `enable_quality_self_review` end-to-end (plumbing only)

No new behavior yet — just gets the flag from the API request down to
`RAGEngine.query()`, matching the existing pattern used by `enable_routing`
and the diversity-tuning kwargs.

**Files:**
- Modify: `backend/api/query.py:46-68` (`QueryRequest`), `~241` (trace
  parameters dict), `~252` (orchestrator call)
- Modify: `backend/services/query_orchestrator.py:149-165` (`query()`
  signature), the `agent.execute(...)` call inside it, and the RAG-fallback
  `rag_agent.execute(...)` call
- Modify: `backend/services/rag_agent.py:52-85` (`execute()`'s
  `engine_kwargs` loop)
- Modify: `backend/services/rag_engine.py:208-221` (`query()` signature)
- Test: `backend/tests/test_rag_engine.py`, `backend/tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing test**

Add to `TestRAGEngine` in `backend/tests/test_rag_engine.py`:

```python
    async def test_query_accepts_enable_quality_self_review_flag(self):
        """Plumbing check: the flag must be accepted without error and must
        not change behavior when nothing triggers the review (this test
        doesn't assert any new behavior yet — Task 7/8 add the detector and
        retry that actually use it)."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])
        results = [self._make_chunk("DOC1", "ATT1", "Doc", 0.9)]
        self.mock_vector_store.search = Mock(return_value=results)
        self.mock_llm_service.generate = AsyncMock(return_value="Answer [S1]")

        await self.rag_engine.query(
            question, library_ids, top_k=1, enable_quality_self_review=True
        )

        self.mock_llm_service.generate.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/cboulanger/Code/zotero-rag
uv run pytest backend/tests/test_rag_engine.py -k test_query_accepts_enable_quality_self_review_flag -v
```

Expected: FAIL — `TypeError: query() got an unexpected keyword argument
'enable_quality_self_review'`.

- [ ] **Step 3: Add the parameter to `RAGEngine.query()`**

In `backend/services/rag_engine.py`, add `enable_quality_self_review: bool =
False,` to the `query()` signature (current lines 208-221), as the last
parameter before the closing `) -> QueryResult:`:

```python
    async def query(
        self,
        question: str,
        library_ids: List[str],
        top_k: int = 5,
        min_score: float = 0.3,  # Fallback default, should use preset value from API layer
        filters: Optional[MetadataFilters] = None,
        trace: Optional[TraceCollector] = None,
        diversity_floor: int = _DIVERSITY_FLOOR,
        diversity_escalation_factor: int = _DIVERSITY_ESCALATION_FACTOR,
        diversity_escalation_max_top_k: int = _DIVERSITY_ESCALATION_MAX_TOP_K,
        max_chunks_per_document: int = _MAX_CHUNKS_PER_DOCUMENT,
        low_diversity_available_floor: int = _LOW_DIVERSITY_AVAILABLE_FLOOR,
        enable_quality_self_review: bool = False,
    ) -> QueryResult:
```

Also add one line to the docstring's `Args:` section, after the
`low_diversity_available_floor` entry:

```
            enable_quality_self_review: When True, if the generated answer's
                citation coverage looks thin (see _thin_context_coverage),
                retry once with escalated retrieval before returning.
                Default False — doubles latency on queries that trigger it.
```

- [ ] **Step 4: Thread it through `RAGAgent.execute()`**

In `backend/services/rag_agent.py`, add `"enable_quality_self_review"` to the
tuple of forwarded kwargs (current lines 69-73):

```python
        engine_kwargs = {}
        for key in (
            "diversity_floor", "diversity_escalation_factor",
            "diversity_escalation_max_top_k", "max_chunks_per_document",
            "low_diversity_available_floor", "enable_quality_self_review",
        ):
            if key in kwargs:
                engine_kwargs[key] = kwargs[key]
```

- [ ] **Step 5: Thread it through `QueryOrchestrator.query()`**

In `backend/services/query_orchestrator.py`, add `enable_quality_self_review:
bool = False,` to the `query()` signature (current lines 149-165, as the
last parameter before `) -> QueryResult:`), add it to the `Args:` docstring
next to the existing `low_diversity_available_floor` entry, and add
`enable_quality_self_review=enable_quality_self_review,` to both places that
call `agent.execute(...)` with the diversity kwargs: the main `asyncio.gather`
call (current lines ~254-270, next to `low_diversity_available_floor=...`)
and the RAG-fallback call (current lines ~305-315, next to
`low_diversity_available_floor=...`).

- [ ] **Step 6: Thread it through the API layer**

In `backend/api/query.py`:

1. Add to `QueryRequest` (current lines 64-68, after
   `low_diversity_available_floor`):

```python
    enable_quality_self_review: bool = False
    # When True, a thin/low-coverage answer triggers one escalated-retrieval
    # retry before returning (see rag_engine.py's _thin_context_coverage).
    # Off by default — see docs/superpowers/specs/2026-10-05-routing-retrieval-quality-design.md §6.
```

2. Add `"enable_quality_self_review": query.enable_quality_self_review,` to
   the `parameters` dict passed to `TraceCollector` (current lines ~236-246,
   next to `"low_diversity_available_floor": low_diversity_available_floor,`).

3. Add `enable_quality_self_review=query.enable_quality_self_review,` to the
   `orchestrator.query(...)` call (current lines ~250-264, next to
   `low_diversity_available_floor=low_diversity_available_floor,`).

- [ ] **Step 7: Run tests to verify they pass**

```bash
cd /Users/cboulanger/Code/zotero-rag
uv run pytest backend/tests/test_rag_engine.py backend/tests/test_orchestrator.py -v
```

Expected: PASS, all tests (this is pure plumbing — no existing test should
change behavior since the new parameter defaults to `False` everywhere).

- [ ] **Step 8: Commit**

```bash
cd /Users/cboulanger/Code/zotero-rag
git add backend/api/query.py backend/services/query_orchestrator.py backend/services/rag_agent.py backend/services/rag_engine.py backend/tests/test_rag_engine.py
git commit -m "feat(quality-review): thread enable_quality_self_review flag end-to-end

Plumbing only, default False everywhere — no behavior change yet. Prepares
for the thin-context detector and escalated-retry logic in the next commits.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 7: Add the thin-context-coverage detector

**Files:**
- Modify: `backend/services/rag_engine.py` (near the existing quality-check
  helpers, lines 37-122)
- Test: `backend/tests/test_rag_engine.py`

- [ ] **Step 1: Write the failing tests**

Add a new test class to `backend/tests/test_rag_engine.py`, after the
`TestRAGEngine` class (before `TestSourceInfo`):

```python
class TestThinContextCoverage(unittest.TestCase):
    """_thin_context_coverage detects a different failure mode than the
    existing _low_citation_diversity: the answer DOES cite sources, and
    doesn't necessarily cite only one, but the retrieved pool itself looks
    like it didn't actually address the question (either the model says so,
    or it uses little of a small retrieved pool)."""

    def test_true_when_answer_hedges_in_german(self):
        from backend.services.rag_engine import _thin_context_coverage
        answer = "Der Kontext enthält keine ausreichenden Informationen dazu [S1]."
        self.assertTrue(_thin_context_coverage(answer, documents_grouped=6))

    def test_true_when_answer_hedges_in_english(self):
        from backend.services.rag_engine import _thin_context_coverage
        answer = "The context does not contain enough information to answer this [S1]."
        self.assertTrue(_thin_context_coverage(answer, documents_grouped=6))

    def test_true_when_citation_utilization_is_low_on_a_small_pool(self):
        from backend.services.rag_engine import _thin_context_coverage
        answer = "Only one source is relevant here [S1]."
        self.assertTrue(_thin_context_coverage(answer, documents_grouped=6))

    def test_false_when_citation_utilization_is_adequate(self):
        from backend.services.rag_engine import _thin_context_coverage
        answer = "The first [S1], second [S2], and third [S3] sources all matter."
        self.assertFalse(_thin_context_coverage(answer, documents_grouped=6))

    def test_false_when_pool_is_large_even_if_few_cited(self):
        """A large retrieved pool with few citations is a citation-behavior
        issue (see _low_citation_diversity), not evidence the pool itself
        was thin — don't double-trigger on it here."""
        from backend.services.rag_engine import _thin_context_coverage
        answer = "Only the first source matters here [S1]."
        self.assertFalse(_thin_context_coverage(answer, documents_grouped=20))

    def test_false_on_a_normal_well_cited_short_answer(self):
        from backend.services.rag_engine import _thin_context_coverage
        answer = "The only relevant source states X [S1]."
        self.assertFalse(_thin_context_coverage(answer, documents_grouped=1))
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/cboulanger/Code/zotero-rag
uv run pytest backend/tests/test_rag_engine.py -k TestThinContextCoverage -v
```

Expected: FAIL — `ImportError: cannot import name '_thin_context_coverage'`.

- [ ] **Step 3: Implement the detector**

In `backend/services/rag_engine.py`, add this after `_low_citation_diversity`
and before `_quality_issue_reinforcement` (after current line 87, before
line 90):

```python
# Detects a different failure mode than _low_citation_diversity above: the
# answer DOES cite sources (so missing/low-diversity checks don't fire), but
# either the model itself says the context was insufficient, or it used very
# little of a small retrieved pool — both suggest the retrieved content
# itself didn't address the question, not just that the model under-cited
# material that genuinely was relevant. Observed live: a question about the
# relationship between two legal-theory concepts retrieved 6 documents, only
# one of which actually discussed the connection — the model correctly used
# mostly that one source, but the answer was thin because there was little
# else to say, not because the model was lazy.
_THIN_CONTEXT_HEDGE_PATTERN = re.compile(
    r"nicht genügend informationen|der kontext enthält keine|"
    r"aus dem kontext nicht ersichtlich|context does not contain|"
    r"does not provide enough information",
    re.IGNORECASE,
)


def _thin_context_coverage(
    answer: str, documents_grouped: int, low_diversity_floor: int = _LOW_DIVERSITY_AVAILABLE_FLOOR
) -> bool:
    """True if the answer suggests the retrieved context itself was too thin
    or off-topic, rather than the model merely under-citing relevant
    material. Only applies to a small retrieved pool (<= 2x the low-diversity
    floor) — a large pool with few citations is a citation-behavior issue,
    not evidence the pool itself was thin."""
    if _THIN_CONTEXT_HEDGE_PATTERN.search(answer):
        return True
    if documents_grouped == 0 or documents_grouped > low_diversity_floor * 2:
        return False
    cited = len(_extract_cited_source_numbers(answer))
    return cited < documents_grouped / 2
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/cboulanger/Code/zotero-rag
uv run pytest backend/tests/test_rag_engine.py -v
```

Expected: PASS, all tests.

- [ ] **Step 5: Commit**

```bash
cd /Users/cboulanger/Code/zotero-rag
git add backend/services/rag_engine.py backend/tests/test_rag_engine.py
git commit -m "feat(quality-review): add _thin_context_coverage detector

Not yet wired into query() — Task 8 adds the escalated-retry path that
uses it, gated behind enable_quality_self_review.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 8: Escalated-retrieval retry when thin context is detected

**Files:**
- Modify: `backend/services/rag_engine.py` (prompt-building extraction,
  `query()`'s tail, trace recording)
- Modify: `backend/models/trace.py:41-48` (`AgentExecutionTrace`)
- Test: `backend/tests/test_rag_engine.py`

- [ ] **Step 1: Write the failing tests**

Add to `TestRAGEngine` in `backend/tests/test_rag_engine.py`, after
`test_query_accepts_enable_quality_self_review_flag` (Task 6):

```python
    async def test_quality_review_escalates_and_adopts_better_retry(self):
        """When enable_quality_self_review=True and the first answer's
        citation coverage looks thin, retry once with escalated retrieval
        and adopt the retry if it has fewer quality issues."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        narrow_results = [self._make_chunk("DOC1", "ATT1", "First Doc", 0.9)]
        escalated_results = narrow_results + [
            self._make_chunk("DOC2", "ATT2", "Second Doc", 0.85),
            self._make_chunk("DOC3", "ATT3", "Third Doc", 0.8),
        ]
        self.mock_vector_store.search = Mock(side_effect=[narrow_results, escalated_results])

        thin_answer = "Der Kontext enthält keine ausreichenden Informationen dazu [S1]."
        better_answer = "The first [S1] and second [S2] sources both address this."
        self.mock_llm_service.generate = AsyncMock(side_effect=[thin_answer, better_answer])

        result = await self.rag_engine.query(
            question, library_ids, top_k=1, enable_quality_self_review=True,
        )

        self.assertEqual(self.mock_vector_store.search.call_count, 2)
        self.assertEqual(self.mock_llm_service.generate.call_count, 2)
        self.assertEqual(result.answer, better_answer)
        self.assertEqual(len(result.sources), 3)

    async def test_quality_review_keeps_original_when_retry_is_not_better(self):
        """If the escalated retry's answer has the same or more quality
        issues, keep the original answer rather than discarding a decent
        first attempt for a worse one."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        narrow_results = [self._make_chunk("DOC1", "ATT1", "First Doc", 0.9)]
        escalated_results = narrow_results + [self._make_chunk("DOC2", "ATT2", "Second Doc", 0.85)]
        self.mock_vector_store.search = Mock(side_effect=[narrow_results, escalated_results])

        thin_answer = "Der Kontext enthält keine ausreichenden Informationen dazu [S1]."
        still_thin_answer = "Der Kontext enthält keine ausreichenden Informationen dazu [S1]."
        self.mock_llm_service.generate = AsyncMock(side_effect=[thin_answer, still_thin_answer])

        result = await self.rag_engine.query(
            question, library_ids, top_k=1, enable_quality_self_review=True,
        )

        self.assertEqual(self.mock_llm_service.generate.call_count, 2)
        self.assertEqual(result.answer, thin_answer)
        self.assertEqual(len(result.sources), 1)

    async def test_quality_review_does_not_fire_when_disabled(self):
        """Default False — a thin-looking answer must not trigger a retry
        unless the caller explicitly opts in."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        results = [self._make_chunk("DOC1", "ATT1", "First Doc", 0.9)]
        self.mock_vector_store.search = Mock(return_value=results)
        thin_answer = "Der Kontext enthält keine ausreichenden Informationen dazu [S1]."
        self.mock_llm_service.generate = AsyncMock(return_value=thin_answer)

        result = await self.rag_engine.query(question, library_ids, top_k=1)

        self.mock_llm_service.generate.assert_called_once()
        self.assertEqual(result.answer, thin_answer)

    async def test_quality_review_does_not_fire_when_answer_already_good(self):
        """No wasted retry when the first answer already looks fine."""
        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])

        results = [
            self._make_chunk("DOC1", "ATT1", "First Doc", 0.9),
            self._make_chunk("DOC2", "ATT2", "Second Doc", 0.85),
        ]
        self.mock_vector_store.search = Mock(return_value=results)
        good_answer = "The first [S1] and second [S2] sources both address this."
        self.mock_llm_service.generate = AsyncMock(return_value=good_answer)

        await self.rag_engine.query(
            question, library_ids, top_k=2, enable_quality_self_review=True,
        )

        self.mock_llm_service.generate.assert_called_once()

    async def test_quality_review_records_trace_block(self):
        from backend.services.trace_collector import TraceCollector

        question, library_ids = "Question", ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1])
        self.mock_llm_service.model_name = "test-model"

        narrow_results = [self._make_chunk("DOC1", "ATT1", "First Doc", 0.9)]
        escalated_results = narrow_results + [self._make_chunk("DOC2", "ATT2", "Second Doc", 0.85)]
        self.mock_vector_store.search = Mock(side_effect=[narrow_results, escalated_results])

        thin_answer = "Der Kontext enthält keine ausreichenden Informationen dazu [S1]."
        better_answer = "The first [S1] and second [S2] sources both address this."
        self.mock_llm_service.generate = AsyncMock(side_effect=[thin_answer, better_answer])

        collector = TraceCollector(question, library_ids, {})
        await self.rag_engine.query(
            question, library_ids, top_k=1, enable_quality_self_review=True, trace=collector,
        )
        trace = collector.finalize()

        review = trace.agent_executions[0].quality_review
        self.assertIsNotNone(review)
        self.assertTrue(review["triggered"])
        self.assertTrue(review["retry_improved"])
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/cboulanger/Code/zotero-rag
uv run pytest backend/tests/test_rag_engine.py -k quality_review -v
```

Expected: FAIL — no escalation/retry happens yet, and
`AgentExecutionTrace.quality_review` doesn't exist.

- [ ] **Step 3: Add `quality_review` to the trace model**

In `backend/models/trace.py`, modify `AgentExecutionTrace` (current lines
41-48):

```python
class AgentExecutionTrace(Trace):
    """Summary of a single agent's execution within a query."""
    agent_name: str
    retrieval: Optional[RetrievalTrace] = None   # populated for RAG agent
    catalog_results: Optional[list[dict]] = None  # populated for metadata agent
    context_text: str                             # context assembled and passed to LLM
    sources_count: int
    duration_ms: int
    quality_review: Optional[dict] = None
    # Populated only when enable_quality_self_review triggered the
    # thin-context retry: {"triggered": bool, "reason": str, "retry_improved": bool}.
```

- [ ] **Step 4: Extract the generation prompt into a helper**

In `backend/services/rag_engine.py`, add this function after
`_assemble_context` (from Task 5) and before `class SourceInfo`:

```python
def _build_generation_prompt(context: str, question: str) -> str:
    return f"""
Based on the following context from academic documents, please answer the question.

Context:
{context}

Question: {question}

Provide a comprehensive answer based on the context above. Only use information from the context. If the context doesn't contain enough information to fully answer the question, state clearly what is missing and stop there — do not supplement your answer with general knowledge, guesses, or suggestions that are not grounded in and cited from the context above.

Answer directly. Do not narrate your process or describe what you are about to do (e.g. do not write "I will look through the context" or "Here are some relevant sources:") — begin with the substantive answer itself.

You have no tools, functions, or external APIs available. Respond only with plain natural-language prose that directly answers the question — never emit tool-call or function-call syntax.

CRITICAL CITATION RULE: The sources above are labelled [S1], [S2], [S3] etc. You MUST cite them using ONLY that notation. Every sentence that states a specific fact, feature, or claim drawn from the sources MUST end with an inline citation in that notation — if you cannot attribute a claim to a specific source, do not state it. The ONLY acceptable citation formats are:
  - [SN]        — reference to source N (e.g. [S1], [S3])
  - [SN:P]      — source N, page P — P is a plain integer, e.g. [S2:7] NOT [S2:p.7].
                  Replace P with the real page number; never write the literal
                  letter "P". If you don't know the specific page, write [SN]
                  with no colon instead.
  - [SN,SM]     — multiple sources (e.g. [S1,S2,S3])
  - [SN:P,SM:Q] — multiple sources with pages (e.g. [S1:10,S2:20])

IMPORTANT: Page numbers are integers only. Write [S1:3] not [S1:p.3].
NEVER cite a page range like [S1:305-306] — pick the single page where the cited claim
actually appears.
NEVER use plain numbers like [1] or [4] — those are bibliography references inside the documents, not source labels.
NEVER write "Source 1", "S1", or any form other than the bracket notation above.

PAGE SELECTION RULE: When citing a specific page, only cite pages that contain substantive content (arguments, analysis, findings). Do NOT cite pages that consist primarily of bibliographies, reference lists, or footnote-only content — use a different page from the same source instead, or omit the page number.
"""
```

Then replace the inline prompt f-string in `query()` (current lines 388-418,
the block starting `prompt = f"""` and ending at the closing `"""`) with:

```python
        prompt = _build_generation_prompt(context, question)
```

Also add a helper to count quality issues, right after
`_quality_issue_reinforcement` (after current line 122):

```python
def _count_quality_issues(
    answer: str, documents_grouped: int, low_diversity_floor: int = _LOW_DIVERSITY_AVAILABLE_FLOOR
) -> int:
    """Count detectable quality issues in `answer`, used to compare an
    original answer against an escalated-retrieval retry."""
    issues = 0
    if _looks_like_tool_call_leak(answer):
        issues += 1
    if _missing_citations(answer):
        issues += 1
    if _low_citation_diversity(answer, documents_grouped, low_diversity_floor):
        issues += 1
    if _thin_context_coverage(answer, documents_grouped, low_diversity_floor):
        issues += 1
    return issues
```

- [ ] **Step 5: Add the escalated-retry path and move trace recording to the end**

In `backend/services/rag_engine.py`, the trace-recording block currently
builds `retrieval_trace` right after context assembly (before generation,
current lines 351-385) but only calls `trace.record(...)` with it at the
very end (current line 483-491). Move the *construction* of `retrieval_trace`
down to just before that final `trace.record(...)` call, so it can reflect
whichever result set (original or escalated) actually produced the returned
answer.

Replace this block (current lines 351-385, right after the `context, doc_representatives = _assemble_context(...)` line from Task 5 and before `# Step 4: Generate prompt with context`):

```python
        # Record retrieval trace before calling the LLM
        if trace is not None:
            scores = [r.score for r in search_results]
            chunk_traces = [
                ChunkTrace(
                    item_key=r.chunk.metadata.document_metadata.item_key or "",
                    attachment_key=r.chunk.metadata.document_metadata.attachment_key,
                    title=r.chunk.metadata.document_metadata.title or "",
                    authors=r.chunk.metadata.document_metadata.authors or [],
                    year=r.chunk.metadata.document_metadata.year,
                    page_number=r.chunk.metadata.page_number,
                    score=r.score,
                    text_preview=r.chunk.metadata.text_preview,
                )
                for r in search_results
            ]
            retrieval_trace = RetrievalTrace(
                embedding_model=embedding_model,
                embedding_dims=len(query_embedding),
                search_params={
                    "top_k": top_k,
                    "min_score": min_score,
                    "library_ids": library_ids,
                    "filters": active_filters.model_dump() if active_filters else None,
                },
                escalated=escalated,
                raw_results_count=len(search_results),
                score_stats={
                    "min": min(scores),
                    "max": max(scores),
                    "avg": sum(scores) / len(scores),
                },
                documents_grouped=len(sorted_doc_keys),
                chunks=chunk_traces,
            )

```

with simply (deferring the construction):

```python
```

(i.e. delete this block entirely from here — it moves to Step-5's new
location below the quality-review retry, shown later in this step).

Next, replace the existing quality-retry + generation tail (current lines
422-456, from `# Step 5: Get LLM completion` through
`logger.info("Answer generated successfully")`) with:

```python
        # Step 5: Get LLM completion
        # Use max_answer_tokens from preset configuration (calibrated for each model)
        preset = self.settings.get_hardware_preset()
        max_tokens = preset.llm.max_answer_tokens

        logger.debug(f"Generating answer with LLM (max_tokens={max_tokens})...")
        t_llm = time.monotonic()
        final_prompt = prompt
        answer = await self.llm_service.generate(
            prompt=final_prompt,
            max_tokens=max_tokens,
            temperature=0.7
        )

        available_sources = len(sorted_doc_keys)
        reinforcement = _quality_issue_reinforcement(answer, available_sources, low_diversity_available_floor)
        if reinforcement:
            logger.warning(
                f"LLM answer had a quality issue; retrying once. {reinforcement} "
                f"Original answer: {answer[:200]!r}"
            )
            final_prompt = prompt + f"\n\nIMPORTANT: {reinforcement}"
            answer = await self.llm_service.generate(
                prompt=final_prompt,
                max_tokens=max_tokens,
                temperature=0.7
            )
            if _quality_issue_reinforcement(answer, available_sources, low_diversity_available_floor):
                logger.warning(
                    f"Retry still had a quality issue; using it anyway: {answer[:200]!r}"
                )

        quality_review: Optional[dict] = None
        if (
            enable_quality_self_review
            and top_k < diversity_escalation_max_top_k
            and _thin_context_coverage(answer, available_sources, low_diversity_available_floor)
        ):
            escalated_top_k = min(top_k * diversity_escalation_factor, diversity_escalation_max_top_k)
            logger.info(
                f"Thin context coverage detected; escalating retrieval to "
                f"top_k={escalated_top_k} and regenerating once"
            )
            retry_search_results = await asyncio.to_thread(
                self.vector_store.search,
                query_vector=query_embedding,
                limit=escalated_top_k,
                score_threshold=min_score,
                library_ids=library_ids if library_ids else None,
                filters=active_filters,
            )
            if len(retry_search_results) > len(search_results):
                retry_doc_chunks, retry_sorted_doc_keys = _group_chunks_by_document(retry_search_results)
                retry_context, retry_doc_representatives = _assemble_context(
                    retry_doc_chunks, retry_sorted_doc_keys, max_chunks_per_document
                )
                retry_prompt = _build_generation_prompt(retry_context, question)
                retry_answer = await self.llm_service.generate(
                    prompt=retry_prompt, max_tokens=max_tokens, temperature=0.7
                )
                original_issues = _count_quality_issues(answer, available_sources, low_diversity_available_floor)
                retry_issues = _count_quality_issues(
                    retry_answer, len(retry_sorted_doc_keys), low_diversity_available_floor
                )
                retry_improved = retry_issues < original_issues
                if retry_improved:
                    answer = retry_answer
                    final_prompt = retry_prompt
                    context = retry_context
                    search_results = retry_search_results
                    sorted_doc_keys = retry_sorted_doc_keys
                    doc_representatives = retry_doc_representatives
                    available_sources = len(sorted_doc_keys)
                    escalated = True
                quality_review = {
                    "triggered": True,
                    "reason": "thin_context_coverage",
                    "retry_improved": retry_improved,
                }

        llm_duration_ms = int((time.monotonic() - t_llm) * 1000)

        logger.info("Answer generated successfully")
```

Finally, in the final trace-recording block (current lines 483-501),
reconstruct `retrieval_trace` from whichever result set won, and attach
`quality_review`:

```python
        if trace is not None:
            scores = [r.score for r in search_results]
            chunk_traces = [
                ChunkTrace(
                    item_key=r.chunk.metadata.document_metadata.item_key or "",
                    attachment_key=r.chunk.metadata.document_metadata.attachment_key,
                    title=r.chunk.metadata.document_metadata.title or "",
                    authors=r.chunk.metadata.document_metadata.authors or [],
                    year=r.chunk.metadata.document_metadata.year,
                    page_number=r.chunk.metadata.page_number,
                    score=r.score,
                    text_preview=r.chunk.metadata.text_preview,
                )
                for r in search_results
            ]
            retrieval_trace = RetrievalTrace(
                embedding_model=embedding_model,
                embedding_dims=len(query_embedding),
                search_params={
                    "top_k": top_k,
                    "min_score": min_score,
                    "library_ids": library_ids,
                    "filters": active_filters.model_dump() if active_filters else None,
                },
                escalated=escalated,
                raw_results_count=len(search_results),
                score_stats={
                    "min": min(scores),
                    "max": max(scores),
                    "avg": sum(scores) / len(scores),
                },
                documents_grouped=len(sorted_doc_keys),
                chunks=chunk_traces,
            )
            trace.record(AgentExecutionTrace(
                agent_name="rag",
                retrieval=retrieval_trace,
                catalog_results=None,
                context_text=context,
                sources_count=len(sources),
                duration_ms=int((time.monotonic() - t_start) * 1000),
                quality_review=quality_review,
            ))
            trace.record(LLMCallTrace(
                call_type="rag_generation",
                model=self.llm_service.model_name,
                prompt=final_prompt,
                response=answer,
                temperature=0.7,
                max_tokens=max_tokens,
                duration_ms=llm_duration_ms,
                timestamp=datetime.now(timezone.utc).isoformat(),
            ))
```

(The `sources` list below this, built from `doc_representatives` in the
existing `# Step 6: Extract source citations` block, already runs after this
point in the function and is unaffected — it will correctly use the final
`doc_representatives`, whichever result set won, since Python resolves `sources
= []` / the loop over `doc_representatives` later in the function body. Make
sure the existing Step 6 block and `return QueryResult(...)` remain exactly
where they are, after this trace block.)

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd /Users/cboulanger/Code/zotero-rag
uv run pytest backend/tests/test_rag_engine.py -v
```

Expected: PASS, all tests in the file, including every pre-existing test
(the retry path only activates when `enable_quality_self_review=True`, which
defaults to `False`, so no existing test's behavior changes).

- [ ] **Step 7: Run the full backend test suite**

```bash
cd /Users/cboulanger/Code/zotero-rag
uv run pytest backend/tests/ -v
```

Expected: PASS, all tests (confirms Phase 1-3 changes together, plus
`test_orchestrator.py` and `test_query.py` if present, have no regressions).

- [ ] **Step 8: Commit**

```bash
cd /Users/cboulanger/Code/zotero-rag
git add backend/services/rag_engine.py backend/models/trace.py backend/tests/test_rag_engine.py
git commit -m "feat(quality-review): escalate retrieval once on thin-context answers

Behind enable_quality_self_review (default False). When the first answer's
citation coverage looks thin (_thin_context_coverage), re-run retrieval at
an escalated top_k, regenerate once, and keep whichever answer has fewer
detectable quality issues. Traced via a new quality_review block on
AgentExecutionTrace so a future trace inspection can see directly whether
this fired and whether it helped.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Plan self-review notes

- **Spec coverage:** §4 (router prompt + validation) → Tasks 2-3. §5 (dedup +
  follow-up) → Tasks 4-5 (the §5.4 root-cause investigation is explicitly
  scoped in the spec as a separate, non-blocking follow-up task, not part of
  this plan). §6 (self-review) → Tasks 6-8. §7 (testing) → every task writes
  its own tests first. §8 (rollout) → Phase 1/2 tasks are unconditional;
  Phase 3 tasks are flag-gated, default off, matching "ship 1+2 now, 3
  separately."
- **Type consistency:** `QueryPlan.dropped_filters` (Task 1) is produced and
  consumed with the same `dict[str, list[str]]` shape throughout Tasks 1-2.
  `_dedup_chunks_by_text` / `_group_chunks_by_document` / `_assemble_context`
  (Task 5) are defined once and reused unchanged by the Task 8 retry path.
  `enable_quality_self_review` keeps the same name across
  `QueryRequest` → `QueryOrchestrator.query()` → `RAGAgent.execute()` kwargs
  → `RAGEngine.query()` (Task 6).
- **Placeholder scan:** none found — every step shows the actual code to
  write, not a description of it.
