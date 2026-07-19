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
     ├─── selected agents — run in parallel via asyncio.gather
     │         ├── RAGAgent.execute(...)       → AgentResult
     │         └── MetadataAgent.execute(...)  → AgentResult
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
`RAGAgent` (faster, but no metadata-filter extraction or catalog search).

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
