# Stand-in Presets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a query fails because a provider's quota is exhausted, offer ranked stand-in presets; a chosen stand-in applies to the caller's queries until the quota resets.

**Architecture:** Providers declare a cost class and presets may add `cost`, `standin`, `standin_priority`. A new `backend/services/standin.py` derives stand-ins (compatibility + exhausted-provider rule + needs) and reads the server's own rate-limit state. The stand-in is stored per user (or once for loopback) with an expiry in `user_settings.json`; the auth middleware resolves it only for query and configuration routes. `POST /api/query` returns a structured 503 for long quota errors; the plugin shows the offer and the active stand-in.

**Tech Stack:** Python 3.12/FastAPI/pydantic, unittest via `uv run pytest`; plugin JavaScript with `node --test`.

**Spec:** `docs/superpowers/specs/2026-11-16-preset-standin-design.md`

## Global Constraints

- Never switch automatically; a stand-in is only stored on the user's explicit choice.
- Offer only when the reset is at least 15 minutes away: `MIN_STANDIN_SECONDS = 900`.
- Stand-in resolution is opt-in per request: only `POST /api/query`, `/api/config*` and `/api/required-keys` get it; indexing, uploads, the auto-index resolver, cron and scheduler never do.
- The stand-in's `until` comes from the server's rate-limit state (`usage_meters.recorder`), never from the client.
- Cost classes: `kisski`, `mpcdf` free; `runpod`, `huggingface`, `openai`, `google`, `anthropic`, `generic` paid; a `model_type: "local"` side free. Order: free first, then `standin_priority` descending, then name.
- No key value in offers, stored settings or logs. Async handlers do file and provider work in `asyncio.to_thread`.
- User-facing docs describe current behaviour only.

## Review Focus

- A stand-in that expires between two questions: the second runs on the user's own preset without any action. → Task 3 test `test_expired_standin_is_dropped`.
- Backend restarted after the quota error (rate-limit state lost): choosing a stand-in returns 409, not a switch without expiry. → Task 5 test `test_put_standin_without_exhausted_quota_is_409`.
- User picks a stand-in, then chooses a preset by hand: the stand-in is gone. → Task 5 test `test_choosing_a_preset_clears_the_standin`.
- The stand-in becomes incompatible while active (admin changes the default's embedding model): it is ignored, the default applies. → Task 3 test `test_incompatible_standin_is_ignored`.
- The re-sent question still names the old preset's `llm_model`: the plugin drops it on resend, so the backend does not answer 400. → Task 6 test `retry after a stand-in drops llmModel`.

---

### Task 1: Cost declarations

**Files:**
- Modify: `backend/providers/base.py` (`Provider`), `backend/providers/{kisski,mpcdf,runpod,huggingface,openai,google,anthropic}.py`, `backend/config/presets.py` (`HardwarePreset`)
- Create: `backend/services/standin.py` (only `preset_cost` in this task)
- Test: `backend/tests/test_standin.py`

**Interfaces:**
- Produces: `Provider.cost: ClassVar[Literal["free", "paid"]]` (base `"paid"`); `HardwarePreset.cost: Optional[Literal["free","paid"]] = None`, `HardwarePreset.standin: bool = True`, `HardwarePreset.standin_priority: int = 0`; `preset_cost(preset: HardwarePreset) -> Literal["free","paid"]` in `backend/services/standin.py`.

- [ ] **Step 1: Write the failing tests** (`CostTest`): `preset_cost(get_preset("remote-kisski")) == "free"`, `remote-mpcdf` → free, `kisski-rerank-mpcdf` → free, `runpod` → paid, `cpu-only` → free (all local); a preset copy with `cost="free"` on `runpod` → free; default `standin is True`, `standin_priority == 0`; every registered provider class has `cost` in `("free","paid")`.
- [ ] **Step 2: Run** `uv run pytest backend/tests/test_standin.py -q` — expected: import error.
- [ ] **Step 3: Implement** the class attributes, the three preset fields (with descriptions) and `preset_cost` (explicit `preset.cost` wins; else free iff every present side is local or its provider class is free).
- [ ] **Step 4: Run** the test file — expected: PASS; then `uv run pytest backend/tests/test_config.py backend/tests/test_providers_registry.py -q` — PASS.
- [ ] **Step 5: Commit** `feat(presets): declare a cost class per provider and stand-in fields per preset`.

### Task 2: Finding stand-ins

**Files:**
- Modify: `backend/services/standin.py`
- Test: `backend/tests/test_standin.py`

**Interfaces:**
- Consumes: `preset_cost` (Task 1), `is_compatible` (`backend/services/effective_preset.py`), `list_presets`/`get_preset`, `get_providers`, `Provider.session_status`, `Provider.describe()`.
- Produces: `MIN_STANDIN_SECONDS = 900`; `@dataclass ExhaustedSide(side: str, provider: str, available_at: datetime)`; `@dataclass StandIn(preset: str, cost: str, label: str, needs: list[str])` with `to_dict()`; `find_standins(settings, current: HardwarePreset, exhausted: list[ExhaustedSide], client_keys: dict[str, str], user_id: Optional[int]) -> list[StandIn]`.

- [ ] **Step 1: Write the failing tests** (`FindStandinsTest`, temp data dir seeded with `ensure_default_presets` plus custom preset files):
  - KISSKI embedding exhausted, current `kisski-rerank-mpcdf`: result contains `remote-mpcdf` (free) before `runpod` (paid); excludes `kisski-gemini`, `remote-kisski`, `kisski-docling` (KISSKI embedding) and the current preset.
  - KISSKI LLM exhausted: `kisski-gemini` (KISSKI embedding, Google LLM) **is** offered; `remote-kisski` is not.
  - A preset with `"standin": false` is never offered; `standin_priority: 5` puts a paid preset before another paid one with 0.
  - A preset with a different embedding model is never offered.
  - `needs`: `runpod` without `RUNPOD_API_KEY` in `client_keys` → `"key:RUNPOD_API_KEY"`; with it → no key need; `remote-mpcdf` with a patched `session_status` returning None/`{"state":"login_required"}` → `"login:mpcdf"`, with `{"state":"active"}` → none.
- [ ] **Step 2: Run** — expected: FAIL (names missing).
- [ ] **Step 3: Implement** `find_standins` per the spec's rules 1-4 and order (platform filter as `get_user_preset` does; skip presets that fail `get_providers`). `label` = the preset description's first sentence. `needs` order: `login:*`, then `key:*`; no `start:*` (Ruling: start needs are discovered by the query dialog's existing pipeline check after the switch).
- [ ] **Step 4: Run** — expected: PASS.
- [ ] **Step 5: Commit** `feat(standin): derive and rank stand-in presets`.

### Task 3: Stored stand-in and request-scoped resolution

**Files:**
- Modify: `backend/services/user_settings.py`, `backend/services/effective_preset.py`, `backend/main.py` (`api_key_middleware`)
- Test: `backend/tests/test_standin.py` (`StoredStandinTest`), `backend/tests/test_effective_preset.py`

**Interfaces:**
- Produces: `get_standin(data_path, user_id: Optional[int]) -> Optional[dict]`, `set_standin(data_path, user_id: Optional[int], preset: str, until: datetime, replaces: str) -> None`, `clear_standin(data_path, user_id: Optional[int]) -> None` (user_id None = the loopback entry, key `"loopback"`); `get_user_preset(settings, identity, allow_standin: bool = False)`; `STANDIN_ROUTES` predicate `standin_applies(path: str) -> bool` in `effective_preset.py`.

- [ ] **Step 1: Write the failing tests:**
  - `test_active_standin_is_used`: user 7 with own choice `kisski-rerank-mpcdf` and stand-in `remote-mpcdf` until now+1h → `get_user_preset(..., allow_standin=True).name == "remote-mpcdf"`; with `allow_standin=False` → `kisski-rerank-mpcdf`.
  - `test_expired_standin_is_dropped`: until now-1 s → own choice, and `get_standin(...)` is None afterwards.
  - `test_incompatible_standin_is_ignored`: stand-in with another embedding model → own choice/default.
  - `test_loopback_standin`: identity None, loopback stand-in active → stand-in when allowed, None when not.
  - `test_standin_applies`: True for `/api/query`, `/api/config`, `/api/config/preset-view`, `/api/required-keys`; False for `/api/index`, `/api/documents/upload`, `/api/autoindex/scheduler/run-now`.
  - `test_resolver_ignores_standins`: `autoindex_resolver` path calls `get_effective_preset` without stand-ins (assert the stored stand-in does not change the resolved `preset_name`).
- [ ] **Step 2: Run** — expected: FAIL.
- [ ] **Step 3: Implement** the storage (same file, atomic write as today), `allow_standin` in `get_user_preset` (expired → `clear_standin`), and in the middleware pass `allow_standin=standin_applies(request.url.path)`. `get_effective_preset` keeps `allow_standin=False`.
- [ ] **Step 4: Run** `uv run pytest backend/tests/test_standin.py backend/tests/test_effective_preset.py -q` — PASS.
- [ ] **Step 5: Commit** `feat(standin): store a temporary stand-in and apply it to queries only`.

### Task 4: Structured quota error with the offer

**Files:**
- Modify: `backend/api/query.py` (the `except (... RateLimitExhaustedError ...)` branch), `backend/services/standin.py`
- Test: `backend/tests/test_standin.py` (`QuotaDetailTest`)

**Interfaces:**
- Consumes: `find_standins`, `ExhaustedSide`, `MIN_STANDIN_SECONDS`.
- Produces: `quota_error_detail(settings, preset: HardwarePreset, side: str, available_at: datetime, message: str, client_keys: dict, user_id: Optional[int], now: Optional[datetime] = None) -> Optional[dict]` returning `{"code": "quota_exhausted", "message", "side", "provider", "available_at" (ISO, Z), "standins": [StandIn.to_dict()]}` or None when the reset is under 15 min away.

- [ ] **Step 1: Write the failing tests:** reset in 4 h → dict with `code == "quota_exhausted"`, `provider == "kisski"`, `side == "embedding"`, non-empty `standins`; reset in 5 min → None; `message` names the provider label and the reset time ("KISSKI's embedding quota is used up until 23:00 UTC.").
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** the helper; in `query.py` map `EmbeddingRateLimitExhaustedError` → side `embedding`, `LLMRateLimitExhaustedError` → `llm`, provider from the effective preset's side, call the helper in `asyncio.to_thread`, raise 503 with the dict, else the string detail as today.
- [ ] **Step 4: Run** `uv run pytest backend/tests/test_standin.py backend/tests/test_query*.py -q` — PASS.
- [ ] **Step 5: Commit** `feat(query): offer stand-ins when a provider's quota is exhausted`.

### Task 5: Stand-in endpoints

**Files:**
- Modify: `backend/api/config.py` (`PUT /config/my-preset/standin`, `DELETE /config/my-preset/standin`, `GET /config` field, `PUT /config/my-preset` and the server-default change clear stand-ins), `backend/services/standin.py`
- Test: `backend/tests/test_standin_api.py`

**Interfaces:**
- Consumes: Tasks 2-3; `usage_meters.recorder.rate_limited_until(side, fingerprint)`, `key_fingerprint`, `get_client_api_keys`, `admin_settings_store.resolve_shared_value`.
- Produces: `current_exhausted_sides(settings, preset, client_keys, now=None) -> list[ExhaustedSide]` (sides whose recorded limit ends ≥ 15 min from now); `GET /api/config` gains `standin: {preset, until, replaces} | null`.

- [ ] **Step 1: Write the failing tests** (TestClient on a temp data dir, loopback and identity variants, recorder state set with `recorder.mark_rate_limited("embedding", until, key_fingerprint(<test key>))`):
  - `test_put_standin_stores_with_server_until`: PUT `{"preset_name": "remote-mpcdf"}` → 200, stored `until` equals the recorded time, `GET /api/config` reports it and `preset_name == "remote-mpcdf"`.
  - `test_put_standin_without_exhausted_quota_is_409`.
  - `test_put_standin_not_a_standin_is_409` (e.g. `remote-kisski` while KISSKI embedding is exhausted).
  - `test_delete_standin_ends_it`.
  - `test_choosing_a_preset_clears_the_standin` (identity) and `test_changing_server_default_clears_loopback_standin`.
  - `test_index_route_ignores_standin`: with an active stand-in, an indexing route's request-scoped preset is the own preset (call a cheap indexing-side endpoint or assert via the middleware predicate plus `get_hardware_preset` inside a dependency override).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement** the helper (key per side: `client_keys[api_key_env]` else the shared value; fingerprint; recorder lookup), the endpoints (sync `def` handlers), and the clears.
- [ ] **Step 4: Run** `uv run pytest backend/tests -q` — all pass.
- [ ] **Step 5: Commit** `feat(config): choose and end a temporary stand-in`.

### Task 6: Plugin offer and active stand-in

**Files:**
- Modify: `plugin/src/zotero-rag.js` (`submitQuery` error, `useStandin`, `endStandin`), `plugin/src/dialog.js` (offer notice, active line, retry), `plugin/src/dialog.xhtml` (notice and line elements), plugin locale/help text if the dialog uses it
- Test: `plugin/test/zotero-rag.test.js`, `plugin/test/dialog.test.js`

**Interfaces:**
- Consumes: the 503 detail of Task 4; endpoints of Task 5.
- Produces: `class QuotaExhaustedError extends Error` with `detail`; `plugin.useStandin(presetName: string): Promise<void>` (PUT, then refresh required keys); `plugin.endStandin(): Promise<void>`; `dialog.describeStandinOffer(detail, now: Date) -> {text: string, choices: Array<{preset: string, label: string, cost: string, needs: string[]}>}` (pure, tested).

- [ ] **Step 1: Write the failing tests:**
  - `submitQuery throws QuotaExhaustedError with the structured detail` (fetch stub returns 503 with `{detail: {code: "quota_exhausted", ...}}`); a string-detail 503 still throws a plain Error.
  - `useStandin PUTs the preset name and refreshes required keys`.
  - `describeStandinOffer` text: "KISSKI's embedding quota is used up until 23:00 (in 4 h)."; free choices first as given; a `key:` need is dropped when the key pref is set.
  - `retry after a stand-in drops llmModel` (the resend's options have no `llmModel`).
- [ ] **Step 2: Run** `node --test plugin/test/*.test.js` — the new tests FAIL.
- [ ] **Step 3: Implement:** on `QuotaExhaustedError` the dialog shows the notice (choices with **Use**, and **Not now**) instead of the plain error. **Use** → `useStandin` → `checkPipelineOnOpen()` (opens Configure preset for a login, key or start) → resend the question without `llmModel`. When `GET /api/config` reports `standin`, show "Using <preset> until <HH:MM> (<provider> quota) — End now"; **End now** → `endStandin` and refresh.
- [ ] **Step 4: Run** `node --test plugin/test/*.test.js` and the type check (`cd plugin && npx --no-install tsc -p jsconfig.json --noEmit | wc -l` must not exceed the count before this task).
- [ ] **Step 5: Commit** `feat(plugin): offer a stand-in preset when a quota is exhausted`.

### Task 7: Docs and live check

**Files:**
- Modify: `docs/presets.md` (fields `cost`, `standin`, `standin_priority`), `docs/providers.md` (cost class), `docs/plugin-settings.md` (the offer and the active line), `docs/query-routing.md` or the API docs that list `POST /api/query` errors

- [ ] **Step 1:** Write the docs sections (current behaviour only).
- [ ] **Step 2: Live check.** The backend's rate-limit state can only be set by a real 429 inside the backend process, so the server side is covered end to end by `backend/tests/test_standin_api.py` (TestClient on the real app). In the running dev Zotero, open the query dialog and, via `zotero_execute_js`, feed the notice a sample `quota_exhausted` detail (the dialog's error path with a `QuotaExhaustedError`), then check that it renders the choices and that **Use** sends `PUT /api/config/my-preset/standin` to the dev backend (expected answer there: 409, since no quota is exhausted). Write in the PR what was exercised live and what only by tests.
- [ ] **Step 3: Run** `uv run pytest backend/tests -q` and `node --test plugin/test/*.test.js` — all pass.
- [ ] **Step 4: Commit** `docs: stand-in presets`.

## Follow-up (found in live use, 2026-11-16)

The offer only appeared on a failed query, but the query dialog's open-time pipeline check stops the user before that: a quota-exhausted side shows as "down", the dialog sends the user to Configure preset, and that dialog says "Check the credentials in the account card below." The offer must appear where the user meets the exhausted quota. Separately, account cards of providers that cannot start endpoints (KISSKI) offer "Save and start".

### Task 8: Offer on request, quota-aware status, honest card button

**Files:**
- Modify: `backend/services/standin.py`, `backend/api/config.py`, `backend/services/preset_view.py`, `backend/providers/ui.py` (if a field is needed)
- Test: `backend/tests/test_standin.py`, `backend/tests/test_standin_api.py`, `backend/tests/test_preset_view_api.py`

**Interfaces:**
- Produces: `standin_offer(settings, preset, exhausted: list[ExhaustedSide], client_keys, user_id, now=None) -> Optional[dict]` in `standin.py` (the 503 detail shape; `quota_error_detail` becomes a thin wrapper over it); `GET /api/config/standins` (sync `def`) → `{"offer": <detail dict> | null}` for the caller's own current preset (null while a stand-in is active or nothing is exhausted ≥ 15 min); in the preset view, a side whose probe failed for an exhausted quota carries the message "<Provider label>'s <side> quota is used up until HH:MM UTC." instead of "Check the credentials in the account card below." and an action `{"id": "standin", "label": "Use a stand-in until HH:MM", enabled}`; `AccountCard.auto_start` is False when the card's provider has `supports_provisioning = False`.

- [ ] **Step 1: Write the failing tests:** `standin_offer` with two exhausted sides lists stand-ins valid for both; `GET /api/config/standins` returns the offer when the recorder holds a ≥ 15 min limit for the caller's key, `{"offer": null}` otherwise and while a stand-in is active; preset view of `kisski-rerank-mpcdf` with an embedding probe failed for quota shows the quota message and the `standin` action and not the credentials message; the KISSKI account card has `auto_start == False`, an MPCDF/RunPod card keeps its current value.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest backend/tests -q` — all pass.
- [ ] **Step 5: Commit** `feat(standin): offer stand-ins on request and report exhausted quotas in the preset view`.

### Task 9: Plugin: offer where the quota is met

**Files:**
- Modify: `plugin/src/dialog.js` (`checkPipelineOnOpen`), `plugin/src/zotero-rag.js` (`fetchStandinOffer`), `plugin/src/preset-dialog-view.js` / `plugin/src/preset-dialog.js` (the `standin` action)
- Test: `plugin/test/dialog.test.js`, `plugin/test/zotero-rag.test.js`, `plugin/test/preset-dialog-view.test.js`

**Interfaces:**
- Consumes: `GET /api/config/standins`, the preset view's `standin` action (Task 8); existing `showStandinOffer`, `useStandin`, `describeStandinOffer`.
- Produces: `plugin.fetchStandinOffer(): Promise<object|null>`.

- [ ] **Step 1: Write the failing tests:** `checkPipelineOnOpen` with a quota-blocked side and a non-null offer shows the stand-in offer and neither alerts nor opens the Configure preset dialog; with a null offer it behaves as today; the Configure preset dialog's `standin` action fetches the offer, lists the choices with **Use**, and **Use** calls `useStandin` then refreshes the view; the account card button reads "Save" when `auto_start` is false (existing behaviour, assert it).
- [ ] **Step 2: Run** `node --test plugin/test/*.test.js` — new tests FAIL.
- [ ] **Step 3: Implement.** While the offer is shown in the query dialog, the question input stays usable; after **Use** the dialog reloads its config and health (no question to re-send at open time).
- [ ] **Step 4: Run** the node tests and the type check (≤ the count before the task).
- [ ] **Step 5: Commit** `feat(plugin): offer stand-ins when the dialogs find an exhausted quota`.
