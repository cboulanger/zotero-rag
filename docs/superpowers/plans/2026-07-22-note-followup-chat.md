# Note Follow-Up Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **This plan is implemented directly on the checked-out branch `feature/note-followup-chat` — do NOT create a worktree.** Before the first task, confirm with `git branch --show-current` that it prints `feature/note-followup-chat`; if it doesn't, `git checkout feature/note-followup-chat` first. Never commit to `main`.

**Goal:** Add a follow-up chat panel to RAG result notes (via a new Zotero item-pane section) that reuses previously-retrieved evidence instead of re-running routing/retrieval, plus a trash-from-note button on the same panel.

**Architecture:** A new `ContinuationAgent` rides the existing router/agent-registry framework to answer follow-ups from cached evidence refs (Qdrant `chunk_id` payload values, re-fetched by a new synchronous `VectorStore.get_chunks_by_ids()`); a generalized `NeedsUserInputError` family covers both the existing mentions two-phase protocol and a new too-broad-query clarification path; the plugin holds all conversation state client-side in a new `ChatPane` module and appends every turn to the note's saved HTML.

**Tech Stack:** Python/FastAPI/Pydantic/Qdrant (backend), vanilla JS + `Zotero.ItemPaneManager` (plugin), `unittest`/`node --test` for tests.

**Spec:** `docs/superpowers/specs/2026-07-22-note-followup-chat-design.md`

---

## Task 1: `AgentResult`/`QueryPlan` new fields + `NeedsUserInputError` hierarchy

**Files:**
- Modify: `backend/services/base_agent.py`
- Test: `backend/tests/test_base_agent.py` (new file)

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for AgentResult/QueryPlan new fields and the NeedsUserInputError hierarchy."""

import unittest

from backend.services.base_agent import (
    AgentResult, NeedsClarificationError, NeedsClientEvidenceError,
    NeedsUserInputError, QueryPlan,
)


class TestAgentResultNewFields(unittest.TestCase):
    def test_defaults(self):
        r = AgentResult(agent_name="rag", context_text="x")
        self.assertEqual(r.source_refs, [])
        self.assertFalse(r.needs_clarification)
        self.assertIsNone(r.clarification_message)
        self.assertEqual(r.clarification_suggestions, [])

    def test_can_set_clarification_fields(self):
        r = AgentResult(
            agent_name="metadata", context_text="too many",
            needs_clarification=True, clarification_message="narrow it down",
        )
        self.assertTrue(r.needs_clarification)
        self.assertEqual(r.clarification_message, "narrow it down")


class TestQueryPlanNewFields(unittest.TestCase):
    def test_defaults(self):
        p = QueryPlan()
        self.assertFalse(p.clarification_needed)
        self.assertIsNone(p.clarification_question)


class TestNeedsUserInputHierarchy(unittest.TestCase):
    def test_client_evidence_error_is_a_needs_user_input_error(self):
        exc = NeedsClientEvidenceError(citation_targets=[], plan=QueryPlan())
        self.assertIsInstance(exc, NeedsUserInputError)

    def test_clarification_error_is_a_needs_user_input_error(self):
        exc = NeedsClarificationError(message="narrow it down", plan=QueryPlan())
        self.assertIsInstance(exc, NeedsUserInputError)
        self.assertEqual(exc.message, "narrow it down")
        self.assertIsInstance(exc.plan, QueryPlan)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_base_agent -v`
Expected: FAIL — `ImportError: cannot import name 'NeedsClarificationError'` (and `AttributeError`/`TypeError` on the new fields once that import is fixed).

- [ ] **Step 3: Implement**

Modify `backend/services/base_agent.py`:

```python
class AgentResult(BaseModel):
    """Uniform result returned by every agent."""

    agent_name: str
    context_text: str        # formatted text block for the synthesis LLM prompt
    # SourceInfo is defined in rag_engine to avoid a circular import; typed as Any here.
    sources: list = []
    source_refs: list[str] = []              # opaque evidence refs (payload chunk_id values)
                                              # a later ContinuationAgent call can re-fetch by ID
    needs_clarification: bool = False        # this agent judges the query too broad to answer well
    clarification_message: Optional[str] = None    # human-readable narrowing prompt
    clarification_suggestions: list[str] = []       # optional hints; unpopulated in v1 — a hook
                                                     # for later facet-based suggestions


class QueryPlan(BaseModel):
    """Routing decision produced by the QueryRouter."""

    agents_to_use: list[str] = ["rag"]    # names of agents to invoke (must match registered names)
    filters: MetadataFilters = MetadataFilters()
    routing_description: Optional[str] = None   # LLM's brief reasoning, used in synthesis
    clarification_needed: bool = False          # router judged the question too broad, pre-execution
    clarification_question: Optional[str] = None


class NeedsUserInputError(Exception):
    """Base: the orchestrator cannot proceed to synthesis without more from the user."""


class NeedsClientEvidenceError(NeedsUserInputError):
    """
    Raised by QueryOrchestrator when the routing plan selects the "mentions"
    agent but no client-gathered full-text evidence was supplied. Citation
    ("who cites X") evidence lives only in the user's local Zotero full-text
    index — the backend cannot retrieve it itself. The API layer catches this
    and returns a "needs_client_evidence" response asking the plugin to
    search the client-side index and resubmit, echoing `plan` back so a
    second routing LLM call isn't needed.
    """

    def __init__(self, citation_targets: list[CitationTarget], plan: QueryPlan):
        self.citation_targets = citation_targets
        self.plan = plan
        super().__init__(f"Need client evidence for {len(citation_targets)} citation target(s)")


class NeedsClarificationError(NeedsUserInputError):
    """
    Raised when the router or an executed agent judges the question too broad
    to answer usefully (e.g. an unconstrained catalog dump). The API layer
    returns a "needs_clarification" response with `message` asking the user
    to narrow their question; `plan` is echoed back so the next turn's
    filters can be refined rather than reset.
    """

    def __init__(self, message: str, plan: QueryPlan):
        self.message = message
        self.plan = plan
        super().__init__(message)
```

`NeedsClientEvidenceError`'s class body is otherwise unchanged — only its base class changes from `Exception` to `NeedsUserInputError`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_base_agent -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full existing suite to confirm nothing else broke**

Run: `uv run pytest backend/tests/ -k "not container" -q`
Expected: PASS (existing `NeedsClientEvidenceError` usages are unaffected — only its base class changed)

- [ ] **Step 6: Commit**

```bash
git add backend/services/base_agent.py backend/tests/test_base_agent.py
git commit -m "feat: add clarification fields and NeedsUserInputError hierarchy"
```

---

## Task 2: `ChatTurn` model

**Files:**
- Create: `backend/models/conversation.py`
- Test: `backend/tests/test_conversation.py`

- [ ] **Step 1: Write the failing test**

```python
"""Unit tests for ChatTurn."""

import unittest

from backend.models.conversation import ChatTurn
from backend.services.base_agent import QueryPlan


class TestChatTurn(unittest.TestCase):
    def test_defaults(self):
        turn = ChatTurn(question="Q1", answer="A1")
        self.assertEqual(turn.agents_used, [])
        self.assertEqual(turn.source_refs, [])
        self.assertIsNone(turn.query_plan)

    def test_carries_query_plan(self):
        plan = QueryPlan(agents_to_use=["rag"])
        turn = ChatTurn(question="Q1", answer="A1", query_plan=plan)
        self.assertEqual(turn.query_plan.agents_to_use, ["rag"])

    def test_round_trips_through_json(self):
        turn = ChatTurn(question="Q1", answer="A1", agents_used=["rag"], source_refs=["c1", "c2"])
        restored = ChatTurn.model_validate_json(turn.model_dump_json())
        self.assertEqual(restored, turn)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest backend.tests.test_conversation -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.models.conversation'`

- [ ] **Step 3: Implement**

Create `backend/models/conversation.py`:

```python
"""
Conversation state for follow-up chat turns on a RAG result note.

A ChatTurn is echoed back verbatim by the client on every follow-up request —
the backend keeps no server-side session state. See docs/query-routing.md's
"Follow-up conversations" section: this deployment restarts often (see root
CLAUDE.md's hotfix workflow), so a stateless design means a backend restart
mid-conversation is a non-event, not a failure mode.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel

from backend.services.base_agent import QueryPlan


class ChatTurn(BaseModel):
    """One prior turn in a follow-up conversation."""

    question: str
    answer: str
    agents_used: list[str] = []
    source_refs: list[str] = []   # opaque evidence refs from that turn — see AgentResult.source_refs
    query_plan: Optional[QueryPlan] = None
```

A clarification turn (the backend asked the user to narrow their question) is represented the same way: `answer` holds the clarification message, `source_refs`/`agents_used` stay empty — no separate variant type.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest backend.tests.test_conversation -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/models/conversation.py backend/tests/test_conversation.py
git commit -m "feat: add ChatTurn model for follow-up conversation state"
```

---

## Task 3: Settings — `metadata_narrowing_threshold` + `max_conversation_context_chars`

**Files:**
- Modify: `backend/config/settings.py`
- Test: `backend/tests/test_chat_settings.py` (new file)

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for the follow-up-chat settings fields."""

import os
import unittest
from unittest.mock import patch

from backend.config.settings import Settings


class ChatSettingsTest(unittest.TestCase):
    def test_metadata_narrowing_threshold_defaults_to_50(self):
        s = Settings()
        self.assertEqual(s.metadata_narrowing_threshold, 50)

    def test_metadata_narrowing_threshold_from_env(self):
        with patch.dict(os.environ, {"METADATA_NARROWING_THRESHOLD": "10"}):
            s = Settings()
        self.assertEqual(s.metadata_narrowing_threshold, 10)

    def test_max_conversation_context_chars_defaults_to_6000(self):
        s = Settings()
        self.assertEqual(s.max_conversation_context_chars, 6000)

    def test_max_conversation_context_chars_from_env(self):
        with patch.dict(os.environ, {"MAX_CONVERSATION_CONTEXT_CHARS": "2000"}):
            s = Settings()
        self.assertEqual(s.max_conversation_context_chars, 2000)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_chat_settings -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'metadata_narrowing_threshold'`

- [ ] **Step 3: Implement**

In `backend/config/settings.py`, add near the other standalone `Field(...)` declarations (e.g. next to `min_abstract_words`):

```python
    # Follow-up chat
    metadata_narrowing_threshold: int = Field(
        default=50,
        description="MetadataAgent: max distinct catalog items to answer directly; "
                    "above this, ask the user to narrow their question instead."
    )
    max_conversation_context_chars: int = Field(
        default=6000,
        description="Max characters of prior Q&A turns included in the routing prompt "
                    "for a follow-up chat turn; older turns are dropped once this is exceeded."
    )
```

No custom validator needed — both are plain integers and `case_sensitive=False` (already set in `model_config`) picks up `METADATA_NARROWING_THRESHOLD`/`MAX_CONVERSATION_CONTEXT_CHARS` automatically.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_chat_settings -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/config/settings.py backend/tests/test_chat_settings.py
git commit -m "feat: add metadata_narrowing_threshold and max_conversation_context_chars settings"
```

---

## Task 4: `SourceInfo.chunk_id` + `QueryResult.source_refs` + `RAGAgent` source_refs

**Files:**
- Modify: `backend/services/rag_engine.py:40-59` (`SourceInfo`, `QueryResult`), `backend/services/rag_engine.py:275-289` (SourceInfo construction)
- Modify: `backend/services/rag_agent.py:78-83`
- Test: `backend/tests/test_rag_engine.py`, `backend/tests/test_rag_agent.py` (new file)

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_rag_engine.py` (inside `TestRAGEngine`, alongside `test_query_with_results` — reuse its existing chunk/mock setup pattern with `chunk_id="chunk1"`):

```python
    async def test_source_info_carries_chunk_id(self):
        question = "What is machine learning?"
        library_ids = ["12345"]
        self.mock_embedding_service.embed_text = AsyncMock(return_value=[0.1, 0.2, 0.3])

        chunk = DocumentChunk(
            text="Machine learning is a subset of artificial intelligence.",
            metadata=ChunkMetadata(
                chunk_id="chunk1",
                document_metadata=DocumentMetadata(
                    library_id="12345", item_key="ABC123", attachment_key="ATT1",
                    title="Introduction to ML", authors=["Smith, J."], year=2023,
                    item_type="journalArticle",
                ),
                page_number=5, text_preview="Machine learning is a", chunk_index=0,
                content_hash="h1",
            ),
        )
        self.mock_vector_store.search.return_value = [
            SearchResult(chunk=chunk, score=0.9),
        ]
        self.mock_llm_service.generate = AsyncMock(return_value="An answer [S1].")

        result = await self.rag_engine.query(question, library_ids)
        self.assertEqual(result.sources[0].chunk_id, "chunk1")
```

Create `backend/tests/test_rag_agent.py`:

```python
"""Unit tests for RAGAgent."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.services.rag_agent import RAGAgent
from backend.services.rag_engine import QueryResult, SourceInfo
from backend.models.filters import MetadataFilters


class TestRAGAgentSourceRefs(unittest.IsolatedAsyncioTestCase):
    async def test_source_refs_populated_from_chunk_ids(self):
        agent = RAGAgent(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        fake_result = QueryResult(
            question="Q", answer="A",
            sources=[
                SourceInfo(item_id="A", library_id="1", title="T1", score=0.9, chunk_id="c1"),
                SourceInfo(item_id="B", library_id="1", title="T2", score=0.8, chunk_id=None),
            ],
        )
        with patch("backend.services.rag_agent.RAGEngine") as MockEngine:
            MockEngine.return_value.query = AsyncMock(return_value=fake_result)
            result = await agent.execute(question="Q", library_ids=["1"], filters=MetadataFilters())
        self.assertEqual(result.source_refs, ["c1"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_rag_engine backend.tests.test_rag_agent -v`
Expected: FAIL — `TypeError: 'chunk_id' is an invalid keyword argument for SourceInfo` and `AssertionError: [] != ['c1']`

- [ ] **Step 3: Implement**

Modify `backend/services/rag_engine.py` lines 40-59:

```python
class SourceInfo(BaseModel):
    """Source citation information."""
    item_id: str
    library_id: str
    title: str
    authors: list[str] = []
    year: int | None = None
    page_number: int | None = None
    text_anchor: str | None = None
    score: float
    chunk_id: str | None = None   # payload chunk_id backing this citation's representative
                                   # chunk — lets a follow-up turn re-fetch the same evidence
                                   # via VectorStore.get_chunks_by_ids()


class QueryResult(BaseModel):
    """RAG query result."""
    question: str
    answer: str
    sources: List[SourceInfo]
    model_name: Optional[str] = None
    agents_used: list[str] = []
    source_refs: list[str] = []   # union of every contributing source's chunk_id
```

Modify the `SourceInfo(...)` construction at lines 275-289 (inside the `for result in doc_representatives:` loop) — add one line:

```python
            source = SourceInfo(
                item_id=doc_meta.item_key or "unknown",
                library_id=doc_meta.library_id,
                title=doc_meta.title or "Unknown Document",
                authors=doc_meta.authors or [],
                year=doc_meta.year,
                page_number=None,
                text_anchor=metadata.text_preview,
                score=result.score,
                chunk_id=metadata.chunk_id,
            )
```

Modify `backend/services/rag_agent.py` lines 78-83:

```python
        sources = result.sources
        return AgentResult(
            agent_name=self.name,
            context_text=result.answer,
            sources=sources,
            source_refs=[s.chunk_id for s in sources if s.chunk_id],
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_rag_engine backend.tests.test_rag_agent -v`
Expected: PASS

- [ ] **Step 5: Run the full backend suite**

Run: `uv run pytest backend/tests/ -k "not container" -q`
Expected: PASS (no other test asserts on `SourceInfo`'s exact field set)

- [ ] **Step 6: Commit**

```bash
git add backend/services/rag_engine.py backend/services/rag_agent.py backend/tests/test_rag_engine.py backend/tests/test_rag_agent.py
git commit -m "feat: propagate chunk_id through SourceInfo and RAGAgent.source_refs"
```

---

## Task 5: `VectorStore.get_chunks_by_ids()` + `chunk_id` payload index

**Files:**
- Modify: `backend/db/vector_store.py:786` (`_ensure_chunks_indexes` keyword_fields), and add a new method near `get_item_chunks` (~line 1191)
- Test: `backend/tests/test_vector_store.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_vector_store.py` (inside `TestVectorStore`, reusing the `test_add_chunk`-style fixture):

```python
    def test_get_chunks_by_ids_returns_matching_payloads(self):
        chunk_a = DocumentChunk(
            text="Chunk A text",
            metadata=ChunkMetadata(
                chunk_id="chunk-A", document_metadata=DocumentMetadata(
                    library_id="1", item_key="ITEM1", title="Doc A",
                ),
                page_number=1, text_preview="Chunk A", chunk_index=0, content_hash="hA",
            ),
            embedding=[0.1] * 384,
        )
        chunk_b = DocumentChunk(
            text="Chunk B text",
            metadata=ChunkMetadata(
                chunk_id="chunk-B", document_metadata=DocumentMetadata(
                    library_id="1", item_key="ITEM2", title="Doc B",
                ),
                page_number=1, text_preview="Chunk B", chunk_index=0, content_hash="hB",
            ),
            embedding=[0.2] * 384,
        )
        self.vector_store.add_chunk(chunk_a)
        self.vector_store.add_chunk(chunk_b)

        found = self.vector_store.get_chunks_by_ids(["chunk-A", "chunk-nonexistent"])

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["payload"]["chunk_id"], "chunk-A")
        self.assertEqual(found[0]["payload"]["text"], "Chunk A text")

    def test_get_chunks_by_ids_empty_list_returns_empty(self):
        self.assertEqual(self.vector_store.get_chunks_by_ids([]), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest backend.tests.test_vector_store -v`
Expected: FAIL with `AttributeError: 'VectorStore' object has no attribute 'get_chunks_by_ids'`

- [ ] **Step 3: Implement**

In `backend/db/vector_store.py`, extend the keyword index list in `_ensure_chunks_indexes` (around line 786):

```python
        keyword_fields = ("library_id", "item_key", "item_type", "author_lastnames", "tags_lower", "chunk_id")
```

Add a new method near `get_item_chunks` (after it, ~line 1221):

```python
    def get_chunks_by_ids(self, chunk_ids: list[str]) -> list[dict]:
        """
        Payload-only lookup by the payload's `chunk_id` field (content-hash-derived,
        stable — NOT the internal Qdrant point ID, which this module never exposes
        externally). No embedding call, no similarity search.

        Synchronous (the Qdrant client is sync) — callers must wrap in
        asyncio.to_thread() from an async context, same as get_items_by_metadata().

        Returns:
            List of {"id": <qdrant point id>, "payload": <dict>} — same shape as
            get_item_chunks(). Missing IDs are silently omitted, never an error.
        """
        if not chunk_ids:
            return []
        points, _ = self.client.scroll(
            collection_name=self.CHUNKS_COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="chunk_id", match=MatchAny(any=chunk_ids)),
            ]),
            limit=len(chunk_ids),
            with_payload=True,
            with_vectors=False,
        )
        return [{"id": p.id, "payload": p.payload} for p in points]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m unittest backend.tests.test_vector_store -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/db/vector_store.py backend/tests/test_vector_store.py
git commit -m "feat: add VectorStore.get_chunks_by_ids for follow-up chat continuation"
```

---

## Task 6: `MetadataAgent` — narrowing threshold + `chunk_id`/`source_refs`

**Files:**
- Modify: `backend/services/metadata_agent.py`
- Modify: `backend/tests/test_metadata_agent.py`

- [ ] **Step 1: Write the failing tests**

Replace the existing `test_custom_limit_via_kwargs` test (lines 209-215 of `backend/tests/test_metadata_agent.py`) — it tests a `metadata_limit` kwarg that this task removes in favor of `metadata_narrowing_threshold`-driven fetching:

```python
    async def test_narrowing_threshold_controls_fetch_limit(self):
        store = MagicMock()
        store.get_items_by_metadata.return_value = []
        agent = MetadataAgent(store)
        await agent.execute(
            question="Q", library_ids=[], filters=MetadataFilters(),
            metadata_narrowing_threshold=10,
        )
        call_kwargs = store.get_items_by_metadata.call_args.kwargs
        self.assertEqual(call_kwargs["limit"], 11)

    async def test_default_narrowing_threshold_is_50(self):
        store = MagicMock()
        store.get_items_by_metadata.return_value = []
        agent = MetadataAgent(store)
        await agent.execute(question="Q", library_ids=[], filters=MetadataFilters())
        call_kwargs = store.get_items_by_metadata.call_args.kwargs
        self.assertEqual(call_kwargs["limit"], 51)

    async def test_over_threshold_returns_clarification_not_items(self):
        payloads = [
            {"item_key": f"I{i}", "library_id": "1", "title": f"T{i}", "authors": []}
            for i in range(11)
        ]
        store = MagicMock()
        store.get_items_by_metadata.return_value = payloads
        agent = MetadataAgent(store)
        result = await agent.execute(
            question="Q", library_ids=["1"], filters=MetadataFilters(),
            metadata_narrowing_threshold=10,
        )
        self.assertTrue(result.needs_clarification)
        self.assertIn("more than 10", result.clarification_message)
        self.assertEqual(result.sources, [])

    async def test_under_threshold_returns_normal_items(self):
        payloads = [
            {"item_key": "I1", "library_id": "1", "title": "T1", "authors": []},
        ]
        store = MagicMock()
        store.get_items_by_metadata.return_value = payloads
        agent = MetadataAgent(store)
        result = await agent.execute(
            question="Q", library_ids=["1"], filters=MetadataFilters(),
            metadata_narrowing_threshold=10,
        )
        self.assertFalse(result.needs_clarification)
        self.assertEqual(len(result.sources), 1)

    async def test_source_refs_populated_from_chunk_id(self):
        payloads = [
            {"item_key": "I1", "library_id": "1", "title": "T1", "authors": [], "chunk_id": "c1"},
        ]
        store = MagicMock()
        store.get_items_by_metadata.return_value = payloads
        agent = MetadataAgent(store)
        result = await agent.execute(question="Q", library_ids=["1"], filters=MetadataFilters())
        self.assertEqual(result.source_refs, ["c1"])
```

Delete the old `test_custom_limit_via_kwargs` test (lines 209-215).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_metadata_agent -v`
Expected: FAIL — `AssertionError: 30 != 11` and `AttributeError`-style failures on `needs_clarification`/`source_refs` (fields already exist from Task 1, but `MetadataAgent` doesn't populate them yet)

- [ ] **Step 3: Implement**

Replace `MetadataResult` in `backend/services/metadata_agent.py` (add one field) and rewrite `execute()`:

```python
class MetadataResult(BaseModel):
    """A single item returned by the metadata catalog search."""

    item_id: str
    library_id: str
    title: str
    authors: list[str]
    year: Optional[int]
    item_type: Optional[str]
    text_preview: Optional[str]   # first few words of the first indexed chunk
    has_content: bool = True      # False for catalog-only stubs (no attachment/abstract to embed)
    tags: list[str] = []          # Zotero tags/keywords assigned to the item
    chunk_id: Optional[str] = None   # payload chunk_id of the representative chunk this
                                      # catalog entry came from — enables follow-up continuation
```

```python
    async def execute(
        self,
        question: str,
        library_ids: list[str],
        filters: MetadataFilters,
        trace: Optional[TraceCollector] = None,
        **kwargs,
    ) -> AgentResult:
        narrowing_threshold: int = kwargs.get("metadata_narrowing_threshold", 50)
        t_start = time.monotonic()

        raw = await asyncio.to_thread(
            self._vector_store.get_items_by_metadata,
            library_ids=library_ids if library_ids else None,
            filters=filters,
            limit=narrowing_threshold + 1,
        )

        if len(raw) > narrowing_threshold:
            message = (
                f"Found more than {narrowing_threshold} matching items — "
                "try narrowing by year, author, or item type."
            )
            logger.info(
                f"MetadataAgent: {len(raw)} matches exceed narrowing threshold {narrowing_threshold}"
            )
            if trace is not None:
                trace.record(AgentExecutionTrace(
                    agent_name=self.name,
                    retrieval=None,
                    catalog_results=None,
                    context_text=message,
                    sources_count=0,
                    duration_ms=int((time.monotonic() - t_start) * 1000),
                ))
            return AgentResult(
                agent_name=self.name,
                context_text=message,
                sources=[],
                needs_clarification=True,
                clarification_message=message,
            )

        results: list[MetadataResult] = []
        for payload in raw:
            results.append(MetadataResult(
                item_id=payload.get("item_key", "unknown"),
                library_id=payload.get("library_id", ""),
                title=payload.get("title") or "Untitled",
                authors=payload.get("authors") or [],
                year=payload.get("year"),
                item_type=payload.get("item_type"),
                text_preview=payload.get("text_preview"),
                has_content=payload.get("has_content", True),
                tags=payload.get("tags") or [],
                chunk_id=payload.get("chunk_id"),
            ))

        # Sort by year (ascending, unknowns last)
        results.sort(key=lambda r: (r.year is None, r.year or 0))

        # Build SourceInfo list so the orchestrator can link items in the UI
        sources = [
            dict(
                item_id=r.item_id,
                library_id=r.library_id,
                title=r.title,
                authors=r.authors,
                year=r.year,
                score=1.0,   # metadata matches have no similarity score
                chunk_id=r.chunk_id,
            )
            for r in results
        ]

        context_text = _results_to_context(results)
        logger.info(f"MetadataAgent returned {len(results)} items")

        if trace is not None:
            trace.record(AgentExecutionTrace(
                agent_name=self.name,
                retrieval=None,
                catalog_results=[r.model_dump() for r in results],
                context_text=context_text,
                sources_count=len(results),
                duration_ms=int((time.monotonic() - t_start) * 1000),
            ))

        return AgentResult(
            agent_name=self.name,
            context_text=context_text,
            sources=sources,
            source_refs=[r.chunk_id for r in results if r.chunk_id],
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_metadata_agent -v`
Expected: PASS (all tests, including the pre-existing ones — none of them exceed the default 50-item threshold)

- [ ] **Step 5: Commit**

```bash
git add backend/services/metadata_agent.py backend/tests/test_metadata_agent.py
git commit -m "feat: MetadataAgent narrowing-threshold clarification and source_refs"
```

---

## Task 7: `ContinuationAgent`

**Files:**
- Create: `backend/services/continuation_agent.py`
- Test: `backend/tests/test_continuation_agent.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for ContinuationAgent."""

import unittest
from unittest.mock import MagicMock

from backend.models.conversation import ChatTurn
from backend.models.filters import MetadataFilters
from backend.services.continuation_agent import ContinuationAgent


def _chunk(chunk_id, title="Doc", text="Some text", page=1):
    return {
        "id": f"point-{chunk_id}",
        "payload": {
            "chunk_id": chunk_id, "text": text, "title": title,
            "item_key": f"ITEM-{chunk_id}", "library_id": "1",
            "authors": [], "year": None, "page_number": page,
            "text_preview": text[:20],
        },
    }


class TestContinuationAgent(unittest.IsolatedAsyncioTestCase):
    def _make_agent(self, chunks_by_ids_return):
        store = MagicMock()
        store.get_chunks_by_ids.return_value = chunks_by_ids_return
        return ContinuationAgent(store), store

    async def test_re_fetches_prior_source_refs(self):
        agent, store = self._make_agent([_chunk("c1"), _chunk("c2")])
        history = [ChatTurn(question="Q0", answer="A0", source_refs=["c1", "c2"])]

        result = await agent.execute(
            question="Tell me more", library_ids=["1"], filters=MetadataFilters(),
            conversation_history=history,
        )

        store.get_chunks_by_ids.assert_called_once_with(["c1", "c2"])
        self.assertEqual(result.agent_name, "continuation")
        self.assertEqual(result.source_refs, ["c1", "c2"])
        self.assertEqual(len(result.sources), 2)
        self.assertIn("Q0", result.context_text)
        self.assertIn("Some text", result.context_text)

    async def test_missing_chunks_tolerated(self):
        agent, store = self._make_agent([_chunk("c1")])  # c2 no longer exists
        history = [ChatTurn(question="Q0", answer="A0", source_refs=["c1", "c2"])]

        result = await agent.execute(
            question="Tell me more", library_ids=["1"], filters=MetadataFilters(),
            conversation_history=history,
        )
        self.assertEqual(result.source_refs, ["c1"])
        self.assertEqual(len(result.sources), 1)

    async def test_no_source_refs_falls_back_to_history_only(self):
        agent, store = self._make_agent([])
        history = [ChatTurn(question="Q0", answer="A0", source_refs=[])]  # e.g. mentions-derived turn

        result = await agent.execute(
            question="Tell me more", library_ids=["1"], filters=MetadataFilters(),
            conversation_history=history,
        )
        store.get_chunks_by_ids.assert_not_called()
        self.assertEqual(result.sources, [])
        self.assertEqual(result.source_refs, [])
        self.assertIn("Q0", result.context_text)

    async def test_no_conversation_history_produces_empty_result(self):
        agent, store = self._make_agent([])
        result = await agent.execute(question="Q", library_ids=["1"], filters=MetadataFilters())
        store.get_chunks_by_ids.assert_not_called()
        self.assertEqual(result.sources, [])

    def test_capability_prompt_mentions_conversation_only(self):
        agent, _ = self._make_agent([])
        self.assertIn("conversation", agent.capability_prompt.lower())

    def test_name_is_continuation(self):
        agent, _ = self._make_agent([])
        self.assertEqual(agent.name, "continuation")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_continuation_agent -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.services.continuation_agent'`

- [ ] **Step 3: Implement**

Create `backend/services/continuation_agent.py`:

```python
"""
Continuation agent — answers follow-up chat turns by re-fetching evidence
already retrieved earlier in the conversation instead of running a new
search. See docs/query-routing.md's "Follow-up conversations" section.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from backend.db.vector_store import VectorStore
from backend.models.conversation import ChatTurn
from backend.models.filters import MetadataFilters
from backend.services.base_agent import AgentResult, BaseAgent

if TYPE_CHECKING:
    from backend.services.trace_collector import TraceCollector

logger = logging.getLogger(__name__)


class ContinuationAgent(BaseAgent):
    """Elaborates on, clarifies, or compares evidence already retrieved in
    this conversation, without running a new search."""

    def __init__(self, vector_store: VectorStore):
        self._vector_store = vector_store

    @property
    def name(self) -> str:
        return "continuation"

    @property
    def capability_prompt(self) -> str:
        return (
            "Use when the follow-up elaborates on, clarifies, or compares evidence "
            "already retrieved in this conversation, without needing a new search "
            "(e.g. 'explain that more', 'what does source 3 say about X', "
            "'how do these two sources differ'). Only applicable when there is a "
            "conversation history — never select this for the first question."
        )

    async def execute(
        self,
        question: str,
        library_ids: list[str],
        filters: MetadataFilters,
        trace: Optional[TraceCollector] = None,
        **kwargs,
    ) -> AgentResult:
        conversation_history: list[ChatTurn] = kwargs.get("conversation_history") or []

        prior_refs: list[str] = []
        for turn in conversation_history:
            for ref in turn.source_refs:
                if ref not in prior_refs:
                    prior_refs.append(ref)

        chunks: list[dict] = []
        if prior_refs:
            chunks = await asyncio.to_thread(self._vector_store.get_chunks_by_ids, prior_refs)
            if len(chunks) < len(prior_refs):
                logger.info(
                    "ContinuationAgent: %d of %d prior evidence chunks no longer found",
                    len(prior_refs) - len(chunks), len(prior_refs),
                )

        context_text = _render_history(conversation_history)
        if chunks:
            context_text += "\n\n" + _render_chunks(chunks)
        elif prior_refs:
            context_text += (
                "\n\n(The specific passages cited earlier are no longer available — "
                "answer from the conversation history above only.)"
            )

        return AgentResult(
            agent_name=self.name,
            context_text=context_text,
            sources=_sources_from_chunks(chunks),
            source_refs=[c["payload"]["chunk_id"] for c in chunks if c["payload"].get("chunk_id")],
        )


def _render_history(history: list[ChatTurn]) -> str:
    if not history:
        return ""
    lines = ["Conversation so far:"]
    for turn in history:
        lines.append(f"Q: {turn.question}")
        lines.append(f"A: {turn.answer}")
    return "\n".join(lines)


def _render_chunks(chunks: list[dict]) -> str:
    lines = ["Previously retrieved passages:"]
    for i, c in enumerate(chunks, 1):
        payload = c["payload"]
        title = payload.get("title") or "Unknown Document"
        page = payload.get("page_number")
        page_label = f" [p. {page}]" if page else ""
        lines.append(f"[S{i}: {title}{page_label}]\n{payload.get('text', '')}")
    return "\n\n".join(lines)


def _sources_from_chunks(chunks: list[dict]) -> list[dict]:
    sources = []
    for c in chunks:
        payload = c["payload"]
        sources.append(dict(
            item_id=payload.get("item_key", "unknown"),
            library_id=payload.get("library_id", ""),
            title=payload.get("title") or "Unknown Document",
            authors=payload.get("authors") or [],
            year=payload.get("year"),
            page_number=payload.get("page_number"),
            text_anchor=payload.get("text_preview"),
            score=1.0,
            chunk_id=payload.get("chunk_id"),
        ))
    return sources
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_continuation_agent -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/continuation_agent.py backend/tests/test_continuation_agent.py
git commit -m "feat: add ContinuationAgent for follow-up chat turns"
```

---

## Task 8: `QueryRouter` — conversation-aware prompt + clarification parsing

**Files:**
- Modify: `backend/services/query_router.py`
- Test: `backend/tests/test_query_router.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_query_router.py`:

```python
from backend.models.conversation import ChatTurn


class TestConversationHistoryInPrompt(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_history_included_in_prompt(self):
        router = _make_router('{"agents": ["continuation"]}')
        agents = [_make_agent("continuation", "cap")]
        history = [ChatTurn(question="First question", answer="First answer")]

        await router.route("Follow-up", agents, conversation_history=history)

        sent_prompt = router._llm.generate.call_args.kwargs["prompt"]
        self.assertIn("First question", sent_prompt)
        self.assertIn("First answer", sent_prompt)

    async def test_no_conversation_history_omits_block(self):
        router = _make_router('{"agents": ["rag"]}')
        agents = [_make_agent("rag", "cap")]

        await router.route("A fresh question", agents)

        sent_prompt = router._llm.generate.call_args.kwargs["prompt"]
        self.assertNotIn("Conversation so far", sent_prompt)

    async def test_long_history_is_truncated_to_max_chars(self):
        router = _make_router('{"agents": ["rag"]}')
        agents = [_make_agent("rag", "cap")]
        history = [
            ChatTurn(question=f"Q{i}" * 50, answer=f"A{i}" * 50) for i in range(10)
        ]

        await router.route("Follow-up", agents, conversation_history=history,
                            max_conversation_context_chars=200)

        sent_prompt = router._llm.generate.call_args.kwargs["prompt"]
        # Only the most recent turn(s) fit under a 200-char budget — the first turn's
        # question text must have been dropped.
        self.assertNotIn("Q0" * 50, sent_prompt)
        self.assertIn("Q9" * 50, sent_prompt)


class TestClarificationParsing(unittest.IsolatedAsyncioTestCase):
    async def test_parses_clarification_needed_true(self):
        router = _make_router(
            '{"agents": ["metadata"], "clarification_needed": true, '
            '"clarification_question": "Which years?"}'
        )
        agents = [_make_agent("metadata", "cap")]
        plan = await router.route("What has Luhmann written?", agents)
        self.assertTrue(plan.clarification_needed)
        self.assertEqual(plan.clarification_question, "Which years?")

    async def test_clarification_needed_defaults_false(self):
        router = _make_router('{"agents": ["rag"]}')
        agents = [_make_agent("rag", "cap")]
        plan = await router.route("Q", agents)
        self.assertFalse(plan.clarification_needed)
        self.assertIsNone(plan.clarification_question)
```

(These reuse `_make_router`/`_make_agent` already defined near the top of `backend/tests/test_query_router.py`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_query_router -v`
Expected: FAIL — `TypeError: route() got an unexpected keyword argument 'conversation_history'`

- [ ] **Step 3: Implement**

Modify `backend/services/query_router.py`. Add the import and a rendering helper:

```python
from backend.models.conversation import ChatTurn
```

```python
def _render_conversation_history(history: list[ChatTurn], max_chars: int) -> str:
    """Render the most recent turns that fit under max_chars, dropping older ones."""
    if not history:
        return ""
    kept: list[str] = []
    total = 0
    for turn in reversed(history):
        block = f"Q: {turn.question}\nA: {turn.answer}"
        if total + len(block) > max_chars and kept:
            break
        kept.append(block)
        total += len(block)
    kept.reverse()
    return "\n\n".join(kept)
```

Update `_PROMPT_TEMPLATE` to add a `{conversation_block}` slot and the two new JSON fields (insert `conversation_block` right after the agent list, and add the two fields to the example JSON object and field explanations):

```python
_PROMPT_TEMPLATE = """\
You are a query router for an academic library system.
You have these agents available:

{agent_blocks}
{conversation_block}
Question: "{question}"

Return ONLY a valid JSON object — no other text:
{{
  "agents": ["rag"],
  "year_min": null,
  "year_max": null,
  "authors": [],
  "item_types": [],
  "title_keywords": [],
  "citation_targets": [],
  "routing_description": null,
  "clarification_needed": false,
  "clarification_question": null
}}

Field explanations:
- agents: list of agent names to invoke (must be from the agents listed above)
- year_min / year_max: earliest/latest year mentioned (integer or null)
- authors: author last names mentioned in the question (lowercase strings) — WRITTEN BY only
- item_types: e.g. ["book", "journalArticle"] if item type is specified; empty otherwise
- title_keywords: ONLY populate when the user explicitly names a specific document title
  they want to find (e.g. "find the paper called 'X'"). Leave empty for topic/keyword searches.
- citation_targets: list of {{author, year, title_keywords}} for works the question asks about
  being CITED/DISCUSSED by other publications (see "mentions" agent below). Leave empty unless
  the question is clearly about citation/discussion of specific named work(s).
- routing_description: brief one-sentence note on why these agents were chosen
- clarification_needed: true ONLY when the question is an unconstrained catalog-style request
  (e.g. "What has X written about?", "List everything on topic Y") with no year range, specific
  title, or other narrowing detail — asking the user to narrow it is better than guessing.
  Leave false for any question with enough specificity to search directly, and false whenever
  a conversation history is present and the follow-up is clearly building on it.
- clarification_question: if clarification_needed is true, a short question asking the user to
  narrow by year, author, item type, or topic; otherwise null.
{guidance}"""
```

Update `_build_prompt` and `route`:

```python
    def _build_prompt(
        self,
        agents: list[BaseAgent],
        question: str,
        conversation_history: Optional[list[ChatTurn]] = None,
        max_conversation_context_chars: int = 6000,
    ) -> str:
        agent_blocks = "\n\n".join(
            f"[AGENT: {a.name}]\n{a.capability_prompt}" for a in agents
        )
        conversation_block = ""
        if conversation_history:
            rendered = _render_conversation_history(conversation_history, max_conversation_context_chars)
            if rendered:
                conversation_block = f"\nConversation so far (oldest first):\n{rendered}\n"
        return _PROMPT_TEMPLATE.format(
            agent_blocks=agent_blocks,
            conversation_block=conversation_block,
            question=question,
            guidance=_ROUTING_GUIDANCE,
        )

    async def route(
        self,
        question: str,
        agents: list[BaseAgent],
        trace: Optional[TraceCollector] = None,
        conversation_history: Optional[list[ChatTurn]] = None,
        max_conversation_context_chars: int = 6000,
    ) -> QueryPlan:
        if not agents:
            return QueryPlan()

        prompt = self._build_prompt(agents, question, conversation_history, max_conversation_context_chars)
        valid_names = {a.name for a in agents}
        t0 = time.monotonic()
        raw = ""

        try:
            raw = await self._llm.generate(prompt=prompt, max_tokens=256, temperature=0.0)
            duration_ms = int((time.monotonic() - t0) * 1000)
            data = _parse_json(raw)

            selected = [n for n in data.get("agents", ["rag"]) if n in valid_names]
            if not selected:
                selected = ["rag"]

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

(The rest of `route()` — trace recording, the `except` fallback — is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_query_router -v`
Expected: PASS

- [ ] **Step 5: Run the full backend suite**

Run: `uv run pytest backend/tests/ -k "not container" -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/services/query_router.py backend/tests/test_query_router.py
git commit -m "feat: conversation-aware routing prompt and clarification parsing"
```

---

## Task 9: `QueryOrchestrator` — conversation threading, clarification short-circuit, `ContinuationAgent` registration

**Files:**
- Modify: `backend/services/query_orchestrator.py`
- Test: `backend/tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_orchestrator.py` (new imports at top: `from backend.models.conversation import ChatTurn`, `from backend.services.base_agent import NeedsClarificationError`):

```python
class TestConversationHistoryThreading(unittest.IsolatedAsyncioTestCase):
    async def test_conversation_history_passed_to_agents(self):
        orch = _make_orchestrator()
        captured = {}

        async def fake_execute(**kwargs):
            captured.update(kwargs)
            return AgentResult(agent_name="rag", context_text="ans", sources=[])

        agent = _stub_agent("rag", AgentResult(agent_name="rag", context_text="x"))
        agent.execute = fake_execute
        orch._agents = {"rag": agent}
        history = [ChatTurn(question="Q0", answer="A0")]

        await orch.query("Follow-up", ["1"], enable_routing=False, conversation_history=history)

        self.assertEqual(captured["conversation_history"], history)

    async def test_force_fresh_retrieval_clears_history_for_agents(self):
        orch = _make_orchestrator()
        captured = {}

        async def fake_execute(**kwargs):
            captured.update(kwargs)
            return AgentResult(agent_name="rag", context_text="ans", sources=[])

        agent = _stub_agent("rag", AgentResult(agent_name="rag", context_text="x"))
        agent.execute = fake_execute
        orch._agents = {"rag": agent}
        history = [ChatTurn(question="Q0", answer="A0")]

        await orch.query("Follow-up", ["1"], enable_routing=False,
                          conversation_history=history, force_fresh_retrieval=True)

        self.assertEqual(captured["conversation_history"], [])


class TestClarificationShortCircuit(unittest.IsolatedAsyncioTestCase):
    async def test_router_clarification_needed_raises_before_agents_run(self):
        orch = _make_orchestrator()
        agent = _stub_agent("rag", AgentResult(agent_name="rag", context_text="should not run"))
        orch._agents = {"rag": agent}
        plan = QueryPlan(agents_to_use=["rag"], clarification_needed=True,
                          clarification_question="Which years?")

        with self.assertRaises(NeedsClarificationError) as ctx:
            await orch.query("Broad question", ["1"], preset_plan=plan)

        self.assertEqual(ctx.exception.message, "Which years?")
        agent.execute.assert_not_called()

    async def test_all_agents_flagging_clarification_raises(self):
        orch = _make_orchestrator()
        agent = _stub_agent("metadata", AgentResult(
            agent_name="metadata", context_text="too many",
            needs_clarification=True, clarification_message="Narrow it down",
        ))
        orch._agents = {"rag": agent, "metadata": agent}
        plan = QueryPlan(agents_to_use=["metadata"])

        with self.assertRaises(NeedsClarificationError) as ctx:
            await orch.query("Broad", ["1"], preset_plan=plan)
        self.assertEqual(ctx.exception.message, "Narrow it down")

    async def test_mixed_clarification_proceeds_with_usable_content(self):
        orch = _make_orchestrator()
        orch._llm_service.generate = AsyncMock(return_value="Synthesized answer.")
        orch._settings.get_hardware_preset.return_value.llm.max_answer_tokens = 512
        good = _stub_agent("rag", AgentResult(
            agent_name="rag", context_text="[S1] Good content",
            sources=[_make_source(item_id="A")],
        ))
        broad = _stub_agent("metadata", AgentResult(
            agent_name="metadata", context_text="too many",
            needs_clarification=True, clarification_message="Catalog too broad",
        ))
        orch._agents = {"rag": good, "metadata": broad}
        plan = QueryPlan(agents_to_use=["rag", "metadata"])

        result = await orch.query("Mixed", ["1"], preset_plan=plan)

        self.assertIn("Synthesized answer.", result.answer)
        sent_prompt = orch._llm_service.generate.call_args.kwargs["prompt"]
        self.assertIn("Catalog too broad", sent_prompt)


class TestContinuationAgentRegistration(unittest.IsolatedAsyncioTestCase):
    async def test_continuation_agent_registered_by_default(self):
        with patch("backend.services.query_orchestrator.RAGAgent"), \
             patch("backend.services.query_orchestrator.MetadataAgent"), \
             patch("backend.services.query_orchestrator.ContinuationAgent") as MockContinuation:
            MockContinuation.return_value.name = "continuation"
            orch = QueryOrchestrator(MagicMock(), MagicMock(), MagicMock(), MagicMock())
        self.assertIn("continuation", orch._agents)

    async def test_continuation_dropped_when_no_history(self):
        orch = _make_orchestrator()
        rag = _stub_agent("rag", AgentResult(agent_name="rag", context_text="ans"))
        continuation = _stub_agent("continuation", AgentResult(agent_name="continuation", context_text="x"))
        orch._agents = {"rag": rag, "continuation": continuation}
        plan = QueryPlan(agents_to_use=["continuation"])

        await orch.query("First question", ["1"], preset_plan=plan)

        continuation.execute.assert_not_called()
        rag.execute.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_orchestrator -v`
Expected: FAIL — `TypeError: query() got an unexpected keyword argument 'conversation_history'`

- [ ] **Step 3: Implement**

In `backend/services/query_orchestrator.py`:

Update imports:

```python
from backend.models.conversation import ChatTurn
from backend.services.base_agent import (
    AgentResult, BaseAgent, NeedsClarificationError, NeedsClientEvidenceError, QueryPlan,
)
from backend.services.continuation_agent import ContinuationAgent
```

Register the new agent in `_register_defaults`:

```python
    def _register_defaults(
        self,
        embedding_service: EmbeddingService,
        llm_service: LLMService,
        vector_store: VectorStore,
        settings: Settings,
    ) -> None:
        self.register(RAGAgent(embedding_service, llm_service, vector_store, settings))
        self.register(MetadataAgent(vector_store))
        self.register(MentionsAgent())
        self.register(ContinuationAgent(vector_store))
```

Rewrite `query()`'s signature and body:

```python
    async def query(
        self,
        question: str,
        library_ids: list[str],
        top_k: int = 5,
        min_score: float = 0.3,
        enable_routing: bool = True,
        trace: Optional[TraceCollector] = None,
        client_evidence: Optional[ClientEvidence] = None,
        preset_plan: Optional[QueryPlan] = None,
        conversation_history: Optional[list[ChatTurn]] = None,
        force_fresh_retrieval: bool = False,
    ) -> QueryResult:
        """
        Route the question, run selected agents, and synthesize the final answer.

        Args:
            ... (existing args unchanged) ...
            conversation_history: Prior turns of a follow-up chat conversation, echoed
                back verbatim by the client. Threaded into the routing prompt and into
                every agent's execute(**kwargs) — agents that don't use it ignore it.
            force_fresh_retrieval: When True, conversation_history is still recorded by
                the caller but ignored here for routing/agent selection — runs a full
                fresh pipeline as if this were a brand-new question.

        Raises:
            NeedsClientEvidenceError: ... (unchanged) ...
            NeedsClarificationError: the router judged the question too broad before any
                agent ran, or every selected agent judged it too broad after running.
        """
        effective_history: list[ChatTurn] = [] if force_fresh_retrieval else (conversation_history or [])

        # 1. Routing
        if preset_plan is not None:
            plan = preset_plan
        elif enable_routing and len(self._agents) > 1:
            plan = await QueryRouter(self._llm_service).route(
                question, list(self._agents.values()), trace=trace,
                conversation_history=effective_history,
                max_conversation_context_chars=self._settings.max_conversation_context_chars,
            )
            logger.info(
                "QueryOrchestrator: routing → agents=%s filters=%s description=%s",
                plan.agents_to_use,
                plan.filters.model_dump(exclude_defaults=True) or "{}",
                plan.routing_description,
            )
        else:
            plan = QueryPlan(agents_to_use=["rag"])

        # 1a. The router judged this question too broad before any agent ran.
        if plan.clarification_needed:
            raise NeedsClarificationError(
                message=plan.clarification_question or "Could you narrow your question?",
                plan=plan,
            )

        # 1b. "mentions" evidence only exists client-side ... (unchanged)
        if "mentions" in plan.agents_to_use:
            if not plan.filters.citation_targets:
                plan.agents_to_use = [a for a in plan.agents_to_use if a != "mentions"] or ["rag"]
            elif client_evidence is None:
                raise NeedsClientEvidenceError(
                    citation_targets=plan.filters.citation_targets, plan=plan,
                )

        # 1c. "continuation" only makes sense with a conversation to continue — a
        # spurious router selection on the first question falls back to "rag".
        if "continuation" in plan.agents_to_use and not effective_history:
            plan.agents_to_use = [a for a in plan.agents_to_use if a != "continuation"] or ["rag"]

        # 2. Resolve agents (unknown names fall back to "rag")
        selected: list[BaseAgent] = [
            self._agents[n] for n in plan.agents_to_use if n in self._agents
        ]
        if not selected:
            fallback = self._agents.get("rag") or next(iter(self._agents.values()), None)
            if fallback:
                selected = [fallback]

        # 3. Execute in parallel
        agent_results: list[AgentResult] = await asyncio.gather(*[
            agent.execute(
                question=question,
                library_ids=library_ids,
                filters=plan.filters,
                trace=trace,
                top_k=top_k,
                min_score=min_score,
                client_evidence=client_evidence,
                conversation_history=effective_history,
                metadata_narrowing_threshold=self._settings.metadata_narrowing_threshold,
            )
            for agent in selected
        ])

        # 3a. Clarification partition — an agent that judges the query too broad
        # contributes no usable content; short-circuit only if NONE of the
        # selected agents produced something usable.
        usable_results = [r for r in agent_results if not r.needs_clarification]
        clarifying_results = [r for r in agent_results if r.needs_clarification]
        if clarifying_results and not usable_results:
            message = clarifying_results[0].clarification_message or "Could you narrow your question?"
            raise NeedsClarificationError(message=message, plan=plan)
        agent_results = usable_results

        # 3b. RAG fallback — if no agent produced useful content and "rag" wasn't
        # already selected, run the RAG agent so we don't return an empty answer.
        if _all_empty(agent_results) and "rag" not in plan.agents_to_use:
            rag_agent = self._agents.get("rag")
            if rag_agent:
                logger.info(
                    "QueryOrchestrator: all agents returned empty — falling back to RAG"
                )
                if trace is not None:
                    trace.record(FallbackTrace())
                fallback_result = await rag_agent.execute(
                    question=question,
                    library_ids=library_ids,
                    filters=MetadataFilters(),  # clear filters for the fallback search
                    trace=trace,
                    top_k=top_k,
                    min_score=min_score,
                )
                return _rag_passthrough(
                    fallback_result, question,
                    model_name=self._llm_service.model_name if isinstance(self._llm_service.model_name, str) else None,
                    agents_used=plan.agents_to_use + ["rag"],
                )

        # 4. Synthesize or pass through
        agents_used = [a.name for a in selected]
        _raw_model = self._llm_service.model_name
        llm_model: Optional[str] = _raw_model if isinstance(_raw_model, str) else None
        if len(agent_results) == 1 and agent_results[0].agent_name == "rag":
            return _rag_passthrough(agent_results[0], question,
                                    model_name=llm_model, agents_used=agents_used)

        clarification_caveats = [
            r.clarification_message for r in clarifying_results if r.clarification_message
        ]
        return await self._synthesize(question, plan, agent_results,
                                      model_name=llm_model, agents_used=agents_used,
                                      trace=trace, clarification_caveats=clarification_caveats)

    async def _synthesize(
        self,
        question: str,
        plan: QueryPlan,
        results: list[AgentResult],
        model_name: Optional[str] = None,
        agents_used: Optional[list[str]] = None,
        trace: Optional[TraceCollector] = None,
        clarification_caveats: Optional[list[str]] = None,
    ) -> QueryResult:
        # Renumber each agent's local [S1],[S2]... citations into a global sequence
        # so the merged sources list stays consistent with what the LLM sees.
        offset = 0
        renumbered_blocks = []
        for r in results:
            text = _shift_source_refs(r.context_text, offset)
            renumbered_blocks.append(f"[{r.agent_name.upper()} AGENT RESULTS]\n{text}")
            offset += len(r.sources)

        context_blocks = "\n\n---\n\n".join(renumbered_blocks)
        prompt = _SYNTHESIS_PROMPT.format(
            question=question,
            context_blocks=context_blocks,
        )
        if clarification_caveats:
            caveat_lines = "\n".join(f"- {c}" for c in clarification_caveats)
            prompt += (
                f"\n\nNote: some retrieval was too broad to include in full:\n{caveat_lines}\n"
                "Mention this limitation briefly in your answer if relevant."
            )

        preset = self._settings.get_hardware_preset()
        max_tokens = preset.llm.max_answer_tokens
        t_llm = time.monotonic()
        answer = await self._llm_service.generate(
            prompt=prompt,
            max_tokens=max_tokens,
            temperature=0.7,
        )
        llm_duration_ms = int((time.monotonic() - t_llm) * 1000)

        if trace is not None:
            trace.record(LLMCallTrace(
                call_type="synthesis",
                model=self._llm_service.model_name,
                prompt=prompt,
                response=answer,
                temperature=0.7,
                max_tokens=max_tokens,
                duration_ms=llm_duration_ms,
                timestamp=datetime.now(timezone.utc).isoformat(),
            ))

        sources = _merge_sources(results)
        return QueryResult(
            question=question,
            answer=answer,
            sources=sources,
            model_name=model_name,
            agents_used=agents_used or [],
            source_refs=_merge_source_refs(results),
        )
```

Update `_rag_passthrough` to propagate `source_refs`:

```python
def _rag_passthrough(
    agent_result: AgentResult,
    question: str,
    model_name: Optional[str] = None,
    agents_used: Optional[list[str]] = None,
) -> QueryResult:
    """Convert a single RAGAgent result directly to a QueryResult without synthesis."""
    sources: list[SourceInfo] = []
    for s in agent_result.sources:
        if isinstance(s, SourceInfo):
            sources.append(s)
        elif isinstance(s, dict):
            sources.append(SourceInfo(**s))
    return QueryResult(
        question=question,
        answer=agent_result.context_text,
        sources=sources,
        model_name=model_name,
        agents_used=agents_used or [],
        source_refs=agent_result.source_refs,
    )
```

Add `_merge_source_refs` near `_merge_sources`:

```python
def _merge_source_refs(results: list[AgentResult]) -> list[str]:
    """Union of every agent's source_refs, de-duplicated, order-preserving."""
    seen: set[str] = set()
    merged: list[str] = []
    for r in results:
        for ref in r.source_refs:
            if ref not in seen:
                seen.add(ref)
                merged.append(ref)
    return merged
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_orchestrator -v`
Expected: PASS

- [ ] **Step 5: Run the full backend suite**

Run: `uv run pytest backend/tests/ -k "not container" -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/services/query_orchestrator.py backend/tests/test_orchestrator.py
git commit -m "feat: thread conversation_history through the orchestrator and register ContinuationAgent"
```

---

## Task 10: `backend/api/query.py` — request/response fields + exception dispatch

**Files:**
- Modify: `backend/api/query.py`
- Test: `backend/tests/test_query_api.py` (create if it doesn't already exist — check first)

- [ ] **Step 1: Check for an existing test file, then write the failing tests**

```bash
ls backend/tests/test_query_api.py 2>/dev/null || echo "does not exist yet"
```

If it exists, add the tests below to it (matching its existing fixture/client style); otherwise create `backend/tests/test_query_api.py` following the FastAPI `TestClient` pattern used elsewhere in `backend/tests/` (check `backend/tests/test_batch_metadata_update_fields.py` for the app/client fixture style used with `backend/main.py`'s app, and mirror it).

Regardless of which, the new tests exercise the module directly rather than over HTTP, to stay decoupled from server wiring:

```python
import unittest

from backend.api.query import QueryRequest, QueryResponse, _needs_clarification_response
from backend.services.base_agent import NeedsClarificationError, QueryPlan


class TestNeedsClarificationResponse(unittest.TestCase):
    def test_builds_needs_clarification_response(self):
        req = QueryRequest(question="What has Luhmann written?", library_ids=["1"])
        exc = NeedsClarificationError(message="Please narrow by year.", plan=QueryPlan(agents_to_use=["metadata"]))

        resp = _needs_clarification_response(req, exc)

        self.assertEqual(resp.status, "needs_clarification")
        self.assertEqual(resp.clarification_message, "Please narrow by year.")
        self.assertEqual(resp.query_plan.agents_to_use, ["metadata"])
        self.assertEqual(resp.answer, "")


class TestRequestResponseNewFields(unittest.TestCase):
    def test_conversation_history_defaults_empty(self):
        req = QueryRequest(question="Q", library_ids=["1"])
        self.assertEqual(req.conversation_history, [])

    def test_force_fresh_retrieval_defaults_false(self):
        req = QueryRequest(question="Q", library_ids=["1"])
        self.assertFalse(req.force_fresh_retrieval)

    def test_response_source_refs_defaults_empty(self):
        resp = QueryResponse(question="Q", answer="A", answer_format="text", sources=[], library_ids=["1"])
        self.assertEqual(resp.source_refs, [])

    def test_response_status_accepts_needs_clarification(self):
        resp = QueryResponse(
            question="Q", answer="", answer_format="text", sources=[], library_ids=["1"],
            status="needs_clarification",
        )
        self.assertEqual(resp.status, "needs_clarification")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m unittest backend.tests.test_query_api -v`
Expected: FAIL — `ImportError: cannot import name '_needs_clarification_response'`

- [ ] **Step 3: Implement**

Modify `backend/api/query.py`:

Update imports:

```python
from backend.models.conversation import ChatTurn
from backend.services.base_agent import (
    NeedsClarificationError, NeedsClientEvidenceError, NeedsUserInputError, QueryPlan,
)
```

Update `QueryRequest`:

```python
class QueryRequest(BaseModel):
    """RAG query request."""
    question: str
    library_ids: List[str]
    top_k: Optional[int] = None
    min_score: Optional[float] = None
    enable_routing: bool = True
    llm_model: Optional[str] = None
    include_trace: bool = False
    client_evidence: Optional[ClientEvidence] = None
    query_plan: Optional[QueryPlan] = None
    conversation_history: List[ChatTurn] = []  # prior turns of a follow-up chat conversation
    force_fresh_retrieval: bool = False        # ignore conversation_history for routing this turn
```

Update `QueryResponse`:

```python
class QueryResponse(BaseModel):
    """RAG query response."""
    question: str
    answer: str
    answer_format: str
    sources: List[SourceCitation]
    library_ids: List[str]
    model_name: Optional[str] = None
    agents_used: List[str] = []
    library_document_counts: dict[str, int] = {}
    trace: Optional[QueryTrace] = None
    status: Literal["complete", "needs_client_evidence", "needs_clarification"] = "complete"
    citation_targets: List[CitationTarget] = []
    query_plan: Optional[QueryPlan] = None
    source_refs: List[str] = []                       # union of every used source's chunk_id,
                                                        # echoed back verbatim on the next turn
    clarification_message: Optional[str] = None        # populated when status == "needs_clarification"
```

Add `_needs_clarification_response` next to `_needs_evidence_response`:

```python
def _needs_clarification_response(query: QueryRequest, exc: NeedsClarificationError) -> QueryResponse:
    """Map a NeedsClarificationError to a response asking the user to narrow their question."""
    return QueryResponse(
        question=query.question,
        answer="",
        answer_format="text",
        sources=[],
        library_ids=query.library_ids,
        status="needs_clarification",
        clarification_message=exc.message,
        query_plan=exc.plan,
    )
```

In `query_libraries()`, pass the two new fields through to the orchestrator call:

```python
        result = await orchestrator.query(
            question=query.question,
            library_ids=query.library_ids,
            top_k=top_k,
            min_score=min_score,
            enable_routing=query.enable_routing,
            trace=trace_collector,
            client_evidence=query.client_evidence,
            preset_plan=query.query_plan,
            conversation_history=query.conversation_history,
            force_fresh_retrieval=query.force_fresh_retrieval,
        )
```

...and `source_refs` into the success response:

```python
        return QueryResponse(
            question=query.question,
            answer=answer_html,
            answer_format="html",
            sources=sources,
            library_ids=query.library_ids,
            model_name=result.model_name,
            agents_used=result.agents_used,
            library_document_counts=library_document_counts,
            trace=trace_collector.finalize() if trace_collector is not None else None,
            source_refs=result.source_refs,
        )
```

Replace the exception handling at the bottom to dispatch on the shared base, narrowing on the concrete subtype:

```python
    except NeedsUserInputError as exc:
        if isinstance(exc, NeedsClientEvidenceError):
            return _needs_evidence_response(query, exc)
        if isinstance(exc, NeedsClarificationError):
            return _needs_clarification_response(query, exc)
        raise

    except Exception as e:
        logger.exception("Query failed")
        raise HTTPException(
            status_code=500,
            detail=f"Query failed: {str(e)}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m unittest backend.tests.test_query_api -v`
Expected: PASS

- [ ] **Step 5: Run the full backend suite**

Run: `uv run pytest backend/tests/ -k "not container" -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/api/query.py backend/tests/test_query_api.py
git commit -m "feat: add conversation_history/force_fresh_retrieval to the query API and generalize needs-input handling"
```

---

## Task 11: `docs/query-routing.md` — document the follow-up protocol

**Files:**
- Modify: `docs/query-routing.md`

- [ ] **Step 1: Add a new section** after "## Two-Phase Protocol for the `mentions` Agent" and before "## Schema Versioning":

```markdown
## Follow-up Conversations

`POST /api/query` accepts an optional `conversation_history` — a list of prior
`{question, answer, agents_used, source_refs, query_plan}` turns, echoed back
verbatim by the client on every follow-up request. The backend keeps no
server-side session state: every request is fully self-contained, so a
backend restart mid-conversation (this deployment restarts often — see root
`CLAUDE.md`'s hotfix workflow) is a non-event, not a failure mode.

A dedicated `ContinuationAgent` (`backend/services/continuation_agent.py`)
handles most follow-ups: it re-fetches the previous turn's evidence by the
`chunk_id` values recorded in `source_refs` (`VectorStore.get_chunks_by_ids()`
— no embedding call, no similarity search) and synthesizes from that plus the
conversation text. The router selects it via its `capability_prompt` exactly
like any other agent — no orchestrator changes are needed to add further
chat-specific agents later (e.g. one comparing two cited works).

`source_refs` on `AgentResult`/`QueryResponse` is the payload's `chunk_id`
field (content-hash-derived), not Qdrant's internal point ID, which this
module never exposes externally. `MentionsAgent`-derived turns have no
`source_refs` (client-gathered evidence is never stored server-side) — a
follow-up to such a turn falls back to conversation-history text only.

Set `force_fresh_retrieval: true` on a follow-up request to ignore
`conversation_history` for routing/agent selection and run the normal full
pipeline for that turn, while still recording it as part of the conversation.

### Clarification when a question is too broad

Both the router and individual agents can decide a question needs narrowing
before (or instead of) producing an answer:

- The router can set `clarification_needed`/`clarification_question` directly
  in its JSON response, before any agent runs, for an obviously unconstrained
  catalog-style question.
- `MetadataAgent` sets `AgentResult.needs_clarification` when more than
  `settings.metadata_narrowing_threshold` (default 50) distinct items match —
  it never dumps an oversized, unfiltered catalog listing into the synthesis
  prompt.

If **every** selected agent flags `needs_clarification`, `/api/query` returns
without a synthesis call:

```json
{
  "status": "needs_clarification",
  "clarification_message": "Found more than 50 matching items — try narrowing by year, author, or item type.",
  "query_plan": {"agents_to_use": ["metadata"], "filters": {"...": "..."}}
}
```

If only **some** agents flag it, synthesis still proceeds using the other
agents' usable content, with the flagged agent's message folded into the
synthesis prompt as a caveat instead of blocking the whole answer.

`NeedsClarificationError` and the existing `NeedsClientEvidenceError` (see
above) both extend `NeedsUserInputError` — one exception family, one
`QueryResponse.status` field, so a third future "needs more from the user"
case (e.g. disambiguating two same-named authors) doesn't need a new
wire-protocol shape.
```

- [ ] **Step 2: Commit**

```bash
git add docs/query-routing.md
git commit -m "docs: document the follow-up conversation and clarification protocols"
```

---

## Task 12: Plugin — `zotero-rag.js`: `submitQuery` passthrough + `formatTurnHTML`/`buildLibraryMap` extraction

**Files:**
- Modify: `plugin/src/zotero-rag.js:1115-1173` (`submitQuery`), `plugin/src/zotero-rag.js:1740-1810` (`formatNoteHTML`)
- Test: `plugin/test/zotero-rag.test.js`

- [ ] **Step 1: Check existing test file structure**

```bash
grep -n "loadZoteroRAG\|require(" plugin/test/zotero-rag.test.js | head -10
```

Use whatever vm-loading helper that file already defines (matching `plugin/test/mentions.test.js`'s `loadMentionSearch` pattern) — don't invent a new one.

- [ ] **Step 2: Write the failing tests**

Add to `plugin/test/zotero-rag.test.js`:

```js
test('submitQuery includes conversation_history in the payload when provided', async () => {
	/** @type {any} */
	let capturedBody = null;
	const fetchStub = async (url, opts) => {
		capturedBody = JSON.parse(opts.body);
		return { ok: true, json: async () => ({ answer: 'ok' }) };
	};
	const ZoteroRAG = loadZoteroRAG({ fetch: fetchStub });
	ZoteroRAG.backendURL = 'http://localhost:8119';

	const history = [{ question: 'Q0', answer: 'A0', agents_used: ['rag'], source_refs: ['c1'], query_plan: null }];
	await ZoteroRAG.submitQuery('Follow-up', ['1'], { conversationHistory: history });

	assert.deepStrictEqual(capturedBody.conversation_history, history);
});

test('submitQuery includes force_fresh_retrieval only when true', async () => {
	/** @type {any} */
	let capturedBody = null;
	const fetchStub = async (url, opts) => {
		capturedBody = JSON.parse(opts.body);
		return { ok: true, json: async () => ({ answer: 'ok' }) };
	};
	const ZoteroRAG = loadZoteroRAG({ fetch: fetchStub });
	ZoteroRAG.backendURL = 'http://localhost:8119';

	await ZoteroRAG.submitQuery('Q', ['1'], {});
	assert.strictEqual(capturedBody.force_fresh_retrieval, undefined);

	await ZoteroRAG.submitQuery('Q', ['1'], { forceFreshRetrieval: true });
	assert.strictEqual(capturedBody.force_fresh_retrieval, true);
});

test('formatTurnHTML renders question heading, answer, and bibliography without the outer wrapper', () => {
	const ZoteroRAG = loadZoteroRAG();
	const result = {
		answer: 'The answer.',
		answer_format: 'text',
		sources: [],
	};
	const html = ZoteroRAG.formatTurnHTML('A follow-up question?', result, new Map());
	assert.ok(html.includes('A follow-up question?'));
	assert.ok(html.includes('The answer.'));
	assert.ok(!html.includes('Generated:'));  // metadata footer belongs to formatNoteHTML only
});
```

(If `loadZoteroRAG` doesn't already accept a stub-injection object shaped like `{ fetch, ... }`, adapt the stub construction to match whatever mechanism the existing file uses for stubbing `fetch`/`Zotero` — check `plugin/test/remote_indexer.test.js` for the closest existing precedent, since `remote_indexer.js` also calls `fetch()`.)

- [ ] **Step 3: Run tests to verify they fail**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: FAIL — `capturedBody.conversation_history` is `undefined`, and `ZoteroRAG.formatTurnHTML is not a function`

- [ ] **Step 4: Implement**

Modify `submitQuery` in `plugin/src/zotero-rag.js` (inside the payload-building block, alongside the existing `options.clientEvidence`/`options.queryPlan` handling around line 1147-1152):

```js
			if (options.clientEvidence !== undefined) {
				payload.client_evidence = options.clientEvidence;
			}
			if (options.queryPlan !== undefined) {
				payload.query_plan = options.queryPlan;
			}
			if (options.conversationHistory !== undefined) {
				payload.conversation_history = options.conversationHistory;
			}
			if (options.forceFreshRetrieval) {
				payload.force_fresh_retrieval = true;
			}
```

Also update the `QueryOptions` JSDoc typedef (search for `@typedef {Object} QueryOptions` near the top of the file) to add:

```js
 * @property {Array<Object>} [conversationHistory] - Prior follow-up turns to echo back
 * @property {boolean} [forceFreshRetrieval] - Ignore conversationHistory for this turn's routing
```

Refactor `formatNoteHTML` (lines 1740-1810): extract the question-heading/answer/bibliography portion into a new `formatTurnHTML` method, and have `formatNoteHTML` call it:

```js
	/**
	 * Format one Q&A turn (heading + answer + bibliography) as an HTML fragment,
	 * with no outer wrapper and no metadata footer — reused by formatNoteHTML()
	 * for the first turn and by ChatPane for every follow-up turn appended later.
	 * @param {string} question
	 * @param {QueryResult} result
	 * @param {Map<string, LibraryInfo>} libraryMap
	 * @returns {string} HTML fragment
	 */
	formatTurnHTML(question, result, libraryMap) {
		let html = `<h2>${this.escapeHTML(question)}</h2>`;
		html += `<p><strong>Answer:</strong></p>`;

		let answerHTML = '';
		if (result.answer_format === 'html') {
			answerHTML = this.replaceCitationsInText(result.answer, result.sources || [], libraryMap);
		} else {
			const escapedAnswer = this.escapeHTML(result.answer);
			answerHTML = `<p>${this.replaceCitationsInText(escapedAnswer, result.sources || [], libraryMap)}</p>`;
		}
		html += this.mergeConsecutiveCitations(answerHTML);
		html += this.formatBibliographyHTML(result.sources || [], libraryMap);
		return html;
	}

	/**
	 * Build a library-ID → {name, type} map for the given backend library IDs,
	 * annotated with document counts when available. Shared by formatNoteHTML()
	 * and ChatPane's note-append formatter.
	 * @param {Array<string>} libraryIDs
	 * @param {Record<string, number>} [libraryDocumentCounts]
	 * @returns {Map<string, LibraryInfo>}
	 */
	buildLibraryMap(libraryIDs, libraryDocumentCounts = {}) {
		/** @type {Map<string, LibraryInfo>} */
		const libraryMap = new Map();
		const libraries = this.getLibraries();
		for (let id of libraryIDs) {
			const lib = libraries.find((/** @type {Library} */ l) => l.id === id);
			if (lib) {
				libraryMap.set(id, { name: lib.name, type: lib.type });
			}
		}
		return libraryMap;
	}

	/**
	 * Format the query result as HTML for the note.
	 * @param {string} question - Original question
	 * @param {QueryResult} result - Query result
	 * @param {Array<string>} libraryIDs - Libraries that were queried
	 * @returns {string} HTML content
	 */
	formatNoteHTML(question, result, libraryIDs) {
		const timestamp = new Date().toLocaleString();
		const libraryMap = this.buildLibraryMap(libraryIDs, result.library_document_counts);

		const counts = result.library_document_counts || {};
		const libraryNames = Array.from(libraryMap.entries()).map(([id, info]) => {
			const n = counts[id];
			return n ? `${info.name} (${n} documents)` : info.name;
		}).join(', ');

		let html = `<div>`;
		html += this.formatTurnHTML(question, result, libraryMap);

		// Add metadata
		html += `<hr/>`;
		html += `<p style="font-size: 0.9em; color: #666;">`;
		html += `<em>Generated: ${timestamp}<br/>`;
		html += `Libraries: ${this.escapeHTML(libraryNames)}<br/>`;
		if (result.model_name) {
			html += `Model: ${this.escapeHTML(result.model_name)}<br/>`;
		}
		if (result.agents_used && result.agents_used.length > 0) {
			html += `Agents: ${this.escapeHTML(result.agents_used.join(', '))}<br/>`;
		}
		html += `Plugin: v${this.escapeHTML(this.version)}`;
		html += `</em></p>`;

		if (result.trace) {
			html += `<hr/>`;
			html += `<p><strong>Debugging Trace</strong></p>`;
			html += `<pre style="font-size:0.8em; white-space:pre-wrap; word-break:break-all; background:#f5f5f5; padding:8px; border-radius:4px;">${this.escapeHTML(JSON.stringify(result.trace, null, 2))}</pre>`;
		}

		html += `</div>`;

		return html;
	}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `node --test plugin/test/zotero-rag.test.js`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add plugin/src/zotero-rag.js plugin/test/zotero-rag.test.js
git commit -m "feat: submitQuery conversation passthrough, extract formatTurnHTML/buildLibraryMap"
```

---

## Task 13: Plugin — `chat-pane.js` (pure conversation-state logic)

**Files:**
- Create: `plugin/src/chat-pane.js`
- Test: `plugin/test/chat-pane.test.js` (new file)

This task covers the Zotero-independent half of the module (state storage, payload building) — testable in a bare `vm` context, same technique as `plugin/test/task_queue.test.js`. Task 14 adds the `Zotero`-dependent half (registration, DOM, trash button) to the same file.

- [ ] **Step 1: Write the failing tests**

Create `plugin/test/chat-pane.test.js`:

```js
// Tests for plugin/src/chat-pane.js's conversation-state logic — the part
// with no Zotero dependency. Loaded into a bare vm context, same technique
// as plugin/test/task_queue.test.js. Zotero.ItemPaneManager registration and
// DOM rendering are covered separately (manual verification, not unit tests
// — see docs/superpowers/plans/2026-07-22-note-followup-chat.md Task 14).

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'chat-pane.js');

/**
 * @param {any} [zoteroStub]
 * @returns {any} the ChatPane object
 */
function loadChatPane(zoteroStub = { ItemPaneManager: { registerSection: () => {} }, Items: { trashTx: () => {} } }) {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = { Zotero: zoteroStub, console };
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'chat-pane.js' });
	return context.ChatPane;
}

test('seedConversation stores library ids and turns for a note', () => {
	const ChatPane = loadChatPane();
	ChatPane.seedConversation(101, ['1'], [{ question: 'Q0', answer: 'A0', agents_used: ['rag'], source_refs: ['c1'], query_plan: null }]);

	assert.deepStrictEqual(ChatPane.getTurns(101), [
		{ question: 'Q0', answer: 'A0', agents_used: ['rag'], source_refs: ['c1'], query_plan: null },
	]);
});

test('getTurns returns an empty array for an unseeded note', () => {
	const ChatPane = loadChatPane();
	assert.deepStrictEqual(ChatPane.getTurns(999), []);
});

test('recordTurn appends to an existing conversation', () => {
	const ChatPane = loadChatPane();
	ChatPane.seedConversation(101, ['1'], [{ question: 'Q0', answer: 'A0', agents_used: [], source_refs: [], query_plan: null }]);
	ChatPane.recordTurn(101, ['1'], { question: 'Q1', answer: 'A1', agents_used: [], source_refs: [], query_plan: null });

	assert.strictEqual(ChatPane.getTurns(101).length, 2);
	assert.strictEqual(ChatPane.getTurns(101)[1].question, 'Q1');
});

test('recordTurn on a note with no prior conversation starts a new one', () => {
	const ChatPane = loadChatPane();
	ChatPane.recordTurn(202, ['1'], { question: 'Q0', answer: 'A0', agents_used: [], source_refs: [], query_plan: null });

	assert.strictEqual(ChatPane.getTurns(202).length, 1);
});

test('buildFollowUpPayload reads the stored library ids and turns', () => {
	const ChatPane = loadChatPane();
	ChatPane.seedConversation(101, ['1', '2'], [{ question: 'Q0', answer: 'A0', agents_used: [], source_refs: [], query_plan: null }]);

	const payload = ChatPane.buildFollowUpPayload(101, 'Follow-up');

	assert.strictEqual(payload.question, 'Follow-up');
	assert.deepStrictEqual(payload.libraryIds, ['1', '2']);
	assert.strictEqual(payload.conversationHistory.length, 1);
	assert.strictEqual(payload.forceFreshRetrieval, false);
});

test('buildFollowUpPayload sets forceFreshRetrieval when requested', () => {
	const ChatPane = loadChatPane();
	ChatPane.seedConversation(101, ['1'], []);
	const payload = ChatPane.buildFollowUpPayload(101, 'Q', { forceFresh: true });
	assert.strictEqual(payload.forceFreshRetrieval, true);
});

test('buildFollowUpPayload on an unseeded note returns empty history and libraryIds', () => {
	const ChatPane = loadChatPane();
	const payload = ChatPane.buildFollowUpPayload(999, 'Q');
	assert.deepStrictEqual(payload.libraryIds, []);
	assert.deepStrictEqual(payload.conversationHistory, []);
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test plugin/test/chat-pane.test.js`
Expected: FAIL — `Cannot find module '.../src/chat-pane.js'`

- [ ] **Step 3: Implement (pure-logic half only — the Zotero-dependent half is added in Task 14)**

Create `plugin/src/chat-pane.js`:

```js
// Follow-up chat panel attached to RAG-result notes via Zotero.ItemPaneManager.
// See docs/superpowers/specs/2026-07-22-note-followup-chat-design.md.
//
// Conversation state is session-only and lives entirely in this module's
// in-memory map — nothing is persisted beyond the note's own saved HTML
// (appended per turn by ZoteroRAG.formatTurnHTML(), see zotero-rag.js).
// A Zotero restart or plugin reload loses live continuation state; the next
// follow-up on that note just starts with empty history (full routing runs,
// no error — see docs/query-routing.md's "Follow-up conversations" section).

/**
 * @typedef {Object} ChatTurn
 * @property {string} question
 * @property {string} answer
 * @property {string[]} agents_used
 * @property {string[]} source_refs
 * @property {Object|null} query_plan
 */

var ChatPane = {
	/** @type {Map<number, {libraryIds: string[], turns: ChatTurn[]}>} */
	_conversations: new Map(),

	/**
	 * Seed (or replace) the conversation for a note — called once right after
	 * ZoteroRAG.createResultNote() saves the note, using the first turn's own
	 * result so continuation context (source_refs, query_plan) is available
	 * immediately, with no need to parse it back out of the note's HTML.
	 * @param {number} noteID
	 * @param {string[]} libraryIds
	 * @param {ChatTurn[]} turns
	 */
	seedConversation(noteID, libraryIds, turns) {
		this._conversations.set(noteID, { libraryIds: libraryIds.slice(), turns: turns.slice() });
	},

	/**
	 * @param {number} noteID
	 * @returns {ChatTurn[]}
	 */
	getTurns(noteID) {
		const conv = this._conversations.get(noteID);
		return conv ? conv.turns.slice() : [];
	},

	/**
	 * Append a turn, starting a new conversation entry if none exists yet.
	 * @param {number} noteID
	 * @param {string[]} libraryIds
	 * @param {ChatTurn} turn
	 */
	recordTurn(noteID, libraryIds, turn) {
		let conv = this._conversations.get(noteID);
		if (!conv) {
			conv = { libraryIds: libraryIds.slice(), turns: [] };
			this._conversations.set(noteID, conv);
		}
		conv.turns.push(turn);
	},

	/**
	 * Build the /api/query follow-up request payload from stored conversation state.
	 * @param {number} noteID
	 * @param {string} question
	 * @param {{forceFresh?: boolean}} [opts]
	 * @returns {{question: string, libraryIds: string[], conversationHistory: ChatTurn[], forceFreshRetrieval: boolean}}
	 */
	buildFollowUpPayload(noteID, question, { forceFresh = false } = {}) {
		const conv = this._conversations.get(noteID);
		return {
			question,
			libraryIds: conv ? conv.libraryIds.slice() : [],
			conversationHistory: conv ? conv.turns.slice() : [],
			forceFreshRetrieval: !!forceFresh,
		};
	},
};
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test plugin/test/chat-pane.test.js`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add plugin/src/chat-pane.js plugin/test/chat-pane.test.js
git commit -m "feat: add ChatPane conversation-state logic"
```

---

## Task 14: Plugin — `chat-pane.js` (Zotero glue: registerSection, submitFollowUp, trash button)

**Files:**
- Modify: `plugin/src/chat-pane.js` (same file as Task 13)
- Test: `plugin/test/chat-pane.test.js`

- [ ] **Step 1: Write the failing tests**

Add to `plugin/test/chat-pane.test.js`:

```js
test('init registers an item pane section with a trash button', () => {
	/** @type {any[]} */
	const registered = [];
	const zoteroStub = {
		ItemPaneManager: { registerSection: (opts) => registered.push(opts) },
		Items: { trashTx: () => {} },
	};
	const ChatPane = loadChatPane(zoteroStub);

	ChatPane.init({ pluginID: 'zotero-rag@example.com' });

	assert.strictEqual(registered.length, 1);
	assert.strictEqual(registered[0].paneID, 'zotero-rag-chat');
	assert.strictEqual(registered[0].pluginID, 'zotero-rag@example.com');
	assert.strictEqual(registered[0].sectionButtons.length, 1);
	assert.strictEqual(registered[0].sectionButtons[0].type, 'zotero-rag-trash-note');
});

test('the trash section button calls Zotero.Items.trashTx with the item id', () => {
	/** @type {any[]} */
	const registered = [];
	/** @type {number[][]} */
	const trashedCalls = [];
	const zoteroStub = {
		ItemPaneManager: { registerSection: (opts) => registered.push(opts) },
		Items: { trashTx: (ids) => trashedCalls.push(ids) },
	};
	const ChatPane = loadChatPane(zoteroStub);
	ChatPane.init({ pluginID: 'zotero-rag@example.com' });

	registered[0].sectionButtons[0].onClick({ item: { id: 42 } });

	assert.deepStrictEqual(trashedCalls, [[42]]);
});

test('onItemChange enables the section only for notes tagged RAG Query Result', () => {
	/** @type {any[]} */
	const registered = [];
	const zoteroStub = {
		ItemPaneManager: { registerSection: (opts) => registered.push(opts) },
		Items: { trashTx: () => {} },
	};
	const ChatPane = loadChatPane(zoteroStub);
	ChatPane.init({ pluginID: 'zotero-rag@example.com' });

	/** @type {boolean[]} */
	const enabledCalls = [];
	const setEnabled = (v) => enabledCalls.push(v);

	const taggedNote = { isNote: () => true, hasTag: (t) => t === 'RAG Query Result' };
	registered[0].onItemChange({ item: taggedNote, setEnabled });
	assert.strictEqual(enabledCalls[0], true);

	const untaggedNote = { isNote: () => true, hasTag: () => false };
	registered[0].onItemChange({ item: untaggedNote, setEnabled });
	assert.strictEqual(enabledCalls[1], false);

	registered[0].onItemChange({ item: null, setEnabled });
	assert.strictEqual(enabledCalls[2], false);
});

test('submitFollowUp records the turn and appends it to the note', async () => {
	const zoteroStub = {
		ItemPaneManager: { registerSection: () => {} },
		Items: { trashTx: () => {} },
	};
	const ChatPane = loadChatPane(zoteroStub);
	ChatPane.seedConversation(101, ['1'], []);

	/** @type {any[]} */
	const submittedOptions = [];
	const fakeZoteroRAG = {
		submitQuery: async (question, libraryIds, options) => {
			submittedOptions.push(options);
			return {
				status: 'complete', answer: 'The answer.', answer_format: 'text',
				sources: [], agents_used: ['continuation'], source_refs: ['c1'], query_plan: null,
			};
		},
		formatTurnHTML: () => '<h2>Q</h2><p>The answer.</p>',
	};
	/** @type {string[]} */
	const notedHtml = [];
	const noteStub = {
		id: 101,
		getNote: () => '<div>existing</div>',
		setNote: (html) => notedHtml.push(html),
		saveTx: async () => {},
	};

	const result = await ChatPane.submitFollowUp(fakeZoteroRAG, noteStub, 'Follow-up question');

	assert.strictEqual(result.answer, 'The answer.');
	assert.strictEqual(submittedOptions[0].conversationHistory.length, 0);
	assert.strictEqual(ChatPane.getTurns(101).length, 1);
	assert.strictEqual(ChatPane.getTurns(101)[0].question, 'Follow-up question');
	assert.strictEqual(notedHtml.length, 1);
	assert.ok(notedHtml[0].includes('The answer.'));
});

test('submitFollowUp records a needs_clarification turn using the clarification message as the answer', async () => {
	const zoteroStub = { ItemPaneManager: { registerSection: () => {} }, Items: { trashTx: () => {} } };
	const ChatPane = loadChatPane(zoteroStub);
	ChatPane.seedConversation(101, ['1'], []);

	const fakeZoteroRAG = {
		submitQuery: async () => ({
			status: 'needs_clarification', answer: '', answer_format: 'text', sources: [],
			agents_used: [], source_refs: [], query_plan: { agents_to_use: ['metadata'] },
			clarification_message: 'Please narrow by year.',
		}),
		formatTurnHTML: () => '<p>Please narrow by year.</p>',
	};
	const noteStub = { id: 101, getNote: () => '', setNote: () => {}, saveTx: async () => {} };

	await ChatPane.submitFollowUp(fakeZoteroRAG, noteStub, 'What has Luhmann written?');

	const turn = ChatPane.getTurns(101)[0];
	assert.strictEqual(turn.answer, 'Please narrow by year.');
	assert.deepStrictEqual(turn.source_refs, []);
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test plugin/test/chat-pane.test.js`
Expected: FAIL — `ChatPane.init is not a function`

- [ ] **Step 3: Implement**

Append to `plugin/src/chat-pane.js` (after the object literal from Task 13 — convert the trailing `};` to add these as properties, or append via `ChatPane.x = function... ` style; use the latter for a minimal diff against Task 13's version):

```js
/**
 * Register the item-pane section. Called once from bootstrap.js after
 * ZoteroRAG.init() (this module's registerSection call doesn't depend on
 * ZoteroRAG itself — only on the plugin id it was given at startup).
 * @param {{pluginID: string}} opts
 */
ChatPane.init = function ({ pluginID }) {
	Zotero.ItemPaneManager.registerSection({
		paneID: 'zotero-rag-chat',
		pluginID,
		header: { l10nID: 'zotero-rag-chat-header', icon: 'chrome://zotero-rag/content/icons/chat16.svg' },
		sidenav: { l10nID: 'zotero-rag-chat-sidenav', icon: 'chrome://zotero-rag/content/icons/chat20.svg' },
		onItemChange: ({ item, setEnabled }) => {
			setEnabled(!!item && item.isNote() && item.hasTag('RAG Query Result'));
		},
		onRender: ({ body, item }) => ChatPane._render(body, item),
		sectionButtons: [
			{
				type: 'zotero-rag-trash-note',
				icon: 'chrome://zotero/skin/16/universal/trash.svg',
				l10nID: 'zotero-rag-chat-trash-button',
				onClick: ({ item }) => Zotero.Items.trashTx([item.id]),
			},
		],
	});
};

/**
 * Render the transcript + input box into the section body. DOM-only — not
 * unit tested (see plugin/test/chat-pane.test.js's header comment);
 * verified manually per this plan's final task.
 * @param {Element} body
 * @param {any} item
 */
ChatPane._render = function (body, item) {
	body.textContent = '';
	const doc = body.ownerDocument;

	const transcript = doc.createElement('div');
	for (const turn of ChatPane.getTurns(item.id)) {
		const q = doc.createElement('p');
		q.textContent = `Q: ${turn.question}`;
		const a = doc.createElement('p');
		a.textContent = `A: ${turn.answer}`;
		transcript.appendChild(q);
		transcript.appendChild(a);
	}
	body.appendChild(transcript);

	const input = doc.createElement('textarea');
	body.appendChild(input);

	const askButton = doc.createElement('button');
	askButton.textContent = 'Ask follow-up';
	askButton.addEventListener('click', async () => {
		const question = input.value.trim();
		if (!question) return;
		input.value = '';
		await ChatPane.submitFollowUp(ZoteroRAG, item, question);
		ChatPane._render(body, item);
	});
	body.appendChild(askButton);

	const freshButton = doc.createElement('button');
	freshButton.textContent = 'Start fresh search';
	freshButton.addEventListener('click', async () => {
		const question = input.value.trim();
		if (!question) return;
		input.value = '';
		await ChatPane.submitFollowUp(ZoteroRAG, item, question, { forceFresh: true });
		ChatPane._render(body, item);
	});
	body.appendChild(freshButton);
};

/**
 * Submit a follow-up turn, handling the needs_client_evidence two-phase
 * round trip the same way dialog.js does for the very first question, then
 * record and persist the turn.
 * @param {any} zoteroRAG - ZoteroRAG (passed explicitly for testability)
 * @param {any} note
 * @param {string} question
 * @param {{forceFresh?: boolean}} [opts]
 * @returns {Promise<any>} the final QueryResponse
 */
ChatPane.submitFollowUp = async function (zoteroRAG, note, question, { forceFresh = false } = {}) {
	const payload = ChatPane.buildFollowUpPayload(note.id, question, { forceFresh });

	let result = await zoteroRAG.submitQuery(question, payload.libraryIds, {
		conversationHistory: payload.conversationHistory,
		forceFreshRetrieval: payload.forceFreshRetrieval,
	});

	if (result.status === 'needs_client_evidence') {
		const zoteroLibraryIDs = payload.libraryIds
			.map((/** @type {string} */ id) => zoteroRAG._resolveZoteroLibraryID(id))
			.filter((/** @type {number|null} */ id) => id !== null);
		const evidence = await MentionSearch.findMentionEvidence(result.citation_targets, zoteroLibraryIDs);
		result = await zoteroRAG.submitQuery(question, payload.libraryIds, {
			conversationHistory: payload.conversationHistory,
			forceFreshRetrieval: payload.forceFreshRetrieval,
			clientEvidence: evidence,
			queryPlan: result.query_plan,
		});
	}

	const turn = {
		question,
		answer: result.status === 'needs_clarification' ? result.clarification_message : result.answer,
		agents_used: result.agents_used || [],
		source_refs: result.source_refs || [],
		query_plan: result.query_plan || null,
	};
	ChatPane.recordTurn(note.id, payload.libraryIds, turn);

	const libraryMap = zoteroRAG.buildLibraryMap(payload.libraryIds);
	const turnHtml = zoteroRAG.formatTurnHTML(question, result, libraryMap);
	note.setNote(note.getNote() + turnHtml);
	await note.saveTx();

	return result;
};
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test plugin/test/chat-pane.test.js`
Expected: PASS (12 tests total across Tasks 13 and 14)

- [ ] **Step 5: Commit**

```bash
git add plugin/src/chat-pane.js plugin/test/chat-pane.test.js
git commit -m "feat: register ChatPane item-pane section with trash button and submitFollowUp"
```

---

## Task 15: Plugin — `createResultNote()` seeds the chat, `bootstrap.js` loads the new module

**Files:**
- Modify: `plugin/src/zotero-rag.js:1183-1220` (`createResultNote`)
- Modify: `plugin/src/bootstrap.js`
- Modify: `plugin/locale/en-US/zotero-rag.ftl`

- [ ] **Step 1: Modify `createResultNote()`**

In `plugin/src/zotero-rag.js`, change the note-opening call and add the seed call (replacing the `zoteroPane.openNoteWindow` block at the end of the method):

```js
		note.addTag('RAG Query Result');
		// Save note
		await note.saveTx();
		await this._ensureRAGResultsSearch(note.libraryID);

		ChatPane.seedConversation(note.id, libraryIDs, [{
			question,
			answer: result.answer,
			agents_used: result.agents_used || [],
			source_refs: result.source_refs || [],
			query_plan: result.query_plan || null,
		}]);

		// Select the note in the main library view so the item pane — and the
		// new chat section — is visible. A standalone note window (Zotero's own
		// "New Note Window" command) has no item pane, so it can't show ours;
		// this is why we no longer open one automatically here.
		zoteroPane.selectItem(note.id);

		return note;
	}
```

(This removes the previous `zoteroPane.openNoteWindow(note.id)` / `findNoteWindow` / `resizeTo` lines.)

- [ ] **Step 2: Load `mentions.js` and `chat-pane.js` eagerly, call `ChatPane.init()`**

In `plugin/src/bootstrap.js`, insert two new `loadSubScript` calls and one `init()` call:

```js
	Services.scriptloader.loadSubScript(rootURI + 'toolkit.bundle.js');

	// Eager, plugin-lifetime scripts — loaded once at startup, not per dialog window.
	Services.scriptloader.loadSubScript(rootURI + 'task_queue.js');
	Services.scriptloader.loadSubScript(rootURI + 'mentions.js');
	Services.scriptloader.loadSubScript(rootURI + 'chat-pane.js');
	Services.scriptloader.loadSubScript(rootURI + 'zotero-rag.js');
	Services.scriptloader.loadSubScript(rootURI + 'preferences.js');
	ZoteroRAG.init({ id, version, rootURI });
	ChatPane.init({ pluginID: id });
```

(`mentions.js` is separately still loaded inside `dialog.xhtml` for that window's own scope — leave that load as-is; this adds a second, independent load into the plugin-lifetime scope that `chat-pane.js` needs.)

- [ ] **Step 3: Add the new localization strings**

In `plugin/locale/en-US/zotero-rag.ftl`, add:

```fluent
zotero-rag-chat-header = Follow-up Chat
zotero-rag-chat-sidenav =
    .tooltiptext = Follow-up Chat
zotero-rag-chat-trash-button =
    .tooltiptext = Move this note to Trash
```

- [ ] **Step 4: Manually sanity-check the load order doesn't throw**

Run: `npm start` (per this repo's "Live Server" instructions in root `CLAUDE.md`) and check `logs/server.log` / the Zotero Browser Console for any `ReferenceError`/`TypeError` during plugin startup — `chat-pane.js`'s top-level code only defines `ChatPane`, it doesn't call `Zotero.ItemPaneManager.registerSection` until `ChatPane.init()` runs from `bootstrap.js`, so there's no load-order dependency on `Zotero.ItemPaneManager` existing at parse time.

- [ ] **Step 5: Commit**

```bash
git add plugin/src/zotero-rag.js plugin/src/bootstrap.js plugin/locale/en-US/zotero-rag.ftl
git commit -m "feat: seed chat conversation on note creation, load chat-pane.js at plugin scope"
```

---

## Task 16: Manual end-to-end verification

**No new files — this task exercises the whole feature live**, per root `CLAUDE.md`'s "Live Query Debugging" section and this plan's own architecture. Use the running dev backend (`npm start`) and dev Zotero (`zotero-plugin dev` / hot-reload).

- [ ] **Step 1: Run the full automated suites one more time before manual testing**

```bash
uv run pytest backend/tests/ -k "not container" -q
node --test plugin/test/
```

Expected: PASS

- [ ] **Step 2: Drill-down follow-up**

Ask a question that gets a multi-source answer, confirm the note is created and selected in the main pane (not a popup window), confirm the "Follow-up Chat" section appears in the item pane, ask a follow-up like "tell me more about the second source" and confirm the answer is specific to that source and returns quickly (check `backend/`'s console/log output to confirm no embedding/routing LLM call happened for that turn — only the synthesis call).

- [ ] **Step 3: Too-broad clarification**

Ask an intentionally unconstrained question (e.g. "What has \<a prolific author in your test library\> written about?") and confirm a `needs_clarification` response renders as an assistant turn asking to narrow it; reply with a narrower question and confirm it resolves normally.

- [ ] **Step 4: Backend restart mid-conversation**

Start a conversation, stop and restart the local backend (`npm start`), ask a follow-up, and confirm it still works (re-fetches evidence by `chunk_id` from Qdrant — no in-memory backend state to lose).

- [ ] **Step 5: Zotero restart fallback**

Restart the dev Zotero instance, reselect an existing "RAG Query Result" note from a previous run, confirm the chat section shows an empty transcript with just the input box, and confirm a follow-up there still works (falls back to a full fresh query, no error).

- [ ] **Step 6: Trash button**

Click the trash icon in the section header on a result note; confirm it lands in Zotero's Trash (Edit → Undo or the Trash collection shows it, recoverable) and the item pane cleanly deselects.

- [ ] **Step 7: Report results**

If any step fails, use `superpowers:systematic-debugging` before making fixes rather than guessing. Once all six checks pass, this plan is complete.
