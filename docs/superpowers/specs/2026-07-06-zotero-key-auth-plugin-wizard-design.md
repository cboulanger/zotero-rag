# Design: Plugin setup wizard + Zotero-key auth cutover (Part 3 + Part 5 cutover)

## Problem this fixes

`docs/history/plan-zotero-key-auth.md` (Parts 1–2) already replaced the backend's shared-secret
auth with per-user Zotero-key identity + an access gate — but only on the backend. The plugin
still sends the old shared `X-API-Key` (`extensions.zotero-rag.apiKey`), and the backend still
accepts it as a transitional fallback (`resolve_zotero_identity` in `backend/dependencies.py`).
There is also no plugin UI to obtain, validate, or store a personal Zotero API key, and no
backend endpoint for such a UI to call.

The user owns and controls every deployed server, so this plan completes the migration in one
step: switch the plugin to the new auth model and remove the transitional backend fallback,
rather than running both indefinitely.

## Decisions

1. **Backend cutover now.** Remove `settings.api_key` and the legacy `X-API-Key`/`?api_key=`
   branch in `resolve_zotero_identity` entirely. Loopback no-auth mode (Part 4) is untouched.
2. **New endpoint, not the design doc's original proposal.** Add `GET /api/auth/whoami` that
   reads back the identity the auth middleware already resolved (via `get_zotero_identity`),
   rather than the design doc's `POST /api/auth/validate` with its own validation path. This
   avoids a second, parallel validation code path — the middleware already does 401/403/503
   handling for any authenticated route.
3. **Preferences pane keeps direct key entry** (masked field with inline validation) in addition
   to the wizard, so a quick key rotation doesn't require stepping through the wizard.
4. **Auto-indexing becomes a single on/off checkbox** reusing the already-configured Zotero API
   key — no second key entry for auto-indexing.
5. **Wizard auto-launches** on a 401/403 startup failure against a non-loopback backend (once per
   session), in addition to a manual "Run Setup Wizard" button in Preferences.
6. **No new "join group" deep-link feature.** On a 403 gate rejection, the wizard shows the
   backend's plain error text. Building operator-configurable contact/join-URL info is a
   speculative feature not requested and is left out.

## Backend changes

### Remove the legacy shared-secret path

- `backend/config/settings.py`: remove the `api_key` field.
- `backend/dependencies.py::resolve_zotero_identity`: remove the
  `legacy_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")` branch
  and its `settings.api_key` comparison. A missing `X-Zotero-API-Key` header on a non-loopback
  request is unconditionally a 401.
- `backend/main.py`: update the `api_key_middleware` docstring (no more "legacy shared secret"
  mention) and remove any other reference to `settings.api_key`.
- Remove/update tests that exercised the legacy shared-key path (see Testing section).
- `.env.dist`: remove the `API_KEY` documentation for remote mode (loopback mode needs no key at
  all, so there is nothing to replace it with there).

### `GET /api/auth/whoami`

New router (or added to an existing small router, e.g. `backend/api/autoindex.py` is auto-index
specific — prefer a new `backend/api/auth.py`), registered under `/api` prefix, subject to the
normal auth middleware (i.e. NOT in `_AUTH_EXEMPT_PATHS`):

```python
@router.get("/auth/whoami")
def whoami(identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity)) -> dict:
    if identity is None:
        return {"authorized": True, "loopback": True}
    return {
        "authorized": True,
        "loopback": False,
        "user_id": identity.user_id,
        "username": identity.username,
        "targets": identity.targets,
    }
```

Failure cases (missing key, invalid/revoked key, gate rejection, zotero.org unreachable) never
reach the handler — the middleware already raises 401/403/503 with a `detail` message. The wizard
and Preferences pane read the HTTP status + `detail` directly.

## Plugin changes

### Preferences pane (`preferences.xhtml` / `preferences.js`)

**Backend Server section:**
- Remove the "API Key" row (`zotero-rag-api-key-row`, `zotero-rag-api-key-desc`) entirely.
- Add a "Zotero API Key" row (new id `zotero-rag-zotero-api-key`) bound to a new pref
  `extensions.zotero-rag.zoteroApiKey`, saved on `change` like other fields.
- Add a status line below it, populated by a background call to `GET /api/auth/whoami` (using the
  candidate key) after save: `"✓ Authenticated as <username> — N libraries accessible"` on
  success, the error `detail` text on failure, or `"Not required for a local server."` when the
  configured backend host is `localhost`/`127.0.0.1`.
- Add a button `zotero-rag-run-wizard` ("Run Setup Wizard…") in this section that calls
  `openSetupWizard(win)`.

**Automatic indexing section:**
- Replace the "Read-only API key" input, "Enable auto-indexing" and "Remove" buttons with a
  single checkbox `zotero-rag-autoindex-toggle` ("Enable automatic indexing of my libraries").
- On pane load, call `GET /api/autoindex/keys` to determine current state (a key is registered
  for this user) and set the checkbox accordingly.
- On check: `POST /api/autoindex/keys` with `{api_key: <zoteroApiKey pref value>}`. On uncheck:
  `DELETE /api/autoindex/keys` with the same body. Status text below reports the result (library
  count / error), same as today.
- The checkbox is disabled, with a hint text ("Configure your Zotero API key above first"), when
  `zoteroApiKey` is empty.

**Service API Keys section:** unchanged.

### `zotero-rag.js`

- Rename the `apiKey` property/pref usage to `zoteroApiKey` throughout
  (`this.apiKey` at init, `getAuthHeaders()`, `fetchRequiredApiKeys()`'s header, etc.). Pref key
  becomes `extensions.zotero-rag.zoteroApiKey`.
- `getAuthHeaders()`: send `X-Zotero-API-Key` instead of `X-API-Key`.
- Delete `addApiKeyParam()` (dead code today — 0 callers, no SSE/EventSource consumer exists in
  the plugin) rather than updating it for the new key name.
- `checkBackendVersion()`: currently throws a generic `Error` with the status folded into the
  message string. Change it to preserve the HTTP status code (e.g. attach it as an `.status`
  property on the thrown Error, or return a `{status, detail}` shape) so `main()` can distinguish
  "auth/gate failure" (401/403) from "server unreachable" (network error) or other statuses.
- `main()`: after a 401/403 from `checkBackendVersion()`, if the configured backend host is not
  `localhost`/`127.0.0.1`, and the wizard hasn't already auto-opened this session (a simple
  in-memory flag, not a pref — so it offers again next Zotero restart if still unresolved), call
  `openSetupWizard(win)` on the active main window.

### New setup wizard (`setup-wizard.xhtml`, `setup-wizard.js`)

Modeled on the existing `fix-unavailable.xhtml`/`.js` pattern: an XHTML dialog loaded via
`Services.scriptloader.loadSubScript`, opened with
`win.openDialog('chrome://zotero-rag/content/setup-wizard.xhtml', 'zotero-rag-setup-wizard', 'chrome,centerscreen,resizable=yes,width=520,height=480', { plugin: this })`.
A single dialog with a step indicator (not separate windows), three steps:

**Step 1 — Server**
- Server URL input, pre-filled from `extensions.zotero-rag.backendURL`.
- "Next": saves the URL, calls `GET /api/version` to confirm reachability, and checks client-side
  whether the URL's hostname is `localhost`/`127.0.0.1` to decide whether Step 2 is shown or
  skipped (with a note: "Not required for a local server").

**Step 2 — Zotero identity** (skipped when loopback)
- Explanatory text on why a Zotero key is required.
- "Create key on zotero.org" button → `Zotero.launchURL('https://www.zotero.org/settings/keys/new')`.
- Password input for the key + "Validate" button → calls `GET /api/auth/whoami` with the
  candidate key in `X-Zotero-API-Key` (not yet saved to prefs).
- On success: saves the key to `extensions.zotero-rag.zoteroApiKey`, shows the authenticated
  username + accessible library count, and reveals a checkbox "Also enable automatic indexing of
  these libraries" (checked state submitted via `POST /api/autoindex/keys` on Finish).
- On failure: shows the response's `detail` text inline; "Next" stays disabled until validation
  succeeds (or the user goes back to fix the Server URL).

**Step 3 — Service API Keys**
- Renders the same dynamic list as Preferences' "Service API Keys" section
  (`GET /api/required-keys`), each saved to `extensions.zotero-rag.serviceApiKey.<key_name>` on
  change. Reuse the rendering logic from `preferences.js` (extract into a small shared helper
  rather than duplicating the DOM-building code — see Testing/implementation note below).

**Finish**
- Values are already saved incrementally per step; "Finish" just closes the dialog. If the
  Preferences pane is open in another window, it isn't live-refreshed automatically — closing
  and reopening Preferences shows the new values (no cross-window sync is being added; out of
  scope).

## Migration / cleanup

- The old `extensions.zotero-rag.apiKey` pref is simply no longer read or written. No explicit
  migration/clearing — an orphaned value in `about:config` is harmless.
- `docs/history/plan-zotero-key-auth.md` Part 5 migration steps 3–4 (announce cutover, monitor
  legacy usage, remove legacy fallback) collapse into this plan's backend cutover, since there is
  no announcement/monitoring window — same person controls plugin and server rollout.
- Doc updates (per the design doc's Part 6 table, scoped to what's now built): `CLAUDE.md`
  cron-indexer section already documents the auto-index key flow — add a note that the same key
  now also authenticates normal plugin use. `.env.dist` update as above. No new
  `docs/zotero-auth.md` — the existing design doc plus this spec cover it; a dedicated reference
  doc is not needed for a single-operator deployment.

## Testing

- **Backend:** unit tests for `GET /api/auth/whoami` (success with identity, success with
  loopback, 401/403/503 pass-through from the dependency). Remove/update tests in
  `backend/tests/` that relied on `settings.api_key` / legacy shared-key acceptance. Add a test
  confirming a request with only the old `X-API-Key` header (no `X-Zotero-API-Key`) now gets 401
  on a non-loopback host.
- **Plugin:** `plugin/test/` — tests for `getAuthHeaders()` sending `X-Zotero-API-Key`; a test for
  the auto-indexing checkbox reading initial state from `GET /api/autoindex/keys` and toggling
  correctly; wizard step-transition tests mocking `GET /api/version` and `GET /api/auth/whoami`
  responses (success, 401, 403, loopback-skip).
- **Manual verification (per `verify` skill):** run the plugin against a local dev backend
  (loopback — wizard should skip Step 2) and, if feasible, against a non-loopback test
  configuration to exercise the full 401 → auto-launch → validate → success path.

## Out of scope

- Operator-configurable "join group" URL / contact message on gate rejection.
- Cross-window live refresh of Preferences when the wizard changes settings from a dialog.
- Prefill query params on the zotero.org "create key" URL (not verified whether the site supports
  them; ship a plain link, revisit later if desired).
- Any change to the `/public/*` unauthenticated query UI (unrelated to plugin/user auth).
