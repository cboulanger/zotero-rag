# Implementation plan: auto-index status dialog rate limits, preset switch, Ask-dialog button

Spec: `docs/superpowers/specs/2026-10-09-autoindex-status-rate-limits-preset-switch-design.md`
Branch: `claude/quirky-feynman-8wy83q` (work in `/home/user/zotero-rag`; do NOT create other branches; do not commit to `main`).

## API contract (shared by both workstreams)

- `GET /api/autoindex/status` adds `rate_limits`: `{available: bool, limits?: Record<string,string>, as_of?: ISO str, source?: "run"|"cache"}`. Never probes.
- `GET /api/config` adds `switchable_presets`: `[{name: str, active: bool, credentials: "ok"|"missing"}]` (active always listed; others only if credentials ok). `compatible_presets` unchanged.
- `POST /api/config`: 400 if target preset lacks credentials (names of keys only); on success calls `reset_rate_limit_cache()` and `AutoIndexKeyStore.clear_rate_limits()`.
- `GET /api/rate-limits`: unchanged behaviour, uses shared helper.

## Workstream A - Backend (Python, `uv run pytest`)

1. `backend/services/rate_limit_info.py`: `get_cached_rate_limits(settings)` -> in-process `_last_rate_limit_headers`, then `cron_status.json["last_rate_limit_headers"]` (ignored if `last_rate_limit_preset` != active preset name). No probe. Returns dict with `limits`, `as_of`, `source` or None.
2. `embeddings.py`: `reset_rate_limit_cache()`.
3. `cron_indexer.py`: persist `last_rate_limit_headers_at` and `last_rate_limit_preset` next to `last_rate_limit_headers` (~line 463).
4. `autoindex_key_store.py`: `clear_rate_limits()` (clear `embedding_key_rate_limit_until`, reset status "rate_limited" -> "ok"; keep "invalid"); helper counting stored keys by `embedding_key_name` excluding invalid.
5. `api/autoindex.py` `status()`: add `rate_limits` (via `asyncio.to_thread`, FastAPI no-blocking rule).
6. `api/config.py`: `_preset_credentials(preset, settings, request)` per spec; `switchable_presets` in `ConfigResponse`/`get_config`; credential check + resets in `update_config`.
7. `api/rate_limits.py`: use shared helper before probing.
8. Tests under `backend/tests/` per spec "Testing - Backend"; update existing tests for additive fields. Run the full backend suite.
9. Docs: `docs/presets.md`, `docs/cron-indexing.md` (Admin Controls), status-quo wording only.

## Workstream B - Plugin (JS, `node --test`, JSDoc types, `// @ts-check`)

1. `plugin/src/rate-limit-widget.js` (`ZoteroRAGRateLimitWidget`: `fetch`, `render`, `describe`), registered where plugin scripts are loaded; shared CSS.
2. Refactor `dialog.js`/`dialog.xhtml` to use it with identical behaviour (ids unchanged).
3. `autoindex-status.xhtml/js`: widget in `#status-section` under `#run-banner` (ids prefixed `ai-rate-limit-*`), fed from `data.rate_limits`; one-time `GET /api/rate-limits` fallback at open; banner line for earliest `rate_limit_until`; admin preset row (select + status text + count hint) populated from `GET /api/config` `switchable_presets` at open and after switch; hidden if non-admin or <2 options; disabled while `data.running`; on change `POST /api/config`, revert and show `detail` on error, refresh after success.
4. `dialog.xhtml/js`: `#autoindex-status-button` before `#cancel-button`, hidden by default; `fetchAutoindexStatus()` helper shared with `isServerIndexingRunning()`; `refreshAutoindexButton()`: visible iff `enabled && (scheduler.active || keys_registered > 0)`; click -> `plugin.openAutoindexStatusDialog(window)`.
5. Tests in `plugin/test/` per spec; run `node --test`. Do NOT rebuild the plugin.
6. Docs: `docs/plugin-settings.md` if relevant.

## Review and integration

1. Both workstreams run in parallel on disjoint files. Agents must NOT commit; the orchestrator commits.
2. After both finish: run backend + plugin test suites, then a review pass of the combined diff against the spec; fix findings.
3. Commit in atomic commits (backend, plugin, docs) and push to the designated branch.
4. Add a short implementation-progress note at the end of this file.
