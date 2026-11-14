# Optional Re-ranking Step: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** draft for review (2026-11-14). Nothing here is implemented.

**Goal:** Add re-ranking as an official but optional retrieval step. A preset may declare a `rerank` section; the step then rescores the vector-search candidate pool with a multilingual cross-encoder before dedup and the per-document cap. It runs on the server's CPU (local) or on a GPU endpoint on MPCDF or RunPod that each user starts with their own credentials. Users can switch it off in Preferences or per query, and a query never waits for it. Along the way, the MPCDF and RunPod embedding and LLM sides move to per-user credentials as well (Phase 7), since the providers' terms forbid sharing accounts.

**Background:** `docs/history/2026-11-14-reranking-brainstorm.md` (experiments so far, model and host options, latency estimates, evaluation method).

**Architecture:** `rerank` becomes a third provider *side* next to `embedding` and `llm`, so it reuses the existing provider layer (descriptors, health, provisioning jobs, per-user slots, the Preferences sections) instead of a parallel mechanism. A new `backend/services/rerank.py` holds a local cross-encoder and a remote client behind one interface. `rag_engine.py` calls it in the slot `lexical_rerank`/`embedding_mmr_rerank` use today. The plugin gets a third Preferences section, a query-dialog checkbox and a "not running" prompt. Remote re-ranking accepts only the `user` credential scope.

**Tech stack:** Python 3.12, FastAPI, `sentence-transformers` `CrossEncoder` (already a dependency; optional ONNX backend), `httpx`, the `mpcdf-llm` library (git dependency), plugin JS with JSDoc types, `unittest`/`pytest`, `node --test`.

## Decisions taken in the brainstorm

- Providers for re-ranking: local CPU, MPCDF, RunPod. KISSKI and LLM-based (listwise) re-ranking are out.
- Local CPU is acceptable at up to 15 s extra latency per query.
- Credentials are per user only. No admin-set key or URL for the `rerank` side (provider terms forbid sharing accounts).
- Re-ranking is optional at three levels: the preset declares it, the user's Preferences switch, and a per-query checkbox under Advanced options.
- A query never blocks on the re-ranker. Not configured, switched off, endpoint not ready, timeout or error all mean the vector order is kept and the trace says why.

## Open decisions for review

1. **Default model and precision** are chosen by Phase 0's eval. The plan assumes `BAAI/bge-reranker-v2-m3`, with int8 ONNX on CPU if fp32 misses the 15 s budget.
2. **Bundled presets:** add a `rerank` section to the existing CPU/KISSKI presets once Phase 0 passes, or ship separate `*-rerank` presets first? The plan assumes it is added to existing presets in Phase 6, after the judged run.
3. **Shared MPCDF/managed RunPod sides for embedding and LLM:** decided 2026-11-14: they move to per-user credentials too (Phase 7). How auto-indexing gets an embedding endpoint without a user present is deferred; auto-indexing keeps using KISSKI embeddings for now.
4. **Hybrid candidates** (whole-library BM25 nominees re-ranked together with the dense pool): included as an optional `bm25_candidates` setting in Phase 2, off by default, only if Phase 0 shows a gain.
5. **Other `managed` providers:** decided 2026-11-14: `google` and `huggingface` keep the `managed` scope for now. Phase 7 removes it only from `runpod`, so the `managed` scope, the admin-only operate path and the global provisioning slot stay in core.

## File map

| File | Change |
| --- | --- |
| `backend/config/presets.py` | `RerankConfig`, `HardwarePreset.rerank: Optional[RerankConfig]` |
| `backend/providers/types.py`, `registry.py`, `base.py` | `Side` gains `"rerank"`; `get_providers()` returns a `rerank` entry only when the preset has one; scope rule for `rerank` |
| `backend/providers/mpcdf.py` | `rerank` side with `user` scope, per-user sessions, provisioning through `mpcdf-llm` |
| `backend/providers/runpod.py` | `rerank` in `supported_sides`; worker template for a scoring model |
| `backend/services/rerank.py` (new) | `Reranker` protocol, `LocalCrossEncoderReranker`, `RemoteReranker`, `get_reranker(preset)` |
| `backend/services/mpcdf_sessions.py` (new) | One `ProvisioningManager` per Zotero user, sessions sealed via `secret_store` |
| `backend/services/rag_engine.py` | Re-rank hook; trace fields |
| `backend/api/query.py` | `QueryRequest.enable_reranking` |
| `backend/api/config.py` | `rerank` side in `/config/providers`, `/config/health`, `/config/provision`; MPCDF login/logout routes |
| `plugin/src/provider-sections.js`, `preferences.js`, `preferences.xhtml` | Third section; "Use re-ranking" switch; session-login credential kind |
| `plugin/src/dialog.xhtml`, `dialog.js` | "Disable re-ranking" checkbox; not-running prompt |
| `docs/presets.md`, `docs/providers.md`, `docs/plugin-settings.md`, `docs/query-routing.md` | Document the step |
| `skills/rag-quality-eval/` | Re-rank variants for retrieval-only runs |
| `backend/config/default_presets/remote-mpcdf.json` | Phase 7: per-user `mpcdf` sides with endpoint options instead of admin-set URL/key fields |
| `backend/services/autoindex_resolver.py`, `bin/index_libraries.py` | Phase 7: skip auto-indexing for a preset whose embedding side needs a per-user session, with an explicit issue |
| `bin/provision.py`, `CLAUDE.md` | Phase 7: drop the admin (`managed`) RunPod path |

---

## Phase 0: Offline evaluation (decides model, pool size, precision)

No product code. Uses the HF evaluation store and about 100 KISSKI chat requests in total (budget per `kisski-quota-limits`).

### Task 0.1: Retrieval-only variants

**Files:** the retrieval study script (`evaluations/retrieval-improvements-20261113/tools/variants.py` in the HF store), copied into a new `evaluations/reranking-<date>/` folder.

- [ ] Restore the Docling and Kreuzberg 800 snapshots and the cached query embeddings (prefix `dense/acad`); re-embed only missing queries, capped at 200 requests.
- [ ] Add variants: mMiniLM, bge-reranker-v2-m3 fp32, bge int8 ONNX, Qwen3-Reranker-0.6B seq-cls; pools 50, 100, 200; plus dense+BM25-top-50 union with bge.
- [ ] Report P@1, P@10, MRR, gold facts in top 10/30 of 26, split English / non-English / cross-lingual, exact McNemar against `dense/acad`.

### Task 0.2: CPU timing

- [ ] Time each variant per query (median and 95th percentile) and record peak RSS on a 4-vCPU container, single process, `torch.set_num_threads(4)`.
- [ ] Pass mark: 95th percentile under 15 s.

### Task 0.3: Gold facts in context and judged run

- [ ] Best passing variant through the real pipeline with `MOCK_LLM=1` (`tools/cached_backend.py`); count gold facts in context (of 39). Needs Phase 2's hook behind a flag, so run it after Task 2.3 if it is not prototyped in the script.
- [ ] Judged gold run (about 100 requests): prefix-only baseline vs winner. Publish both to the HF archive (not `latest`).
- [ ] Adoption criteria: gold facts in top 10 +3 of 26 with no losses on non-English questions, benchmark P@10 p < .05, CPU budget met. Record the decision in `docs/history/`.

---

## Phase 1: Preset schema and provider side

### Task 1.1: `RerankConfig`

**Files:** Modify `backend/config/presets.py`; test `backend/tests/test_config.py`.

- [ ] Write failing tests: a preset without `rerank` loads with `preset.rerank is None`; a preset with a local `rerank` section loads with defaults; unknown keys and `pool_size < 1` are rejected.
- [ ] Add:

```python
class RerankConfig(BaseModel):
    """Optional re-ranking step (see docs/presets.md)."""

    model_type: Literal["local", "remote"] = "local"
    model_name: str
    pool_size: int = Field(default=100, ge=1, le=500)
    max_length: int = Field(default=512, description="Token limit per question+chunk pair")
    timeout_seconds: float = Field(default=15.0, gt=0)
    bm25_candidates: int = Field(default=0, ge=0, description="Whole-library BM25 nominees added to the pool; 0 disables")
    model_kwargs: dict = Field(default_factory=dict, description="Local: CrossEncoder kwargs (e.g. backend='onnx'); remote: endpoint fields")
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
```

and `rerank: Optional[RerankConfig] = None` on `HardwarePreset`. An added optional key is not a breaking schema change; keep `PRESET_SCHEMA_VERSION`.
- [ ] Run `uv run pytest backend/tests/test_config.py`; commit.

### Task 1.2: `rerank` as a provider side

**Files:** Modify `backend/providers/types.py`, `registry.py`, `base.py`, each provider's `supported_sides`; test `backend/tests/test_providers*.py`.

- [ ] Tests: `get_providers()` returns no `rerank` key for a preset without the section; returns one for a local section (generic provider); rejects `kisski`, `openai`, `google`, `anthropic`, `huggingface` on the `rerank` side; rejects any scope but `user` on a remote `rerank` side with a message naming the rule.
- [ ] `Side = Literal["embedding", "llm", "rerank"]`; keep `SIDES` as the two mandatory sides and add `OPTIONAL_SIDES = ("rerank",)`; `get_providers()` iterates both, skipping an absent optional side. Callers that loop over `SIDES` keep their meaning.
- [ ] `supported_sides` on `generic` (local only), `mpcdf`, `runpod` include `"rerank"`.
- [ ] Commit.

---

## Phase 2: Backend step with the local re-ranker

### Task 2.1: `backend/services/rerank.py`

**Files:** Create `backend/services/rerank.py`; test `backend/tests/test_rerank.py`.

- [ ] Tests with a fake scorer: results come back sorted by the new score; `rerank_score` and `pre_rerank_rank` are set on each result; only the first `pool_size` are scored, the rest keep their order after them; a scorer exception or timeout returns the input unchanged with a reason.
- [ ] Implement:
  - `class Reranker(Protocol)`: `async def score(self, question: str, texts: list[str]) -> list[float]`.
  - `LocalCrossEncoderReranker`: loads `CrossEncoder(model_name, max_length=..., **model_kwargs)` lazily, once per process, guarded by a lock; scores in `asyncio.to_thread` in batches.
  - `RemoteReranker`: `POST {base_url}/rerank` with `{"model", "query", "documents"}` (the Jina/Cohere format vLLM serves), maps `results[].index/relevance_score` back; one retry after a 401/503/connection error with fresh credentials from the provider.
  - `async def rerank(results, question, reranker, cfg) -> RerankOutcome` with `status` in `ran | off | not_ready | timeout | error`, applying `asyncio.wait_for(cfg.timeout_seconds)`.
  - `get_reranker(preset, request_ctx)` returns `None` when the preset has no `rerank` section.
- [ ] Commit.

### Task 2.2: Query flag

**Files:** Modify `backend/api/query.py`; test `backend/tests/test_query_api.py` (or the existing query API test module).

- [ ] Add `enable_reranking: bool = True` to `QueryRequest`, pass it through like `enable_routing`, and add it to the trace `parameters`.
- [ ] Commit.

### Task 2.3: Hook in `rag_engine.py`

**Files:** Modify `backend/services/rag_engine.py`; test `backend/tests/test_rag_engine*.py`.

- [ ] Tests: with a fake reranker, the capped top-k follows the new order; with `enable_reranking=False` or no `rerank` section the result equals today's; on the diversity escalation the re-ranker scores at most `pool_size` again; a failing reranker leaves today's order and sets the trace status.
- [ ] Call the step after `_apply_lexical_fusion`/`_apply_embedding_mmr` and before `_apply_near_dup_filter`/`_dedup_and_cap_chunks_per_document`, at both call sites (initial search and escalation). When `bm25_candidates > 0`, add the whole-library BM25 nominees to the pool before scoring (only if Phase 0 showed a gain; otherwise leave the setting out).
- [ ] Trace: `rerank: {status, model, provider, pool_size, duration_ms}` and per source `pre_rerank_rank`, `rerank_score`.
- [ ] `uv run pytest`; commit.

### Task 2.4: Docs

- [ ] `docs/presets.md`: the `rerank` section, its fields, local example. `docs/query-routing.md`: where the step sits. Commit.

---

## Phase 3: Plugin, local re-ranking

### Task 3.1: Query dialog checkbox

**Files:** Modify `plugin/src/dialog.xhtml`, `plugin/src/dialog.js`; test `plugin/test/*.test.js`.

- [ ] Add "Disable re-ranking" next to "Disable query routing" in Advanced options; show it only when `/api/config/providers` reports a `rerank` side; persist as `extensions.zotero-rag.disableReranking`; send `enable_reranking`.
- [ ] `node --test`; commit.

### Task 3.2: Preferences section

**Files:** Modify `plugin/src/provider-sections.js` (`SIDES` gains `'rerank'`, rendered only when present), `preferences.js`, `preferences.xhtml`; tests.

- [ ] A "Re-ranking" section: provider (or "On this server"), status, and a "Use re-ranking" checkbox (`extensions.zotero-rag.enableReranking`, default on). The query dialog's box starts unchecked-disabled when this is off.
- [ ] Commit.

---

## Phase 4: Remote re-ranking on MPCDF, per user

### Task 4.1: Dependency and per-user managers

**Files:** Modify `pyproject.toml` (`mpcdf-llm @ git+https://gitlab.mpcdf.mpg.de/cboul/llm-mpcdf.git@<commit>`), `Dockerfile` if the build needs git access; create `backend/services/mpcdf_sessions.py`; tests against `mpcdf_llm.testing.FakeLlmis`.

- [ ] One `ProvisioningManager` per Zotero user id, data dir `<data_path>/system/mpcdf/<user-hash>/`, session read from and written to `secret_store` (sealed), never the password or one-time code.
- [ ] Start managers in the FastAPI lifespan for users with a stored session; `start_supervisor()` in every worker; close on shutdown. Set the `httpx` logger to WARNING.
- [ ] Tests: two users' managers are isolated; a user cannot reach another user's endpoint; a lapsed session reports `login_required`.

### Task 4.2: Provider `rerank` side

**Files:** Modify `backend/providers/mpcdf.py`; tests.

- [ ] `Options` for the rerank side: `model`, `machine`, `gpu_type`, `gpus`, `time`, `extra_args` (default `--enforce-eager` plus the scoring task flag), `until`.
- [ ] `key_scopes` for `rerank` = `{"user"}`; `supports_provisioning = True`; `provision()` starts the user's preset through their manager; `health()` maps the manager's endpoint state to `ready | unreachable` and `login_required` to `unreachable` with a "log in again" detail.
- [ ] `RemoteReranker` gets credentials from the user's manager per call and calls `touch()` after each.
- [ ] Manual check (not in CI): start bge-reranker-v2-m3 on 1 x MI300A, send one `/rerank` request, add the result to the library's `docs/provider-notes.md`.

### Task 4.3: Session login API and descriptor

**Files:** Modify `backend/api/config.py`, `backend/providers/types.py` (descriptor credential kind `session_login` with fields), `plugin/src/provider-sections.js`.

- [ ] `POST /api/config/session/login` (`{side, username, password, otp}`), `POST /api/config/session/logout`, `GET /api/config/session` for the caller only; rate-limited; errors sanitised.
- [ ] The descriptor tells the plugin to render a login form instead of a key field; no vendor names in the plugin.
- [ ] Commit.

### Task 4.4: Not-running prompt

**Files:** Modify `plugin/src/dialog.js`.

- [ ] Before sending a query with re-ranking on, read `/api/config/health`. If the `rerank` side is not `ready` and its provider supports provisioning, show a prompt: **Start it** (opens the same login/key form, then `POST /api/config/provision {sides:["rerank"]}`), **Skip this time**, **Turn off re-ranking**. The query is sent immediately in every case, with `enable_reranking` reflecting the choice; at most one prompt per dialog session.
- [ ] Commit.

---

## Phase 5: Remote re-ranking on RunPod, per user

### Task 5.1: Spike the worker image

- [ ] Check whether `runpod/worker-v1-vllm` serves vLLM's `/rerank` (or `/score`) for `BAAI/bge-reranker-v2-m3` under the endpoint's OpenAI route. If not, try an Infinity-based image. Record the result in `docs/providers.md`. If neither works, drop this phase and say so.

### Task 5.2: Provider side

**Files:** Modify `backend/providers/runpod.py`; tests.

- [ ] `rerank` side with `user` scope only, its own template and endpoint name, the image and env from 5.1; health, provision and pause as for the other sides. Costs go to the user's account; the descriptor's hint says so.
- [ ] The not-running prompt from 4.4 works unchanged (key form instead of login form).

---

## Phase 6: Presets, docs, release

- [ ] If Phase 0 passed: add a local `rerank` section to the bundled CPU presets (decision 2), and an MPCDF and a RunPod variant as separate bundled presets.
- [ ] `docs/providers.md`: `rerank` side, scopes table, MPCDF per-user sessions. `docs/plugin-settings.md`: the Preferences section, the query-dialog option, the prompt.
- [ ] Container smoke test (`uv run pytest -m container -v -s`) because of the new dependency.
- [ ] Judged gold run with the shipped preset; publish to the HF archive.

## Phase 7: Per-user credentials for MPCDF and RunPod embedding and LLM sides

The provider terms that rule out sharing for re-ranking apply to the other sides as well. After this phase no `mpcdf` or `runpod` side uses an admin-set credential. Builds on Phase 4's per-user MPCDF sessions.

**Deferred:** how auto-indexing reaches an embedding endpoint without a user present. Auto-indexing keeps using KISSKI embeddings (per-user keys from the auto-index key store) for now; a preset whose embedding side needs a per-user session is skipped by auto-indexing with an explicit issue, not indexed with someone else's credentials.

### Task 7.1: MPCDF embedding and LLM sides per user

**Files:** Modify `backend/providers/mpcdf.py`, `backend/config/default_presets/remote-mpcdf.json`, `backend/services/embeddings.py`, `backend/services/llm.py` (credential lookup only); tests.

- [ ] Tests: a preset that gives an `mpcdf` side `shared_base_url_env`/`shared_api_key_env` or `scope: "shared"` is rejected with a message pointing to per-user sessions; an `mpcdf` side with `scope: "user"` resolves base URL and key from the caller's `ProvisioningManager` at request time; a caller without a session gets the "log in to MPCDF" hint, never another user's endpoint.
- [ ] `MpcdfProvider.key_scopes = {"user"}`, `supports_provisioning = True` for all three sides; side `Options` describe the endpoint to start (model, framework, machine, GPU type and count, run time, `extra_args`), as for the rerank side.
- [ ] Rewrite `remote-mpcdf.json`: embedding `intfloat/multilingual-e5-large-instruct` and an LLM side as per-user vLLM endpoints (one MPCDF job each; whether both can share one job is a manual check, not assumed).
- [ ] The embedding and LLM services ask the provider for credentials per request (the same path `RemoteReranker` uses), refresh once after a 401/503/connection error, and call `touch()`.
- [ ] Indexing from the plugin ("index now") runs with the requesting user's endpoint; that user's embedding endpoint must be ready, otherwise the plugin shows the same not-running prompt as Phase 4.4.

### Task 7.2: Remove the admin-set MPCDF fields

**Files:** Modify `backend/api/config.py` (`/config/remote-fields`, `/required-keys`), `backend/services/admin_settings_store.py` callers, `plugin/src/preferences.js`; tests.

- [ ] No admin form for `MPCDF_*_BASE_URL`/`MPCDF_*_API_KEY` remains. Stored values under those names are ignored and dropped at the next write of the shared settings file.
- [ ] Health and descriptor responses for `mpcdf` sides report the caller's own session state.

### Task 7.3: RunPod without `managed`

**Files:** Modify `backend/providers/runpod.py` (`key_scopes = {"user"}`), `backend/api/config.py` (`_caller_may_operate`, `_slot_for` stay, since `google` and `huggingface` keep `managed` (decision 5)), `backend/services/provisioning.py`, `bin/provision.py`, `CLAUDE.md` ("Secrets and Credentials": the `--preset runpod` admin provisioning path); tests.

- [ ] Tests: a `runpod` side with `scope: "managed"` is rejected; provisioning, pause and health use only the caller's own key and slot.
- [ ] `bin/provision.py`: drop the RunPod admin path, or keep it as a per-user CLI that takes the user's own key from the secret store (decide during the task; the bundled `runpod` preset is already `user` scope).

### Task 7.4: Auto-indexing guard

**Files:** Modify `backend/services/autoindex_resolver.py`, `bin/index_libraries.py`; tests.

- [ ] When the active preset's embedding side is `user`-scoped on a provider that needs a per-user session (MPCDF) or a per-user endpoint (RunPod), the run indexes nothing and records one issue ("auto-indexing is not available with this preset; use KISSKI embeddings") in `cron_status.json`, shown in Preferences.
- [ ] Docs: `docs/providers.md` scopes table (no `shared`; `managed` only for `google` and `huggingface`), `docs/cron-indexing.md`, `docs/presets.md`.

## Deferred

- How auto-indexing gets an embedding endpoint without a user present when the preset embeds on MPCDF or RunPod. Until then auto-indexing uses KISSKI embeddings (decision 3, Task 7.4).
