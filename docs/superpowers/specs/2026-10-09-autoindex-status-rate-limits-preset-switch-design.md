# Auto-index status dialog: rate limits, preset switching, and Ask-dialog entry point

## Problem

The "Automatic Indexing Status" dialog (`plugin/src/autoindex-status.{xhtml,js}`)
is where users and admins watch the server-side index run. Three gaps:

1. When the embedding endpoint is rate limited (KISSKI), a run silently stalls
   or skips libraries (`skip_reason: "embedding_rate_limit"`). The "Ask
   Question" dialog already shows remaining requests per hour/day
   (`#rate-limit-section`, `dialog.js` `updateRateLimitDisplay()`), but this
   dialog does not, so users can't see *why* indexing is paused or when
   capacity returns.
2. When the quota of the active preset is exhausted, the only remedy is the
   "Active preset" dropdown buried in Preferences. An admin watching a stalled
   run cannot switch to another preset (e.g. `remote-kisski` -> `remote-mpcdf`)
   from the place where they see the problem.
3. The status dialog is only reachable indirectly (a click on the library
   auto-index icon, or the Ask dialog's Index button when a server run is in
   progress — `dialog.js` `submitIndexOnly()`). There is no permanent entry
   point from the Ask dialog, even though the server-side run is what keeps its
   libraries current.

## Goals

- Show rate-limit bars in the status dialog's **Status** section whenever the
  active embedding service exposes rate limits, reusing the Ask dialog's
  rendering code (extracted, not copied).
- Provide an admin-only dropdown in the **Admin** section to switch the active
  preset at runtime, listing only presets that are switchable *and* have
  usable credentials.
- Add a footer button in the Ask dialog, left of "Close", that opens the status
  dialog; hidden when server-side auto-indexing is not configured.

## Non-goals

- No new preset-switching mechanism. `POST /api/config` and
  `compatible_presets` (`backend/api/config.py`) stay the single switch path;
  the new dropdown is a second client of it. Local-model presets and presets
  with a different embedding model remain restart-only.
- No change to how rate-limit headers are captured
  (`RemoteEmbeddingService._capture_rate_limit_headers`) or how
  `GET /api/rate-limits` resolves them.
- No automatic switching when a limit is hit; the admin chooses.
- No change to per-user key storage or the cron indexer's key resolution.

## Current state (relevant facts)

- `GET /api/rate-limits` (`backend/api/rate_limits.py`) returns
  `{available, limits}` where `limits` holds `x-ratelimit-{limit,remaining}-{hour,day}`.
  Resolution: in-process cache -> `cron_status.json["last_rate_limit_headers"]`
  (written by `CronIndexer` after each slug) -> live probe call. It builds the
  embedding service from the *caller's* client keys (`get_client_api_keys`).
- Ask dialog: `rateLimitAvailable` is set from `config.embedding_model_type === 'remote'`;
  `fetchRateLimitHeaders()` + `updateRateLimitDisplay()` fill two bars
  (green < 75 %, amber >= 75 %, red >= 95 % used) and a text such as
  "123 requests left/hour". Markup/CSS is in `dialog.xhtml` (`#rate-limit-section`,
  `.rate-limit-row`, `.rate-limit-bar-track`, `.rate-limit-bar`, `.rate-limit-text`).
- `GET /api/autoindex/status` already returns `enabled`, `is_admin`, `scheduler`,
  per-slug `skip_reason`/`rate_limit_until`, and (admin) `system_health`. The
  dialog polls it every 5 s (`autoindex-status.js` `fetchAndRender`).
- `GET /api/config` returns `preset_name`, `embedding_model_type`,
  `available_presets`, `compatible_presets`. `POST /api/config`
  (`require_authorized_group_admin`) validates against `compatible_presets` and
  writes `active_preset_override` (`admin_settings_store`), which
  `Settings.get_hardware_preset()` honours for every request *and* the cron
  indexer. Preferences already has a dropdown using exactly this
  (`preferences.js`, `#zotero-rag-preset-select`).
- `GET /api/required-keys` reports key requirements of the **active** preset only,
  with `is_set` for `shared_*` kinds only.
- Personal keys (`api_key_env`, e.g. `KISSKI_API_KEY`) are per-user request
  headers; the cron job uses each user's stored (Fernet-encrypted) embedding key
  (`AutoIndexKeyStore.get_decrypted_embedding_key`), tagged with `embedding_key_name`.

## Design

### 1. Rate limits in the Status section

**Shared module.** Extract the rate-limit logic from `dialog.js` into a new
plugin script `plugin/src/rate-limit-widget.js` exposing a global
`ZoteroRAGRateLimitWidget` (same loading pattern as other plugin scripts,
registered in `manifest.json`/loaded via `loadSubScript` in each xhtml):

```js
/** @typedef {Record<string,string>} RateLimitHeaders */
ZoteroRAGRateLimitWidget = {
  /** Fetch GET /api/rate-limits; resolves headers or null. */
  async fetch(plugin): Promise<RateLimitHeaders|null>,
  /** Paint bars into a container holding the standard rows. */
  render(doc, headers, {visible}): void,
  /** Thresholds/colour/text logic (pure, unit-testable). */
  describe(headers, period): {limit, remaining, usedPct, color, text}|null,
}
```

`describe()` is the existing body of `updateRateLimitDisplay()` (percent used,
75/95 thresholds, `"N requests left/<period>"`). `dialog.js` is refactored to
call the module; its element ids and behaviour are unchanged. The markup block
and its CSS move to a shared stylesheet/snippet (`rate-limit.css`, plus the
identical row markup duplicated in the two xhtml files — XUL/XHTML dialogs have
no includes). Element ids in the status dialog are prefixed
(`ai-rate-limit-*`) to avoid colliding with the Ask dialog's ids in tests.

**Data threading.** Two options were considered:

| Option | Pro | Con |
|---|---|---|
| A. Status dialog calls `GET /api/rate-limits` itself | No backend change | Second request/5 s poll; live-probe fallback spends a real API call on every poll |
| B. Add `rate_limits` to `GET /api/autoindex/status` | One request; reuses the cron-written snapshot | Small backend change |

**Chosen: B**, with a cheap source. `status()` adds:

```json
"rate_limits": {
  "available": true,
  "limits": {"x-ratelimit-limit-hour": "...", ...},
  "as_of": "2026-10-09T10:12:03+00:00",   // when the headers were captured, if known
  "source": "run" | "cache"
}
```

Resolution is a new helper `get_cached_rate_limits(settings)` in
`backend/services/rate_limit_info.py`, shared with `api/rate_limits.py` so both
endpoints use one implementation of steps 1-2: (1) in-process
`_last_rate_limit_headers`, (2) `cron_status.json["last_rate_limit_headers"]`.
The **live probe is intentionally excluded** from the status endpoint (polled
every 5 s; a probe consumes quota). If neither cache has data, the field is
`{"available": false}`; the dialog then falls back to one `fetch()` of
`GET /api/rate-limits` at open time (which may probe), not on each poll.

`cron_indexer` also stores `last_rate_limit_headers_at` (ISO timestamp)
alongside the headers so the dialog can show staleness ("as of 14 min ago");
without it `as_of` is omitted.

**Visibility rule.** Show the widget when `rate_limits.available` is true
*and* the active preset's embedding is remote (`rate_limits.available` is
already false for local services, since `get_rate_limit_info()` returns None
there). Placed in `#status-section`, under `#run-banner`, using the shared
renderer. When any slug has `skip_reason === "embedding_rate_limit"`, the
banner area additionally shows the earliest `rate_limit_until` ("Embedding
rate limit reached; resumes at HH:MM") — this data already exists per slug; the
spec only surfaces it in the Status section instead of only per-row.

Visible to all users (rate-limit counts are server-wide quota numbers, not
per-user data), consistent with the Ask dialog showing them to everyone.

### 2. Admin preset dropdown

**UI** (`#admin-controls`, new row below the buttons):

```
Embedding/LLM preset: [ remote-kisski v ]   (status text)
```

- Visible only for admins (inherits `#admin-controls` visibility from
  `is_admin`), and only when the server reports at least 2 switchable presets.
- Populated from a new backend field (below); the active preset is selected;
  presets that are switchable but lack credentials are **omitted** (per the
  request: "only those presets which have valid API keys configured"). The
  active preset is always listed even if its own credentials are currently
  missing, so the control never shows a blank selection.
- On change: `POST /api/config {preset_name}` (existing endpoint). On success
  refresh `fetchAndRender()` and the rate-limit widget (headers differ per
  provider; the cached headers belong to the previous preset — see
  "Cache invalidation"). On error show `detail` in the status text and revert
  the select (same pattern as `preferences.js`).
- Disabled while a run is in progress (`data.running`), with a tooltip: the
  running subprocess has already built its embedding service from the old
  preset; the switch only takes effect from the next run. (Allowing it while
  running is a possible follow-up; the safe default avoids a user believing a
  mid-run switch resumed a rate-limited run. The "Run full index now" button
  is how the admin applies the switch immediately after.)

**Backend: which presets are listed.** Extend `GET /api/config` response with an
additive field:

```json
"switchable_presets": [
  {"name": "remote-kisski", "active": true,  "credentials": "ok"},
  {"name": "remote-mpcdf",  "active": false, "credentials": "ok"}
]
```

`compatible_presets` is kept as is (Preferences uses it unchanged).
`switchable_presets` = `compatible_presets` filtered by a credential check.
Credential check `_preset_credentials(preset, settings, request)`, for the
preset's embedding **and** LLM `required_client_fields`:

- `shared_base_url` / `shared_api_key`: satisfied iff `get_remote_config_value`
  or `os.environ` has a non-empty value (same logic as `/required-keys` `is_set`).
- `api_key` (personal): satisfied iff the *requesting admin's* header
  (`env_var_to_header`) or a server env var is present, **or** — for the
  embedding key, which is what the cron job needs — at least one stored
  auto-index embedding key exists whose `embedding_key_name` equals the
  required `key_name` and whose `embedding_key_status` is not `"invalid"`.
  This is what makes an offered preset actually work for the next auto-index
  run, which is the dialog's purpose.
- "Valid" means *present and not known-invalid*, not live-validated: calling
  `validate_embedding_key` (a real embedding call) per preset per poll would
  burn quota on the very endpoint that is rate limited. Known-invalid is
  derived from the stored `embedding_key_status` (set by the cron run and by
  `POST /autoindex/keys` validation). An explicit live validation is out of
  scope; a wrong key surfaces in the existing `key_issues`/`Problems` section
  after the next run.
- Presets whose files fail to load are skipped with a warning, mirroring
  `_compatible_presets`.

To keep `GET /api/config` cheap and unchanged in cost for existing callers, the
status dialog does not poll it; it fetches `GET /api/config` once at open and
again after a switch. (Admin-only data is not added to the 5 s status payload.)

**Backend: switching.** Reuse `POST /api/config` as is, with two small
changes:

1. Reject (HTTP 400) a switch to a preset that fails the credential check, so a
   stale/misbehaving client can't switch to a preset that would immediately fail
   every run. Message names the missing key(s) (names only, never values).
2. Clear the process-local rate-limit cache on a successful switch (see below).

**Cache invalidation.** `_last_rate_limit_headers` in `embeddings.py` is a
module-level global, so after a switch the widget would show the previous
provider's numbers until the next call. Add `reset_rate_limit_cache()` in
`embeddings.py`, called from `update_config` after `set_active_preset_override`.
`cron_status.json["last_rate_limit_headers"]` is likewise tagged with the preset
name that produced it (`last_rate_limit_preset`); `get_cached_rate_limits`
ignores headers whose preset differs from the active one. When nothing matches,
`rate_limits.available` is false and the dialog's open-time `GET /api/rate-limits`
fallback applies (live probe under the new preset, using the caller's keys).

**Effect on the scheduler and cron.** `Settings.get_hardware_preset()` already
reads the override on each call, so the next scheduler tick / `run-now` /
`POST /autoindex/run` uses the new preset with no restart. Libraries that were
skipped with `embedding_rate_limit` under the old preset remain in the
persisted rate-limit-skip state (`rate_limit_until`); the switch should clear
those skips so the admin's "Run full index now" actually retries them — the
cron's rate-limit gating is keyed by the preset-agnostic embedding key
fingerprint today, so this spec adds: on preset switch, clear
`embedding_key_rate_limit_until` for all stored keys (`AutoIndexKeyStore`
helper `clear_rate_limits()`), and document it. Keys with `invalid` status stay.

**Concern - stored embedding key vs. provider.** A user's stored embedding key
is for one provider (`embedding_key_name`). If the admin switches KISSKI ->
MPCDF, users who only have a KISSKI key stored will be skipped
(`shared_api_key` presets need no per-user key, so MPCDF works for all; a
different personal-key preset would skip users lacking that key). The
credential check above already requires at least one matching stored key for
personal-key presets, but cannot guarantee *every* user has one. The dialog
shows a hint under the dropdown when the selected preset's personal key is
registered by fewer users than the number of registered keys ("N of M users
have a key for this preset; others will be skipped"). Counts only — no user
identities.

### 3. "Indexing status" button in the Ask dialog footer

`dialog.xhtml` `#input-buttons`: insert before `#cancel-button`

```html
<button id="autoindex-status-button" type="button" class="dialog-button"
        style="display:none;" title="Show server-side automatic indexing status">Indexing status</button>
```

(Label "Indexing status" - consistent with the dialog's own title; an icon is
optional and follows the existing `icon-button` pattern if a suitable glyph is
chosen during implementation.)

**Visibility = "a server-side index run is configured".** Defined as
`GET /api/autoindex/status` -> `enabled === true` **and** (`scheduler.active`
or `keys_registered > 0`). Rationale: `enabled` only means `AUTOINDEX_SECRET`
is set (the feature is available); with no scheduler and no registered keys,
nothing will ever run server-side, so the button would open an empty dialog.
If a deployment uses the external cron instead of the built-in scheduler,
`keys_registered > 0` covers it. On any fetch error or when `backendURL` is
unset, the button stays hidden.

**Wiring.** In `dialog.js` `init`/after config load, call a new
`refreshAutoindexButton()` which fetches the status once (same
request as `isServerIndexingRunning()`, factored into one helper
`fetchAutoindexStatus()` to avoid duplicating the fetch) and toggles
`display`. Click handler: `this.plugin.openAutoindexStatusDialog(window)`
(already exists, reuses an open window via focus). The button is shown in all
dialog modes where `#input-buttons` is visible (input and index-only); it is
not added to the result-state footer (`#result-close-button` row), which has
its own layout and no indexing context. Keep it enabled during client-side
indexing (the status dialog already disables its own Run buttons through
`plugin.isClientIndexingActive()`).

Footer layout: the button sits in the existing flex row before Close; the
rate-limit section and version label keep their positions. At narrow widths the
row wraps as it does today with the force-reindex label.

## API summary

| Endpoint | Change |
|---|---|
| `GET /api/autoindex/status` | + `rate_limits` `{available, limits?, as_of?, source?}` (cache only, no probe) |
| `GET /api/config` | + `switchable_presets` `[{name, active, credentials}]` |
| `POST /api/config` | + 400 if target preset lacks credentials; resets rate-limit cache and clears stored embedding-key rate-limit skips on success |
| `GET /api/rate-limits` | unchanged behaviour; refactored onto shared `get_cached_rate_limits` |

All additions are backward compatible (new optional fields).

## Files

Backend
- `backend/services/rate_limit_info.py` (new): `get_cached_rate_limits`.
- `backend/services/embeddings.py`: `reset_rate_limit_cache()`.
- `backend/services/cron_indexer.py`: persist `last_rate_limit_headers_at`, `last_rate_limit_preset`.
- `backend/services/autoindex_key_store.py`: `clear_rate_limits()`, helper to count stored keys by `embedding_key_name`.
- `backend/api/autoindex.py`: add `rate_limits` to `status`.
- `backend/api/config.py`: `_preset_credentials`, `switchable_presets`, validation + cache reset in `update_config`.
- `backend/api/rate_limits.py`: use shared helper.

Plugin
- `plugin/src/rate-limit-widget.js` (new) + `manifest.json`/xhtml script loading.
- `plugin/src/dialog.js` / `dialog.xhtml`: use the widget; add footer button + `refreshAutoindexButton()`.
- `plugin/src/autoindex-status.js` / `.xhtml`: Status-section widget, admin preset row, handlers.
- Locale strings if the Ask dialog's labels are localised (check `plugin/src/locale`).

Docs
- `docs/presets.md` (runtime switching + credential filter), `docs/cron-indexing.md`
  "Admin Controls", `docs/plugin-settings.md`; status-quo wording only, per the
  project's documentation rule.

## Testing

Backend (`unittest`/pytest):
- `get_cached_rate_limits`: in-process hit; cron-status fallback; preset-mismatch ignored; neither -> unavailable; never probes.
- `status` includes `rate_limits`; absent/unavailable for local-model preset.
- `_preset_credentials`: shared key set via store / via env / missing; personal key via request header / env / stored non-invalid key / only invalid stored key; LLM-side requirement also checked.
- `GET /api/config`: `switchable_presets` excludes credential-less presets, always includes active, `compatible_presets` unchanged.
- `POST /api/config`: 400 on missing credentials (message contains key *names*, no values); success resets cache and clears stored rate-limit skips; still 403 for non-admins.
- Existing `test_api.py` / `test_config.py` / `test_autoindex_api.py` updated for additive fields.

Plugin (`node --test`, `plugin/test/`):
- `describe()` thresholds (74/75/94/95 %), missing/zero limits -> null, text format.
- Status dialog: widget hidden when `rate_limits.available` false; shown with bars otherwise; rate-limit banner uses earliest `rate_limit_until`.
- Preset row: hidden for non-admins and when < 2 options; select reverts and shows `detail` on 400; disabled while running; refreshes after success.
- Ask dialog: footer button hidden when `enabled=false`, when no scheduler and 0 keys, on fetch error; shown when scheduler active or keys > 0; click calls `openAutoindexStatusDialog`.
- Regression: Ask dialog rate-limit bars behave identically after the refactor.

Manual (per CLAUDE.md "Live Query Debugging"): use the `test-rag-plugin` group
library; with a KISSKI key, verify bars in both dialogs match; switch presets as
admin and confirm the bars/ids refresh and a subsequent "Run full index now"
uses the new preset (backend log shows the preset name).

## Risks / open questions

1. **Definition of "valid API key"** is presence + not-known-invalid (no live
   call), to avoid spending rate-limited quota. If the owner wants real
   validation, it should be an explicit, admin-triggered "Check keys" action,
   not part of listing.
2. **Switching mid-run** is disabled in the UI; the server does not enforce
   this (an admin may still `POST /api/config` directly, as Preferences allows).
3. **Preset-agnostic stored embedding keys** (see Concern above): switching
   between personal-key providers can skip users without a matching key; the
   dialog warns with counts but does not block.
4. **Button visibility heuristic** (`scheduler.active || keys_registered > 0`)
   is a judgement call; alternative is `enabled` alone (simpler, but shows the
   button on servers where no run can ever happen).
5. The two xhtml dialogs duplicate the rate-limit row markup (no includes in
   XUL/XHTML dialogs); the shared JS module keeps the logic single-sourced.
