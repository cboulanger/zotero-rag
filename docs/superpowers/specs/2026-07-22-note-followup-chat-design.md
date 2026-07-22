# Interactive Follow-Up Chat on RAG Result Notes — Design

## 1. Goal

Today, asking a question is a one-shot operation: the plugin sends one `/api/query` request, the backend routes it through agents and synthesizes an answer, and the plugin writes a static note. There is no way to refine the answer, ask a follow-up, or drill into a specific cited source without starting an entirely new query from scratch. This feature adds a follow-up chat surface attached to each RAG result note, backed by a backend extension that lets follow-up turns reuse the evidence already retrieved instead of re-running routing and retrieval from zero.

## 2. Current state (what already exists)

- **Query pipeline is fully stateless.** `QueryOrchestrator.query()` (`backend/services/query_orchestrator.py`) takes no session or conversation concept. Every call independently routes (`QueryRouter`), runs selected agents (`RAGAgent`, `MetadataAgent`, `MentionsAgent`), and synthesizes once. See `docs/query-routing.md`.
- **A precedent for echoing state back to the client already exists.** The `mentions` two-phase protocol has the backend return `status: "needs_client_evidence"` plus a `query_plan`; the plugin gathers local full-text evidence and resubmits the same `query_plan`, letting `QueryOrchestrator.query(..., preset_plan=...)` skip a second routing call. This is the template this feature generalizes.
- **`QueryResponse` (`backend/api/query.py`) only carries citation metadata** (`item_id`, `library_id`, `title`, `page_number`, `text_anchor`, `relevance_score`) — not the actual chunk text used to synthesize the answer. That text is assembled into a prompt and discarded once the LLM call returns.
- **The result note is a static, one-time export.** `ZoteroRAG.createResultNote()` (`plugin/src/zotero-rag.js:1183`) builds HTML via `formatNoteHTML()` from a single `QueryResult` and opens it with `zoteroPane.openNoteWindow(note.id)`.
- **`note.xhtml` (Zotero's standalone note window) has no item pane.** Checked directly in the Zotero source (`chrome/content/zotero/note.xhtml`): it is a bare ProseMirror editor window (`windowtype="zotero:note"`), with no sidenav or item-pane region. `Zotero.ItemPaneManager.registerSection()` (`chrome/content/zotero/xpcom/pluginAPI/itemPaneManager.js:548`) only renders into the main window's item pane or the reader's context pane — never into that standalone window. This means `createResultNote()`'s current behavior of popping a standalone window is incompatible with attaching a live chat panel to the note, and must change (§5.6).
- **No generic "agent/router needs more from the user" abstraction exists** — `NeedsClientEvidenceError` (`backend/services/base_agent.py`) is a single-purpose exception handled by one `except` clause in `backend/api/query.py`.

## 3. Scope decisions

Confirmed during design and shaping the rest of this spec:

| Decision | Choice |
|---|---|
| Primary use case | Drilling into the already-retrieved answer (clarify, expand, ask about a specific source) — not a general open-ended chatbot |
| Where the chat UI lives | A new `Zotero.ItemPaneManager` section attached to notes tagged `RAG Query Result`, not a persistent dialog window or standalone chat tab |
| Conversation persistence | Session-only. Conversation state lives in the plugin's in-memory map for as long as Zotero/the note stays open. Every turn is also appended to the note's saved HTML, so the *record* survives; only the *cheap-continuation* mechanics are session-scoped |
| Context carried per follow-up | Full structured context — prior Q&A, which agents ran, and opaque per-agent evidence references — not just plain transcript text |
| Extensibility | Follow-up handling is a new **agent** (`ContinuationAgent`), riding the existing router/agent-registry framework, not a special-cased branch outside it. A generalized "needs more input from the user" channel replaces the single-purpose mentions exception, so a third future case doesn't need a fourth bespoke protocol |
| Too-broad-query handling | Both the router (before running any agent, from the question text alone) and individual agents (after execution, once they know the actual hit count) can signal "ask the user to narrow this" instead of proceeding to synthesis |
| Mixed clarification policy | If some selected agents return usable content while another flags "too broad," synthesis proceeds using the usable content, with a caveat about the oversized listing folded into the prompt. Only short-circuits to a clarification response if *no* selected agent produced usable content |
| Mentions-derived turns | `MentionsAgent` evidence is gathered client-side and never stored server-side, so it has no re-fetchable evidence reference. Continuing a mentions-derived answer works via conversation-history text only (no chunk re-fetch) — an accepted v1 limitation, not a blocker |
| Backend restart resilience | No backend-side session state is introduced. Every follow-up request is self-contained (it carries the full conversation history and evidence references), so a backend restart mid-conversation (this deployment restarts frequently — see root `CLAUDE.md`'s hotfix workflow) does not break continuation; the next request just works against a freshly started process the same as any other request |

## 4. Architecture

```
Zotero: select "RAG Query Result" note in main pane
        │
        ▼
ItemPaneManager section "zotero-rag-chat" (plugin/src/chat-pane.js)
        │  reads ChatPane._conversations.get(note.id)  (in-memory, empty after
        │  a Zotero restart or on a note never seeded — see §5.6)
        ▼
User types follow-up → ChatPane.submitFollowUp(note, question)
        │  builds request from stored ChatTurn[] history + library_ids
        ▼
POST /api/query  { question, library_ids, conversation_history: ChatTurn[] }
        │
        ▼
QueryRouter.route(...)                       (backend/services/query_router.py)
        │  prompt now includes a compact rendering of conversation_history
        │  ├─ may set clarification_needed/clarification_question directly
        │  └─ else picks agents_to_use, possibly including "continuation"
        ▼
QueryOrchestrator.query(...)                 (backend/services/query_orchestrator.py)
        │  ├─ clarification_needed from router → short-circuit, no agents run
        │  ├─ else run selected agents in parallel (rag / metadata / mentions / continuation)
        │  │     ContinuationAgent re-fetches prior source_refs by ID from Qdrant
        │  │     (VectorStore.get_chunks_by_ids) — no embedding call, no search
        │  ├─ any agent flags needs_clarification + none produced usable content
        │  │     → short-circuit, no synthesis call
        │  └─ else synthesize (with a caveat folded in if only *some* agents flagged it)
        ▼
QueryResponse { answer, sources, source_refs, status, clarification_message? }
        │
        ▼
ChatPane appends the turn to its in-memory history, re-renders the transcript,
and appends the same Q&A into the note's saved HTML (note.setNote() + saveTx())
```

## 5. Component detail

### 5.1 `backend/models/conversation.py` (new file)

```python
class ChatTurn(BaseModel):
    """One prior turn in a follow-up conversation, echoed back verbatim by the client."""
    question: str
    answer: str
    agents_used: list[str] = []
    source_refs: list[str] = []          # opaque evidence refs from that turn — see AgentResult.source_refs
    query_plan: Optional["QueryPlan"] = None
```

A clarification turn (the backend asked the user to narrow their question) is represented the same way: `answer` is the clarification message, `source_refs`/`agents_used` are empty. No separate variant — one uniform type keeps the router prompt-rendering and the plugin's transcript rendering the same code path regardless of turn kind.

### 5.2 `backend/services/base_agent.py` changes

`AgentResult` gains three additive fields:

```python
class AgentResult(BaseModel):
    agent_name: str
    context_text: str
    sources: list = []
    source_refs: list[str] = []              # NEW: opaque IDs (Qdrant point IDs for rag/metadata) a
                                              #      later ContinuationAgent call can re-fetch by ID
    needs_clarification: bool = False        # NEW: this agent judges the query too broad to answer well
    clarification_message: Optional[str] = None   # NEW: human-readable narrowing prompt
    clarification_suggestions: list[str] = []     # NEW: optional hints (e.g. "filter by year"); empty
                                                   #      in v1 — a hook for later facet-based suggestions,
                                                   #      not populated by any agent yet
```

`QueryPlan` (the router's decision) gains the router-level, pre-execution equivalent:

```python
class QueryPlan(BaseModel):
    agents_to_use: list[str] = ["rag"]
    filters: MetadataFilters = MetadataFilters()
    routing_description: Optional[str] = None
    clarification_needed: bool = False       # NEW
    clarification_question: Optional[str] = None   # NEW
```

A single exception hierarchy replaces today's one-off `NeedsClientEvidenceError`, so the API layer's handling generalizes to a third case without a new bespoke branch:

```python
class NeedsUserInputError(Exception):
    """Base: the orchestrator cannot proceed to synthesis without more from the user."""

class NeedsClientEvidenceError(NeedsUserInputError):
    ...  # unchanged — mentions two-phase protocol

class NeedsClarificationError(NeedsUserInputError):
    def __init__(self, message: str, plan: QueryPlan):
        self.message = message
        self.plan = plan
```

### 5.3 `backend/services/continuation_agent.py` (new file)

```python
class ContinuationAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "continuation"

    @property
    def capability_prompt(self) -> str:
        return (
            "Use when the follow-up elaborates on, clarifies, or compares evidence "
            "already retrieved in this conversation, without needing a new search "
            "(e.g. 'explain that more', 'what does source 3 say about X', "
            "'how do these two sources differ')."
        )

    async def execute(self, question, library_ids, filters, **kwargs) -> AgentResult:
        conversation_history: list[ChatTurn] = kwargs.get("conversation_history", [])
        prior_refs = [r for turn in conversation_history for r in turn.source_refs]
        chunks = await self.vector_store.get_chunks_by_ids(prior_refs) if prior_refs else []
        # Missing IDs (item deleted/reindexed since) are tolerated — proceed with whatever's found;
        # if nothing is found, context_text falls back to conversation history text alone.
        context_text = render_conversation(conversation_history) + render_chunks(chunks)
        sources = sources_from_chunks(chunks)
        return AgentResult(
            agent_name=self.name,
            context_text=context_text,
            sources=sources,
            source_refs=[c.chunk_id for c in chunks],  # re-propagate for the *next* follow-up turn
        )
```

Registered in `QueryOrchestrator.__init__` alongside `rag`/`metadata`/`mentions`. `MentionsAgent`-sourced turns have no `source_refs` (client evidence is never stored server-side), so `ContinuationAgent` on such a turn falls back to conversation-history text only — no chunk re-fetch, no error.

### 5.4 `backend/db/vector_store.py` addition

```python
async def get_chunks_by_ids(self, chunk_ids: list[str]) -> list[DocumentChunk]:
    """Payload-only retrieve by point ID — no embedding call, no similarity search."""
    points = await asyncio.to_thread(self.client.retrieve, collection_name=self.CHUNKS_COLLECTION, ids=chunk_ids)
    return [self._point_to_chunk(p) for p in points]
```

### 5.5 `backend/services/query_router.py` and `query_orchestrator.py` changes

- `QueryRouter`'s prompt builder now includes a compact rendering of `conversation_history` when present (question/answer pairs, capped at `max_conversation_context_chars` — default `6000`, new `Settings` field, keeping the most recent turns and dropping older ones once the cap is hit), so the router can judge continuation vs. fresh search per turn, and can set `clarification_needed`/`clarification_question` directly for a question that's obviously unconstrained (e.g. a catalog dump with zero filters across a large library) before any agent runs.
- `QueryOrchestrator.query()` gains an optional `conversation_history: list[ChatTurn] = []` parameter, threaded into each agent's `execute(**kwargs)` call (agents that don't use it — today's `rag`/`metadata`/`mentions` — simply ignore the kwarg, no signature change required).
- After routing: if `plan.clarification_needed`, raise `NeedsClarificationError` immediately — no agents execute.
- After agent execution: partition results by `needs_clarification`. If **all** executed agents flagged it, raise `NeedsClarificationError` using the first non-empty `clarification_message` (no synthesis call — this is also strictly cheaper than today's always-synthesize path). If **some but not all** did, proceed to synthesis, appending each flagged agent's `clarification_message` into the synthesis prompt as a caveat instruction (e.g. "mention narrower filtering is available for the catalog portion of this answer").
- `MetadataAgent` gains the actual threshold check: after its Qdrant scroll, if the matched count exceeds `settings.metadata_narrowing_threshold` (new `Settings` field, default `50`, env var `METADATA_NARROWING_THRESHOLD`), it sets `needs_clarification=True` and a generic `clarification_message` (e.g. `f"Found {count} matching items — try narrowing by year, author, or item type."`) instead of formatting all of them into `context_text`.

### 5.6 `backend/api/query.py` changes

- `QueryRequest` gains `conversation_history: list[ChatTurn] = []` and `force_fresh_retrieval: bool = False` (the follow-up UI's "start fresh search" override — when true, the orchestrator still records the turn but ignores `conversation_history` for routing/agent selection, running a full `enable_routing` pass as if it were a brand-new question).
- `QueryResponse` gains `source_refs: list[str] = []` (union of every executed agent's `source_refs`, so the plugin can store it verbatim for the next turn) and generalizes `status` to `Literal["complete", "needs_client_evidence", "needs_clarification"]`, with `clarification_message: Optional[str] = None` populated alongside the existing `citation_targets`/`query_plan` echo fields.
- A new `_needs_clarification_response()` mirrors the existing `_needs_evidence_response()`; both are now dispatched from a single `except NeedsUserInputError` structure that narrows on the concrete subtype.

### 5.7 `plugin/src/chat-pane.js` (new file)

Loaded eagerly from `bootstrap.js` (same pattern as `task_queue.js`), so the section is registered for the plugin's whole lifetime:

```js
Zotero.ItemPaneManager.registerSection({
    paneID: 'zotero-rag-chat',
    pluginID: ZOTERO_RAG_PLUGIN_ID,
    header: { l10nID: 'zotero-rag-chat-header', icon: 'chrome://zotero-rag/content/icons/chat16.svg' },
    sidenav: { l10nID: 'zotero-rag-chat-sidenav', icon: 'chrome://zotero-rag/content/icons/chat20.svg' },
    onItemChange: ({ item, setEnabled }) => {
        setEnabled(!!item && item.isNote() && item.hasTag('RAG Query Result'));
    },
    onRender: ({ body, item }) => ChatPane._render(body, item),
});
```

New localization strings (`zotero-rag-chat-header`, `zotero-rag-chat-sidenav`) added to `plugin/locale/en-US/zotero-rag.ftl`.

`ChatPane._conversations: Map<noteID, ChatTurn[]>` holds session state. Two entry points populate it:

1. **Seeding at creation.** `createResultNote()` (§5.8) calls `ChatPane.seedConversation(note.id, libraryIds, [firstTurn])` right after the note is saved, so the chat section has continuation context (`source_refs`, `query_plan`) available immediately — no need to parse it back out of the note's HTML.
2. **A note selected with no map entry** (e.g. after a Zotero restart, or any note that predates this feature) simply renders an empty transcript plus the input box. The first follow-up submitted in that state carries an empty `conversation_history`, which the backend treats exactly like a brand-new question — full routing, full retrieval, no error, just no continuation cheapness. This is the deliberate fallback rather than reconstructing state by parsing the note's saved HTML back into structured turns, which would be brittle and isn't needed given the "session-only" scope decision.

`ChatPane.submitFollowUp(note, question, { forceFresh = false } = {})`:

- Reads `_conversations.get(note.id) || []`, POSTs to `/api/query` with `question`, the seeded `library_ids`, `conversation_history` (mapped straight from stored `ChatTurn`s), and `force_fresh_retrieval: forceFresh`.
- On `status: "complete"`: appends the new `ChatTurn` to the map, re-renders the transcript, and appends the turn's HTML (reusing a factored-out per-turn formatter shared with `formatNoteHTML()`) to the note via `note.setNote(existingHtml + turnHtml)` + `note.saveTx()`.
- On `status: "needs_clarification"`: appends a `ChatTurn` whose `answer` is the `clarification_message` (rendered in the transcript as an assistant question, no citations) — the user's next typed message continues the same `conversation_history`, so the previously-extracted filters in `query_plan` carry forward and get refined rather than reset.
- On `status: "needs_client_evidence"`: reuses the existing client-side evidence gathering in `mentions.js` unchanged, then resubmits with the echoed `query_plan` — the only change from today is that this round-trip is now reachable mid-conversation via the chat pane's submit path, not only from the original dialog.

A "Start fresh search" button in the pane calls `submitFollowUp(note, question, { forceFresh: true })` for the (expected to be rare) case where a follow-up genuinely needs new retrieval rather than continuing.

### 5.8 `plugin/src/zotero-rag.js` changes

`createResultNote()` changes from `zoteroPane.openNoteWindow(note.id)` to `zoteroPane.selectItem(note.id)` (selecting it in the main library view, where the item pane — and the new chat section — is visible), and calls `ChatPane.seedConversation(...)` right after `note.saveTx()`. A user can still open a standalone note window via Zotero's own UI if they want the larger editor view; like every other item-pane section, ours simply won't be visible there, matching how all `ItemPaneManager` sections behave today.

## 6. Data flow — worked examples

**Drill-down follow-up (the common case).** User asks "Where does Luhmann define autopoiesis?", gets an answer citing three sources, tagged as a note. They select the note, the chat pane appears (seeded with `source_refs` for those three chunks), and type "Can you say more about the second one?" `submitFollowUp` sends `conversation_history: [that first turn]`. The router sees the conversation and picks `agents_to_use: ["continuation"]`. `ContinuationAgent` re-fetches the three chunks by ID from Qdrant (no embedding call, no vector search), synthesizes an answer focused on the second source, and returns fresh `source_refs` (same three IDs). The turn is appended to the pane and to the note.

**Too-broad query triggers clarification.** User asks "What has Luhmann written about?" across a library with 400 indexed items. The router's own pre-check (unconstrained catalog-style question, no filters) sets `clarification_needed: true` directly — no agent runs — and the response comes back as `status: "needs_clarification"` with a message asking to narrow by year, work, or topic. This renders as an assistant turn in the chat pane. The user replies "Just about autopoiesis in the 1980s" — this becomes the next `conversation_history` turn; the router, now seeing both messages, extracts `authors: ["Luhmann"]`, `year_min/max`, `title_keywords: ["autopoiesis"]` and proceeds normally.

## 7. Error handling summary

| Situation | Behavior |
|---|---|
| Backend restarts mid-conversation | No effect on correctness — every request is self-contained (`conversation_history` + `source_refs` sent by the client each time); the next follow-up works against the freshly started process identically to any other request |
| Zotero itself restarts / plugin reloads | In-memory `_conversations` map is lost; the note's saved HTML still shows the full past conversation, but the next follow-up starts with empty history — full routing/retrieval runs, no error, no continuation cheapness |
| A `source_ref` chunk ID no longer exists (item deleted/reindexed since that turn) | `get_chunks_by_ids()` returns fewer chunks than requested; `ContinuationAgent` proceeds with whatever was found, or falls back to conversation-history text only if none were found — never errors |
| Conversation grows long | Router prompt keeps the most recent turns fitting under `max_conversation_context_chars` (default 6000), dropping older turns silently; no user-facing truncation notice in v1 |
| Some agents flag `needs_clarification`, others return usable content | Synthesis proceeds using the usable content; a caveat about the oversized/ambiguous portion is folded into the synthesis prompt rather than blocking the whole answer |
| All selected agents flag `needs_clarification` | Short-circuits to `status: "needs_clarification"` before any synthesis call — cheaper than today's always-synthesize path, not just safer |
| Follow-up needs genuinely new evidence, not covered by prior `source_refs` | User (or eventually the router) can force `force_fresh_retrieval: true`, running the normal full pipeline for that turn while still recording it as part of the same conversation |
| Continuing a `mentions`-derived turn | No stored server-side evidence to re-fetch (client-gathered, never persisted); `ContinuationAgent` falls back to conversation-history text only — accepted v1 limitation, not an error path |

## 8. Extensibility

Two abstractions are introduced specifically so future work doesn't require touching the orchestrator core:

1. **Continuation is an agent, not a special case.** A future `CompareAgent` ("how do these two sources differ"), `CritiqueAgent` ("are you sure about that citation"), or similar chat-specific behavior registers the same way `ContinuationAgent` does — a `capability_prompt` the router can choose, an `execute()` that optionally reads `conversation_history` from kwargs. No changes to `QueryOrchestrator` or `QueryRouter` are needed to add one, exactly as already documented for non-chat agents in `docs/query-routing.md`.
2. **`NeedsUserInputError` is a family, not a single case.** Today it covers `needs_client_evidence` (mentions) and `needs_clarification` (too-broad query). A third future case — e.g. disambiguating two same-named authors — adds one more subtype and one more `QueryResponse.status` literal value, without inventing a new wire-protocol shape.

`AgentResult.clarification_suggestions` is defined now but left unpopulated by any agent in v1 — a hook for a later enhancement (e.g. real facet aggregation: top matching authors/years/types) without a schema change when that's built.

## 9. Testing plan

- **Backend unit tests**: `ContinuationAgent.execute()` against a mocked `vector_store.get_chunks_by_ids` (found chunks, partially-missing chunks, zero chunks → text-only fallback); router response parsing for `clarification_needed`/`clarification_question`; `QueryOrchestrator`'s partition logic for mixed vs. all-clarification agent results (fake agents forcing each combination); `MetadataAgent`'s threshold behavior (count above/below `metadata_narrowing_threshold`).
- **Backend integration tests**: a full `/api/query` round trip with `conversation_history` populated and `enable_routing=False` + `query_plan` preset to `agents_to_use: ["continuation"]` (bypassing the real router LLM call), confirming the response's `source_refs` match what was re-fetched.
- **Plugin unit tests** (`plugin/test/`): `ChatPane` transcript rendering from a `ChatTurn[]` array; `submitFollowUp` payload construction (correct `conversation_history`/`source_refs` echoed, `force_fresh_retrieval` flag wiring); per-turn note-HTML-append formatter.
- **Manual verification**: a real end-to-end drill-down conversation on a live note; a deliberately unconstrained query that triggers `needs_clarification` and a follow-up reply that resolves it; a backend restart (`sudo systemctl restart zotero-rag.service` in a dev/hotfix-style setup, or just restarting the local `npm start` server) mid-conversation confirming continuation still works; a Zotero restart confirming the graceful fallback to a fresh, full-routing query.
