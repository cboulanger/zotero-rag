# Provider Layer, Credential Scopes, Default Preset and Hugging Face Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Work happens in a git worktree off `devel`; every subagent prompt must state the worktree's absolute path and tell the agent to `cd` there, verify `pwd` and `git branch --show-current`, and never commit to `main` (see CLAUDE.md, "Working with git worktrees and subagents").

**Goal:** Replace the RunPod-specific health and provisioning code with a per-side `Provider` class layer selected by id in preset JSON; add the three credential scopes (`user`, `managed`, `shared`) with no provider keys in `.env`; add a server default preset with per-user preset choice; add a provider-agnostic per-side configuration UI with provisioning, retry and Pause/Resume; and add Hugging Face Inference Endpoints as the first provider built on the layer.

**Spec:** `docs/superpowers/specs/2026-10-10-huggingface-preset-and-provisioner-adapters-design.md` (referred to below as "the spec"; section ids like A4 or B2c point into it). The spec is the source of truth for behaviour; this plan fixes order, files, tests and slicing.

**Base:** `devel` at `8b35032` (after PR #70 "Guard Kreuzberg against pathological scans", merged 2026-10-10). Where #70 touches this work it is called out in "Interplay with PR #70" below and again in the affected tasks.

**Tech stack:** Python 3.12 (`uv run`), FastAPI, pydantic, `httpx`, pytest; plugin in plain JavaScript (JSDoc types) tested with `node --test`.

**Branching:** Do the work on a feature branch off `devel`, one PR per slice (see "Slices"). `main` is release-only; open a `devel` to `main` PR only when asked.

---

## Interplay with PR #70 (checked against `devel` @ `8b35032`)

PR #70 added page-aware PDF handling, a deferred-upload quarantine and the `rag-failed` tag. It does not touch presets or the provider/credential code, but four things in it constrain this plan:

1. **Systemic embedding errors must stay systemic.** `backend/services/cron_indexer.py` defines `_SYSTEMIC_EMBEDDING_ERROR_TYPES = _FATAL_UPLOAD_ERROR_TYPES - {"KreuzbergUnavailableError"}` and passes `count_toward_quarantine=False` for them to `pending_upload_cache.note_failure`. Every new "endpoint is paused", "not provisioned" or "managed endpoint unavailable" failure raised by this plan must be an `EmbeddingEndpointUnavailableError` (already in the fatal set), so it never counts toward the `PENDING_UPLOAD_MAX_ATTEMPTS` quarantine and never produces a `rag-failed` tag. Tasks 2.6 and 8.3 carry a test for this.
2. **Persistent 5xx is now wrapped.** `RemoteEmbeddingService` (around `embeddings.py:681`) raises `EmbeddingEndpointUnavailableError` after the last retry. `classify_http_error()` and `unavailable_hint()` (Task 2.6) must be applied at that wrap point and at the existing 404/405 and connection-error branches, not in a parallel code path.
3. **New per-process stores and test isolation.** #70 added `failed_attachments.json` and an autouse fixture in `backend/tests/conftest.py` that keeps failure records out of the real data directory. New stores in this plan (`user_settings.json`, the endpoint-URL cache, the key store map) must use the same pattern: paths derived from `Settings.data_path`, and tests running against a temp data path via the existing fixtures.
4. **Files edited by #70 that this plan also edits** (expect rebase friction, keep edits surgical): `backend/config/settings.py`, `backend/services/cron_indexer.py`, `backend/services/embeddings.py`, `plugin/src/preferences.xhtml` (the "Indexed-status tags" fieldset text), `plugin/src/zotero-rag.js`, `plugin/test/zotero-rag.test.js`, `.env.dist`.

Not affected by this plan: `failed_attachments.py`, `pending_upload_cache.py`, `pdf_splitter.py`, `indexed_tags.py`, `indexed_tag_sync.py`, `index_event_log.py` and the plugin's `indexed-tags.js` / `fix-unavailable.js` flows.

## Facts about current `devel` that the tasks rely on

- Presets: `backend/config/presets.py` (`EmbeddingConfig`, `LLMConfig`, `HardwarePreset`, `ensure_default_presets`, `_load_preset_file`, `get_preset`, `list_presets`); bundled files in `backend/config/default_presets/` (10 files: `apple-silicon-32gb`, `apple-silicon-kisski`, `cloud-server-kisski`, `cpu-only`, `high-memory`, `remote-kisski`, `remote-mpcdf`, `remote-openai`, `runpod`, `windows-test`).
- Active preset is global: `Settings.get_hardware_preset()` (`config/settings.py:356`); about 30 call sites (regenerate with `grep -rn "get_hardware_preset()" backend bin scripts --include=*.py`).
- Health and provisioning: `backend/utils/endpoint_health.py` (`check_runpod_health`, `HEALTH_CHECKS`), `backend/services/provisioning.py` (subprocess job, `PROVISION_RESULT:`), `backend/api/config.py` (`_check_side`, `get_endpoint_health`, `_provisioning_key`, `start_provisioning`, `get_provisioning_status`), `bin/provision_runpod_endpoints.py` (~600 lines, `REST_BASE_URL = https://rest.runpod.io/v1`).
- Credentials read from the environment today: `Settings.get_api_key` (`settings.py:432`), `embeddings.py:561` and `llm.py:366/404` (`os.getenv`), `admin_settings_store.resolve_shared_value` (`:113`), `api/config.py` (`:143`, `:147`, `:268`, `:610`, `:661`), `services/provisioning.py:74`, `bin/provision_runpod_endpoints.py:495/498/583`. `embeddings.py:315` and `llm.py:133` export `HF_TOKEN` for local-weights downloads, which is not a provider endpoint credential (decision in Task 3.2).
- Vendor code to move: `backend/utils/kisski.py` (used by `api/config.py` `get_config`/`get_models_status` and gated by `LLMConfig.models_status_url`, also tested in `api/query.py:176`); `_KNOWN_HEADERS`, `_KNOWN_DOCS_URLS`, `docs_url_for_key` in `embeddings.py:~172-195`; model-name sniffing in `llm.py:~292` and `:442`.
- Rate limits: capture in `RemoteEmbeddingService._capture_rate_limit_headers` (module globals `_last_rate_limit_headers`), `services/rate_limit_info.py`, `api/rate_limits.py`, per-key status in `autoindex_key_store.py`, widget `plugin/src/rate-limit-widget.js` (KISSKI `hour`/`day` only).
- Key store: `AutoIndexKeyStore` (single `set_embedding_key(fp, key, key_name)` per user); resolver `autoindex_resolver.resolve_targets`; cron builds the embedding service per target at `cron_indexer.py:~749` (`create_embedding_service(preset.embedding, api_key=target["embedding_key"])`).
- Plugin: `plugin/src/preferences.js` (preset group, health rows and provision button at ~270-410, `refreshPresetState`, `refreshEndpointHealth`), `preferences.xhtml` (~50-84 preset and service-key groups), `zotero-rag.js` (`getAuthHeaders` ~351, `fetchRequiredApiKeys` ~704, `renderServiceApiKeyFields` ~774, `setSharedRemoteField` ~720), tests in `plugin/test/`.
- Tests: Python in `backend/tests/` (notably `test_config.py`, `test_endpoint_health.py`, `test_provision_job.py`, `test_provision_runpod_endpoints.py`, `test_embeddings.py`, `test_llm.py`, `test_rate_limit_info.py`, `test_autoindex_*.py`, `test_cron_indexer.py`), plugin tests in `plugin/test/`.

## Conventions for every task

- TDD: write the failing test, run it and see it fail for the right reason, implement, run it green, then run the wider affected suites, then commit. Commit messages are descriptive and one logical change each.
- Run commands: `uv run pytest <file> -q` per task; `uv run pytest -q` at the end of each phase; `node --test plugin/test/` after any plugin change (`npm run` plugin scripts are not needed; the dev server hot-reloads, never rebuild the plugin while developing).
- After any change to `backend/main.py`, `backend/dependencies.py` or `backend/config/settings.py` run the container smoke test: `uv run pytest -m container -v -s` (see CLAUDE.md). After any change to `docker-compose.yml` or `bin/container.mjs` run `uv run python scripts/test_startup_sequence.py` (this plan should not need it).
- Python: type hints and docstrings on everything public, no Unicode emoji in `print`, async handlers never call blocking I/O (blocking provider calls go through `asyncio.to_thread`, handlers that only call sync code are plain `def`).
- No backward-compatibility code (spec "Decision"): change shapes in place. The only upgrade mechanisms are the bundled-preset overwrite and the custom-preset version warnings.
- Live checks use the `groups/6297749` "test-rag-plugin" library only, via `scripts/debug_live_query.py`.
- Progress documentation (CLAUDE.md): at the end of each phase add `docs/history/implementation/<phase>.md` describing what was done, and a short summary linking to it at the end of the master plan if one exists; keep the files sufficient to resume in a new session.

## Slices (pull requests into `devel`)

| Slice | Phases | Why this boundary |
|---|---|---|
| PR 1 | 0, 1, 2 | The provider layer plus every vendor port must land together (spec B5): otherwise RunPod, KISSKI, MPCDF and OpenAI temporarily lose features. Suite green at each commit inside the branch because old code is deleted only in Task 2.8 |
| PR 2 | 3, 4 | Credential scopes, no env keys, per-user endpoints, default preset and per-user choice. Both change who is "the caller" on every request path |
| PR 3 | 5, 6 | Backend descriptors and multi-side job, then the plugin UI built on them |
| PR 4 | 7, 8 | Hugging Face provider, then Pause/Resume across RunPod and HF |
| (final) | 9 | Docs, supersession of older specs, cleanup. Can ride with PR 4 |

---

## Phase 0: Verification spike (no production code) - done for RunPod and Hugging Face; MPCDF and restricted-key checks pending

**Purpose:** answer the two questions that can change the design, plus the cheap factual ones, before PR 1. Output is a short results note; nothing here ships.

**Files:** Create `docs/history/implementation/provider-layer-phase-0-spike.md`. Scratch scripts go in the session scratchpad, not the repo. Never print or store a key; use `$RUNPOD_API_KEY` style references only (CLAUDE.md "Security").

### Task 0.1: RunPod facts

- [ ] **Step 1: List endpoints read-only** with `GET https://rest.runpod.io/v1/endpoints` and record the field names present (`id`, `name`, `workersMin`, `workersMax`, `templateId`, ...). Confirms the lookup-by-name and the `workersMax` read used by `health()` and `endpoint_url()` (spec B3).
- [ ] **Step 2: Create a throwaway endpoint and test `workersMax=0`.** Using a throwaway template and endpoint named `zotero-rag-spike-*`, `PATCH`/update `workersMax` to `0`, read it back, then delete both. Record: accepted or rejected, error text, and whether queued jobs are purged. Ask the user before creating billable resources; scale-to-zero endpoints cost nothing while idle.
- [ ] **Step 3: Restricted key listing.** With a key restricted to a single endpoint, does `GET /v1/endpoints` work, return only that endpoint, or fail? Needs a restricted key from the user. Outcome decides whether `endpoint_url()` can work for restricted keys (spec open question, A10).
- [ ] **Step 4: Record outcomes** in the results note and update the spec: if `workersMax=0` is rejected, mark RunPod `supports_suspend = False` in B3 and note the fallback; if listing fails for restricted keys, state the "key needs list permission" requirement in A10.

### Task 0.2: Hugging Face and MPCDF facts (as access allows)

- [x] **Step 1: HF.** (Done live.) With a token and billing enabled: create a TEI endpoint for `intfloat/multilingual-e5-large-instruct` on the smallest GPU and check `/v1/embeddings` batching, echoed model name and vector size 1024; create a vLLM endpoint and record the model name it serves; record `scale_to_zero_timeout` default and minimum; pause one endpoint and record the error a request gets; delete everything.
- [x] **Step 2: Decide `huggingface_hub` versus plain `httpx` REST** (spec C2): plain REST, recorded in the results note.
- [ ] **Step 3: MPCDF.** If a job is available, check an authenticated `GET {base}/models` (200 with a valid key, status for a bad key, status after expiry). If none is available, mark `MpcdfProvider.health()` as "unverified" and keep the implementation behind its unit tests only.
- [ ] **Step 4: Commit** the results note (`docs: provider layer spike results`).

**Status (2026-10-10):** done except MPCDF and the restricted-key lookups. Results are in `docs/history/implementation/provider-layer-phase-0-spike.md`. RunPod: `workersMax=0` is accepted by a `PATCH` and blocks requests with HTTP 409 `ENDPOINT_PAUSED` (creation with 0 fails with HTTP 500). Hugging Face (live, after a payment method and the endpoint hosts were enabled): plain `httpx` REST chosen over `huggingface_hub`; TEI needs `MAX_CLIENT_BATCH_SIZE` set (default batch limit 32); a paused endpoint answers HTTP 400 "endpoint is paused", a cold one HTTP 503; the scale-to-zero minimum is 15 minutes; vLLM serves the repository id; `eu-west-1` has only T4 and A10G GPUs. Still needing the user: restricted-token lookup (RunPod and HF) and MPCDF.

**Exit criteria:** every spec "To verify in a spike" item has an answer; Phase 7 and 8 scope is adjusted accordingly. The restricted-key answer gates Task 3.3's lookup-failure behaviour; the remaining HF answers (TEI batching and echoed model name, vLLM served model name, data-plane error of a paused endpoint, scale-to-zero minimum) gate Task 7.1 only and can be collected during Phase 7, once the account has a payment method and the endpoint host is reachable.

---

## Phase 1: Provider foundation (additive; suite stays green)

All of Phase 1 is additive: old fields (`health_check_provider`, `provisioning_script`, `models_status_url`) stay valid until Task 2.8.

### Task 1.1: Preset schema additions

**Files:** Modify `backend/config/presets.py`, `backend/tests/test_config.py`.

- [ ] **Step 1: Write failing tests** in `test_config.py`: `HardwarePreset` accepts `version` (default 1); `EmbeddingConfig`/`LLMConfig` accept `provider` as `{"id": ..., "scope": ..., "options": {...}}` and default to `{"id": "generic", "scope": None, "options": {}}`; `PRESET_SCHEMA_VERSION == 2`; unknown `scope` value is rejected by the model.
- [ ] **Step 2: Implement** `ProviderConfig`, `provider` on both side configs, `version`, `PRESET_SCHEMA_VERSION = 2` exactly as in spec A1. Keep the old fields for now.
- [ ] **Step 3: Run** `uv run pytest backend/tests/test_config.py -q`; commit `feat(presets): add per-side provider config and preset version`.

### Task 1.2: Always re-seed bundled presets; version-check custom ones

**Files:** Modify `backend/config/presets.py`, `backend/tests/test_config.py`; modify all bundled `backend/config/default_presets/*.json` (add `"version": 2`).

- [ ] **Step 1: Write failing tests** (spec B4/B7): a modified bundled file in the temp data dir is overwritten and a WARNING naming it is logged (use `assertLogs`); an identical file is not rewritten (compare `st_mtime_ns`); a deleted bundled file is recreated; a custom file is never touched; a custom file with missing/old `version` warns exactly once per process (reset the per-process warned set in a fixture); a custom file with the current version does not warn; a custom file with removed keys (`health_check_provider`, `provisioning_script`, `models_status_url`) names them in the warning and still loads; a custom file whose LLM `model_names[0]` contains "claude" with a non-`anthropic` provider warns once (this check is wired in Task 2.2 once `anthropic` exists, so write it then).
- [ ] **Step 2: Implement.** In `ensure_default_presets`, replace "skip if exists" by "write unless bytes identical", keeping the temp-file-and-`os.replace` pattern and the `_seeded_paths` cache; log the WARNING when replacing a differing file. In `_load_preset_file`, after validation, if the file is not a bundled name, run the version check and the removed-keys check (a small module-level table `REMOVED_KEYS = {2: [...]}`) with a per-path "already warned" set. Update the module docstring (it currently states "never overwrites").
- [ ] **Step 3: Bump** every bundled JSON to `"version": 2` (no other change yet).
- [ ] **Step 4: Run** `uv run pytest backend/tests/test_config.py backend/tests/test_api.py -q`; commit `feat(presets): always re-seed bundled presets and version-check custom ones`.

### Task 1.3: Provider types, base class, registry, validation

**Files:** Create `backend/providers/__init__.py`, `backend/providers/types.py`, `backend/providers/base.py`, `backend/providers/generic.py` (only the registration; the class body lives in `base.py`, see below), `backend/tests/test_providers_registry.py`.

- [ ] **Step 1: Write failing tests**: registry discovers modules by import and finds `generic`; `get_providers(preset)` returns one instance per side; an unknown id, invalid `options`, a side outside `supported_sides`, a scope outside `key_scopes`, key kinds that do not match the effective scope (`api_key_env` versus `shared_*_env`), two sides sharing one key env var with different provider ids, and a non-generic provider on a local side each raise `ProviderConfigError` with the rule named (spec A1 validation list); a valid preset passes; results are cached per preset object.
- [ ] **Step 2: Implement the types** in `types.py`: `Side = Literal["embedding", "llm"]`, `Scope = Literal["user", "shared", "managed"]`, `Health`, `ModelInfo`, `Meter`, `Credentials`, `ProvisionContext` (side, preset, credential, data_path, `recreate`, `skip_warmup`, `deadline`), `ProviderDescriptor` (+ provisioning and credential sub-models; pydantic so they serialise for Task 5.1), `NoOptions`, `ProviderConfigError`.
- [ ] **Step 3: Implement `Provider`** per spec A2 with every hook defaulting to the generic behaviour, `__init_subclass__` registration by `id`, and `GenericProvider = Provider` registered as `generic` with `key_scopes = {"user", "shared", "managed"}`, `default_scope = "user"`. Import-time registration: `backend/providers/__init__.py` imports all sibling modules with `pkgutil.iter_modules`.
- [ ] **Step 4: Implement `get_providers`** and the A1 validation, including the effective-scope rule. The key-kind check reads `model_kwargs` for `api_key_env` / `shared_api_key_env` / `shared_base_url_env`.
- [ ] **Step 5: Run** `uv run pytest backend/tests/test_providers_registry.py -q`; commit `feat(providers): add Provider base class, registry and preset validation`.

### Task 1.4: Generic usage parsers

**Files:** Create `backend/providers/usage.py`, `backend/tests/test_provider_usage.py`; modify `backend/providers/base.py` (wire `parse_usage`).

- [ ] **Step 1: Write failing tests** (spec A9): OpenAI-style `x-ratelimit-limit-requests`/`-remaining-requests`/`-reset-requests` and the `-tokens` set with reset strings `1s`, `6m0s`, `1h2m3s`; IETF `RateLimit-Limit`/`-Remaining`/`-Reset`, with and without `RateLimit-Policy: "x";q=100;w=3600` giving `period="hour"`; header names case-insensitive; non-numeric and missing values yield no meter and no exception; unrelated headers yield `[]`; `remaining > limit` is clamped or dropped (pick one and assert it).
- [ ] **Step 2: Implement** pure functions `parse_openai_headers`, `parse_ietf_headers`, `parse_duration`, and `Provider.parse_usage` calling both. `as_of` and `source` are filled by the caller (Task 2.5), so parsers take them as arguments.
- [ ] **Step 3: Run** and commit `feat(providers): parse OpenAI-style and IETF rate-limit headers`.

### Task 1.5: Provider contract test harness

**Files:** Create `backend/tests/test_provider_contract.py`, `backend/tests/provider_fakes.py` (fake HTTP client in the style of `FakeClient` in `test_provision_runpod_endpoints.py`).

- [ ] **Step 1: Write the parametrised suite** from spec A6 over `PROVIDERS` and each supported side: options validation, `apply_defaults` idempotence, `describe()` has no secrets and includes scope, `health()` never raises, `parse_usage()` never raises and returns well-formed meters for the provider's fixture, capabilities consistent with flags (`NotImplementedError` where unsupported), side/scope rejection. Each provider supplies a `ContractFixture` (a small dataclass in its test module or on the class under test) so the harness stays generic; today only `generic` is registered.
- [ ] **Step 2: Run** and commit `test(providers): add provider contract test harness`.

**Phase 1 exit:** `uv run pytest -q` green; no behaviour change anywhere.

---

## Phase 2: Vendor providers and call-site swap (PR 1)

Strangler order: add each provider and switch its call sites, keep the old path until Task 2.8 deletes it. After every task the suite is green.

### Task 2.1: `KisskiProvider`

**Files:** Create `backend/providers/kisski.py`, `backend/tests/test_provider_kisski.py`; modify `backend/api/config.py` (`get_config`, `get_models_status`), `backend/api/query.py` (`:~176`), `backend/config/default_presets/{remote-kisski,apple-silicon-kisski,cloud-server-kisski,windows-test}.json`; later delete `backend/utils/kisski.py` (Task 2.8).

- [ ] **Step 1: Port tests** from the existing KISSKI model-list tests to `KisskiProvider.live_models()` (filtering, `coder`/`devstral` exclusion, ordering, demand to availability labels, network failure returns `None`); add `parse_usage` tests for the KISSKI `hour`/`day` headers (partial, non-numeric, unrelated) that also confirm the generic dialects still work via `super()`.
- [ ] **Step 2: Implement** `KisskiProvider(Provider)` with `key_scopes = {"user"}`, `live_models()` (wrapping the logic from `backend/utils/kisski.py`, URL from `options.models_url` or `{base_url}/models`), `parse_usage()` extending the generic parser, `default_key_env()` = `KISSKI_API_KEY`, `key_docs_url()` = the SAIA dashboard.
- [ ] **Step 3: Switch call sites**: `get_config` and `get_models_status` call `providers["llm"].live_models(...)` when it returns non-`None`; `query.py` accepts an unlisted `llm_model` when the LLM provider has live models. Keep `models_status_url` honoured until Task 2.8 by mapping it to the same behaviour only if no provider is set (delete in 2.8).
- [ ] **Step 4: Add provider blocks** to the four KISSKI preset files (`kisski` on the remote sides; `cloud-server-kisski` only on `llm`). Assert in `test_config.py` that each loads and validates.
- [ ] **Step 5: Run** `uv run pytest backend/tests/test_provider_kisski.py backend/tests/test_config.py backend/tests/test_api.py backend/tests/test_query_api.py -q`; commit.

### Task 2.2: `OpenAIProvider`, `AnthropicProvider`, and protocol routing

**Files:** Create `backend/providers/openai.py`, `backend/providers/anthropic.py`, tests `backend/tests/test_provider_openai_anthropic.py`; modify `backend/services/llm.py` (`required_client_fields_for_config` ~277-330, `generate` ~417-450), `backend/services/embeddings.py` (`required_client_fields` ~476-530, `_KNOWN_HEADERS`/`_KNOWN_DOCS_URLS` ~172-195), `backend/config/default_presets/remote-openai.json`.

- [ ] **Step 1: Write failing tests**: a Claude-named model on an `openai` provider stays on the OpenAI protocol (no model-name check remains); `AnthropicProvider.llm_api == "anthropic"` routes `RemoteLLMService.generate` to `_generate_anthropic`; `required_client_fields` takes the default key env and docs URL from the side's provider; `anthropic` on the embedding side is rejected by the registry; the computed header name equals the old table values case-insensitively (`OPENAI_API_KEY` to `X-Openai-Api-Key`, and so on). Add the custom-preset "claude without anthropic provider" warning test deferred from Task 1.2.
- [ ] **Step 2: Implement** both providers (`key_scopes = {"user", "managed"}`) and route `RemoteLLMService` on `providers["llm"].llm_api` instead of the model name; compute header names algorithmically only; read `docs_url` and default key env from the provider.
- [ ] **Step 3: Update** `remote-openai.json` (`openai` on both sides, description without "Anthropic"); add the claude warning to the preset loader.
- [ ] **Step 4: Run** `uv run pytest backend/tests/test_provider_openai_anthropic.py backend/tests/test_llm.py backend/tests/test_embeddings.py backend/tests/test_config.py -q`; commit.

### Task 2.3: `MpcdfProvider`

**Files:** Create `backend/providers/mpcdf.py`, `backend/tests/test_provider_mpcdf.py`; modify `backend/config/default_presets/remote-mpcdf.json`.

- [ ] **Step 1: Write failing tests** with a fake client: `health()` for 200 (`ready`), 401 and 403 (`unreachable`, "key rejected"), 404, 405, timeout and connection error (`unreachable`, "job expired or not started"); never `cold`; `key_scopes == {"shared"}`; `unavailable_hint()` text; `supports_provisioning` is `False`.
- [ ] **Step 2: Implement** per spec B2d (`GET {base_url}/models` with the bearer key; short timeout; never raises). Mark `health()` "unverified against a live job" in its docstring if Task 0.2 Step 3 could not confirm it.
- [ ] **Step 3: Add** `mpcdf` blocks to `remote-mpcdf.json` (both sides); keep its `shared_*_env` fields.
- [ ] **Step 4: Switch** `_check_side` health to call providers for any provider whose `health()` is not `None` (RunPod still goes through the old path until 2.4). Commit.

### Task 2.4: `RunPodProvider` and the in-process provision job

**Files:** Create `backend/providers/runpod.py`, `backend/tests/test_provider_runpod.py`, `bin/provision.py`; modify `backend/services/provisioning.py`, `backend/api/config.py`, `bin/provision_runpod_endpoints.py` (becomes a shim), `backend/config/default_presets/runpod.json`; adapt `backend/tests/test_provision_runpod_endpoints.py` and `backend/tests/test_provision_job.py`.

- [ ] **Step 1: Move tests first.** Port the RunPod REST tests (`FakeClient` pattern in `test_provision_runpod_endpoints.py`) to `test_provider_runpod.py` against the new class: ensure-template and ensure-endpoint idempotency by name, `--recreate` semantics, warm-up retry and give-up, health classification (`ready`/`cold`/`throttled`/`unreachable`, from `test_endpoint_health.py`), `classify_http_error` for the gateway 405 page, `apply_defaults` writes the key pattern. Keep the old tests passing until 2.8.
- [ ] **Step 2: Implement `RunPodProvider`** by extracting logic from `bin/provision_runpod_endpoints.py` (functions `_request`, `_find_by_name`, `_ensure_template`, endpoint ensure, warm-up, teardown) into methods operating on one side; per-side `Options` (`gpu`, `workers_max`, `idle_timeout`, `data_centers`); served model = the side's model name; progress callback at each step. `supports_provisioning = True`; suspend arrives in Phase 8. Keep `key_scopes = {"user", "managed"}`; in this phase RunPod still resolves its URL through the existing shared fields (the move to `endpoint_url()` is Task 3.3), so `provision()` returns `{shared_base_url_env: url}` for now and `runpod.json` keeps its shared fields.
- [ ] **Step 3: Rewrite `services/provisioning.py`** as an in-process runner: `run_job(providers, sides, ctx)` executing `provision()` per side via `asyncio.to_thread`, recording `progress` (bounded list, side-prefixed) and per-side results; deadline enforcement; same `idle/running/succeeded/failed` states; single global slot for now (per-caller slots in Task 3.4). Remove the subprocess helpers (`start_job`, `await_job`, `parse_result`) in 2.8.
- [ ] **Step 4: Update `start_provisioning`** in `api/config.py` to build contexts from the active preset's providers and accept `{"keys": {...}, "sides": [...]}` (spec A4); keep admin gating for now. Per-side partial results: apply a side's returned dict through `update_remote_config` as soon as that side succeeds.
- [ ] **Step 5: `bin/provision.py`**: generic CLI (`--preset`, `--side`, `--pause` placeholder, `--teardown`, `--recreate`, `--yes`, `--skip-warmup`) that prompts for the key on stdin or reads the key store, never env or argv; `bin/provision_runpod_endpoints.py` becomes a thin call into it.
- [ ] **Step 6: Run** `uv run pytest backend/tests/test_provider_runpod.py backend/tests/test_provision_job.py backend/tests/test_endpoint_health.py backend/tests/test_provision_runpod_endpoints.py backend/tests/test_config.py -q`; commit in two commits (provider port; job and API).

### Task 2.5: Usage meters (rate limits)

**Files:** Modify `backend/services/embeddings.py` (capture ~595-625 and the module globals ~150-170), `backend/services/llm.py` (add capture), `backend/services/rate_limit_info.py`, `backend/api/rate_limits.py`, `backend/services/cron_indexer.py` (persisted status keys ~463-470, ~788), `plugin/src/rate-limit-widget.js`, `plugin/test/rate-limit-widget.test.js`, `backend/tests/test_rate_limit_info.py`.

- [ ] **Step 1: Write failing tests**: snapshots are stored per side and per key fingerprint (two keys do not see each other's headers); `get_cached_rate_limits` returns meters through the side's provider (`source="run"`/`"cache"`); `GET /api/rate-limits` returns `meters` and no raw `limits`; the cron status file round-trips per-side snapshots and still ignores snapshots produced under a different preset; plugin: widget renders bars from `meters`, shows nothing for empty `meters`, no vendor header names in the widget source.
- [ ] **Step 2: Implement** a small `RateLimitRecorder` (module in `backend/services/`) replacing the two module globals, keyed `(side, fingerprint)`; embedding and LLM services call it with response headers; `rate_limit_info` builds meters via `providers[side].parse_usage`; update `api/rate_limits.py`; update the cron writer and reader to the per-side shape.
- [ ] **Step 3: Plugin**: rewrite `ZoteroRAGRateLimitWidget.describe/render` to consume `meters` (existing 75% amber and 95% red thresholds, label "`remaining` `unit` left/`period`"); keep the element id scheme so `dialog.js` and `autoindex-status.js` still render it; update both callers and `autoindex-status.test.js` fixtures.
- [ ] **Step 4: Run** `uv run pytest backend/tests/test_rate_limit_info.py backend/tests/test_embeddings.py backend/tests/test_cron_indexer.py backend/tests/test_api.py -q` and `node --test plugin/test/rate-limit-widget.test.js plugin/test/autoindex-status.test.js plugin/test/dialog.test.js`; commit.

### Task 2.6: Vendor-neutral error text, hints and the #70 interplay

**Files:** Modify `backend/services/embeddings.py` (`EmbeddingEndpointUnavailableError` docstring ~92-110 and the raise sites ~681-697, ~780-810), `backend/services/llm.py` (~440-480), `backend/tests/test_embeddings.py`, `backend/tests/test_llm.py`, `backend/tests/test_cron_indexer.py`.

- [ ] **Step 1: Write failing tests**: the 404/405, connection-error and exhausted-5xx paths raise `EmbeddingEndpointUnavailableError` whose message is vendor-neutral (no "RunPod") and ends with the side provider's `unavailable_hint()` when it has one; `classify_http_error()` returning `"cold"`/`"paused"` selects the matching message; **a cron drain test** (extend `test_cron_indexer.py`): an embedding failure of this class is passed to `pending_upload_cache.note_failure` with `count_toward_quarantine=False` (the #70 behaviour) and never quarantines an entry after `PENDING_UPLOAD_MAX_ATTEMPTS` repeats.
- [ ] **Step 2: Implement**: single helper `endpoint_unavailable_error(provider, detail)` used at every raise site (including the #70 5xx wrap); the neutral base text; the provider hint appended. Do not change `_FATAL_UPLOAD_ERROR_TYPES`.
- [ ] **Step 3: Run** the three test files; commit.

### Task 2.7: Preset JSON for RunPod and docs for the layer

**Files:** Modify `backend/config/default_presets/runpod.json`, `docs/presets.md`, `backend/tests/test_config.py`.

- [ ] **Step 1: Tests**: `runpod.json` has `version: 2`, both sides `provider.id == "runpod"` with per-side options, no `health_check_provider`/`provisioning_script`, no `shared_*_pattern` (patterns come from `apply_defaults`); `get_providers` validates it.
- [ ] **Step 2: Edit** the file per spec B3 (shared fields stay until Task 3.3). Update `docs/presets.md`: bundled presets are always re-seeded and customised by new file name; `version` changelog table; provider ids and the per-side `provider` block; `generic` documented as "any OpenAI-compatible API" including the standard rate-limit headers. User-facing docs describe only current behaviour (CLAUDE.md "Documentation").
- [ ] **Step 3: Commit.**

### Task 2.8: Delete the old paths

**Files:** Delete `backend/utils/kisski.py`, `backend/utils/endpoint_health.py` (and `HEALTH_CHECKS`), the subprocess code in `backend/services/provisioning.py`, the old `bin/provision_runpod_endpoints.py` body, the old tests superseded by Tasks 2.1 to 2.4; modify `backend/config/presets.py` (remove `health_check_provider`, `provisioning_script`, `models_status_url`), `backend/api/config.py` (remove `_check_side` fallback, `provisionable`, `models_status_url` branches).

- [ ] **Step 1: Remove** each item; run `grep -rn "HEALTH_CHECKS\|models_status_url\|provisioning_script\|health_check_provider\|fetch_kisski_rag_models\|PROVISION_RESULT" backend bin plugin scripts docs --include=*.py --include=*.js --include=*.json --include=*.md` and fix every hit that is not a historical spec.
- [ ] **Step 2: Full suite** `uv run pytest -q`, `node --test plugin/test/`, and the container smoke test `uv run pytest -m container -v -s`.
- [ ] **Step 3: Live check** against the test library: `uv run python scripts/debug_live_query.py "What is Zotero used for?" --repeat 3` on `remote-kisski` (demand dots, live model list, rate-limit bars), and health on `runpod` if endpoints exist.
- [ ] **Step 4: Phase doc** `docs/history/implementation/provider-layer-phase-2.md`; open PR 1.

---

## Phase 3: Credential scopes and no keys in the environment (PR 2)

### Task 3.1: Scopes in validation and key requirements

**Files:** Modify `backend/providers/base.py`, `backend/services/embeddings.py` and `llm.py` (`required_client_fields*`), `backend/api/config.py` (`required-keys`, `ApiKeyRequirement`), tests `test_providers_registry.py`, `test_config.py`, `test_embeddings.py`, `test_llm.py`.

- [ ] **Step 1: Write failing tests**: effective scope resolves from `provider.scope` or `default_scope`; `required_client_fields` returns `kind: "api_key"` for `user`, `kind: "shared_api_key"` (+ `shared_base_url` only where the provider cannot derive its URL) for `managed`/`shared`; a scope the class does not allow is rejected (KISSKI `managed`, MPCDF `user`); `GET /api/required-keys` reports `is_set` only for shared kinds.
- [ ] **Step 2: Implement** by letting the provider supply the key kinds for its side (`Provider.key_requirements()` used by both services), keeping `model_kwargs` as the data source.
- [ ] **Step 3: Run** and commit.

### Task 3.2: Remove every credential fallback to `os.environ`

**Files:** Modify `backend/config/settings.py` (`get_api_key`), `backend/services/embeddings.py:~561`, `backend/services/llm.py:~366,~404`, `backend/services/admin_settings_store.py:~113`, `backend/api/config.py` (`:~143,~147,~268,~610,~661`), `backend/dependencies.py` (`hf_token=settings.get_api_key("HF_TOKEN")`), `bin/*` tools that read provider keys, `.env.dist`, `CLAUDE.md`, `docs/presets.md`; tests across `test_embeddings.py`, `test_llm.py`, `test_config.py`, `test_admin_settings_store.py`, `test_api.py`.

- [ ] **Step 1: Write failing tests** (spec B7 credentials bullet): with a provider key present only in `os.environ` (monkeypatch), the embedding service, LLM service, `_check_side`, `get_models_status`, `required-keys` `is_set` and the `bin/` tools all behave as "no key"; `AUTOINDEX_SECRET` is still honoured; `scripts/` code is not tested for this.
- [ ] **Step 2: Remove the fallbacks** at each site listed under "Facts"; keep `resolve_shared_value` reading only `admin_settings.json`. **Decision for local-weights `HF_TOKEN`** (`embeddings.py:315`, `llm.py:133`): it downloads gated model weights for local models and is not an endpoint credential, so it stays as an environment/`Settings` value; record this in `docs/presets.md` so "no provider keys in `.env`" is not misread.
- [ ] **Step 3: Docs**: `.env.dist` loses provider key entries and the RunPod/provisioning block (keep non-credential settings and `AUTOINDEX_SECRET`); `CLAUDE.md` sections that mention keys in `.env`, `RUNPOD_API_KEY`, the deploy env files and the old provisioning script are updated to the current behaviour; `docs/presets.md` credential section rewritten around the three scopes.
- [ ] **Step 4: Run** the listed suites and `uv run pytest -m container -v -s` (settings changed); commit.

### Task 3.3: Per-key endpoint lookup and cache

**Files:** Create `backend/services/endpoint_cache.py`; modify `backend/providers/runpod.py`, `backend/services/embeddings.py` (`_get_client` ~535-590), `backend/services/llm.py` (`_get_openai_client` ~345-395), `backend/config/default_presets/runpod.json`; tests `test_endpoint_cache.py`, `test_provider_runpod.py`, `test_embeddings.py`, `test_llm.py`.

- [ ] **Step 1: Write failing tests**: `RunPodProvider.endpoint_url(key)` finds the endpoint by name and returns `https://api.runpod.ai/v2/<id>/openai/v1`, returns `None` when absent, never raises; the cache keys by `(provider id, side, key fingerprint)`, honours a TTL (inject a clock), is invalidated by `invalidate(...)`; two keys resolve to two URLs; services build the client from `endpoint_url()` when the side has neither `base_url` nor `shared_base_url_env`; a missing endpoint raises `EmbeddingEndpointUnavailableError` with "not provisioned" and the provider hint.
- [ ] **Step 2: Implement** the cache (in-memory, thread-safe), the provider method, and the service wiring. Provisioning and a connection failure call `invalidate`.
- [ ] **Step 3: Update `runpod.json`**: `api_key_env: RUNPOD_API_KEY` on both sides, no shared fields; `provision()` returns `{}`.
- [ ] **Step 4: Run** and commit. (If Task 0.1 Step 3 showed restricted keys cannot list endpoints, add the documented "key needs list permission" error text here.)

### Task 3.4: Caller-scoped health, provisioning, job slots and gating

**Files:** Modify `backend/api/config.py` (`get_endpoint_health`, `start_provisioning`, `get_provisioning_status`), `backend/services/provisioning.py`, `backend/dependencies.py` (an `optional_identity`/`require_identity` dependency reusing the Zotero identity helpers used by `require_authorized_group_admin`); tests `test_provision_job.py`, `test_config.py`, `test_main_auth_middleware.py`.

- [ ] **Step 1: Write failing tests**: `GET /api/config/health` uses the caller's header key for `user` scope and the stored admin key for `managed`/`shared`; provisioning for `user` scope works for any authenticated user and runs on that user's key, with 401 when unauthenticated; for `managed` it needs an admin (403 otherwise); job slots are per caller for `user` (two users can run at once, the same user gets 409) and global for `managed`; `GET /api/config/provision/status` returns the caller's job (and the global one for `managed`).
- [ ] **Step 2: Implement** keyed job state `{slot_key: JobState}` in `services/provisioning.py`; gating by effective scope in the handlers; credentials resolved per scope into `ProvisionContext`.
- [ ] **Step 3: Run** and commit.

### Task 3.5: Auto-index key store map and per-owner resolution

**Files:** Modify `backend/services/autoindex_key_store.py` (embedding key becomes `{env_name: {key, status, rate_limit_until}}` per user), `backend/services/autoindex_resolver.py` (the `requires_embedding_key` gate becomes scope-aware: `user` needs the user's key, `managed`/`shared` use the shared key), `backend/services/cron_indexer.py` (`:~749` builds the service from the owner's key and, for derivable endpoints, `endpoint_url`), `backend/api/autoindex.py`, `bin/autoindex_add_key.py`, `bin/debug_get_zotero_key.py`; tests `test_autoindex_key_store.py`, `test_autoindex_resolver.py`, `test_cron_indexer.py`, `test_autoindex_api.py`.

- [ ] **Step 1: Write failing tests**: storing a KISSKI key and an HF token for one user keeps both; status and rate-limit state are per key name; a key for a preset the user no longer selects is kept; only permanently invalid keys are pruned; the resolver does not require a personal key for `managed`/`shared` sides and requires one for `user` sides; cron resolves each user's endpoint from that user's key.
- [ ] **Step 2: Implement.** The store is the on-disk format; no migration code (spec: no BC) beyond reading the new shape. The old single-key shape is not read; the first auto-index run after upgrade reports "no embedding key", which the plugin already surfaces, and users re-enter the key. Document this in the phase doc.
- [ ] **Step 3: Run** and commit; phase suite `uv run pytest -q` plus container smoke test; phase doc.

---

## Phase 4: Server default preset and per-user choice (PR 2)

### Task 4.1: Default preset setting

**Files:** Modify `backend/services/admin_settings_store.py` (add `default_preset` get/set), `backend/config/settings.py` (`get_hardware_preset` becomes `get_default_preset`, kept as the global accessor), `backend/api/config.py` (`update_config` sets the default); tests `test_admin_settings_store.py`, `test_config.py`.

- [ ] **Step 1: Write failing tests**: resolution order stored value, then `MODEL_PRESET`, then `remote-kisski`; `POST /api/config` (admin) persists the default and clears rate-limit caches as it does now; the compatibility rule (`_compatible_presets`) is unchanged (strict: both sides remote, same embedding identity).
- [ ] **Step 2: Implement; commit.**

### Task 4.2: Per-user settings store

**Files:** Create `backend/services/user_settings.py` (`user_settings.json` under `system/`, keyed by Zotero identity fingerprint, atomic writes with the existing `FileLock` pattern), `backend/tests/test_user_settings.py`.

- [ ] **Step 1: Write failing tests**: get/set preferred preset per identity; independent users; corrupt file tolerated; file written atomically. **Step 2: Implement; commit.**

### Task 4.3: `get_effective_preset` and the call-site sweep

**Files:** Create `backend/services/effective_preset.py`; modify every request-path caller from `grep -rn "get_hardware_preset()"` (about 30; the list under "Facts"); tests `test_effective_preset.py`, plus the affected suites.

```python
def get_effective_preset(settings: Settings, identity: ZoteroIdentity | None) -> HardwarePreset:
    """User's chosen preset if valid and compatible with the default; else the default."""
```

- [ ] **Step 1: Write failing tests**: a valid compatible choice wins; a removed, incompatible or unavailable choice falls back to the default; no identity (loopback, public query) returns the default; two identities get different presets in the same process.
- [ ] **Step 2: Implement the function**, then sweep call sites in groups, running each group's suite: (a) `dependencies.py` (`get_client_api_keys`, `make_embedding_service`, `make_llm_service`), (b) `api/query.py`, `api/public_query.py` (no identity: default), `api/config.py` handlers, (c) services built per request (`llm.py` constructors, `rag_engine.py`, `query_orchestrator.py`, `document_processor.py`, `api/document_upload.py`), (d) `api/autoindex.py`, (e) cron: `cron_indexer.py` and `autoindex_resolver.py` resolve per user from `user_settings`, (f) `main.py:241` startup keeps the default, `bin/reindex_oversized_items.py` keeps the default. Pass the preset down explicitly instead of re-reading global state where a function already receives a `settings`.
- [ ] **Step 3: Vector store** stays a singleton on the default's embedding model; add an assertion test that an effective preset with a different embedding model identity cannot be selected.
- [ ] **Step 4: Run** the full suite after each group; commit per group.

### Task 4.4: My-preset API and `selectable_presets`

**Files:** Modify `backend/api/config.py` (`GET`/`PUT /api/config/my-preset`, `ConfigResponse.selectable_presets` replacing per-user `switchable_presets` semantics, `default_preset` and `effective_preset` fields); tests `test_config.py`, `test_api.py`.

- [ ] **Step 1: Write failing tests**: a user can set a compatible preset they have credentials for; an incompatible one is 400; a `managed` preset that is not ready is not selectable; `GET /api/config` reports default, effective and a "fell back" note; admin-only `POST /api/config` still changes only the default.
- [ ] **Step 2: Implement; commit; phase doc; open PR 2.**

---

## Phase 5: Backend descriptors, multi-side job, retry (PR 3)

### Task 5.1: `GET /api/config/providers`

**Files:** Modify `backend/api/config.py`, `backend/providers/base.py` (`describe()` per provider), each provider module; test `test_config.py`, contract suite.

- [ ] **Step 1: Write failing tests**: one descriptor per side of the caller's effective preset with `id`, `label`, effective `key_scope`, `operable_by_caller` (true for `user`; true for `managed` only for admins; false for `shared`), capability flags, credential sub-object, hints; no secret material; runs for every registered provider via the contract suite.
- [ ] **Step 2: Implement; commit.**

### Task 5.2: Per-side job results, progress and retry

**Files:** Modify `backend/services/provisioning.py`, `backend/api/config.py`; tests `test_provision_job.py`.

- [ ] **Step 1: Write failing tests** (spec A4): status includes `progress` (bounded, side-prefixed) and `sides` with per-side `status`/`message`; a failure on the second side keeps the first side's success; `POST` with `sides: ["llm"]` runs only that side; a body without `sides` runs every provisioning-capable side; non-provisioning sides are skipped without error; the deadline marks the running side failed.
- [ ] **Step 2: Implement; commit.**

**Phase 5 exit:** `uv run pytest -q` green. The plugin is unchanged and still works against the old fixed panel only if Task 5.1/5.2 keep the old fields; since BC is not required, this phase merges with Phase 6 in one PR.

---

## Phase 6: Plugin UI (PR 3)

### Task 6.1: Per-side sections renderer

**Files:** Create `plugin/src/provider-sections.js` (pure render functions taking a document, descriptors, health and job state), `plugin/test/provider-sections.test.js`; modify `plugin/src/preferences.xhtml`, `plugin/src/preferences.js`, `plugin/src/zotero-rag.js` (`renderServiceApiKeyFields` reuse), `plugin/src/preferences.css`.

- [ ] **Step 1: Write failing tests** (spec A7) using the existing fake-document helpers in `plugin/test/zotero-rag.test.js`: sections render from descriptor pairs (same provider both sides; two provisioning providers; provisioning plus non-provisioning such as HF plus Anthropic; local plus remote; MPCDF-like pair with `unavailable_hint`); button visibility for each combination of `operable_by_caller`, `supports_provisioning`, `supports_suspend` and health; credential field only when the side can provision; key fields per side from `required-keys`; no provider name in plugin source (grep assertion over `plugin/src`).
- [ ] **Step 2: Implement** the renderer and replace the fixed provisioning rows in `preferences.xhtml` (keep the #70 "Indexed-status tags" fieldset untouched) with two side containers rendered by `preferences.js`.
- [ ] **Step 3: Run** `node --test plugin/test/`; commit.

### Task 6.2: Actions, polling, retry and job resume

**Files:** Modify `plugin/src/preferences.js`, tests `plugin/test/provider-sections.test.js`.

- [ ] **Step 1: Write failing tests**: Provision/Resume posts `sides: [side]` with the one-time key map; Retry posts only the failed side; buttons disabled with a reason while the caller's slot is busy; progress lines shown in the right section; opening the pane while status is `running` resumes polling in the right section; 401/403 shown inline; the one-time key field is cleared after use and never saved.
- [ ] **Step 2: Implement; commit.**

### Task 6.3: Preset group

**Files:** Modify `plugin/src/preferences.xhtml`, `plugin/src/preferences.js` (`refreshPresetState`, `applyPresetSwitch`), `plugin/src/autoindex-status.js` (its admin preset switch); tests.

- [ ] **Step 1: Write failing tests**: "Model preset" dropdown from `selectable_presets` with the default marked; admin-only "Server default preset" control; one control in loopback; changing either refreshes the sections and required keys; the auto-index status dialog's admin switch targets the default.
- [ ] **Step 2: Implement; commit.**

### Task 6.4: Setup wizard

**Files:** Modify `plugin/src/setup-wizard.js`/`.xhtml`; tests.

- [ ] A fresh install shows the default preset and asks only for the keys the default needs; mention the optional upgrade path. **Step 1: tests, Step 2: implement, commit.**

### Task 6.5: Manual verification and PR 3

- [ ] With the dev server and the hot-reloading plugin (do not rebuild): switch the live backend between `remote-kisski` and a mixed preset on the test library; confirm sections, bars, preset group; use the Zotero MCP bridge only against `groups/6297749`. Phase doc; open PR 3.

---

## Phase 7: Hugging Face provider (PR 4)

### Task 7.1: `HuggingFaceProvider`

**Files:** Create `backend/providers/huggingface.py`, `backend/tests/test_provider_huggingface.py`, `backend/config/default_presets/huggingface.json`; modify `backend/tests/test_config.py` (expected bundled names).

- [ ] **Step 1: Write failing tests** with a faked client (no live calls): status to health mapping for `running`, `scaledToZero`, `pending`, `initializing`, `paused`, `failed`, absent; `provision()` for absent (create then wait then warm up), `scaledToZero` and `paused` (resume), `running` (no-op), differing config (warn, `recreate` deletes first), progress steps emitted, deadline respected; `endpoint_url()` found/absent; `classify_http_error` 502 to `cold`; `apply_defaults` token pattern; `key_docs_url`; the contract suite on both sides.
- [ ] **Step 2: Implement** with plain `httpx` REST against `https://api.endpoints.huggingface.cloud/v2` (routes and payload in the Phase 0 results note; no new dependency). Include the 403 "Payment method required" mapping and the `updating`/`updateFailed` statuses. Options per spec C2. `key_scopes = {"user", "managed"}`.
- [ ] **Step 3: Add the preset** per spec C3 and its bundled-name test.
- [ ] **Step 4: Move** the `HF_TOKEN` entries out of any remaining shared tables (they were in `_KNOWN_*`, deleted in Task 2.2; confirm none remain).
- [ ] **Step 5: Run** and commit.

### Task 7.2: Mixed presets

- [ ] Add tests (and, if wanted, example files under `docs/` rather than bundled) for HF embeddings plus Anthropic LLM and local embeddings plus Anthropic LLM: both load, only the HF side provisions, key requirements list `HF_TOKEN` and `ANTHROPIC_API_KEY` once each. Commit.

### Task 7.3: Live smoke test (manual, needs an HF account)

- [ ] Follow spec C5: provision from the Preferences sections with progress lines, close and reopen mid-run, re-run, wake from scale-to-zero via a query, then repeat with the mixed preset and with a second user's token. Record results in the phase doc.

---

## Phase 8: Pause and Resume (PR 4)

Task 0.1 showed that `workersMax=0` works for RunPod, so both providers get Pause. RunPod specifics: create with `workersMax >= 1` and `PATCH` afterwards (creation with 0 returns 500), map HTTP 409 with body code `ENDPOINT_PAUSED` to `paused` in `classify_http_error()`, and read the paused state from the management API only (`/health` does not show it).

### Task 8.1: `suspend()` and the `paused` status

**Files:** Modify `backend/providers/base.py`, `runpod.py`, `huggingface.py`, `backend/providers/types.py` (`Health.status` gains `paused`), tests (contract suite `suspend` cycle).

- [ ] **Step 1: Write failing tests** (spec A6/A8): `suspend` idempotent; `health()` then `paused`; `provision()` returns to `ready`/`cold`; URLs unchanged; RunPod `workersMax` goes to 0 and is restored from options; HF `pause()`/`resume()`.
- [ ] **Step 2: Implement; commit.**

### Task 8.2: Suspend API

**Files:** Modify `backend/api/config.py`, `backend/services/provisioning.py`; tests.

- [ ] `POST /api/config/suspend` with optional `sides`, same scope gating and job slots as provisioning; 400 when no requested side supports suspend; 409 when the slot is busy. Tests and commit.

### Task 8.3: Respect the paused state

**Files:** Modify `backend/services/autoindex_scheduler.py`/`cron_indexer.py`/`autoindex_resolver.py` (skip a user's libraries when the **embedding** side is paused, with a reason surfaced through the existing per-library status reasons), `backend/api/document_upload.py` and the deferred-indexing path, `backend/services/embeddings.py` and `llm.py` (cached `health()` check, about 30 s TTL, only for providers with `supports_suspend`); tests `test_cron_indexer.py`, `test_autoindex_resolver.py`, `test_embeddings.py`, `test_llm.py`, `test_api.py`.

- [ ] **Step 1: Write failing tests**: a paused embedding side skips indexing with the reason and makes no call to the inference URL; a paused LLM side does not block indexing; one user's pause does not affect another user's libraries; queries return the paused-specific error without a network call; **the #70 rule**: the paused error is an `EmbeddingEndpointUnavailableError`, is passed with `count_toward_quarantine=False`, and a paused endpoint never advances a pending-upload entry toward quarantine or produces a `rag-failed` tag.
- [ ] **Step 2: Implement; commit.**

### Task 8.4: Pause/Resume buttons

**Files:** Modify `plugin/src/provider-sections.js`, `plugin/src/preferences.js`; tests.

- [ ] Pause button shown when a suspend-capable side is ready or cold and `operable_by_caller`; Resume (same provision path) when paused; neutral paused row; tests for each combination. Commit; phase doc; open PR 4.

---

## Phase 9: Documentation and cleanup

### Task 9.1: Docs

- [ ] `docs/presets.md`: provider ids, per-side `provider` block, scopes, default preset and personal choice, mixed presets, version changelog; remove references to the script contract. `docs/cron-indexing.md`: per-user key map, paused skipping. `CLAUDE.md`: provisioning and credential sections rewritten to current behaviour (no "previously"). Add a new `docs/providers.md` describing how to add a provider (the contract suite is the checklist).
- [ ] Mark `docs/superpowers/specs/2026-10-09-runpod-preset-design.md` and `2026-10-09-endpoint-health-provisioning-design.md` as superseded by the provider spec (a one-line status note at the top; do not restyle `docs/history` or spec files otherwise).

### Task 9.2: Final checks

- [ ] `uv run pytest -q`, `node --test plugin/test/`, `uv run pytest -m container -v -s`; grep for leftovers (`os.environ` credential reads, `RUNPOD_` in `.env.dist`, "RunPod" in core messages); confirm `bin/` tools never read provider keys from the environment; update the progress docs.

---

## Risks and sequencing notes

| Risk | Mitigation |
|---|---|
| ~~`workersMax=0` rejected (RunPod Pause)~~ | Resolved by the spike: accepted, and blocks with HTTP 409 `ENDPOINT_PAUSED` |
| Restricted RunPod keys cannot list endpoints | Phase 0; document that the key needs list permission, or fall back to storing the URL (spec A10 open question) |
| Large call-site sweep for `get_effective_preset` | Do it in grouped commits with the suite after each; pass the preset down instead of re-reading global state |
| Rebase friction with #70 files | Keep edits to `settings.py`, `cron_indexer.py`, `embeddings.py`, `preferences.xhtml`, `zotero-rag.js` surgical; rebase onto `devel` before each PR |
| Auto-index key store shape change | No migration by decision; users re-enter embedding keys once; stated in the phase 3 doc and release notes |
| Bundled preset overwrite discards local edits | Logged WARNING naming the file; documented; customise by new file name |
| Cost of live HF/RunPod testing | Smallest GPU, scale-to-zero, delete spike resources, ask before creating billable resources |
| HF, MPCDF behaviour unverified until live | Unit tests with fakes; `unverified` docstrings; live checklists in Phases 0 and 7 |

## Deferred (not in this plan)

Scheduled automatic pause, relaxing the personal-choice compatibility rule, naming the admin on managed presets, preset overwrite backups, an `fields` list in the descriptor, and any billing or spend display (spec "Deferred" list).
