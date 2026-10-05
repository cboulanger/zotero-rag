# Routing & Retrieval Quality Hardening — Design Spec

## 1. Goal

A debug trace (`zotero-rag-debug-trace2.json`, query: *"Welche Beziehung
besteht zwischen der systemtheoretischen Rechtssoziologie und der
Rechtsdogmatik?"*) exposed two structural problems in the query pipeline that
together produced a short, poorly-grounded answer citing few relevant
sources, plus a gap in the existing answer-quality safety net:

1. **The router invents author filters that aren't in the question.** The
   question names no author, yet the router LLM populated
   `authors: ["teubner", "wiethölter"]`. Those names became a hard Qdrant
   filter, narrowing retrieval to works by just those two people and
   starving the answer of otherwise-relevant material.
2. **Duplicate chunks waste top-k slots.** 4 of the 10 raw chunks Qdrant
   returned were exact (or case-only-different) duplicates of text already
   present in another chunk of the same document — collapsing the usable
   result pool and crowding out genuinely distinct passages.
3. **The existing quality-retry logic doesn't catch "thin, off-topic
   context."** `rag_engine.py` already retries generation once on tool-call
   leaks, missing citations, or low citation diversity — but none of those
   fired here, because the answer *did* cite multiple sources. The actual
   problem (most retrieved content never addressed the question's core
   concept) has no detector today.

This spec fixes (1) and (2) as unconditional bug fixes, and adds (3) as an
off-by-default enhancement to ship and evaluate separately.

## 2. Current state

- **Router**: `backend/services/query_router.py`. `_ROUTING_GUIDANCE` (28-58)
  and `_PROMPT_TEMPLATE` (60-100) build the routing prompt; `route()`
  (131-208) calls the LLM, parses JSON via `_parse_json()` (227-243), and
  builds a `QueryPlan` (171-184) with no validation that `authors` /
  `title_keywords` / `citation_targets` actually originated from the question
  text.
- **Filter propagation**: `query_orchestrator.py` passes `plan.filters`
  unchanged into every selected agent's `execute()` (243-264). For the `rag`
  agent: `rag_agent.py:52-85` → `rag_engine.py:208-221` →
  `vector_store.py:432-437`, where `filters.authors` becomes a Qdrant
  `FieldCondition(key="author_lastnames", match=MatchAny(...))` inside the
  `must` clause of `search()` — i.e. a hard filter, not a soft boost.
- **Retrieval grouping**: `rag_engine.py:179-508`. Chunks are grouped by
  `item_key` (300-320), correctly merging multiple attachments of one Zotero
  item into a single source. Per-document truncation to
  `max_chunks_per_document` (327-328) is **purely score-based** — there is no
  text-content comparison anywhere in this path, nor at ingestion (the only
  existing dedup there is a whole-*file* SHA256 hash,
  `vector_store.py:773-1079`, which doesn't catch two attachments whose
  *extracted text* coincides).
- **Existing quality retry**: `rag_engine.py:37-122`,
  `_quality_issue_reinforcement()`, checks (a) tool-call-leak text, (b) zero
  `[SN]` citations, (c) ≤1 distinct source cited despite
  `low_diversity_available_floor` sources being available. Any hit appends
  reinforcement text to the *same* prompt/context and retries generation
  once (437-452). No mechanism re-retrieves with a wider net, and no
  mechanism detects "the model answered thinly because the context was thin."

## 3. Scope decisions

| Decision | Choice |
| --- | --- |
| Priority | Problems 1 and 2 ship together, unconditionally, as bug fixes. Problem 3 ships separately, behind a flag, after observing whether 1+2 already resolve most cases |
| Router fix | Both prompt tightening *and* a deterministic post-parse validation (belt and suspenders — an LLM can still ignore prompt instructions) |
| Dedup fix | Retrieval-time, text-hash based, shipped now. Root-cause investigation of *why* duplicate-text attachments exist is a separate follow-up, not blocking this spec |
| Dedup scope | Exact-after-normalization (whitespace-collapsed, casefolded) duplicates only. Fuzzy/near-duplicate detection (differing by more than case/whitespace) is explicitly out of scope — the trace didn't exhibit that case, and it adds similarity-threshold tuning risk for no observed benefit |
| Self-review action | On trigger: re-retrieve with escalated `top_k` and regenerate once, then keep whichever of the two answers has fewer quality issues. Never loop more than one retry |
| Self-review default | Off (`enable_quality_self_review=False`), independent of `enable_routing` |

## 4. Fix 1 — Router must not invent entities absent from the question

### 4.1 Prompt tightening (`query_router.py`, `_ROUTING_GUIDANCE`)

Add one instruction plus a worked example immediately after the existing
"mentions" authors-vs-citation_targets example (the block ending around
line 58):

```text
- Only include a name in `authors`, `title_keywords`, or `citation_targets`
  if that name (or an inflected form of it) is actually written in the
  Question. Never infer a plausible author from the topic — a question
  about a concept or school of thought (e.g. systems-theory jurisprudence)
  must NOT populate authors with scholars commonly associated with that
  topic (e.g. Luhmann, Teubner, Wiethölter) unless one of those names
  literally appears in the Question text.
  Example: "Welche Beziehung besteht zwischen der systemtheoretischen
  Rechtssoziologie und der Rechtsdogmatik?" -> agents: ["rag"],
  authors: [] (NOT ["luhmann", "teubner", "wiethölter"] — none of those
  names appear in the question; it names a concept, not a person).
```

### 4.2 Deterministic post-check (new code, the safety net)

New function in `query_router.py`, called from `route()` immediately after
`_parse_json()` (227-243) and before constructing `QueryPlan`:

```python
def _validate_mentioned_entities(
    raw_filters: dict, question: str
) -> tuple[dict, dict[str, list[str]]]:
    """Drop author/title_keyword/citation_target entries that don't
    literally occur in the question text. Returns (cleaned_filters,
    dropped) for logging/tracing."""
```

- Normalization for comparison only (never mutates the stored value): NFKD
  Unicode decomposition to strip combining marks (ö→o) plus a small German
  transliteration table (ö→oe, ü→ue, ä→ae, ß→ss) tried as a second pass,
  then casefold.
- Match rule: **substring containment**, not exact-token equality — this
  tolerates German inflection for free (`wiethölter` is a substring of the
  normalized `wiethölters`) without needing a stemmer.
- Applies to: `filters.authors` (each entry), `filters.title_keywords` (each
  entry), `filters.citation_targets[].author` (each entry's `author` field
  only — `year` and `title_keywords` inside a citation target are left
  alone, since the question may legitimately not restate a title keyword
  for a target it already names by author).
- Dropped entries are logged at `WARNING` and returned so `route()` can
  attach them to the trace.

### 4.3 Trace visibility

Add `dropped_filters: dict[str, list[str]] | None` to `QueryPlan` (and to
the `RoutingTrace`/`plan` block already recorded in
`backend/models/trace.py`), populated whenever `_validate_mentioned_entities`
drops anything. This makes the exact failure mode from the original trace
directly visible in future trace inspections instead of requiring
cross-referencing the question text against the filters by hand.

### 4.4 Error handling

If `_validate_mentioned_entities` drops *all* entries from `authors` (as in
this trace), the `QueryPlan` simply ends up with `authors: []` — no special-
case handling needed; the rest of the pipeline already treats an empty
filter as "no author restriction."

## 5. Fix 2 — Chunk-text dedup before per-document truncation

### 5.1 Where

`rag_engine.py`, inside the grouping loop (around 300-320), operating on
each `doc_chunks[item_key]` bucket **before** the `max_chunks_per_document`
truncation at 327-328.

### 5.2 Algorithm

```python
def _dedup_chunks_by_text(chunks: list[SearchResult]) -> list[SearchResult]:
    """Within one document's chunks, drop exact-after-normalization text
    duplicates, keeping the highest-scoring copy of each."""
    seen: dict[str, SearchResult] = {}
    for chunk in chunks:  # assumes caller pre-sorts by score desc, or sort here
        key = _normalize_for_dedup(chunk.chunk.text)
        if key not in seen or chunk.score > seen[key].score:
            seen[key] = chunk
    return list(seen.values())

def _normalize_for_dedup(text: str) -> str:
    return " ".join(text.split()).casefold()
```

Call this immediately after retrieving each bucket's `results_for_doc` and
before the existing `len(results_for_doc) > max_chunks_per_document` check,
so a slot freed by a dropped duplicate is available for a genuinely
different chunk, not lost.

### 5.3 What this does and doesn't catch

- **Catches**: the exact failure in the trace — 3 identical copies of one
  1972 passage, 2 identical copies of one 1970 passage from two different
  `attachment_key`s of the same item, and the one case that differed only
  by capitalization ("RechtsVergleichung" vs "Rechtsvergleichung") — caught
  because normalization casefolds.
- **Doesn't catch**: near-duplicates differing by more than whitespace/case
  (e.g. two chunks whose boundaries overlap by a sentence). Explicitly out
  of scope per §3 — no evidence in the trace that this occurs, and the
  threshold-tuning/false-positive risk isn't justified without that
  evidence.

### 5.4 Follow-up (separate task, not blocking this spec)

Investigate why items `8CF29WUG` and `VQ6UR33F` have multiple attachments
that extract to identical text. This requires live inspection of the actual
Zotero attachments for those items (not just a code read) to determine the
cause — candidates include a duplicate/alternate-format attachment (e.g. an
OCR'd copy alongside a text-native original) that the existing whole-file
SHA256 dedup (`vector_store.py:773-1079`) misses because the *files* differ
even though the *extracted text* doesn't. Once confirmed, the likely fix is
extending that dedup to hash per-chunk or per-attachment extracted text
rather than only raw file bytes. Tracked as a follow-up; §5.2's retrieval-
time fix makes it non-urgent since the symptom is already suppressed.

## 6. Fix 3 (optional, off by default) — Thin-context self-review

### 6.1 New detector

Add `_thin_context_coverage()` alongside the existing three checks in
`rag_engine.py:37-122`, folded into `_quality_issue_reinforcement()`'s
detection chain as a fourth, distinct issue type (`"thin_context"`). Fires
when **either**:

- **Low citation-utilization on a small pool**: fewer than half of
  `documents_grouped` distinct sources are actually cited in the answer,
  *and* `documents_grouped <= low_diversity_available_floor * 2` (i.e. the
  pool itself is small — this is a signal that the retrieved pool, not just
  the model's citing behavior, is thin). This is different from the
  existing `_low_citation_diversity()` check, which only looks at whether
  ≤1 source was cited; this one looks at *utilization ratio* over a small
  pool.
- **Self-reported insufficiency**: the answer matches a hedging-phrase regex
  in German or English (e.g. "nicht genügend Informationen", "der Kontext
  enthält keine", "context does not contain", "does not provide enough
  information") — mirroring the existing regex-based style of
  `_looks_like_tool_call_leak()` (43-46).

### 6.2 Retry path — escalate retrieval, not just the prompt

Unlike the existing three checks (which reinforce the *same* context and
retry generation only), a `"thin_context"` hit triggers a different path:

1. Re-run `vector_store.search()` at
   `min(top_k * diversity_escalation_factor, diversity_escalation_max_top_k)`
   — i.e. force the escalation machinery that already exists for the
   `documents_grouped < diversity_floor` case (273-298), but unconditionally
   this one time, regardless of whether the raw-count/floor condition is
   met.
2. Rebuild `doc_chunks` / `context_text` from the escalated results (reusing
   the Fix 2 dedup pass).
3. Regenerate once.
4. Run all four quality checks again on the retry's answer. If the retry is
   no better (same or more issues) than the original, **keep the original
   answer** — never discard a decent first answer for a worse escalated
   one, and never retry more than once regardless of the second outcome.

### 6.3 Gating and tracing

- New setting `enable_quality_self_review` (default `False`), independent
  of `enable_routing`, surfaced the same way other per-query parameters are
  (see `parameters` block in the debug trace).
- Record a `quality_review: {triggered: bool, reason: str,
  retry_improved: bool}` block on `AgentExecutionTrace` whenever this path
  runs, so a future trace inspection can see directly whether it fired and
  whether it helped — the same way `routing.plan.dropped_filters` (§4.3)
  makes Fix 1 debuggable from the trace alone.

### 6.4 Why off by default

This at least doubles latency (one extra retrieval + generation round trip)
on every query that trips the heuristic. Fixes 1 and 2 should eliminate the
specific failure mode seen in this trace outright; shipping Fix 3 off by
default lets real traffic be observed first, so the decision to enable it
by default is based on the residual failure rate after 1+2 land, not a
guess made before any data exists.

## 7. Testing

- **Fix 1**: unit tests for `_validate_mentioned_entities` — exact match,
  diacritic variant (`Wiethölter` / `Wietholter`), German genitive inflection
  (`Wiethölters`), a name absent from the question (must be dropped and
  reported in `dropped_filters`), and the exact trace case (no author named
  → both `teubner`/`wiethölter` dropped, `authors == []`).
- **Fix 2**: synthetic `SearchResult` fixtures reproducing the trace's
  duplicate pattern (3 near-identical chunks including one case-only diff)
  fed into the grouping/truncation logic — assert duplicates collapse to
  one, the higher-scoring copy survives, and a previously-truncated distinct
  chunk now makes it into context instead.
- **Fix 3**: a fixture with one genuinely relevant chunk among several
  off-topic ones — assert the heuristic fires, escalation re-search runs
  exactly once, and a second trigger on the retry's own output does not
  cause a further retry. A second test with the flag off asserts no
  behavior change (regression safety for default-off rollout).

## 8. Rollout

Ship Fixes 1 and 2 together — both are unconditional bug fixes, no flag
needed, safe to enable immediately. Ship Fix 3 behind
`enable_quality_self_review=False` in the same or a follow-up change; flip
the default only after trace data from production confirms Fixes 1+2 did
not already resolve most thin-answer cases.
