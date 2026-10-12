# Query Routing & Agent Architecture

## Overview

Rather than sending every question directly to a single RAG pipeline, the backend first
analyses the question with a lightweight LLM call (the *router*), then dispatches to one
or more specialised *agents* that each contribute context.  A final synthesis step
combines their output into a single answer.

```
User Question
     │
     ▼
QueryOrchestrator  (holds agent registry)
     │
     ├─── QueryRouter (one LLM call)
     │         ├── Assembles prompt from agents' capability_prompt fields
     │         └── Returns QueryPlan
     │               ├── agents_to_use: list[str]
     │               └── filters: MetadataFilters
     │
     ├─── selected agents — run in parallel via asyncio.gather(return_exceptions=True)
     │         ├── RAGAgent.execute(...)       → AgentResult (or isolated failure)
     │         └── MetadataAgent.execute(...)  → AgentResult (or isolated failure)
     │
     └─── Synthesis LLM call → Final Answer
          (skipped when only the RAG agent ran — direct pass-through)
```

## Components

### `QueryOrchestrator` (`backend/services/query_orchestrator.py`)

Entry point.  Manages the agent registry and coordinates the full pipeline.

```python
orchestrator = QueryOrchestrator(
    embedding_service, llm_service, vector_store, settings
)

# Register a custom agent (optional):
orchestrator.register(MyCustomAgent(...))

result = await orchestrator.query(
    question="...",
    library_ids=["1234567"],
    top_k=5,
    min_score=0.3,
    enable_routing=True,   # False → skip routing, use RAGAgent directly
)
```

`register(agent)` stores the agent under `agent.name`.  Registering a second agent
with the same name replaces the first.  Two agents are registered by default:
`"rag"` and `"metadata"`.

**Fault isolation:** selected agents run via `asyncio.gather(..., return_exceptions=True)`.
An agent that raises (e.g. `VectorStoreTimeoutError` from a slow Qdrant query) is logged
and excluded from the result set; sibling agents' results are still used for synthesis.
If every selected agent fails, the orchestrator re-raises the first failure, which
`POST /api/query` maps to a `504` for `VectorStoreTimeoutError` (see [API](#api) below)
or a generic `500` otherwise.

### `QueryRouter` (`backend/services/query_router.py`)

Builds the routing prompt at runtime from each registered agent's `capability_prompt`
and calls the LLM once with `temperature=0.0`.  The expected response is a JSON object:

```json
{
  "agents": ["rag"],
  "year_min": null,
  "year_max": null,
  "authors": [],
  "item_types": [],
  "title_keywords": [],
  "routing_description": "semantic content question"
}
```

On any parse failure or LLM error the router logs a warning and returns the safe default
`QueryPlan(agents_to_use=["rag"])`.

### `BaseAgent` (`backend/services/base_agent.py`)

Abstract base class every agent must implement:

```python
class MyAgent(BaseAgent):
    @property
    def name(self) -> str:
        return "my_agent"          # must be unique in the registry

    @property
    def capability_prompt(self) -> str:
        return "Describe what this agent does and when to use it."

    async def execute(
        self,
        question: str,
        library_ids: list[str],
        filters: MetadataFilters,
        **kwargs,
    ) -> AgentResult:
        ...
        return AgentResult(
            agent_name=self.name,
            context_text="...",    # text block for synthesis prompt
            sources=[...],         # list of SourceInfo or compatible dicts
        )
```

### `RAGAgent` (`backend/services/rag_agent.py`)

Wraps the existing `RAGEngine`.  Performs vector similarity search with optional
metadata pre-filtering.  Its `capability_prompt` emphasises content questions
(arguments, definitions, quotes).

### `MetadataAgent` (`backend/services/metadata_agent.py`)

Executes a payload-only Qdrant scroll (no query vector) via
`VectorStore.get_items_by_metadata()`.  Formats results as a numbered catalog list
`[M1] Author (Year) — Title [item_type]`.  Its `capability_prompt` emphasises
bibliographic listing questions.

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

## `MetadataFilters` (`backend/models/filters.py`)

A single Pydantic model shared across the entire pipeline:

| Field | Type | Qdrant condition |
|-------|------|-----------------|
| `year_min` / `year_max` | `Optional[int]` | `Range(key="year", gte=year_min, lte=year_max)` |
| `authors` | `list[str]` | one `MatchText(key="authors")` per name |
| `item_types` | `list[str]` | `MatchAny(key="item_type", any=[...])` |
| `title_keywords` | `list[str]` | one `MatchText(key="title")` per keyword |

Author and title matching use a full-text index
(`TextIndexParams(tokenizer=TokenizerType.WORD)`) created by
`VectorStore._ensure_chunks_indexes()`, so partial name matches work out of the box.

`MetadataFilters.is_empty()` returns `True` when all fields are at their default
(no filtering).

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
field (positional and stable for unchanged content —
`library_id:item_key:attachment_key:index` — NOT derived from content_hash),
not Qdrant's internal point ID, which this module never exposes externally.
`MentionsAgent`-derived
turns have no `source_refs` (client-gathered evidence is never stored
server-side) — a follow-up to such a turn falls back to conversation-history
text only.

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

## Answer Quality Guards

Independent of routing, both `RAGEngine.query()` (single-agent RAG path) and
`QueryOrchestrator._synthesize()` (multi-agent path) run the same post-generation
checks on the LLM's answer, retrying once with a targeted follow-up instruction if
a problem is detected (`_quality_issue_reinforcement()` in
`backend/services/rag_engine.py`, shared by both call sites):

- **Tool-call leak** — some models occasionally emit tool/function-call
  pseudocode as their entire answer even though no tools were ever offered.
  Detected via a regex over common call patterns; retried with an explicit
  "you have no tools" instruction.
- **Missing citations** — an answer with zero `[SN]` markers anywhere is retried
  with a reminder to cite sources per the CRITICAL CITATION RULE.
- **Low citation diversity** — when at least `low_diversity_available_floor`
  (default 3) distinct documents were available in context but the answer cites
  only one of them, it's retried with a neutral instruction to check whether the
  other sources are also relevant — deliberately not a blanket "cite more,"
  which risks false grounding on tangentially-relevant material.

Each check runs at most once per generation — a retry that's still flawed is used
as-is rather than looping further.

### Retrieval diversity escalation

Before generation, `RAGEngine.query()`'s vector search can come back saturated by
chunks from a single dominant document (e.g. one very finely-chunked paper
crowding out everything else in the top-`k` results). When the initial search
hits its `top_k` cap and returns fewer than `diversity_floor` (default 3) distinct
documents, it's retried once at a larger `top_k` —
`min(top_k * diversity_escalation_factor, diversity_escalation_max_top_k)` —
before generation runs at all. This is cheap relative to the LLM-retry guards
above: an extra vector search, not an extra LLM call. A `max_chunks_per_document`
cap (default 4) separately bounds how many chunks from any single document reach
the assembled context, so escalating can't let one document dominate the prompt
even after more documents are available.

All five constants above — `diversity_floor`, `diversity_escalation_factor`,
`diversity_escalation_max_top_k`, `max_chunks_per_document`,
`low_diversity_available_floor` — are optional fields on `QueryRequest` (see
[API](#api) below), threaded through `RAGAgent.execute()` and
`QueryOrchestrator.query()`/`_synthesize()` with the module-level constants in
`rag_engine.py` as defaults when omitted. The plugin sends its user's Preferences
pane values on every query rather than exposing them in the query dialog — see
[Plugin Settings Reference](plugin-settings.md#retrieval-tuning) for the
end-user-facing explanation of each, and `RetrievalTrace.escalated` /
`QueryTrace.parameters` (visible via `include_trace: true`) for observing them at
runtime.

### Score-proximity escalation

A second, independent escalation trigger alongside the diversity-floor one
above: when `RETRIEVAL_SCORE_PROXIMITY_EPSILON` > 0 (0-1, default 0 = off),
the pre-generation escalation also fires if the best candidate excluded by
the `top_k` cutoff scores within that fraction of the candidate pool's own
score range (max minus min score among the fetched/capped candidates) of the
cutoff score — i.e. the ranking looks like a close call at the boundary, even
when the window already has plenty of distinct documents (so the diversity
trigger never fires). A fixed *absolute* epsilon doesn't work for this:
dense embedding cosine scores cluster tightly and their dynamic range varies
a lot by query (observed 0.025-0.08 total spread across the gold question
set), so it either never fires or fires on almost every query; normalizing
to each query's own range makes it comparable. Implemented in
`_best_excluded_candidate_near_cutoff` (`backend/services/rag_engine.py`).

**Evaluated at `0.01` against the gold suite (#89's jin2026 crowd-out case)
and left at the `0` default (the setting exists to be turned on by whoever
deploys it, not to change the shipped default)**: jin2026's chunk (score
0.883 at rank 19, vs. a rank-10 cutoff score of 0.8857 — a gap of 0.3% of the
pool's own 2.6%-wide score range) triggers the escalation, raising `top_k`
from 10 to 30 and pulling it into context; the generated answer then
correctly cites and contrasts it. The same run also recovered all three of
Q05's previously-missing facts (`zotero_least`, `endnote_most`,
`reason_no_extraction`) — not targeted by this change, but also sitting just
below a close cutoff — with no regressions on the other eight questions
(composite rose +0.03 to +0.04 across both models tested; the one apparent
drop, Q06, had `escalated: false` in both runs, confirming it was LLM
generation variance at temperature 0.7 on a single repetition, not a
retrieval effect). Escalating roughly doubles that query's latency (extra
vector search, plus a larger generation context) — same cost profile the
existing diversity-floor trigger already has, just reached by a different
condition; across the gold set this fired on about 20% of queries. Does not
help #90's Q02/Q06/Q07 cases: their target chunks sit far below the cutoff
(e.g. Q02's ~44th out of 458), not just barely below it, so the gap is never
small relative to the pool's range and the trigger correctly doesn't fire —
see "Embedding-vector MMR reranking" below for what was tried for those.

### Embedding-vector MMR reranking

Prototype for #90's "global ranking dilution" case, where several chunks
from *different*, topically-adjacent-but-not-actually-relevant documents
together outscore a genuinely relevant chunk without any one of them
dominating (so neither the per-document cap/dedup nor the score-proximity
trigger above can reach it — the competition is pool-wide and the gap is
large, not a close call at the boundary). `RETRIEVAL_EMBEDDING_MMR_LAMBDA`
(0-1, default 0 = off) re-ranks the whole raw candidate pool by real MMR
(`lambda * relevance - (1 - lambda) * max_similarity_to_already_selected`)
over actual embedding vectors fetched from Qdrant
(`VectorStore.search(..., with_vectors=True)`) — unlike the existing
per-document `_select_diverse_chunks`, which only runs within one document
and uses a cheap token-overlap similarity proxy, not real embeddings.
Implemented in `backend/services/embedding_mmr_rerank.py`.

**Evaluated at `lambda=0.7` against the gold suite (#90's Q02/Q06/Q07 cases)
and left at the `0` default — not recommended for production use as
implemented, for two independent reasons:**

1. **It doesn't fix the targeted facts.** Q02's `endnote_top`/
   `refmanager_refworks` and Q06/Q07's facts stayed out of context; Q08's
   `jin_negative` (already fixed by the score-proximity trigger above) came
   back *out* of context too. The chunks these facts need are far enough
   below the cutoff that trading relevance for distinctness among the
   already-in-range candidates doesn't reach them — MMR reorders *which*
   distinct chunks win the window, it doesn't widen the window.
2. **The reference implementation is far too slow to ship regardless.**
   Computing pairwise cosine similarity in pure Python over the candidate
   pool is effectively O(n³) in the pool size (each of the ~n greedy
   selection steps scans the remaining candidates, each comparing against a
   growing selected set): a synthetic 100-candidate, 1024-dim benchmark
   measured **~17.7 seconds** of added CPU time alone, and a live gold-suite
   run against real per-query pool sizes (30-51 candidates) measured
   individual queries taking 8-41 seconds versus 2-31 seconds for the same
   questions without it. Fetching the vectors themselves is cheap by
   comparison (+12ms median round-trip over 100 results, measured directly
   against the sidecar) — the Python-side MMR loop is the entire cost.
   Vectorizing with NumPy would likely fix this, but NumPy is only a
   transitive dependency today (via `qdrant-client`), not a declared one —
   and reason 1 above already rules this variant out on its own merits, so
   adding a new direct dependency to fix only the latency wasn't pursued.

### Lexical (BM25) fusion

Vector similarity under-ranks short, term-dense chunks (numeric results, proper
nouns). With `RETRIEVAL_LEXICAL_WEIGHT` > 0 (0-1, default 0 = off), the raw
candidate pool is re-scored before dedup, the per-document cap and the `top_k`
cut as `(1 - w) * norm(vector) + w * norm(BM25)` (min-max over the pool; BM25
document frequencies come from the pool itself, so no extra index is needed).
The blended score replaces the vector score for all downstream ordering.
Implemented in `backend/services/lexical_rerank.py`.

**Evaluated at `0.2`/`0.4`/`0.6` against the gold suite (#89/#90's specific
under-recall cases) and left at the `0` default**: none of the targeted facts
(Lorenzetti's EndNote/Reference-Manager/RefWorks breakdown in Q02, Mendeley's
2 GB figure in Q06, Zotero's least-effort finding in Q05) entered the
retrieved context at any tested weight — the blend doesn't change which
*documents* reach the candidate pool, and these chunks' BM25 score isn't high
enough relative to competing chunks to climb in either. Worse, two previously
retrieved facts regressed: Q07's numeric results (present at `0`) dropped out
of context from `0.2` upward, and `jin2026` (#89's crowd-out case) ranked
*lower* as weight increased (19th at `0`, 30th at `0.2`/`0.4`, 33rd at `0.6`)
— its chunk has strong vector similarity but weak literal term overlap with
the query, so blending in a weak BM25 score pulls its combined score down
rather than up. Aggregate `fact_recall` across the full 10-question gold set
did rise at `0.2`/`0.4` (unrelated facts benefited) before falling back at
`0.6`, but that's a different, broader effect than what this setting was
built to fix, and doesn't offset the specific regressions above. Raising it
further is not expected to help: the failure mode is score dilution, not an
insufficient blend ratio.

## Schema Versioning

`CURRENT_SCHEMA_VERSION` (in `backend/models/document.py`, currently `6`) is stored
in every Qdrant chunk payload. Each bump added a new filterable field — see the
version history comments next to the constant, and
[docs/indexing.md](indexing.md#schema-versioning-and-metadata-migration) for the
full migration mechanism.

When the plugin calls `GET /api/libraries/{library_id}/check-indexed`:
- items with `stored_schema_version < CURRENT_SCHEMA_VERSION` receive
  `needs_metadata_update: true` in the response
- the plugin collects those items and calls `POST /api/index/items/metadata` with
  their current Zotero metadata — no file re-upload, no re-embedding

This keeps the Qdrant payload in sync with the current schema without requiring full
re-indexing of unchanged documents.

## API

### `POST /api/query`

```json
{
  "question": "Where does Luhmann define autopoiesis?",
  "library_ids": ["users/1234567"],
  "top_k": 5,
  "min_score": 0.3,
  "enable_routing": true
}
```

Set `enable_routing: false` to bypass the routing LLM call and go straight to
`RAGAgent` (faster, but no metadata-filter extraction, catalog search, or
citation/mentions search).

Also accepts five optional retrieval/answer-diversity tuning fields —
`diversity_floor`, `diversity_escalation_factor`, `diversity_escalation_max_top_k`,
`max_chunks_per_document`, `low_diversity_available_floor` — each falling back to
`rag_engine.py`'s module-level defaults when omitted. See
[Answer Quality Guards](#answer-quality-guards) above.

**Error responses:**

| Status | Cause |
| --- | --- |
| `504` | The Qdrant search backend did not respond within its configured timeout (`VectorStoreTimeoutError`) |
| `500` | Any other unhandled error |

### `POST /api/index/items/metadata`

Lightweight metadata-only update — no file bytes, no re-embedding. Called both by
the plugin's schema-migration path and by its live client-side metadata sync
(edits pushed within seconds of a title/author/tag/year/type change) — see
[docs/indexing.md](indexing.md#live-client-side-metadata-sync). Every field is
optional except `item_key`; an omitted field leaves the existing stored value
untouched rather than being cleared:

```json
{
  "library_id": "users/1234567",
  "items": [
    {
      "item_key": "ABC123",
      "title": "Social Systems",
      "authors": ["Luhmann, N."],
      "tags": ["systems theory", "sociology"],
      "year": 1984,
      "item_type": "book",
      "item_version": 42,
      "zotero_modified": "2026-01-01T00:00:00Z"
    }
  ]
}
```

## Writing a Custom Agent

1. Subclass `BaseAgent` and implement `name`, `capability_prompt`, and `execute()`.
2. Register it with the orchestrator before the first query:

```python
orchestrator = QueryOrchestrator(...)
orchestrator.register(MyCustomAgent(...))
```

The router will automatically include your agent's `capability_prompt` in the routing
prompt and may select it when appropriate.  No changes to the orchestrator or router
are needed.
