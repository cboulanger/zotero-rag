# Citation/Mention Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Answer questions like "which publications cite Wiethölter (1975) and Teubner's Globale Bukowina?" by adding a new `mentions` query-routing agent that is fed evidence gathered from the Zotero client's own local full-text search index, instead of the backend's Qdrant store.

**Architecture:** The backend never gets new retrieval code for this — it cannot see citation text the server-side extraction pipeline doesn't index for this purpose. Instead: (1) the router is taught to recognise "who cites X" questions and extract structured `citation_targets` (author/year/title, kept strictly separate from `authors` = "written by"); (2) a new `mentions` agent formats evidence but cannot retrieve it itself, so the orchestrator short-circuits with a `NeedsClientEvidenceError` when that evidence is missing; (3) `/api/query` surfaces this as a `status: "needs_client_evidence"` response; (4) the plugin scans Zotero's local `fulltextWord` index + `.zotero-ft-cache` files for snippets and resubmits the same plan (no second routing LLM call) with `client_evidence` attached.

**Tech Stack:** Python/FastAPI/Pydantic (backend), plain JS loaded via `Services.scriptloader` in Zotero's chrome environment (plugin), `unittest`/`pytest` and Node's built-in `node:test` (tests).

---

## Context for the implementing engineer

This plan was derived from empirical probing against a live ~19k-item Zotero library (see conversation history — not reproduced here). Key findings that shaped the design, so you don't re-litigate them:

- Zotero's client-side full-text index (`fulltextWords`/`fulltextItemWords` tables + one `.zotero-ft-cache` plain-text file per attachment) is real, fast (0.26s to scan 291 cache files), and already exposed to plugins via `Zotero.Search`'s `fulltextWord` condition and `Zotero.FullText.getItemCacheFile()`/`getPages()`. The backend has no equivalent — Qdrant chunks are optimized for semantic search, not lexical citation lookup, and adding a text index there was considered and rejected in favor of using data that already exists client-side.
- Raw full-text is too large to ship in a request (up to 500KB/doc); short snippets around each match are not (~7k tokens for a realistic 2-term query). The LLM at synthesis time — not a hand-built ranking algorithm — is what tells a real citation ("Wiethölter, Zum Fortbildungsrecht...") apart from an unrelated use of the same word (Baer's "Bukowina" meaning the historical region, not Teubner's article titled that). Snippets make that judgment possible; a bare match-count index cannot.
- Two false positives specifically must be prevented: (1) a cited author's name leaking into the `authors` filter (which means "written by", inverting the query), and (2) the cited work itself showing up in the "who cites it" answer (self-citation). Both are handled explicitly below.
- Year-proximity filtering ("Wiethölter" within N chars of "1975") was tested and rejected — short-form legal citations frequently omit or vary the year, and it cuts recall hard for weak precision gain. `year` is carried as an LLM disambiguation hint only, never used to filter.

## File Structure

Backend (`backend/`):
- `models/filters.py` — add `CitationTarget`, extend `MetadataFilters`
- `services/base_agent.py` — add `NeedsClientEvidenceError`
- `services/query_router.py` — extend prompt + parsing for `citation_targets`
- `services/mentions_agent.py` — **new** — `MentionsAgent`, `ClientEvidence`/`MentionEvidenceItem`/`TargetMatch` wire models, evidence formatting
- `services/query_orchestrator.py` — register `MentionsAgent`, add `client_evidence`/`preset_plan` params, short-circuit logic
- `api/query.py` — request/response fields, exception→response mapping

Plugin (`plugin/src/`):
- `mentions.js` — **new** — pure evidence-extraction functions + `findMentionEvidence()` Zotero orchestration, exposed as `var MentionSearch`
- `dialog.xhtml` — load `mentions.js` before `dialog.js`
- `zotero-rag.js` — `submitQuery()` gains `clientEvidence`/`queryPlan` options
- `dialog.js` — two-phase submit flow + `resolveZoteroLibraryID()` helper

Tests:
- `backend/tests/test_query_router.py` (extend)
- `backend/tests/test_mentions_agent.py` (new)
- `backend/tests/test_orchestrator.py` (extend)
- `backend/tests/test_query_api.py` (extend)
- `plugin/test/mentions.test.js` (new)
- `plugin/test/dialog.test.js` (extend)

Docs:
- `docs/query-routing.md` (extend)

---

### Task 1: `CitationTarget` model + router extraction

**Files:**
- Modify: `backend/models/filters.py`
- Modify: `backend/services/query_router.py`
- Test: `backend/tests/test_query_router.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_query_router.py`, inside `class TestQueryRouterRoute(unittest.IsolatedAsyncioTestCase):` (after `test_extracts_title_keywords`):

```python
    async def test_extracts_citation_targets(self):
        router = _make_router(
            '{"agents": ["mentions"], "authors": [], '
            '"citation_targets": ['
            '{"author": "wiethölter", "year": 1975, "title_keywords": []}, '
            '{"author": "teubner", "title_keywords": ["bukowina"]}'
            ']}'
        )
        agents = [_make_agent("mentions", "citation search")]
        plan = await router.route(
            "Which publications cite Wiethölter (1975) and Teubner's Globale Bukowina?", agents
        )
        self.assertEqual(len(plan.filters.citation_targets), 2)
        self.assertEqual(plan.filters.citation_targets[0].author, "wiethölter")
        self.assertEqual(plan.filters.citation_targets[0].year, 1975)
        self.assertEqual(plan.filters.citation_targets[1].title_keywords, ["bukowina"])
        self.assertEqual(plan.filters.authors, [])

    async def test_missing_citation_targets_defaults_to_empty(self):
        router = _make_router('{"agents": ["rag"]}')
        agents = [_make_agent("rag", "semantic")]
        plan = await router.route("What is autopoiesis?", agents)
        self.assertEqual(plan.filters.citation_targets, [])

    async def test_ignores_malformed_citation_target_entries(self):
        router = _make_router(
            '{"agents": ["mentions"], '
            '"citation_targets": [{"year": 1975}, "not a dict", {"author": "teubner"}]}'
        )
        agents = [_make_agent("mentions", "citation search")]
        plan = await router.route("q", agents)
        self.assertEqual(len(plan.filters.citation_targets), 1)
        self.assertEqual(plan.filters.citation_targets[0].author, "teubner")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_query_router.py -v -k citation`
Expected: FAIL — `AttributeError: 'MetadataFilters' object has no attribute 'citation_targets'` (or similar)

- [ ] **Step 3: Add `CitationTarget` and extend `MetadataFilters`**

In `backend/models/filters.py`, add above `class MetadataFilters(BaseModel):`:

```python
class CitationTarget(BaseModel):
    """A work the question asks about being CITED/DISCUSSED by other items —
    not authored by. Kept structurally separate from MetadataFilters.authors
    (which means "written by") so the router cannot conflate the two."""

    author: str                     # lowercase surname
    year: Optional[int] = None      # LLM disambiguation hint only — never used to filter
    title_keywords: list[str] = []  # salient words from the cited work's title
```

Then extend `MetadataFilters`:

```python
class MetadataFilters(BaseModel):
    """Bibliographic metadata filters applied during vector search and catalog lookup."""

    year_min: Optional[int] = None
    year_max: Optional[int] = None
    authors: list[str] = []
    item_types: list[str] = []
    title_keywords: list[str] = []
    tags: list[str] = []
    citation_targets: list[CitationTarget] = []

    def is_empty(self) -> bool:
        return not any([
            self.year_min is not None,
            self.year_max is not None,
            self.authors,
            self.item_types,
            self.title_keywords,
            self.tags,
            self.citation_targets,
        ])
```

- [ ] **Step 4: Extend the router prompt and parsing**

In `backend/services/query_router.py`, update the import line:

```python
from backend.models.filters import CitationTarget, MetadataFilters
```

Add a new paragraph to `_ROUTING_GUIDANCE` (after the existing "Combine both agents..." line, before "Default when uncertain..."):

```python
_ROUTING_GUIDANCE = """
General guidance:
- Include "rag" for most questions — it reads document content to answer.
- Use "metadata" ONLY when the question is about the library catalog itself:
  e.g. "What papers by Smith are in my library?", "Show me books from 2010–2015."
  DO NOT use "metadata" alone for questions about real-world topics, concepts,
  organisations, events, or arguments — even if they use "listing" or "exist" language.
  Those questions require "rag" to read document content.
- Combine both agents only when the question BOTH lists catalog items AND asks about content.
- Use "mentions" when the question asks which publications CITE, DISCUSS, RESPOND TO, or
  MENTION a specific named work — as opposed to questions about work BY that person.
  Populate citation_targets with one entry per cited work (author surname, optional year,
  optional distinctive title keywords) and leave the cited author OUT of "authors".
  Example: "Which publications cite Wiethölter's 1975 article and discuss Teubner's Globale
  Bukowina?" -> agents: ["mentions"], authors: [] (NOT ["wiethölter", "teubner"] — they are
  cited, not authored-by), citation_targets: [
    {"author": "wiethölter", "year": 1975, "title_keywords": []},
    {"author": "teubner", "year": null, "title_keywords": ["bukowina"]}
  ].
- "mentions" is expensive (a client-side full-text scan) and approximate (word co-occurrence,
  not a verified citation) — only select it when the question is clearly about citation or
  discussion of a specific named work, not a general topic search (that's "rag").
- Default when uncertain: {"agents": ["rag"], ...rest null/empty}
"""
```

Update the JSON stub and field explanations inside `_PROMPT_TEMPLATE`:

```python
_PROMPT_TEMPLATE = """\
You are a query router for an academic library system.
You have these agents available:

{agent_blocks}

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
  "routing_description": null
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
{guidance}"""
```

In `route()`, replace the `plan = QueryPlan(...)` construction:

```python
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
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_query_router.py -v`
Expected: PASS (all tests, including the 3 new ones and all pre-existing ones)

- [ ] **Step 6: Commit**

```bash
git add backend/models/filters.py backend/services/query_router.py backend/tests/test_query_router.py
git commit -m "feat(backend): add CitationTarget filter and router extraction for citation questions"
```

---

### Task 2: `MentionsAgent` + `NeedsClientEvidenceError`

**Files:**
- Modify: `backend/services/base_agent.py`
- Create: `backend/services/mentions_agent.py`
- Test: `backend/tests/test_mentions_agent.py`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_mentions_agent.py`:

```python
"""
Unit tests for MentionsAgent and its evidence-formatting helpers.
"""

import unittest

from backend.models.filters import CitationTarget, MetadataFilters
from backend.services.mentions_agent import (
    ClientEvidence, MentionEvidenceItem, MentionsAgent, TargetMatch, _evidence_to_context,
)


class TestEvidenceToContext(unittest.TestCase):

    def test_no_items(self):
        text = _evidence_to_context(ClientEvidence(), [CitationTarget(author="teubner")])
        self.assertIn("No publications", text)

    def test_formats_snippet_and_count(self):
        evidence = ClientEvidence(items=[
            MentionEvidenceItem(
                item_key="ABC", library_id="1", title="Systemtheorie",
                authors=["Fischer-Lescano, A."], year=2013,
                target_matches={"0": TargetMatch(count=5, snippets=["...Wiethölter, der..."])},
            )
        ])
        text = _evidence_to_context(evidence, [CitationTarget(author="wiethölter")])
        self.assertIn("[S1]", text)
        self.assertIn("Systemtheorie", text)
        self.assertIn("5 occurrence", text)
        self.assertIn("Wiethölter, der", text)

    def test_self_citation_noted_not_listed_as_citer(self):
        evidence = ClientEvidence(items=[
            MentionEvidenceItem(
                item_key="SELF", library_id="1", title="Globale Bukowina",
                authors=["Teubner, G."],
                target_matches={"0": TargetMatch(count=3, is_self=True)},
            )
        ])
        text = _evidence_to_context(
            evidence, [CitationTarget(author="teubner", title_keywords=["bukowina"])]
        )
        self.assertIn("appears to BE", text)

    def test_partial_index_flag_noted(self):
        evidence = ClientEvidence(items=[
            MentionEvidenceItem(
                item_key="P", library_id="1", title="T",
                target_matches={"0": TargetMatch(count=1)}, partial_index=True,
            )
        ])
        text = _evidence_to_context(evidence, [CitationTarget(author="x")])
        self.assertIn("incomplete", text)

    def test_truncation_noted(self):
        evidence = ClientEvidence(
            items=[MentionEvidenceItem(
                item_key="A", library_id="1", title="T",
                target_matches={"0": TargetMatch(count=1)},
            )],
            truncated=True, total_candidates=99,
        )
        text = _evidence_to_context(evidence, [CitationTarget(author="x")])
        self.assertIn("top 1 of 99", text)


class TestMentionsAgentExecute(unittest.IsolatedAsyncioTestCase):

    async def test_excludes_self_citation_from_sources_but_keeps_genuine_citer(self):
        agent = MentionsAgent()
        evidence = ClientEvidence(items=[
            MentionEvidenceItem(
                item_key="SELF", library_id="1", title="Globale Bukowina",
                authors=["Teubner, G."],
                target_matches={"0": TargetMatch(count=3, is_self=True)},
            ),
            MentionEvidenceItem(
                item_key="CITER", library_id="1", title="Systemtheorie",
                authors=["Fischer-Lescano, A."],
                target_matches={"0": TargetMatch(count=2)},
            ),
        ])
        result = await agent.execute(
            question="Who cites Teubner?", library_ids=["1"],
            filters=MetadataFilters(citation_targets=[CitationTarget(author="teubner")]),
            client_evidence=evidence,
        )
        source_ids = [s["item_id"] for s in result.sources]
        self.assertNotIn("SELF", source_ids)
        self.assertIn("CITER", source_ids)

    async def test_context_text_mentions_citer(self):
        agent = MentionsAgent()
        evidence = ClientEvidence(items=[
            MentionEvidenceItem(
                item_key="CITER", library_id="1", title="Systemtheorie",
                target_matches={"0": TargetMatch(count=2, snippets=["...teubner..."])},
            ),
        ])
        result = await agent.execute(
            question="Who cites Teubner?", library_ids=["1"],
            filters=MetadataFilters(citation_targets=[CitationTarget(author="teubner")]),
            client_evidence=evidence,
        )
        self.assertIn("Systemtheorie", result.context_text)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_mentions_agent.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.services.mentions_agent'`

- [ ] **Step 3: Add `NeedsClientEvidenceError` to `base_agent.py`**

In `backend/services/base_agent.py`, update the import line:

```python
from backend.models.filters import CitationTarget, MetadataFilters
```

Add after the `QueryPlan` class:

```python
class NeedsClientEvidenceError(Exception):
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
```

- [ ] **Step 4: Create `mentions_agent.py`**

Create `backend/services/mentions_agent.py`:

```python
"""
Mentions agent — formats client-gathered full-text citation evidence.

Unlike RAGAgent/MetadataAgent, this agent never queries the backend's own
storage: "who cites work X" can only be answered from the *citing* document's
full text, and the backend has no reliable index for that (Qdrant chunks are
sized/embedded for semantic search, not lexical citation lookup). Instead,
the Zotero client's own local full-text search index — built for every
downloaded attachment — is scanned for mentions client-side, and the
resulting snippets are shipped to the backend in the request. See
docs/query-routing.md for the two-phase request/response protocol this
agent depends on.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel

from backend.models.filters import CitationTarget, MetadataFilters
from backend.services.base_agent import AgentResult, BaseAgent
from backend.services.metadata_agent import _format_authors


class TargetMatch(BaseModel):
    """Evidence for one citation_targets[i] found in one document's full text."""

    count: int = 0
    snippets: list[str] = []
    is_self: bool = False  # True if the document appears to BE the cited work itself


class MentionEvidenceItem(BaseModel):
    """One document's full-text search evidence, gathered client-side."""

    item_key: str
    library_id: str
    title: str
    authors: list[str] = []
    year: Optional[int] = None
    target_matches: dict[str, TargetMatch] = {}  # key = str(index into citation_targets)
    partial_index: bool = False


class ClientEvidence(BaseModel):
    """Wire shape of the `client_evidence` field on POST /api/query."""

    items: list[MentionEvidenceItem] = []
    truncated: bool = False
    total_candidates: int = 0


def _evidence_to_context(evidence: ClientEvidence, citation_targets: list[CitationTarget]) -> str:
    if not evidence.items:
        return (
            "No publications in this library's locally indexed full text mention "
            "the requested work(s)."
        )

    labels = [t.author.title() + (f" ({t.year})" if t.year else "") for t in citation_targets]
    lines = [
        "Publications whose full text mentions " + " and ".join(labels)
        + " (found via the user's local Zotero full-text search index — coverage is "
        "limited to attachments downloaded and indexed on the user's machine; a mention "
        "is word co-occurrence, not a verified citation — judge each snippet yourself):\n"
    ]
    for i, item in enumerate(evidence.items, 1):
        year_str = f" ({item.year})" if item.year else ""
        lines.append(f"[S{i}] {_format_authors(item.authors)}{year_str} — {item.title}")
        for idx, label in enumerate(labels):
            match = item.target_matches.get(str(idx))
            if not match:
                continue
            if match.is_self:
                lines.append(
                    f"      NOTE: this item appears to BE the {label} work itself — "
                    "do not list it as one of the citing publications."
                )
                continue
            lines.append(f"      Mentions \"{label}\" ({match.count} occurrence(s)):")
            for snippet in match.snippets:
                lines.append(f"        \"...{snippet}...\"")
        if item.partial_index:
            lines.append(
                "      NOTE: this document's full-text index is incomplete "
                "(page/length limit) — mentions may be missing, especially near the end."
            )
    if evidence.truncated:
        lines.append(
            f"\n(Showing the top {len(evidence.items)} of {evidence.total_candidates} "
            "matching documents — ask a narrower question to see the rest.)"
        )
    return "\n".join(lines)


class MentionsAgent(BaseAgent):
    """Formats client-supplied citation/mention evidence for synthesis.

    Requires `client_evidence` (a `ClientEvidence`) in execute()'s kwargs.
    QueryOrchestrator guarantees this agent is only invoked once that
    evidence has been supplied — see NeedsClientEvidenceError.
    """

    @property
    def name(self) -> str:
        return "mentions"

    @property
    def capability_prompt(self) -> str:
        return (
            "Finds publications that CITE, DISCUSS, RESPOND TO, or MENTION a specific named "
            "work — as opposed to publications AUTHORED by someone.\n"
            "Use when the question asks who cites/references/discusses a named author's work "
            "(e.g. \"which publications cite Wiethölter's 1975 article\", \"who discusses "
            "Teubner's Globale Bukowina\").\n"
            "Populate citation_targets (NOT authors) with one entry per cited work: author "
            "surname, optional year, optional distinctive title keywords. Never put a cited "
            "author's name in `authors` — that field means 'written by', not 'cited by'.\n"
            "Retrieval runs against the user's local Zotero full-text index via a client round "
            "trip; results are approximate word co-occurrence, not verified citations, and only "
            "cover attachments the user has downloaded and indexed locally."
        )

    async def execute(
        self,
        question: str,
        library_ids: list[str],
        filters: MetadataFilters,
        trace=None,
        **kwargs,
    ) -> AgentResult:
        evidence: ClientEvidence = kwargs["client_evidence"]
        context_text = _evidence_to_context(evidence, filters.citation_targets)
        sources = [
            dict(
                item_id=item.item_key,
                library_id=item.library_id,
                title=item.title,
                authors=item.authors,
                year=item.year,
                score=1.0,
            )
            for item in evidence.items
            if not all(m.is_self for m in item.target_matches.values())
        ]
        return AgentResult(agent_name=self.name, context_text=context_text, sources=sources)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_mentions_agent.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/services/base_agent.py backend/services/mentions_agent.py backend/tests/test_mentions_agent.py
git commit -m "feat(backend): add MentionsAgent to format client-gathered citation evidence"
```

---

### Task 3: `QueryOrchestrator` wiring

**Files:**
- Modify: `backend/services/query_orchestrator.py`
- Test: `backend/tests/test_orchestrator.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_orchestrator.py`. First extend the imports at the top of the file:

```python
from backend.models.filters import CitationTarget, MetadataFilters
from backend.services.base_agent import AgentResult, BaseAgent, NeedsClientEvidenceError, QueryPlan
from backend.services.mentions_agent import ClientEvidence
```

(`MetadataFilters`/`AgentResult`/`BaseAgent`/`QueryPlan` are already imported in the existing file — just add `CitationTarget`, `NeedsClientEvidenceError`, and the new `ClientEvidence` import.)

Then add a new test class at the end of the file:

```python
class TestMentionsShortCircuit(unittest.IsolatedAsyncioTestCase):

    async def test_raises_needs_client_evidence_when_targets_present_and_no_evidence(self):
        orch = _make_orchestrator()
        mentions_agent = _stub_agent(
            "mentions", AgentResult(agent_name="mentions", context_text="", sources=[])
        )
        orch.register(mentions_agent)

        mock_plan = QueryPlan(
            agents_to_use=["mentions"],
            filters=MetadataFilters(citation_targets=[CitationTarget(author="teubner")]),
        )
        with patch("backend.services.query_orchestrator.QueryRouter") as MockRouter:
            instance = MagicMock()
            instance.route = AsyncMock(return_value=mock_plan)
            MockRouter.return_value = instance

            with self.assertRaises(NeedsClientEvidenceError) as cm:
                await orch.query(question="Who cites Teubner?", library_ids=["1"], enable_routing=True)

        self.assertEqual(len(cm.exception.citation_targets), 1)
        self.assertEqual(cm.exception.citation_targets[0].author, "teubner")
        mentions_agent.execute.assert_not_called()

    async def test_drops_mentions_when_no_citation_targets_extracted(self):
        orch = _make_orchestrator()
        rag_result = AgentResult(agent_name="rag", context_text="RAG answer", sources=[])
        rag_agent = _stub_agent("rag", rag_result)
        mentions_agent = _stub_agent(
            "mentions", AgentResult(agent_name="mentions", context_text="", sources=[])
        )
        orch.register(rag_agent)
        orch.register(mentions_agent)

        # Router selected "mentions" but extracted no citation_targets — must be dropped,
        # not passed through to raise NeedsClientEvidenceError for nothing.
        mock_plan = QueryPlan(agents_to_use=["mentions", "rag"], filters=MetadataFilters())
        with patch("backend.services.query_orchestrator.QueryRouter") as MockRouter:
            instance = MagicMock()
            instance.route = AsyncMock(return_value=mock_plan)
            MockRouter.return_value = instance

            result = await orch.query(question="Q?", library_ids=["1"], enable_routing=True)

        mentions_agent.execute.assert_not_called()
        self.assertEqual(result.answer, "RAG answer")

    async def test_runs_mentions_when_evidence_supplied(self):
        orch = _make_orchestrator()
        mentions_result = AgentResult(agent_name="mentions", context_text="found it", sources=[])
        mentions_agent = _stub_agent("mentions", mentions_result)
        orch.register(mentions_agent)
        orch._llm_service.generate = AsyncMock(return_value="Synthesized")
        orch._settings.get_hardware_preset.return_value.llm.max_answer_tokens = 512

        evidence = ClientEvidence()
        mock_plan = QueryPlan(
            agents_to_use=["mentions"],
            filters=MetadataFilters(citation_targets=[CitationTarget(author="teubner")]),
        )
        with patch("backend.services.query_orchestrator.QueryRouter") as MockRouter:
            instance = MagicMock()
            instance.route = AsyncMock(return_value=mock_plan)
            MockRouter.return_value = instance

            result = await orch.query(
                question="Q?", library_ids=["1"], enable_routing=True, client_evidence=evidence,
            )

        mentions_agent.execute.assert_awaited_once()
        self.assertIs(mentions_agent.execute.call_args.kwargs["client_evidence"], evidence)
        self.assertEqual(result.answer, "Synthesized")

    async def test_preset_plan_skips_routing_llm_call(self):
        orch = _make_orchestrator()
        rag_agent = _stub_agent("rag", AgentResult(agent_name="rag", context_text="a", sources=[]))
        orch.register(rag_agent)

        preset_plan = QueryPlan(agents_to_use=["rag"], filters=MetadataFilters())
        with patch("backend.services.query_orchestrator.QueryRouter") as MockRouter:
            await orch.query(question="Q?", library_ids=["1"], preset_plan=preset_plan)
            MockRouter.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_orchestrator.py -v -k Mentions`
Expected: FAIL — `TypeError: QueryOrchestrator.query() got an unexpected keyword argument 'client_evidence'` (or `'preset_plan'`)

- [ ] **Step 3: Wire the orchestrator**

In `backend/services/query_orchestrator.py`, update imports:

```python
from backend.services.base_agent import AgentResult, BaseAgent, NeedsClientEvidenceError, QueryPlan
from backend.services.mentions_agent import ClientEvidence, MentionsAgent
```

In `_register_defaults`, add the new agent:

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
```

Update the `query()` signature and routing block:

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
    ) -> QueryResult:
        """
        Route the question, run selected agents, and synthesize the final answer.

        Args:
            question: User's question.
            library_ids: Libraries to search (empty list searches all).
            top_k: Number of chunks for semantic search (RAGAgent).
            min_score: Minimum similarity score (RAGAgent).
            enable_routing: False skips the routing LLM call and goes straight to RAGAgent.
            trace: Optional collector for recording intermediate trace events.
            client_evidence: Full-text citation evidence gathered client-side, required to
                run the "mentions" agent — see NeedsClientEvidenceError.
            preset_plan: A previously-returned QueryPlan to reuse instead of calling the
                routing LLM again — used on the client's resubmit-with-evidence round trip.

        Raises:
            NeedsClientEvidenceError: the plan selects "mentions" with extracted
                citation_targets, but client_evidence was not supplied.
        """
        # 1. Routing
        if preset_plan is not None:
            plan = preset_plan
        elif enable_routing and len(self._agents) > 1:
            plan = await QueryRouter(self._llm_service).route(
                question, list(self._agents.values()), trace=trace
            )
            logger.info(
                "QueryOrchestrator: routing → agents=%s filters=%s description=%s",
                plan.agents_to_use,
                plan.filters.model_dump(exclude_defaults=True) or "{}",
                plan.routing_description,
            )
        else:
            plan = QueryPlan(agents_to_use=["rag"])

        # 1b. "mentions" evidence only exists client-side. If the router extracted no
        # citation_targets it was a spurious selection — drop it. Otherwise, without
        # client_evidence there is nothing to run yet — short-circuit and let the API
        # layer ask the client to gather it and resubmit.
        if "mentions" in plan.agents_to_use:
            if not plan.filters.citation_targets:
                plan.agents_to_use = [a for a in plan.agents_to_use if a != "mentions"]
            elif client_evidence is None:
                raise NeedsClientEvidenceError(
                    citation_targets=plan.filters.citation_targets, plan=plan,
                )

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
            )
            for agent in selected
        ])
```

(The rest of `query()` — steps 3b/4 and `_synthesize` — is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_orchestrator.py -v`
Expected: PASS (all tests, including pre-existing ones — `client_evidence=None` passed to every agent's `execute()` is harmless since `RAGAgent`/`MetadataAgent` both accept `**kwargs`)

- [ ] **Step 5: Commit**

```bash
git add backend/services/query_orchestrator.py backend/tests/test_orchestrator.py
git commit -m "feat(backend): wire MentionsAgent into QueryOrchestrator with client-evidence short-circuit"
```

---

### Task 4: `/api/query` two-phase protocol

**Files:**
- Modify: `backend/api/query.py`
- Test: `backend/tests/test_query_api.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_query_api.py` (a new top-level test class, alongside the existing `QueryAuthorizationTest`):

```python
class NeedsEvidenceResponseTest(unittest.TestCase):
    def test_maps_exception_to_pending_response(self):
        from backend.api.query import _needs_evidence_response, QueryRequest
        from backend.models.filters import CitationTarget, MetadataFilters
        from backend.services.base_agent import NeedsClientEvidenceError, QueryPlan

        query = QueryRequest(question="Who cites Teubner?", library_ids=["u1"])
        plan = QueryPlan(
            agents_to_use=["mentions"],
            filters=MetadataFilters(citation_targets=[CitationTarget(author="teubner")]),
        )
        exc = NeedsClientEvidenceError(citation_targets=plan.filters.citation_targets, plan=plan)

        response = _needs_evidence_response(query, exc)

        self.assertEqual(response.status, "needs_client_evidence")
        self.assertEqual(response.answer, "")
        self.assertEqual(len(response.citation_targets), 1)
        self.assertEqual(response.citation_targets[0].author, "teubner")
        self.assertEqual(response.query_plan.agents_to_use, ["mentions"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_query_api.py -v -k NeedsEvidence`
Expected: FAIL — `ImportError: cannot import name '_needs_evidence_response' from 'backend.api.query'`

- [ ] **Step 3: Extend `query.py`**

Update the imports at the top of `backend/api/query.py`:

```python
from backend.models.filters import CitationTarget
from backend.services.base_agent import NeedsClientEvidenceError, QueryPlan
from backend.services.mentions_agent import ClientEvidence
```

Extend `QueryRequest`:

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
    client_evidence: Optional[ClientEvidence] = None  # gathered client-side, resubmit round trip
    query_plan: Optional[QueryPlan] = None  # echoed back from a prior "needs_client_evidence" response
```

Extend `QueryResponse`:

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
    status: str = "complete"  # "complete" | "needs_client_evidence"
    citation_targets: List[CitationTarget] = []  # populated when status == "needs_client_evidence"
    query_plan: Optional[QueryPlan] = None  # echo back on the resubmit round trip
```

Add a module-level helper function (place it above `query_libraries`, e.g. right after the `QueryResponse` class):

```python
def _needs_evidence_response(query: QueryRequest, exc: NeedsClientEvidenceError) -> QueryResponse:
    return QueryResponse(
        question=query.question,
        answer="",
        answer_format="text",
        sources=[],
        library_ids=query.library_ids,
        status="needs_client_evidence",
        citation_targets=exc.citation_targets,
        query_plan=exc.plan,
    )
```

In the endpoint handler, pass the two new fields through to the orchestrator call:

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
        )
```

And add a new `except` clause **before** the existing `except Exception as e:` block at the end of the handler:

```python
    except NeedsClientEvidenceError as exc:
        return _needs_evidence_response(query, exc)

    except Exception as e:
        logger.exception("Query failed")
        raise HTTPException(
            status_code=500,
            detail=f"Query failed: {str(e)}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_query_api.py -v`
Expected: PASS (all tests, including the pre-existing `QueryAuthorizationTest` ones)

- [ ] **Step 5: Commit**

```bash
git add backend/api/query.py backend/tests/test_query_api.py
git commit -m "feat(backend): surface needs_client_evidence status on POST /api/query"
```

---

### Task 5: `mentions.js` — pure evidence-extraction functions

**Files:**
- Create: `plugin/src/mentions.js`
- Test: `plugin/test/mentions.test.js`

- [ ] **Step 1: Write the failing tests**

Create `plugin/test/mentions.test.js` with the pure-function tests (the Zotero-orchestration tests are added in Task 6):

```js
// Tests for plugin/src/mentions.js — client-side citation/mention search over
// Zotero's local full-text index.
//
// Same technique as plugin/test/remote_indexer.test.js: evaluate the source in
// a vm context with stubbed Zotero/IOUtils globals, then pull the top-level
// `MentionSearch` object back out.

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE_PATH = path.join(__dirname, '..', 'src', 'mentions.js');

/**
 * @param {any} [zoteroStub]
 * @param {any} [ioUtilsStub]
 * @returns {any} the MentionSearch object
 */
function loadMentionSearch(zoteroStub = {}, ioUtilsStub = {}) {
	const src = fs.readFileSync(SOURCE_PATH, 'utf8');
	const context = { Zotero: zoteroStub, IOUtils: ioUtilsStub, console };
	vm.createContext(context);
	vm.runInContext(src, context, { filename: 'mentions.js' });
	return context.MentionSearch;
}

test('expandVariants includes original, diacritic-folded, and transliterated forms', () => {
	const M = loadMentionSearch();
	const variants = M.expandVariants('Wiethölter');
	assert.ok(variants.includes('wiethölter'));
	assert.ok(variants.includes('wietholter'));
	assert.ok(variants.includes('wiethoelter'));
});

test('buildSearchTerms combines author variants and title keyword variants', () => {
	const M = loadMentionSearch();
	const terms = M.buildSearchTerms({ author: 'Teubner', title_keywords: ['Bukowina'] });
	assert.ok(terms.includes('teubner'));
	assert.ok(terms.includes('bukowina'));
});

test('extractSnippets counts all occurrences but caps snippet collection', () => {
	const M = loadMentionSearch();
	const text = 'a Wiethölter b Wiethölter c Wiethölter d Wiethölter e';
	const { count, snippets } = M.extractSnippets(text, ['wiethölter'], 2, 10);
	assert.strictEqual(count, 4);
	assert.strictEqual(snippets.length, 2);
});

test('extractSnippets is case-insensitive and matches multiple terms', () => {
	const M = loadMentionSearch();
	const { count } = M.extractSnippets('WIETHÖLTER and bukowina', ['wiethölter', 'bukowina']);
	assert.strictEqual(count, 2);
});

test('isSelfCitation is true when author and title keyword both match', () => {
	const M = loadMentionSearch();
	const isSelf = M.isSelfCitation(
		['Gunther Teubner'],
		'Globale Bukowina. Zur Emergenz eines transnationalen Rechtspluralismus',
		{ author: 'teubner', title_keywords: ['bukowina'] }
	);
	assert.strictEqual(isSelf, true);
});

test('isSelfCitation is false when only the author matches a different work', () => {
	const M = loadMentionSearch();
	const isSelf = M.isSelfCitation(
		['Gunther Teubner'], 'Fragmentierung des Weltrechts',
		{ author: 'teubner', title_keywords: ['bukowina'] }
	);
	assert.strictEqual(isSelf, false);
});

test('isSelfCitation is false when the author does not match at all', () => {
	const M = loadMentionSearch();
	const isSelf = M.isSelfCitation(
		['Niklas Luhmann'], 'Globale Bukowina',
		{ author: 'teubner', title_keywords: ['bukowina'] }
	);
	assert.strictEqual(isSelf, false);
});

test('rankAndCap sorts by descending non-self match count and flags truncation', () => {
	const M = loadMentionSearch();
	const items = [
		{ item_key: 'low', target_matches: { 0: { count: 1, is_self: false } } },
		{ item_key: 'high', target_matches: { 0: { count: 9, is_self: false } } },
		{ item_key: 'self-only', target_matches: { 0: { count: 99, is_self: true } } },
	];
	const result = M.rankAndCap(items, 2);
	assert.deepStrictEqual(result.items.map(i => i.item_key), ['high', 'low']);
	assert.strictEqual(result.truncated, true);
	assert.strictEqual(result.total_candidates, 3);
});

test('mergeTargetMatches sums counts and unions snippets across attachments of one item', () => {
	const M = loadMentionSearch();
	const existing = { 0: { count: 2, snippets: ['a'], is_self: false } };
	M.mergeTargetMatches(existing, { 0: { count: 3, snippets: ['b'], is_self: false } }, 5);
	assert.strictEqual(existing[0].count, 5);
	assert.deepStrictEqual(existing[0].snippets, ['a', 'b']);
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test plugin/test/mentions.test.js`
Expected: FAIL — `Error: Cannot find module '.../plugin/src/mentions.js'`

- [ ] **Step 3: Create `mentions.js` (pure functions only for now)**

Create `plugin/src/mentions.js`:

```js
// Client-side citation/mention search over Zotero's local full-text index.
//
// Loaded by dialog.xhtml before dialog.js so ZoteroRAGDialog can call it.
// See docs/query-routing.md for the two-phase "needs_client_evidence" protocol
// this module implements the client half of.

// @ts-check

const MENTION_SNIPPET_CHARS = 240;
const MENTION_MAX_SNIPPETS_PER_TARGET = 3;
const MENTION_MAX_EVIDENCE_ITEMS = 40;

/**
 * Strip diacritics for variant-tolerant matching (NFD decompose, drop combining marks).
 * @param {string} s
 * @returns {string}
 */
function foldDiacritics(s) {
	return s.normalize('NFD').replace(/[̀-ͯ]/g, '');
}

/**
 * German-transliterated form (ö -> oe, etc.) — a second common OCR/typing variant
 * beyond simple diacritic folding.
 * @param {string} s
 * @returns {string}
 */
function transliterateGerman(s) {
	return s
		.replace(/ö/g, 'oe').replace(/Ö/g, 'Oe')
		.replace(/ä/g, 'ae').replace(/Ä/g, 'Ae')
		.replace(/ü/g, 'ue').replace(/Ü/g, 'Ue')
		.replace(/ß/g, 'ss');
}

/**
 * Distinct lowercase spelling variants of a name/word worth searching for.
 * `fulltextWord` matching in Zotero is a left-bound (prefix) match, so suffix
 * variants (plurals, possessives) don't need to be listed separately.
 * @param {string} word
 * @returns {Array<string>}
 */
function expandVariants(word) {
	const lower = word.toLowerCase();
	return [...new Set([lower, foldDiacritics(lower), transliterateGerman(lower)])];
}

/**
 * All search terms for one citation target: author-name variants, plus any
 * distinctive title keywords (matches short-form citations that name the
 * work without repeating the author nearby).
 * @param {{author: string, year?: number|null, title_keywords?: Array<string>}} target
 * @returns {Array<string>}
 */
function buildSearchTerms(target) {
	const terms = expandVariants(target.author);
	for (const kw of target.title_keywords || []) {
		terms.push(...expandVariants(kw));
	}
	return [...new Set(terms)];
}

/**
 * Count occurrences of any of `terms` in `text` and collect up to `maxSnippets`
 * surrounding-context excerpts (case-insensitive substring match).
 * @param {string} text
 * @param {Array<string>} terms
 * @param {number} [maxSnippets]
 * @param {number} [windowChars]
 * @returns {{count: number, snippets: Array<string>}}
 */
function extractSnippets(text, terms, maxSnippets = MENTION_MAX_SNIPPETS_PER_TARGET, windowChars = MENTION_SNIPPET_CHARS) {
	if (!terms.length) return { count: 0, snippets: [] };
	const pattern = new RegExp(terms.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'gi');
	const snippets = [];
	let count = 0;
	let match;
	while ((match = pattern.exec(text)) !== null) {
		count++;
		if (snippets.length < maxSnippets) {
			const start = Math.max(0, match.index - windowChars / 2);
			const end = Math.min(text.length, match.index + match[0].length + windowChars / 2);
			snippets.push(text.slice(start, end).replace(/\s+/g, ' ').trim());
		}
		if (match.index === pattern.lastIndex) pattern.lastIndex++;
	}
	return { count, snippets };
}

/**
 * True when a candidate document's own metadata identifies it as the cited
 * work itself (author + title overlap with the target), rather than a
 * publication citing that work.
 * @param {Array<string>} itemAuthors - "First Last" strings
 * @param {string} itemTitle
 * @param {{author: string, title_keywords?: Array<string>}} target
 * @returns {boolean}
 */
function isSelfCitation(itemAuthors, itemTitle, target) {
	const authorVariants = expandVariants(target.author);
	const authorMatches = itemAuthors.some(a => {
		const folded = foldDiacritics(a.toLowerCase());
		return authorVariants.some(v => folded.includes(v) || folded.includes(foldDiacritics(v)));
	});
	if (!authorMatches) return false;
	if (!target.title_keywords || target.title_keywords.length === 0) return true;
	const foldedTitle = foldDiacritics(itemTitle.toLowerCase());
	return target.title_keywords.some(kw => foldedTitle.includes(foldDiacritics(kw.toLowerCase())));
}

/**
 * Sort by total (non-self) match count across all targets, descending, and
 * cap to `maxItems`.
 * @param {Array<any>} items - MentionEvidenceItem-shaped plain objects
 * @param {number} maxItems
 * @returns {{items: Array<any>, truncated: boolean, total_candidates: number}}
 */
function rankAndCap(items, maxItems = MENTION_MAX_EVIDENCE_ITEMS) {
	const scored = items.map(item => {
		const score = Object.values(item.target_matches)
			.filter((/** @type {any} */ m) => !m.is_self)
			.reduce((sum, /** @type {any} */ m) => sum + m.count, 0);
		return { item, score };
	});
	scored.sort((a, b) => b.score - a.score);
	const capped = scored.slice(0, maxItems).map(s => s.item);
	return {
		items: capped,
		truncated: items.length > maxItems,
		total_candidates: items.length,
	};
}

/**
 * Merge a newly-found attachment's per-target matches into an already-seen
 * parent item's accumulated evidence (an item can have multiple matching
 * attachments, e.g. two language versions).
 * @param {Record<string, any>} existing
 * @param {Record<string, any>} incoming
 * @param {number} [maxSnippets]
 */
function mergeTargetMatches(existing, incoming, maxSnippets = MENTION_MAX_SNIPPETS_PER_TARGET) {
	for (const [key, match] of Object.entries(incoming)) {
		if (!existing[key]) {
			existing[key] = match;
			continue;
		}
		existing[key].count += match.count;
		existing[key].is_self = existing[key].is_self || match.is_self;
		existing[key].snippets = [...existing[key].snippets, ...match.snippets].slice(0, maxSnippets);
	}
}

var MentionSearch = {
	MENTION_SNIPPET_CHARS,
	MENTION_MAX_SNIPPETS_PER_TARGET,
	MENTION_MAX_EVIDENCE_ITEMS,
	foldDiacritics,
	transliterateGerman,
	expandVariants,
	buildSearchTerms,
	extractSnippets,
	isSelfCitation,
	rankAndCap,
	mergeTargetMatches,
};
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test plugin/test/mentions.test.js`
Expected: PASS (all 9 tests)

- [ ] **Step 5: Commit**

```bash
git add plugin/src/mentions.js plugin/test/mentions.test.js
git commit -m "feat(plugin): add pure citation-evidence extraction functions"
```

---

### Task 6: `findMentionEvidence()` — Zotero full-text search orchestration

**Files:**
- Modify: `plugin/src/mentions.js`
- Test: `plugin/test/mentions.test.js`

- [ ] **Step 1: Write the failing tests**

Append to `plugin/test/mentions.test.js`:

```js
// --- findMentionEvidence (Zotero-dependent orchestration) --------------

/**
 * Build a fake Zotero/IOUtils environment: one library, a set of attachments
 * (each with fixed full-text content) and their resolved parent items. Every
 * doc gets a synthetic parent (self-referential when `parentKey` is omitted)
 * — true parentless standalone attachments aren't modeled here, they're rare
 * and orthogonal to the logic under test.
 * @param {Array<{id: number, key: string, parentKey?: string, title: string, authors: Array<string>, text: string, indexedPages?: number, totalPages?: number}>} docs
 */
function makeEvidenceStubs(docs) {
	const files = {};
	const attachmentsByID = new Map();
	const parentsByKey = new Map();

	for (const doc of docs) {
		files[`/cache/${doc.key}`] = doc.text;
		const parentKey = doc.parentKey || doc.key;
		if (!parentsByKey.has(parentKey)) {
			parentsByKey.set(parentKey, {
				key: parentKey, libraryID: 1,
				getField: (/** @type {string} */ f) => (f === 'title' ? doc.title : ''),
			});
		}
		attachmentsByID.set(doc.id, {
			id: doc.id, key: doc.key, libraryID: 1,
			parentItemID: `parent-${parentKey}`,
		});
	}

	const zotero = {
		Search: function () {
			const conditions = [];
			return {
				libraryID: null,
				addCondition(...args) { conditions.push(args); },
				async search() {
					const term = conditions.find(c => c[0] === 'fulltextWord')[2].toLowerCase();
					return docs.filter(d => d.text.toLowerCase().includes(term)).map(d => d.id);
				},
			};
		},
		Items: {
			async getAsync(idsOrId) {
				if (Array.isArray(idsOrId)) {
					return idsOrId.map(id => attachmentsByID.get(id)).filter(Boolean);
				}
				if (typeof idsOrId === 'string' && idsOrId.startsWith('parent-')) {
					return parentsByKey.get(idsOrId.replace('parent-', ''));
				}
				return attachmentsByID.get(idsOrId);
			},
		},
		FullText: {
			getItemCacheFile: (/** @type {any} */ att) => ({ path: `/cache/${att.key}` }),
			getPages: async (/** @type {number} */ id) => {
				const doc = docs.find(d => d.id === id);
				return { indexedPages: doc.indexedPages ?? 1, total: doc.totalPages ?? 1 };
			},
		},
		ZoteroRAG: {
			_extractAuthors: (/** @type {any} */ item) => {
				const doc = docs.find(d => (d.parentKey || d.key) === item.key);
				return doc ? doc.authors : [];
			},
			_extractYear: () => null,
			getBackendLibraryId: (/** @type {number} */ id) => `u${id}`,
		},
	};
	const ioUtils = {
		readUTF8: async (/** @type {string} */ p) => {
			if (!(p in files)) throw new Error('ENOENT');
			return files[p];
		},
	};
	return { zotero, ioUtils };
}

test('findMentionEvidence intersects candidates across multiple citation targets', async () => {
	const docs = [
		{ id: 1, key: 'BOTH', title: 'Neue Theorien des Rechts', authors: ['A Buckel'],
			text: 'Wiethölter and Teubner and Bukowina appear here together.' },
		{ id: 2, key: 'ONLY_WIETHOLTER', title: 'Other Work', authors: ['B Other'],
			text: 'Only Wiethölter is mentioned here, nothing else relevant.' },
	];
	const { zotero, ioUtils } = makeEvidenceStubs(docs);
	const M = loadMentionSearch(zotero, ioUtils);

	const result = await M.findMentionEvidence(
		[{ author: 'wiethölter' }, { author: 'teubner', title_keywords: ['bukowina'] }],
		[1]
	);

	assert.strictEqual(result.items.length, 1);
	assert.strictEqual(result.items[0].item_key, 'BOTH');
});

test('findMentionEvidence flags an item as self-citation for a matching target', async () => {
	const docs = [
		{ id: 1, key: 'SELF', title: 'Globale Bukowina', authors: ['Gunther Teubner'],
			text: 'This is Teubner\'s own Bukowina article, citing Wiethölter once.' },
	];
	const { zotero, ioUtils } = makeEvidenceStubs(docs);
	const M = loadMentionSearch(zotero, ioUtils);

	const result = await M.findMentionEvidence(
		[{ author: 'teubner', title_keywords: ['bukowina'] }],
		[1]
	);

	assert.strictEqual(result.items[0].target_matches['0'].is_self, true);
});

test('findMentionEvidence flags partial indexing from Zotero.FullText.getPages', async () => {
	const docs = [
		{ id: 1, key: 'PARTIAL', title: 'T', authors: [], text: 'mentions Wiethölter here',
			indexedPages: 5, totalPages: 20 },
	];
	const { zotero, ioUtils } = makeEvidenceStubs(docs);
	const M = loadMentionSearch(zotero, ioUtils);

	const result = await M.findMentionEvidence([{ author: 'wiethölter' }], [1]);

	assert.strictEqual(result.items[0].partial_index, true);
});

test('findMentionEvidence returns an empty result when nothing matches', async () => {
	const { zotero, ioUtils } = makeEvidenceStubs([]);
	const M = loadMentionSearch(zotero, ioUtils);

	const result = await M.findMentionEvidence([{ author: 'nobody' }], [1]);

	assert.deepStrictEqual(result, { items: [], truncated: false, total_candidates: 0 });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test plugin/test/mentions.test.js`
Expected: FAIL — `TypeError: M.findMentionEvidence is not a function`

- [ ] **Step 3: Add `findMentionEvidence()` to `mentions.js`**

In `plugin/src/mentions.js`, add this function above the `var MentionSearch = {` block:

```js
/**
 * Search the user's local Zotero full-text index for documents mentioning
 * ALL of `citationTargets` (set intersection across targets), gather
 * evidence snippets from each match's `.zotero-ft-cache` file, and return
 * the ranked, budget-capped `client_evidence` payload for POST /api/query.
 * @param {Array<{author: string, year?: number|null, title_keywords?: Array<string>}>} citationTargets
 * @param {Array<number>} zoteroLibraryIDs - native Zotero library IDs to search
 * @returns {Promise<{items: Array<any>, truncated: boolean, total_candidates: number}>}
 */
async function findMentionEvidence(citationTargets, zoteroLibraryIDs) {
	if (!citationTargets.length || !zoteroLibraryIDs.length) {
		return { items: [], truncated: false, total_candidates: 0 };
	}

	// 1. Per-target candidate attachment ID sets (union over variants/libraries).
	const perTargetIDs = [];
	for (const target of citationTargets) {
		const terms = buildSearchTerms(target);
		const ids = new Set();
		for (const libraryID of zoteroLibraryIDs) {
			for (const term of terms) {
				const search = new Zotero.Search();
				(/** @type {any} */ (search)).libraryID = libraryID;
				search.addCondition('deleted', 'false');
				search.addCondition('fulltextWord', 'contains', term);
				for (const id of await search.search()) ids.add(id);
			}
		}
		perTargetIDs.push(ids);
	}

	// 2. Intersect across targets — "cites A and B" means both must be present.
	let candidateIDs = perTargetIDs[0];
	for (const ids of perTargetIDs.slice(1)) {
		candidateIDs = new Set([...candidateIDs].filter(id => ids.has(id)));
	}
	if (candidateIDs.size === 0) {
		return { items: [], truncated: false, total_candidates: 0 };
	}

	// 3. Read cache files, extract per-target evidence, dedupe by parent item.
	const attachments = await Zotero.Items.getAsync([...candidateIDs]);
	/** @type {Map<string, any>} */
	const byParentKey = new Map();

	for (const att of attachments) {
		let text;
		try {
			text = await IOUtils.readUTF8(Zotero.FullText.getItemCacheFile(att).path);
		} catch (_) {
			continue; // no cache file — shouldn't happen if the word index has this item, but be defensive
		}

		const parent = att.parentItemID ? await Zotero.Items.getAsync(att.parentItemID) : null;
		const subject = parent || att;
		const itemKey = subject.key;
		const title = subject.getField ? (subject.getField('title') || 'Untitled') : 'Untitled';
		const authors = Zotero.ZoteroRAG._extractAuthors(subject);
		const year = Zotero.ZoteroRAG._extractYear(subject);
		const libraryId = Zotero.ZoteroRAG.getBackendLibraryId(subject.libraryID);

		const pages = await Zotero.FullText.getPages(att.id);
		const partialIndex = !!(pages && pages.total && pages.indexedPages < pages.total);

		/** @type {Record<string, any>} */
		const targetMatches = {};
		citationTargets.forEach((target, idx) => {
			const terms = buildSearchTerms(target);
			const { count, snippets } = extractSnippets(text, terms);
			if (count === 0) return;
			targetMatches[String(idx)] = {
				count, snippets, is_self: isSelfCitation(authors, title, target),
			};
		});
		if (Object.keys(targetMatches).length === 0) continue;

		const existing = byParentKey.get(itemKey);
		if (existing) {
			mergeTargetMatches(existing.target_matches, targetMatches);
			existing.partial_index = existing.partial_index || partialIndex;
		} else {
			byParentKey.set(itemKey, {
				item_key: itemKey, library_id: libraryId, title, authors, year,
				target_matches: targetMatches, partial_index: partialIndex,
			});
		}
	}

	return rankAndCap([...byParentKey.values()]);
}
```

Add `findMentionEvidence` to the `MentionSearch` export object:

```js
var MentionSearch = {
	MENTION_SNIPPET_CHARS,
	MENTION_MAX_SNIPPETS_PER_TARGET,
	MENTION_MAX_EVIDENCE_ITEMS,
	foldDiacritics,
	transliterateGerman,
	expandVariants,
	buildSearchTerms,
	extractSnippets,
	isSelfCitation,
	rankAndCap,
	mergeTargetMatches,
	findMentionEvidence,
};
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test plugin/test/mentions.test.js`
Expected: PASS (all 13 tests)

- [ ] **Step 5: Commit**

```bash
git add plugin/src/mentions.js plugin/test/mentions.test.js
git commit -m "feat(plugin): add findMentionEvidence Zotero full-text search orchestration"
```

---

### Task 7: Wire `mentions.js` into the dialog

**Files:**
- Modify: `plugin/src/dialog.xhtml`

- [ ] **Step 1: Add the script-load block**

In `plugin/src/dialog.xhtml`, inside the `DOMContentLoaded` handler, add a new `loadSubScript` block for `mentions.js` — after the `remote_indexer.js` block, before the `dialog.js` block:

```js
      // Load remote indexer (must load before dialog.js)
      try {
        Services.scriptloader.loadSubScript(
          "chrome://zotero-rag/content/remote_indexer.js",
          window
        );
      } catch (e) {
        console.error("Failed to load remote_indexer.js:", e);
      }

      // Load mention/citation search (must load before dialog.js)
      try {
        Services.scriptloader.loadSubScript(
          "chrome://zotero-rag/content/mentions.js",
          window
        );
      } catch (e) {
        console.error("Failed to load mentions.js:", e);
      }

      // Load dialog script
      try {
        Services.scriptloader.loadSubScript(
          "chrome://zotero-rag/content/dialog.js",
          window
        );
      } catch (e) {
        console.error("Failed to load dialog.js:", e);
      }
```

- [ ] **Step 2: Commit**

```bash
git add plugin/src/dialog.xhtml
git commit -m "feat(plugin): load mentions.js in the ask dialog"
```

(No automated test for this step — it's chrome-manifest wiring, verified in Task 9's manual end-to-end check.)

---

### Task 8: `submitQuery()` two-phase support + `resolveZoteroLibraryID()`

**Files:**
- Modify: `plugin/src/zotero-rag.js`
- Modify: `plugin/src/dialog.js`
- Test: `plugin/test/dialog.test.js`

- [ ] **Step 1: Write the failing test**

Add to `plugin/test/dialog.test.js` (near the top-level `test(...)` calls, using the same inline-`vm` style already used by `mergeDownloadFailures`'s neighbors in that file — no changes to the existing `loadDialogMethods()` helper, since it doesn't stub `Zotero` and these tests need to):

```js
test('resolveZoteroLibraryID resolves a group library id via Zotero.Groups', () => {
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {}, console,
		Zotero: { Groups: { get: (/** @type {number} */ id) => (id === 42 ? { libraryID: 99 } : null) } },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ZoteroRAGDialog = context.ZoteroRAGDialog;

	const fakeThis = { plugin: { getLibraries: () => [{ id: '42', type: 'group' }] } };
	const result = ZoteroRAGDialog.resolveZoteroLibraryID.call(fakeThis, '42');

	assert.strictEqual(result, 99);
});

test('resolveZoteroLibraryID resolves a personal library id by parsing it as an integer', () => {
	const context = {
		document: { readyState: 'loading', addEventListener() {} },
		window: {}, console, Zotero: { Groups: { get: () => null } },
	};
	vm.createContext(context);
	vm.runInContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context, { filename: 'dialog.js' });
	const ZoteroRAGDialog = context.ZoteroRAGDialog;

	const fakeThis = { plugin: { getLibraries: () => [{ id: '7', type: 'user' }] } };
	const result = ZoteroRAGDialog.resolveZoteroLibraryID.call(fakeThis, '7');

	assert.strictEqual(result, 7);
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test plugin/test/dialog.test.js`
Expected: FAIL — `TypeError: Cannot read properties of undefined (reading 'call')` (`resolveZoteroLibraryID` doesn't exist yet)

- [ ] **Step 3: Extend `submitQuery()` in `zotero-rag.js`**

In `plugin/src/zotero-rag.js`, inside `submitQuery()`, add alongside the existing optional-option handling (after the `includeTrace` block, before the `fetch` call):

```js
			if (options.includeTrace) {
				payload.include_trace = true;
			}
			if (options.clientEvidence !== undefined) {
				payload.client_evidence = options.clientEvidence;
			}
			if (options.queryPlan !== undefined) {
				payload.query_plan = options.queryPlan;
			}
```

- [ ] **Step 4: Add `resolveZoteroLibraryID()` and two-phase flow to `dialog.js`**

In `plugin/src/dialog.js`, add a new method to the `ZoteroRAGDialog` object (near `downloadMissingAttachments`, whose resolution logic it mirrors):

```js
	/**
	 * Resolve a backend-format library ID (e.g. "u123" or a numeric group ID
	 * string) to the native Zotero integer libraryID needed by
	 * `Zotero.Search()`. Mirrors the resolution in downloadMissingAttachments().
	 * @param {string} libraryId
	 * @returns {number|null}
	 */
	resolveZoteroLibraryID(libraryId) {
		if (!this.plugin) return null;
		const library = this.plugin.getLibraries().find((/** @type {any} */ l) => l.id === libraryId);
		const libraryType = library ? library.type : 'user';
		const zoteroLibraryID = libraryType === 'group'
			? Zotero.Groups.get(parseInt(libraryId, 10))?.libraryID
			: parseInt(libraryId, 10);
		return zoteroLibraryID || null;
	},
```

Then, in the submit handler where `submitQuery` is first called (around line 1105), replace:

```js
			const result = await this.plugin.submitQuery(question, libraryIds, {
				minScore: minScore,
				topK: topK,
				llmModel: llmModel,
				enableRouting: enableRouting,
				includeTrace: includeTrace
			});
```

with:

```js
			let result = await this.plugin.submitQuery(question, libraryIds, {
				minScore: minScore,
				topK: topK,
				llmModel: llmModel,
				enableRouting: enableRouting,
				includeTrace: includeTrace
			});

			// The router determined this question needs citation evidence that only
			// exists in the user's local Zotero full-text index — gather it and resubmit,
			// echoing back query_plan so the backend doesn't re-run the routing LLM call.
			if (result.status === 'needs_client_evidence') {
				this.updateProgress(25, 'Searching local library', 'Scanning full text for citations...');
				const zoteroLibraryIDs = /** @type {Array<number>} */ (
					libraryIds
						.map((/** @type {string} */ id) => this.resolveZoteroLibraryID(id))
						.filter((/** @type {number|null} */ id) => id !== null)
				);
				const evidence = await MentionSearch.findMentionEvidence(result.citation_targets, zoteroLibraryIDs);

				this.updateProgress(40, 'Processing query', 'Sending citation evidence to backend...');
				result = await this.plugin.submitQuery(question, libraryIds, {
					minScore: minScore,
					topK: topK,
					llmModel: llmModel,
					enableRouting: enableRouting,
					includeTrace: includeTrace,
					clientEvidence: evidence,
					queryPlan: result.query_plan
				});
			}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `node --test plugin/test/dialog.test.js`
Expected: PASS (all tests, including pre-existing `mergeDownloadFailures` ones)

- [ ] **Step 6: Commit**

```bash
git add plugin/src/zotero-rag.js plugin/src/dialog.js plugin/test/dialog.test.js
git commit -m "feat(plugin): wire two-phase citation-evidence submit flow in the ask dialog"
```

---

### Task 9: Update `docs/query-routing.md`

**Files:**
- Modify: `docs/query-routing.md`

- [ ] **Step 1: Document the new agent and protocol**

Add a new `### MentionsAgent` subsection to the "Components" section, after the existing `### MetadataAgent (backend/services/metadata_agent.py)` subsection:

```markdown
### `MentionsAgent` (`backend/services/mentions_agent.py`)

Answers "who cites/discusses work X" questions. Unlike `RAGAgent`/`MetadataAgent`,
it never retrieves anything itself — citation evidence only exists in the *citing*
document's full text, and the backend has no reliable lexical index for that (Qdrant
chunks are sized/embedded for semantic search, not citation lookup). Instead, the
Zotero client's own local full-text search index (built for every downloaded
attachment) supplies the evidence — see "Two-Phase Protocol for the `mentions`
Agent" below.

The router extracts `citation_targets` (author/year/title of the *cited* work),
kept structurally separate from `authors` (which means "written by"). Its
`capability_prompt` teaches the router this distinction with a worked example.
```

Add a new top-level section after "## `MetadataFilters` (`backend/models/filters.py`)":

```markdown
## Two-Phase Protocol for the `mentions` Agent

`citation_targets` evidence is gathered from the Zotero client's local full-text
search index (`fulltextWord` condition on `Zotero.Search`, snippets read from each
attachment's `.zotero-ft-cache` file) — the backend cannot retrieve it itself. When
the router selects `"mentions"` with non-empty `citation_targets` and the request
carries no `client_evidence`, `/api/query` returns immediately without running any
agents:

```json
{
  "status": "needs_client_evidence",
  "citation_targets": [{"author": "teubner", "year": null, "title_keywords": ["bukowina"]}],
  "query_plan": {"agents_to_use": ["mentions"], "filters": {"...": "..."}}
}
```

The plugin (`plugin/src/mentions.js`'s `findMentionEvidence()`) then searches the
user's local full-text index for documents whose text contains ALL requested
targets (set intersection), extracts up to 3 snippets per target per document
(240 chars, capped to the top 40 documents by match count), flags documents whose
own metadata identifies them as the cited work itself (`is_self`) or whose local
index is incomplete (`partial_index`), and resubmits:

```json
{
  "question": "...",
  "library_ids": ["..."],
  "client_evidence": {"items": [...], "truncated": false, "total_candidates": 3},
  "query_plan": {"agents_to_use": ["mentions"], "filters": {"...": "..."}}
}
```

Echoing `query_plan` back lets the orchestrator skip a second routing LLM call —
`QueryOrchestrator.query(..., preset_plan=...)` uses it directly.

**Known limitations:** full-text coverage is limited to attachments the user has
downloaded and locally indexed (irrelevant for group-library items other members
haven't synced); a "mention" is word co-occurrence, not a verified citation — the
synthesis LLM, not the search itself, judges each snippet; partial per-document
indexing (Zotero's `fulltext.pdfMaxPages`/`textMaxLength` prefs) can miss mentions
near the end of long documents, flagged via `partial_index` but not otherwise
compensated for.
```

- [ ] **Step 2: Commit**

```bash
git add docs/query-routing.md
git commit -m "docs: document the mentions agent and two-phase client-evidence protocol"
```

---

### Task 10: End-to-end manual verification

**Files:** none (verification only)

- [ ] **Step 1: Start the backend and plugin dev servers**

Follow "Live Server" and "Hot Reload Plugin Development Server" in `CLAUDE.md`. Confirm both are running from this checkout (not a stale process on the same port).

- [ ] **Step 2: Ask the motivating question in the Ask dialog**

In Zotero, open the Ask Question dialog against a library containing legal-theory literature with local full-text-indexed PDFs (per the empirical probing this plan is based on, the user's own library qualifies), and ask:

> Which publications cite Wiethölter (1975) and Teubner's article on the globale Bukowina?

- [ ] **Step 2: Confirm the two-phase round trip**

Watch the progress widget for "Scanning full text for citations..." (confirms `status: "needs_client_evidence"` was returned and the client-side search ran) followed by "Sending citation evidence to backend..." (confirms resubmission). Check `backend`'s log (`logs/server.log`, per `CLAUDE.md`'s Live Server section) for two `POST /api/query` requests for this question, the first fast (~1 LLM call, routing only) and the second slower (full evidence formatting + synthesis).

- [ ] **Step 3: Verify the answer's correctness**

Confirm the final answer:
- Lists publications that actually discuss Wiethölter's work in connection with Teubner's "Globale Bukowina" article (expect legal-theory items — Fischer-Lescano, Buckel/Christensen, Zabel, Auer/Seinecke, Baer, Rehbinder, per the empirical probe this plan is based on).
- Does **not** list Teubner's own "Globale Bukowina" article (or its English translation "Global Bukowina") as one of the citing publications.
- Hedges appropriately on any result flagged `partial_index` in the evidence (visible via `include_trace: true` if you want to inspect the raw evidence sent).

- [ ] **Step 4: Regression-check a plain content question**

Ask an unrelated question with no citation intent (e.g. "What is autopoiesis according to Luhmann?") and confirm it resolves in a single round trip with `status: "complete"` immediately — confirms the router doesn't spuriously select `"mentions"` for ordinary content questions.

- [ ] **Step 5: Run the full test suites one more time**

```bash
uv run pytest backend/tests/ -v -k "router or orchestrator or mentions or query_api"
node --test plugin/test/
```

Expected: PASS across the board.
