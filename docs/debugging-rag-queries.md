# Debugging RAG Queries

When a question returns an unexpected answer — wrong sources, an empty response, a missed topic, or a seemingly hallucinated fact — the query trace reveals exactly what happened at every step. This guide explains how to capture a trace, how to read it, and what to look for in each section.

## Capturing a trace

```bash
uv run python scripts/query_trace.py "Your question here" \
    --library-ids <library_id> \
    --output trace.json
```

Full options:

```
--library-ids ID [ID ...]   One or more library IDs to search (required)
--output FILE               Write JSON to FILE instead of stdout
--url URL                   Backend base URL (default: $ZOTERO_RAG_URL or http://localhost:8119)
--api-key KEY               API key (default: $ZOTERO_RAG_API_KEY)
--top-k N                   Number of chunks to retrieve (default: preset value)
--min-score F               Minimum similarity score threshold (default: preset value)
--no-routing                Skip the routing step and go straight to semantic search
--llm-model MODEL           Override the configured LLM model
--trace-only                Output only the trace section, omitting answer and sources
```

The script calls `POST /api/query` with `include_trace: true`. The same flag can be set in any direct API call to the endpoint.

## The pipeline

Each query passes through three stages in sequence:

```
Question
  │
  ▼
[1. Routing]
  LLM call that classifies the question, selects which agent(s) to invoke,
  and extracts any bibliographic filters (author, year range, item type, …)
  │
  ▼
[2. Agent execution]  ← one or both agents run (in parallel if both)
  │
  ├── RAG agent: semantic vector search over document content → LLM generates answer
  │
  └── Metadata agent: payload-index lookup over bibliographic fields → formatted list
  │
  ▼
[3. Synthesis]  ← only when both agents ran
  LLM call that merges the two agents' results into a single coherent answer
  │
  ▼
Response
```

## The two agents

### RAG agent

The RAG agent answers questions about *content*: arguments, definitions, quotes, explanations, comparisons. It works in two sub-steps:

1. **Embedding + vector search** — the question is converted to a vector and compared against every indexed chunk. The `top_k` highest-scoring chunks above `min_score` are retrieved.
2. **LLM generation** — retrieved chunks are assembled into a numbered context block (`[S1: …]`, `[S2: …]`, …) and passed with the question to the LLM, which writes a grounded answer with inline citations.

Any bibliographic filters extracted by the router (e.g. `authors: ["mertz"]`) are applied to the vector search, so only chunks from matching documents are considered.

### Metadata agent

The metadata agent answers questions about *what items exist in the library*: listing papers by an author, finding books from a decade, enumerating items of a certain type. It does **no semantic search** — it queries the Qdrant payload index directly by author last name, year range, item type, and/or title keywords, and returns a formatted list of matching items.

Because it never reads document content, it cannot answer "what does paper X say about Y". It is only useful when the question is fundamentally about the library catalog itself.

### Routing: how the choice is made

The router makes a single LLM call at temperature 0 (deterministic). It receives the capability descriptions of both agents and the question, and returns a JSON object specifying:

- `agents` — which agent(s) to invoke (`["rag"]`, `["metadata"]`, or `["rag", "metadata"]`)
- `authors`, `year_min`, `year_max`, `item_types`, `title_keywords` — filters to pass to the selected agents
- `routing_description` — one sentence explaining the decision

The key distinction the router tries to make:

| Question type | Correct routing |
|---|---|
| "What does Luhmann say about autopoiesis?" | `rag` |
| "Which books from 2010–2015 are in the library?" | `metadata` |
| "List all papers by Smith and summarise their argument" | `rag` + `metadata` |
| "Which publications by Mertz deal with the law school curriculum?" | `rag` — the word "which" looks like a listing question, but the real ask is *about content* (what do they deal with), so content retrieval is needed |

Both agents are invoked together only when the question explicitly wants a catalog listing *and* asks about the content of what is found.

---

## Reading the trace

The trace JSON has five top-level sections: `parameters`, `routing`, `agent_executions`, `llm_calls`, and the top-level timing fields.

### `parameters`

```json
"parameters": {
  "top_k": 10,
  "min_score": 0.35,
  "enable_routing": true,
  "llm_model": null
}
```

Confirms what settings were actually applied. If `top_k` or `min_score` differs from what you expected, the preset configuration may have overridden your values.

---

### `routing`

```json
"routing": {
  "prompt": "...",
  "llm_response": "{\"agents\": [\"rag\"], \"authors\": [\"mertz\"], ...}",
  "plan": {
    "agents_to_use": ["rag"],
    "filters": { "authors": ["mertz"], ... },
    "routing_description": "The question asks about the content of publications by Mertz, ..."
  },
  "duration_ms": 5767
}
```

**What to check:**

- **`plan.agents_to_use`** — is the right agent selected? If `metadata` was chosen for a content question, or `rag` for a pure catalog question, the routing LLM misclassified. See *Wrong agent selected* below.
- **`plan.filters.authors`** — was the author name extracted? If the question mentions an author but the filter is empty, the vector search won't be restricted. If it's over-restricted (e.g. a co-author's name was extracted when you only meant the first author), that limits recall.
- **`plan.filters.year_min` / `year_max`** — year ranges narrow the search. A misread year silently drops everything outside the window.
- **`routing_description`** — the LLM's one-sentence rationale. Reading this often immediately reveals whether it understood the question correctly.
- **`duration_ms`** — the routing call alone. If this is several seconds, the LLM endpoint is slow. Use `--no-routing` to bypass routing entirely and isolate the problem.

---

### `agent_executions`

One entry per agent that ran. For the RAG agent this contains a nested `retrieval` block.

#### RAG agent — `retrieval`

```json
"retrieval": {
  "embedding_model": "unknown",
  "embedding_dims": 1024,
  "search_params": {
    "top_k": 10,
    "min_score": 0.35,
    "library_ids": ["4691570"],
    "filters": { "authors": ["mertz"], ... }
  },
  "raw_results_count": 10,
  "score_stats": { "min": 0.844, "max": 0.877, "avg": 0.852 },
  "documents_grouped": 6,
  "chunks": [ ... ]
}
```

**What to check:**

- **`raw_results_count`** — how many chunks passed the `min_score` threshold. If this is 0, the answer will be "I couldn't find any relevant information". See *No results* below.
- **`score_stats`** — the similarity score range. Scores cluster near 1.0 for good embeddings. A `max` below 0.6 suggests the question is asking about topics not well represented in the indexed documents. A very wide spread (e.g. 0.4–0.9) means only the top few chunks are actually relevant.
- **`documents_grouped`** — after deduplication by document, how many unique papers are in the context. The LLM context has one `[Sn]` label per document, so if 10 chunks from 2 documents were retrieved, the LLM only sees `[S1]` and `[S2]`.
- **`chunks`** — the full list of retrieved chunks before grouping, each with `title`, `score`, `page_number`, and `text_preview`. Scan these to verify the right documents were retrieved. If irrelevant papers dominate, a filter may be missing or the `min_score` threshold is too low.

#### RAG agent — `context_text`

```
"context_text": "[S1: Mertz (2011) — Undervaluing Indeterminacy ...]\n[p. 6] 10. Mertz, THE LANGUAGE OF LAW SCHOOL ...\n\n[S2: Yovel & Mertz (2008) — ...]..."
```

This is the **exact text passed to the generation LLM**. It is the single most useful field for diagnosing wrong or incomplete answers:

- If the answer mentions something not in `context_text`, the LLM fabricated it.
- If the expected answer *is* in `context_text` but the LLM didn't include it, the LLM missed it during generation — try `top_k` higher or `temperature` lower.
- If `context_text` contains mostly bibliography entries and footnotes (e.g. `[p. 6] 10. Mertz, THE LANGUAGE OF LAW SCHOOL, supra note 6`), the retrieved chunks came from reference sections rather than body text. This is a chunking/filtering issue, not a routing issue.

#### Metadata agent — `catalog_results`

```json
"catalog_results": [
  { "item_id": "YTUYXSMA", "title": "Undervaluing Indeterminacy ...", "authors": ["Elizabeth Mertz"], "year": 2011, ... },
  ...
]
```

The raw list of items returned by the payload-index query. If this is empty, the filters extracted by the router matched nothing in the library — check `routing.plan.filters` for a misread author name or an impossible year range.

---

### `llm_calls`

Ordered list of every LLM call made during the query, with `call_type`, full `prompt`, full `response`, `model`, `temperature`, `max_tokens`, and `duration_ms`.

```json
{ "call_type": "routing",         "duration_ms": 5767,  ... }
{ "call_type": "rag_generation",  "duration_ms": 13812, ... }
{ "call_type": "synthesis",       "duration_ms": ...,   ... }  // only if both agents ran
```

**What to check:**

- **`rag_generation.response`** — the raw LLM output before any post-processing. If citations in the final answer are wrong, compare against this. If the raw response has correct citations but the displayed answer does not, the issue is in post-processing.
- **`rag_generation.prompt`** — the full prompt including the numbered source context. Verify the `[S1]`, `[S2]` labels match the sources you expected. Check that the citation instruction block is present (it follows the context in the prompt).
- **`duration_ms`** — split between routing and generation. If total latency is high, this shows whether it's the router LLM, the embedding+search step, or the generation LLM that is slow.

---

### Top-level timing

```json
"timestamp_start": "2026-05-12T20:48:51.345526+00:00",
"timestamp_end":   "2026-05-12T20:49:11.224171+00:00",
"total_duration_ms": 19878,
"fallback_triggered": false
```

- **`total_duration_ms`** — end-to-end wall time. For the example above: 5.8 s routing + 14.1 s RAG agent (0.3 s retrieval + 13.8 s LLM generation) = ~20 s total.
- **`fallback_triggered`** — `true` means the router chose `metadata` but the catalog query returned nothing, so the system automatically re-ran the RAG agent with no filters. This appears in the second `agent_executions` entry. If you see this, the metadata filter was likely too restrictive.

---

## Common failure modes

### Wrong agent selected

**Symptom:** The answer is a flat catalog list when you expected a content summary, or vice versa.

**Diagnosis:** Check `routing.plan.agents_to_use` and `routing.routing_description`.

**Fix:**

- For a misrouted catalog question (answered by RAG instead of metadata), add `--no-routing` and verify the question is unambiguous: "List all books in the library by Smith" is clearer than "What has Smith written?".
- For a content question routed to metadata, the routing guidance already strongly prefers `rag` for content. If it still picks `metadata`, the question phrasing triggered the catalog path — rephrase to ask *what* a publication says rather than *which* publications exist.
- You can also force the agent by calling the API directly with `enable_routing: false` (always uses RAG).

### No results / empty answer

**Symptom:** "I couldn't find any relevant information…"

**Diagnosis:** Check `retrieval.raw_results_count`. If 0, either:

1. `min_score` is too high — lower it with `--min-score 0.2`.
2. The filter is too restrictive — check `routing.plan.filters`; a misspelled author name or impossible year range drops all chunks.
3. The topic is not in the indexed documents at all — check `library_document_counts` in the response to confirm the library has indexed items.

### Irrelevant sources retrieved

**Symptom:** The cited papers are off-topic, or the scores in `retrieval.score_stats` are low (max < 0.7).

**Diagnosis:** Check `retrieval.chunks` and compare `text_preview` against what you expected. Low scores mean the question's semantic embedding is far from any indexed content.

**Fix:** Rephrase the question to match the vocabulary of the domain. If scores are reasonable but the *documents* are wrong, check whether a filter is over-including irrelevant authors — the `authors` filter matches any author on a paper, not just the first author.

### Answer ignores part of the question

**Symptom:** The LLM answered only part of what was asked.

**Diagnosis:** Check `context_text`. If the relevant passage isn't there, the retrieval step didn't find it — increase `--top-k`. If it *is* there, the LLM skipped it during generation — this is a generation problem. Try with a more capable model via `--llm-model`.

### Cited page numbers are wrong

**Symptom:** The answer cites `[S1:6]` but page 6 of that paper has nothing to do with the answer.

**Diagnosis:** Check `retrieval.chunks`. If several chunks from the same paper were retrieved and one is a footnote/bibliography page, the LLM may cite the wrong page number. This is a known limitation of chunks from reference sections. The `chunks[].text_preview` field shows the first words of each chunk — look for chunks whose preview starts with a number or looks like a reference list.

### Fallback triggered unexpectedly

**Symptom:** `fallback_triggered: true` and a second entry in `agent_executions` for the `rag` agent.

**Diagnosis:** The router chose `metadata` but the catalog query returned nothing. Check `routing.plan.filters.authors` — a last-name variant (e.g. "von Neumann" vs "neumann") or impossible year range is the usual cause. The fallback re-runs RAG without filters, which often recovers a useful answer but may not match what the user wanted.

---

## Example walkthrough

The question *"Which publications by Mertz deal with the law school curriculum?"* produces the trace in [data/logs/trace.json](../data/logs/trace.json). Walking through it:

1. **Routing** chose `rag` with `authors: ["mertz"]`. The `routing_description` explains: *"The question asks about the content of publications by Mertz, specifically those dealing with the law school curriculum."* Although the phrasing "which publications" superficially resembles a catalog question, the qualifier "deal with the law school curriculum" signals a content question — the routing LLM correctly identified this.

2. **Retrieval** returned 10 chunks from 6 unique documents, all scoring between 0.844 and 0.877 — a tight, high-confidence band. The author filter (`mertz`) restricted the search space before the vector similarity comparison. The 10 chunks collapsed to 6 source labels in the context (`[S1]`–`[S6]`).

3. **Context** (`agent_executions[0].context_text`) contains substantive passages from S3 and S5 that directly discuss the law school curriculum. S4 and S6 contain mostly reference lists — passages that scored highly on author-name match but contain little relevant content.

4. **LLM generation** correctly identified S1, S3, and S5 as the most relevant and noted that S2, S4, S6 are less directly relevant. The answer is accurate given the context, but the retrieval of S4 and S6 (bibliography-heavy chunks) shows that the `min_score` of 0.35 was not the binding constraint — the author filter pulled in reference-list chunks that happened to mention Mertz.

   A possible improvement: raise `--min-score` slightly or increase `--top-k` to dilute the reference-list chunks with more substantive ones.
