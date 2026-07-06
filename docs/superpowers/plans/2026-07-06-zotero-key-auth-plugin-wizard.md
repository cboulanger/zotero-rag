# Zotero Key Auth: Plugin Wizard + Backend Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch the plugin from the legacy shared `X-API-Key` to a personal Zotero API key
(`X-Zotero-API-Key`), add a setup wizard to obtain/validate that key and the service API keys,
simplify auto-indexing to a single on/off toggle, and remove the backend's now-unneeded
transitional shared-secret fallback.

**Architecture:** Two independent slices that share one contract (`X-Zotero-API-Key` +
`GET /api/auth/whoami`): (1) a small backend change — delete the legacy fallback branch in
`resolve_zotero_identity`, add `GET /api/auth/whoami` which just reads back the identity the
existing auth middleware already resolved; (2) plugin changes — rename the stored credential,
rebuild two Preferences sections, and add a new 3-step dialog (`setup-wizard.xhtml`/`.js`) modeled
directly on the existing `fix-unavailable.xhtml`/`.js` dialog pattern.

**Tech Stack:** FastAPI + pydantic-settings + unittest/TestClient (backend); vanilla JS + XUL/HTML
hybrid XHTML dialogs, Zotero.Prefs, `Services.scriptloader.loadSubScript` (plugin). No JS test
harness exists in this repo (`plugin/test/` referenced in CLAUDE.md as an aspirational convention
does not exist yet) — plugin verification in this plan is manual, via the `verify` skill, not a
new automated harness (out of scope; would be a disproportionate side-project).

Design doc: `docs/superpowers/specs/2026-07-06-zotero-key-auth-plugin-wizard-design.md`.
Prior design doc (Parts 1–2, already implemented on this branch):
`docs/history/plan-zotero-key-auth.md`.

---

## Task 1: Backend — remove the legacy shared-secret fallback

**Files:**
- Modify: `backend/config/settings.py:34-42`
- Modify: `backend/dependencies.py:25-60`
- Modify: `backend/main.py:150-166`
- Modify: `.env.dist:71-76`
- Modify: `docker-compose.yml` (the `API_KEY` line, currently around line 69)
- Modify (tests): `backend/tests/test_resolve_zotero_identity.py`
- Modify (tests): `backend/tests/test_main_auth_middleware.py`
- Modify (tests): `backend/tests/test_autoindex_api.py:22`

- [ ] **Step 1: Remove the 3 legacy-fallback tests and add one confirming the fallback is gone**

In `backend/tests/test_resolve_zotero_identity.py`, delete these three test methods entirely:
`test_legacy_shared_key_accepted_as_transitional_fallback`, `test_legacy_key_via_query_param_accepted`,
`test_wrong_legacy_key_rejected` (they reference `s.api_key`, which this plan removes from
`Settings`).

Replace them with a single test confirming the old header alone no longer authenticates:

```python
    def test_legacy_shared_key_no_longer_accepted(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        r = self.client.get("/probe", headers={"X-API-Key": "SHARED"})
        self.assertEqual(r.status_code, 401)
```

Add it right after `test_remote_missing_key_rejected` (which it parallels).

- [ ] **Step 2: Run the test file to verify the new test fails for the right reason**

Run: `uv run pytest backend/tests/test_resolve_zotero_identity.py -v`

Expected: `test_legacy_shared_key_no_longer_accepted` FAILS (currently the `X-API-Key` header still
authenticates via the legacy branch, so today's response is 200, not 401). The removed tests are
gone, no collection errors.

- [ ] **Step 3: Remove the legacy fallback branch in `resolve_zotero_identity`**

In `backend/dependencies.py`, replace lines 44-50:

```python
    zotero_key = request.headers.get("X-Zotero-API-Key")
    if not zotero_key:
        legacy_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
        if settings.api_key and legacy_key == settings.api_key:
            logger.warning("Request authenticated via legacy shared API_KEY (transitional path)")
            return None
        raise HTTPException(status_code=401, detail="Missing X-Zotero-API-Key header.")
```

with:

```python
    zotero_key = request.headers.get("X-Zotero-API-Key")
    if not zotero_key:
        raise HTTPException(status_code=401, detail="Missing X-Zotero-API-Key header.")
```

Also update the function's docstring (lines 26-38) to drop the "transitional legacy-shared-key
path" mention:

```python
async def resolve_zotero_identity(request: Request) -> Optional[ZoteroIdentity]:
    """Resolve and gate the caller's Zotero identity for the current request.

    Returns None for the loopback no-auth path (Part 4) — single trusted local
    user, no per-library enforcement needed (see access_gate.assert_can_access()).
    Raises HTTPException: 401 for a missing/invalid/revoked key, 403 if the
    Part 2 gate rejects an otherwise-valid identity, 503 if zotero.org is
    unreachable with no cached validation to fall back on.

    Called once per request by the auth middleware in backend/main.py, which
    stashes the result on request.state.zotero_identity for every /api/*
    request. Route handlers should depend on get_zotero_identity instead,
    not this function directly, to avoid re-validating the key a second time.
    """
```

- [ ] **Step 4: Remove the `api_key` field from `Settings`**

In `backend/config/settings.py`, delete lines 37-42:

```python
    api_key: Optional[str] = Field(
        default=None,
        description="API key for remote access (X-API-Key header). "
                    "When set, all requests must include this key. "
                    "Leave unset for local-only deployments."
    )
```

(leave `api_host`, `api_port`, and `public_libraries_config` where they are — only the `api_key`
field block is removed).

- [ ] **Step 5: Remove the now-dead `s.api_key` line in the autoindex test**

In `backend/tests/test_autoindex_api.py:22`, delete the line:

```python
        s.api_key = None  # disable auth middleware for the test
```

(it was already a no-op comment on a loopback-default test — `api_host` defaults to `localhost`,
which already skips auth entirely regardless of `api_key`).

- [ ] **Step 6: Update `test_main_auth_middleware.py`**

Delete the `test_remote_with_legacy_shared_key_still_works` method (lines 64-70) from
`backend/tests/test_main_auth_middleware.py`. Update the module docstring's mention of "Response
*content* filtering is covered later" is unrelated and can stay; no other change needed in this
file.

- [ ] **Step 7: Update `api_key_middleware`'s docstring in `backend/main.py`**

Replace the docstring at `backend/main.py:152-166`:

```python
async def api_key_middleware(request: Request, call_next):
    """Resolve and gate the caller's Zotero identity for every /api/* request.

    Loopback deployments (api_host in {localhost, 127.0.0.1}) skip this
    entirely (Part 4) — there is exactly one trusted local user. Everywhere
    else the caller must present a valid, gate-approved Zotero API key
    (X-Zotero-API-Key). The resolved identity (or None for loopback) is
    stashed on request.state.zotero_identity so downstream route dependencies
    (backend.dependencies.get_zotero_identity) don't re-validate.

    OPTIONS requests (CORS preflight) are always allowed so the browser can
    complete the preflight handshake.
    """
```

- [ ] **Step 8: Run the full backend auth test suite to verify everything passes**

Run: `uv run pytest backend/tests/test_resolve_zotero_identity.py backend/tests/test_main_auth_middleware.py backend/tests/test_autoindex_api.py -v`

Expected: all PASS, including the new `test_legacy_shared_key_no_longer_accepted`.

- [ ] **Step 9: Remove `API_KEY` from `.env.dist` and `docker-compose.yml`**

In `.env.dist`, delete lines 71-76 (the "Legacy shared secret..." comment block and the
`# API_KEY=your_shared_secret` line), leaving the blank line before `# Document extraction
backend.` intact.

In `docker-compose.yml`, delete the two lines:

```yaml
      # set a secret api key for a real deployment!
      API_KEY: ${API_KEY:-}
```

(the field no longer exists on `Settings`; `extra="ignore"` in `SettingsConfigDict` means leaving
a stray env var wouldn't break anything, but keeping it would be actively misleading to an
operator reading the compose file).

- [ ] **Step 10: Commit**

```bash
git add backend/config/settings.py backend/dependencies.py backend/main.py \
        backend/tests/test_resolve_zotero_identity.py backend/tests/test_main_auth_middleware.py \
        backend/tests/test_autoindex_api.py .env.dist docker-compose.yml
git commit -m "fix: remove transitional legacy shared-secret auth fallback

Zotero-key auth is now the only non-loopback auth path — no plugin
still depends on X-API-Key, so the transitional fallback from the
zotero-key-auth-backend migration is no longer needed."
```

---

## Task 2: Backend — add `GET /api/auth/whoami`

**Files:**
- Create: `backend/api/auth.py`
- Modify: `backend/main.py:11,190-198` (import + router registration)
- Create: `backend/tests/test_auth_whoami.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_auth_whoami.py`:

```python
"""Tests for GET /api/auth/whoami — used by the plugin's Preferences pane and
setup wizard to validate a Zotero API key and show the caller's identity."""

import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.main import app
from backend.config.settings import get_settings, reset_settings
from backend.services.zotero_identity import reset_identity_cache
from backend.zotero.key_validator import KeyValidation


class AuthWhoamiTest(unittest.TestCase):
    def setUp(self):
        reset_settings()
        reset_identity_cache()
        self.client = TestClient(app)

    def tearDown(self):
        reset_settings()
        reset_identity_cache()

    def test_loopback_reports_loopback_true(self):
        r = self.client.get("/api/auth/whoami")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"authorized": True, "loopback": True})

    def test_remote_valid_gated_key_returns_identity(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        validation = KeyValidation(user_id=1, username="alice", targets=["users/1", "groups/999"], read_only=True)
        with patch("backend.services.zotero_identity.validate_key", new=AsyncMock(return_value=validation)):
            r = self.client.get("/api/auth/whoami", headers={"X-Zotero-API-Key": "KEY"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {
            "authorized": True,
            "loopback": False,
            "user_id": 1,
            "username": "alice",
            "targets": ["users/1", "groups/999"],
        })

    def test_remote_missing_key_is_401(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        r = self.client.get("/api/auth/whoami")
        self.assertEqual(r.status_code, 401)

    def test_remote_ungated_key_is_403(self):
        s = get_settings()
        s.api_host = "rag.example.com"
        s.authorized_group_id = 999
        validation = KeyValidation(user_id=1, username="alice", targets=["users/1"], read_only=True)
        with patch("backend.services.zotero_identity.validate_key", new=AsyncMock(return_value=validation)):
            r = self.client.get("/api/auth/whoami", headers={"X-Zotero-API-Key": "KEY"})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest backend/tests/test_auth_whoami.py -v`

Expected: FAIL with `404 Not Found` on every request (no `/api/auth/whoami` route registered yet).

- [ ] **Step 3: Create `backend/api/auth.py`**

```python
"""Identity-check endpoint used by the plugin's Preferences pane and setup
wizard to validate a Zotero API key before/without saving it, and to show
the caller which libraries their key can access.

Deliberately thin: all the validation, caching, and gate logic already runs
in the auth middleware (backend.dependencies.resolve_zotero_identity) for
every /api/* request. This handler only reads back the result — a 401/403/503
never reaches it, the middleware already returned that response.
"""

from typing import Optional

from fastapi import APIRouter, Depends

from backend.dependencies import get_zotero_identity
from backend.services.zotero_identity import ZoteroIdentity

router = APIRouter()


@router.get("/auth/whoami", summary="Validate the caller's Zotero API key and return their identity")
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

- [ ] **Step 4: Register the router in `backend/main.py`**

Add `auth` to the import at `backend/main.py:16`:

```python
from backend.api import config, libraries, indexing, query, document_upload, registration, rate_limits, public_query, autoindex, auth
```

Add the include next to the other routers, after line 198 (`app.include_router(autoindex.router, ...)`):

```python
app.include_router(auth.router, prefix="/api", tags=["auth"])
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest backend/tests/test_auth_whoami.py -v`

Expected: all 4 tests PASS.

- [ ] **Step 6: Run the full backend suite**

Run: `uv run pytest backend/tests/ -v`

Expected: all PASS (no regressions from Task 1 or 2).

- [ ] **Step 7: Commit**

```bash
git add backend/api/auth.py backend/main.py backend/tests/test_auth_whoami.py
git commit -m "feat: add GET /api/auth/whoami for plugin key validation"
```

---

## Task 3: Plugin — rename `apiKey` to `zoteroApiKey`, switch header, add identity/loopback helpers

**Files:**
- Modify: `plugin/src/zotero-rag.js:106-107,172,206-230,440-464,488-503`

- [ ] **Step 1: Rename the stored credential and its pref**

In the constructor (`plugin/src/zotero-rag.js:106-107`), replace:

```js
		/** @type {string} */
		this.apiKey = '';
```

with:

```js
		/** @type {string} */
		this.zoteroApiKey = '';

		/** @type {boolean} */
		this._wizardAutoLaunchedThisSession = false;
```

In `init()` (`plugin/src/zotero-rag.js:172`), replace:

```js
		// Load optional API key (required when backend is on a remote host)
		this.apiKey = Zotero.Prefs.get('extensions.zotero-rag.apiKey', true) || '';
```

with:

```js
		// Load the personal Zotero API key (required when backend is on a remote host)
		this.zoteroApiKey = Zotero.Prefs.get('extensions.zotero-rag.zoteroApiKey', true) || '';
```

- [ ] **Step 2: Switch the auth header and delete the unused query-param helper**

Replace `getAuthHeaders()` and delete `addApiKeyParam()` (`plugin/src/zotero-rag.js:200-230`):

```js
	/**
	 * Return HTTP headers to include in all backend requests.
	 * Adds X-Zotero-API-Key when a personal Zotero API key is configured.
	 * @param {Record<string, string>} [extra] - Additional headers to merge
	 * @returns {Record<string, string>}
	 */
	getAuthHeaders(extra = {}) {
		/** @type {Record<string, string>} */
		const headers = { ...extra };
		if (this.zoteroApiKey) {
			headers['X-Zotero-API-Key'] = this.zoteroApiKey;
		}
		// Include any service API keys the user has configured
		for (const keyInfo of this.requiredApiKeys) {
			const value = Zotero.Prefs.get(`extensions.zotero-rag.serviceApiKey.${keyInfo.key_name}`, true) || '';
			headers[keyInfo.header_name] = value;
		}
		return headers;
	}

	/**
	 * True if the configured backend URL points at a loopback address, where
	 * the backend skips Zotero-key auth entirely (single trusted local user).
	 * @returns {boolean}
	 */
	isLoopbackBackend() {
		try {
			const host = new URL(this.backendURL).hostname;
			return host === 'localhost' || host === '127.0.0.1';
		} catch (_) {
			return false;
		}
	}

	/**
	 * Validate a Zotero API key against the backend and return the caller's
	 * identity. Does not read or write any pref — callers pass the candidate
	 * key explicitly so it can be checked before being saved.
	 * @param {string} candidateKey
	 * @returns {Promise<{authorized: true, loopback: boolean, user_id?: number, username?: string, targets?: string[]}>}
	 * @throws {Error} with a `status` property set to the HTTP status code, and
	 *   a message taken from the response's `detail` field, on 401/403/503.
	 */
	async checkZoteroIdentity(candidateKey) {
		const response = await fetch(`${this.backendURL}/api/auth/whoami`, {
			headers: candidateKey ? { 'X-Zotero-API-Key': candidateKey } : {},
		});
		if (!response.ok) {
			const body = await response.json().catch(() => ({}));
			const err = /** @type {Error & {status?: number}} */ (new Error(body.detail || `HTTP ${response.status}`));
			err.status = response.status;
			throw err;
		}
		return response.json();
	}
```

- [ ] **Step 3: Preserve the HTTP status code on `checkBackendVersion()` failures**

Replace `checkBackendVersion()` (`plugin/src/zotero-rag.js:440-464`):

```js
	/**
	 * Check backend version for compatibility.
	 * @returns {Promise<string>} Backend version string
	 * @throws {Error & {status?: number}} If backend is not reachable or returns error.
	 *   `status` is set to the HTTP status code when the server responded (e.g. 401/403),
	 *   and left undefined for network-level failures (server unreachable).
	 */
	async checkBackendVersion() {
		if (!this.backendURL) {
			throw new Error('Backend URL not configured');
		}

		let response;
		try {
			response = await fetch(`${this.backendURL}/api/version`, {
				headers: this.getAuthHeaders()
			});
		} catch (e) {
			const errorMessage = e instanceof Error ? e.message : String(e);
			throw new Error(`Failed to check backend version: ${errorMessage}`);
		}
		if (!response.ok) {
			const body = await response.json().catch(() => ({}));
			const err = /** @type {Error & {status?: number}} */ (
				new Error(`GET /api/version: HTTP ${response.status}${body.detail ? ` — ${body.detail}` : ''}`)
			);
			err.status = response.status;
			throw err;
		}
		const data = /** @type {BackendVersion} */ (await response.json());
		this.log(`Backend version: ${data.api_version}`);

		// TODO: Add version compatibility checking
		// Refresh the list of required API keys from the server
		await this.fetchRequiredApiKeys();
		return data.api_version;
	}
```

- [ ] **Step 4: Update `fetchRequiredApiKeys()`'s header**

In `plugin/src/zotero-rag.js:488-503`, replace:

```js
			const resp = await fetch(`${this.backendURL}/api/required-keys`, {
				headers: {
					'Content-Type': 'application/json',
					...(this.apiKey ? { 'X-API-Key': this.apiKey } : {}),
				},
			});
```

with:

```js
			const resp = await fetch(`${this.backendURL}/api/required-keys`, {
				headers: this.getAuthHeaders({ 'Content-Type': 'application/json' }),
			});
```

- [ ] **Step 5: Verify no remaining references to the old credential**

Run: `grep -n "\.apiKey\b\|extensions.zotero-rag.apiKey\b\|X-API-Key\|addApiKeyParam" plugin/src/zotero-rag.js`

Expected: no output (all occurrences renamed/removed). Service API keys use
`extensions.zotero-rag.serviceApiKey.*`, a different pref namespace — unaffected.

- [ ] **Step 6: Commit**

```bash
git add plugin/src/zotero-rag.js
git commit -m "feat: switch plugin auth from shared X-API-Key to personal Zotero API key"
```

---

## Task 4: Plugin — extract the Service API Keys renderer into a shared helper

The wizard's Step 3 needs the exact same dynamic Service API Keys UI that `preferences.js`
already builds inline. Extract it once so neither file duplicates the DOM-building logic.

**Files:**
- Modify: `plugin/src/zotero-rag.js` (add new method)
- Modify: `plugin/src/preferences.js:64-133` (use the extracted method)

- [ ] **Step 1: Add `renderServiceApiKeyFields()` to `zotero-rag.js`**

Add this method to the `ZoteroRAGPlugin` class in `plugin/src/zotero-rag.js` (near
`fetchRequiredApiKeys`, e.g. directly after it):

```js
	/**
	 * Render service API key input fields into `container`, one row + description
	 * per required key, each bound to `extensions.zotero-rag.serviceApiKey.<key_name>`.
	 * Shared between the Preferences pane and the setup wizard so both stay in sync.
	 * @param {Document} doc
	 * @param {HTMLElement} container - Element to render rows into (existing dynamic rows are cleared first)
	 * @param {HTMLElement|null} placeholder - Shown/hidden depending on whether requiredKeys is empty
	 * @param {Array<{key_name: string, header_name: string, description: string, docs_url?: string|null, required_for: string[]}>} requiredKeys
	 * @returns {void}
	 */
	renderServiceApiKeyFields(doc, container, placeholder, requiredKeys) {
		if (!container) return;

		container.querySelectorAll('.service-key-row, .service-key-desc').forEach(el => el.remove());

		if (!requiredKeys || requiredKeys.length === 0) {
			if (placeholder) placeholder.style.display = '';
			return;
		}
		if (placeholder) placeholder.style.display = 'none';

		for (const keyInfo of requiredKeys) {
			const prefKey = `extensions.zotero-rag.serviceApiKey.${keyInfo.key_name}`;
			const storedValue = Zotero.Prefs.get(prefKey, true) || '';

			const row = doc.createElementNS('http://www.w3.org/1999/xhtml', 'div');
			row.className = 'setting-row service-key-row';

			const label = doc.createElementNS('http://www.w3.org/1999/xhtml', 'label');
			label.textContent = `${keyInfo.key_name}:`;
			label.setAttribute('for', `zotero-rag-key-${keyInfo.key_name}`);

			const input = /** @type {HTMLInputElement} */ (doc.createElementNS('http://www.w3.org/1999/xhtml', 'input'));
			input.type = 'password';
			input.id = `zotero-rag-key-${keyInfo.key_name}`;
			input.className = 'setting-input';
			input.value = storedValue;
			input.placeholder = 'Enter API key';
			input.addEventListener('change', (e) => {
				Zotero.Prefs.set(prefKey, /** @type {HTMLInputElement} */ (e.target).value, true);
			});

			row.appendChild(label);
			row.appendChild(input);
			container.appendChild(row);

			if (keyInfo.description) {
				const desc = doc.createElementNS('http://www.w3.org/1999/xhtml', 'div');
				desc.className = 'setting-description service-key-desc';
				desc.textContent = `${keyInfo.description} (used for: ${keyInfo.required_for.join(', ')})`;
				if (keyInfo.docs_url && /^https?:\/\//i.test(keyInfo.docs_url)) {
					desc.appendChild(doc.createTextNode(' '));
					const link = doc.createElementNS('http://www.w3.org/1999/xhtml', 'a');
					link.setAttribute('href', keyInfo.docs_url);
					link.setAttribute('target', '_blank');
					link.textContent = 'Get key';
					desc.appendChild(link);
				}
				container.appendChild(desc);
			}
		}
	}
```

- [ ] **Step 2: Use it from `preferences.js`**

In `plugin/src/preferences.js`, delete the local `renderApiKeyFields` closure (lines 64-124) and
replace its two call sites and definition with a call to the shared method. Replace lines 64-133:

```js
	/**
	 * Render service API key input fields from the given list.
	 * @param {Array<{key_name: string, header_name: string, description: string, docs_url?: string|null, required_for: string[]}>} requiredKeys
	 */
	const renderApiKeyFields = (requiredKeys) => {
		const container = doc.getElementById('zotero-rag-service-keys-container');
		const placeholder = doc.getElementById('zotero-rag-service-keys-placeholder');
		if (!container) return;

		// Remove previously rendered dynamic rows
		container.querySelectorAll('.service-key-row, .service-key-desc').forEach(el => el.remove());

		if (!requiredKeys || requiredKeys.length === 0) {
			if (placeholder) placeholder.style.display = '';
			return;
		}
		if (placeholder) placeholder.style.display = 'none';

		for (const keyInfo of requiredKeys) {
			const prefKey = `extensions.zotero-rag.serviceApiKey.${keyInfo.key_name}`;
			const storedValue = Zotero.Prefs.get(prefKey, true) || '';

			const row = doc.createElementNS('http://www.w3.org/1999/xhtml', 'div');
			row.className = 'setting-row service-key-row';

			const label = doc.createElementNS('http://www.w3.org/1999/xhtml', 'label');
			label.textContent = `${keyInfo.key_name}:`;
			label.setAttribute('for', `zotero-rag-key-${keyInfo.key_name}`);

			const input = /** @type {HTMLInputElement} */ (doc.createElementNS('http://www.w3.org/1999/xhtml', 'input'));
			input.type = 'password';
			input.id = `zotero-rag-key-${keyInfo.key_name}`;
			input.className = 'setting-input';
			input.value = storedValue;
			input.placeholder = 'Enter API key';
			input.addEventListener('change', (e) => {
				Zotero.Prefs.set(prefKey, /** @type {HTMLInputElement} */ (e.target).value, true);
			});

			row.appendChild(label);
			row.appendChild(input);
			container.appendChild(row);

			if (keyInfo.description) {
				const desc = doc.createElementNS('http://www.w3.org/1999/xhtml', 'div');
				desc.className = 'setting-description service-key-desc';
				desc.textContent = `${keyInfo.description} (used for: ${keyInfo.required_for.join(', ')})`;
				// Link to the provider portal where the key can be created/managed.
				// Clicks are routed to the system browser by the pane-wide handler above.
				if (keyInfo.docs_url && /^https?:\/\//i.test(keyInfo.docs_url)) {
					desc.appendChild(doc.createTextNode(' '));
					const link = doc.createElementNS('http://www.w3.org/1999/xhtml', 'a');
					link.setAttribute('href', keyInfo.docs_url);
					link.setAttribute('target', '_blank');
					link.textContent = 'Get key';
					desc.appendChild(link);
				}
				container.appendChild(desc);
			}
		}
	};

	// Render from cache immediately so fields appear without needing a server round-trip
	try {
		const cached = Zotero.Prefs.get('extensions.zotero-rag.requiredApiKeys', true) || '[]';
		renderApiKeyFields(JSON.parse(cached));
	} catch (_) {}

	// Refresh from server in background and re-render if the list has changed
	this.fetchRequiredApiKeys().then(() => renderApiKeyFields(this.requiredApiKeys));
```

with:

```js
	const serviceKeysContainer = doc.getElementById('zotero-rag-service-keys-container');
	const serviceKeysPlaceholder = doc.getElementById('zotero-rag-service-keys-placeholder');

	// Render from cache immediately so fields appear without needing a server round-trip
	try {
		const cached = Zotero.Prefs.get('extensions.zotero-rag.requiredApiKeys', true) || '[]';
		this.renderServiceApiKeyFields(doc, serviceKeysContainer, serviceKeysPlaceholder, JSON.parse(cached));
	} catch (_) {}

	// Refresh from server in background and re-render if the list has changed
	this.fetchRequiredApiKeys().then(() =>
		this.renderServiceApiKeyFields(doc, serviceKeysContainer, serviceKeysPlaceholder, this.requiredApiKeys)
	);
```

- [ ] **Step 3: Manually verify no syntax errors**

Run: `node --check plugin/src/preferences.js && node --check plugin/src/zotero-rag.js`

Expected: no output (both parse cleanly as JS — this is a syntax-only check; Zotero-global calls
like `Zotero.Prefs` aren't executed by `node --check`).

- [ ] **Step 4: Commit**

```bash
git add plugin/src/zotero-rag.js plugin/src/preferences.js
git commit -m "refactor: extract renderServiceApiKeyFields for reuse by the setup wizard"
```

---

## Task 5: Plugin — Preferences markup: Zotero API Key field, wizard button, auto-index checkbox

**Files:**
- Modify: `plugin/src/preferences.xhtml:12-78`
- Modify: `plugin/src/preferences.css` (append)

- [ ] **Step 1: Replace the Backend Server and Automatic indexing sections**

In `plugin/src/preferences.xhtml`, replace lines 12-78 (from `<!-- Backend Settings -->` through
the closing `</html:fieldset>` of the Automatic indexing section) with:

```xml
  <!-- Backend Settings -->
  <html:fieldset class="settings-group">
    <html:legend>Backend Server</html:legend>

    <html:div class="setting-row">
      <html:label for="zotero-rag-backend-url">Server URL:</html:label>
      <html:input id="zotero-rag-backend-url"
                  type="text"
                  placeholder="http://localhost:8119"
                  class="setting-input"/>
    </html:div>

    <html:div class="setting-description">
      Connects the plugin to the indexing and search service.
    </html:div>

    <html:div class="setting-row">
      <html:label for="zotero-rag-zotero-api-key">Zotero API Key:</html:label>
      <html:input id="zotero-rag-zotero-api-key"
                  type="password"
                  placeholder="Your personal Zotero API key"
                  class="setting-input"/>
    </html:div>

    <html:div class="setting-description">
      Your personal <html:a href="https://www.zotero.org/settings/keys"
                            target="_blank">Zotero API key</html:a>
      — used to authenticate you to the server and determine which libraries you can access.
      Not required when the server runs on your own machine (localhost).
    </html:div>

    <html:div class="setting-description" id="zotero-rag-zotero-api-key-status"></html:div>

    <html:div class="setting-row">
      <html:button id="zotero-rag-run-wizard">Run Setup Wizard…</html:button>
    </html:div>
  </html:fieldset>

  <!-- Service API Keys (populated dynamically from the backend's /api/required-keys) -->
  <html:fieldset class="settings-group" id="zotero-rag-service-keys-group">
    <html:legend>Service API Keys</html:legend>
    <html:div id="zotero-rag-service-keys-container">
      <html:div class="setting-description" id="zotero-rag-service-keys-placeholder">
        No API keys required for this backend configuration, or not yet connected.
      </html:div>
    </html:div>
  </html:fieldset>

  <!-- Automatic indexing -->
  <html:fieldset class="settings-group">
    <html:legend>Automatic indexing</html:legend>

    <html:div class="setting-description">
      Have the backend automatically index your libraries on a schedule, using the Zotero API
      key configured above.
    </html:div>

    <html:div style="display:flex;align-items:center;margin:6px 0 4px 0;">
      <html:input type="checkbox" id="zotero-rag-autoindex-toggle"/>
      <html:label for="zotero-rag-autoindex-toggle" style="margin-left:6px;cursor:pointer;">Enable automatic indexing of my libraries</html:label>
    </html:div>

    <html:div class="setting-description" id="zotero-rag-autoindex-status"></html:div>
  </html:fieldset>
```

- [ ] **Step 2: Add a status-line color helper to `preferences.css`**

Append to `plugin/src/preferences.css`:

```css
.status-ok {
  color: #006600;
}

.status-error {
  color: #cc0000;
}
```

- [ ] **Step 3: Verify the XHTML is well-formed**

Run: `python3 -c "import xml.dom.minidom as m; m.parse('plugin/src/preferences.xhtml')" 2>&1 | head -5`

Expected: no output (parses without error). Note: this file is an XML fragment (no single root
element required by the surrounding `preferences.xhtml` host in Zotero), so a parse error here
would only be a well-formedness issue (unclosed tags etc.), not a "missing root element" false
positive — if minidom complains specifically about multiple top-level elements, that's expected
and not a bug; only unclosed/mismatched tag errors matter.

- [ ] **Step 4: Commit**

```bash
git add plugin/src/preferences.xhtml plugin/src/preferences.css
git commit -m "feat: replace shared API key field with Zotero API key + wizard button in Preferences"
```

---

## Task 6: Plugin — Preferences logic: wire the Zotero API Key field, wizard button, auto-index checkbox

**Files:**
- Modify: `plugin/src/preferences.js`

- [ ] **Step 1: Replace the API Key init/listener with the Zotero API Key field + validation**

Replace lines 11-41 of `plugin/src/preferences.js` (from `const backendURL = ...` through the end
of the `zotero-rag-api-key` change listener):

```js
	const backendURL = Zotero.Prefs.get('extensions.zotero-rag.backendURL', true) || '';
	const apiKey = Zotero.Prefs.get('extensions.zotero-rag.apiKey', true) || '';
	const maxQueries = Zotero.Prefs.get('extensions.zotero-rag.maxQueries', true) || 5;

	// Show stored value; leave blank so the placeholder shows when nothing is saved
	doc.getElementById('zotero-rag-backend-url').value = backendURL;
	doc.getElementById('zotero-rag-api-key').value = apiKey;
	doc.getElementById('zotero-rag-max-queries').value = maxQueries;

	doc.getElementById('zotero-rag-backend-url').addEventListener('change', (e) => {
		const value = /** @type {HTMLInputElement} */ (e.target).value.trim();
		if (value === '') {
			// Clearing the field resets to the default — remove the stored pref
			Zotero.Prefs.clear('extensions.zotero-rag.backendURL', true);
			this.backendURL = 'http://localhost:8119';
		} else {
			try {
				new URL(value);
				Zotero.Prefs.set('extensions.zotero-rag.backendURL', value, true);
				this.backendURL = value;
			} catch (_) {
				Zotero.debug('Zotero RAG: Invalid URL: ' + value);
			}
		}
	});

	doc.getElementById('zotero-rag-api-key').addEventListener('change', (e) => {
		const key = /** @type {HTMLInputElement} */ (e.target).value;
		Zotero.Prefs.set('extensions.zotero-rag.apiKey', key, true);
		this.apiKey = key;
	});
```

with:

```js
	const backendURL = Zotero.Prefs.get('extensions.zotero-rag.backendURL', true) || '';
	const zoteroApiKey = Zotero.Prefs.get('extensions.zotero-rag.zoteroApiKey', true) || '';
	const maxQueries = Zotero.Prefs.get('extensions.zotero-rag.maxQueries', true) || 5;

	// Show stored value; leave blank so the placeholder shows when nothing is saved
	doc.getElementById('zotero-rag-backend-url').value = backendURL;
	doc.getElementById('zotero-rag-zotero-api-key').value = zoteroApiKey;
	doc.getElementById('zotero-rag-max-queries').value = maxQueries;

	const zoteroApiKeyStatus = doc.getElementById('zotero-rag-zotero-api-key-status');

	/**
	 * Validate the currently-configured Zotero API key against the backend
	 * and show the result in the status line below the field. Also refreshes
	 * the auto-indexing checkbox, since its availability depends on this key.
	 * @returns {Promise<void>}
	 */
	const refreshZoteroIdentityStatus = async () => {
		if (!zoteroApiKeyStatus) return;
		if (this.isLoopbackBackend()) {
			zoteroApiKeyStatus.textContent = 'Not required for a local server.';
			zoteroApiKeyStatus.className = 'setting-description';
			await refreshAutoindexToggle();
			return;
		}
		if (!this.zoteroApiKey) {
			zoteroApiKeyStatus.textContent = '';
			zoteroApiKeyStatus.className = 'setting-description';
			await refreshAutoindexToggle();
			return;
		}
		try {
			const result = await this.checkZoteroIdentity(this.zoteroApiKey);
			const count = Array.isArray(result.targets) ? result.targets.length : 0;
			zoteroApiKeyStatus.textContent = `✓ Authenticated as ${result.username} — ${count} librar${count === 1 ? 'y' : 'ies'} accessible.`;
			zoteroApiKeyStatus.className = 'setting-description status-ok';
		} catch (e) {
			zoteroApiKeyStatus.textContent = `✗ ${e instanceof Error ? e.message : String(e)}`;
			zoteroApiKeyStatus.className = 'setting-description status-error';
		}
		await refreshAutoindexToggle();
	};

	doc.getElementById('zotero-rag-backend-url').addEventListener('change', (e) => {
		const value = /** @type {HTMLInputElement} */ (e.target).value.trim();
		if (value === '') {
			// Clearing the field resets to the default — remove the stored pref
			Zotero.Prefs.clear('extensions.zotero-rag.backendURL', true);
			this.backendURL = 'http://localhost:8119';
		} else {
			try {
				new URL(value);
				Zotero.Prefs.set('extensions.zotero-rag.backendURL', value, true);
				this.backendURL = value;
			} catch (_) {
				Zotero.debug('Zotero RAG: Invalid URL: ' + value);
			}
		}
		refreshZoteroIdentityStatus();
	});

	doc.getElementById('zotero-rag-zotero-api-key').addEventListener('change', (e) => {
		const key = /** @type {HTMLInputElement} */ (e.target).value;
		Zotero.Prefs.set('extensions.zotero-rag.zoteroApiKey', key, true);
		this.zoteroApiKey = key;
		refreshZoteroIdentityStatus();
	});

	doc.getElementById('zotero-rag-run-wizard').addEventListener('click', () => {
		this.openSetupWizard(_window);
	});
```

- [ ] **Step 2: Replace the autoindex key-entry section with the on/off checkbox**

Find the "Automatic indexing section" block in `plugin/src/preferences.js` — it starts with the
comment `// Automatic indexing section: submit/remove a read-only Zotero API key` (originally
lines 256-356, but Task 4's edit shifted this earlier in the file by roughly 60 lines, so search
for the comment text rather than trusting an exact line number) and runs through the closing of
the `autoindexRemoveBtn` click listener, right before the function's final closing `};`. Replace
that entire block with:

```js
	// Automatic indexing section: a single on/off toggle reusing the same
	// Zotero API key already configured above (no separate key entry).
	const autoindexToggle = /** @type {HTMLInputElement|null} */ (doc.getElementById('zotero-rag-autoindex-toggle'));
	const autoindexStatus = doc.getElementById('zotero-rag-autoindex-status');

	/**
	 * @param {string} message
	 * @returns {void}
	 */
	const setAutoindexStatus = (message) => {
		if (autoindexStatus) autoindexStatus.textContent = message;
	};

	/**
	 * Reflect current auto-indexing state in the checkbox: checked if a key
	 * matching the caller's identity is already registered; disabled if no
	 * Zotero API key is configured yet (nothing to submit).
	 * @returns {Promise<void>}
	 */
	const refreshAutoindexToggle = async () => {
		if (!autoindexToggle) return;
		if (!this.zoteroApiKey && !this.isLoopbackBackend()) {
			autoindexToggle.checked = false;
			autoindexToggle.disabled = true;
			setAutoindexStatus('Configure your Zotero API key above first.');
			return;
		}
		try {
			const response = await fetch(`${this.backendURL}/api/autoindex/keys`, {
				headers: this.getAuthHeaders(),
			});
			if (!response.ok) {
				autoindexToggle.disabled = true;
				const err = await response.json().catch(() => ({}));
				setAutoindexStatus(err.detail || 'Auto-indexing is not available on this server.');
				return;
			}
			/** @type {{keys: Array<unknown>}} */
			const data = await response.json();
			autoindexToggle.disabled = false;
			autoindexToggle.checked = Array.isArray(data.keys) && data.keys.length > 0;
			setAutoindexStatus(autoindexToggle.checked ? 'Automatic indexing is enabled.' : '');
		} catch (e) {
			autoindexToggle.disabled = true;
			setAutoindexStatus(`Error: ${e}`);
		}
	};

	if (autoindexToggle) {
		autoindexToggle.addEventListener('change', async () => {
			const enabling = autoindexToggle.checked;
			const requestURL = `${this.backendURL}/api/autoindex/keys`;
			setAutoindexStatus(enabling ? 'Enabling auto-indexing...' : 'Disabling auto-indexing...');
			try {
				const response = await fetch(requestURL, {
					method: enabling ? 'POST' : 'DELETE',
					headers: this.getAuthHeaders({ 'Content-Type': 'application/json' }),
					body: JSON.stringify({ api_key: this.zoteroApiKey }),
				});
				if (!response.ok) {
					const err = await response.json().catch(() => ({}));
					setAutoindexStatus(`Error: ${err.detail || response.status}`);
					autoindexToggle.checked = !enabling;
					return;
				}
				if (enabling) {
					/** @type {{targets: string[]}} */
					const data = await response.json();
					const count = Array.isArray(data.targets) ? data.targets.length : 0;
					setAutoindexStatus(count === 1 ? 'Auto-indexing enabled for 1 library.' : `Auto-indexing enabled for ${count} libraries.`);
				} else {
					setAutoindexStatus('Auto-indexing disabled.');
				}
				this.invalidateAutoIndexedLibraryIds();
				populateLibraryVisibilityList();
			} catch (e) {
				setAutoindexStatus(`Error: ${e}`);
				autoindexToggle.checked = !enabling;
			}
		});
	}
```

Note: `refreshAutoindexToggle` and `populateLibraryVisibilityList` are referenced before their
point of use relative to source order in some cases (`refreshZoteroIdentityStatus` above calls
`refreshAutoindexToggle`) — this is fine because they're `const` closures all declared in the
same `initPrefPane` function scope and none of them run until the DOM event listeners fire or the
explicit calls in Step 3 run, by which point every `const` in the function has been assigned.

- [ ] **Step 3: Trigger the initial status refresh once, after both closures exist**

At the very end of `initPrefPane` (after the `autoindexRemoveBtn`-equivalent block from Step 2,
i.e. the new last lines of the function), add:

```js

	// Initial population, now that both closures above exist
	refreshZoteroIdentityStatus();
```

- [ ] **Step 4: Verify no remaining references to the old pref/id names**

Run: `grep -n "zotero-rag-api-key\b\|extensions.zotero-rag.apiKey\b\|zotero-rag-autoindex-key\|zotero-rag-autoindex-enable\|zotero-rag-autoindex-remove" plugin/src/preferences.js plugin/src/preferences.xhtml`

Expected: no output.

- [ ] **Step 5: Syntax check**

Run: `node --check plugin/src/preferences.js`

Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add plugin/src/preferences.js
git commit -m "feat: wire Zotero API key validation and on/off auto-indexing in Preferences"
```

---

## Task 7: Plugin — setup wizard dialog

**Files:**
- Create: `plugin/src/setup-wizard.xhtml`
- Create: `plugin/src/setup-wizard.js`
- Modify: `plugin/src/zotero-rag.js` (add `openSetupWizard()`, modeled on `openFixUnavailableDialog()` at line 1574)

- [ ] **Step 1: Create `plugin/src/setup-wizard.xhtml`**

```xml
<?xml version="1.0"?>
<?xml-stylesheet href="chrome://global/skin/" type="text/css"?>
<?xml-stylesheet href="chrome://zotero/skin/zotero.css" type="text/css"?>
<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">

<head>
  <title>Zotero RAG Setup Wizard</title>
  <meta charset="utf-8"/>
  <script>
    document.addEventListener("DOMContentLoaded", () => {
      try {
        Services.scriptloader.loadSubScript("chrome://zotero/content/include.js", window);
      } catch (e) {
        console.error("Failed to load include.js:", e);
      }
      try {
        Services.scriptloader.loadSubScript("chrome://zotero-rag/content/setup-wizard.js", window);
      } catch (e) {
        console.error("Failed to load setup-wizard.js:", e);
      }
    });
  </script>
  <style>
    html, body { margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; font-size: 13px; }
    .wizard-container { display: flex; flex-direction: column; padding: 16px; box-sizing: border-box; }
    .wizard-step { display: none; }
    .wizard-step.active { display: block; }
    .wizard-step p { color: #444; line-height: 1.5; }
    .wizard-row { display: flex; align-items: center; margin: 10px 0; gap: 8px; }
    .wizard-row label { min-width: 140px; font-weight: 500; }
    .wizard-input { flex: 1; padding: 6px 10px; font-size: 13px; border: 1px solid #ccc; border-radius: 4px; font-family: inherit; }
    .wizard-status { font-size: 12px; margin: 6px 0; min-height: 15px; }
    .status-ok { color: #006600; }
    .status-error { color: #cc0000; }
    .wizard-buttons { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; border-top: 1px solid #ddd; padding-top: 12px; }
    .wizard-button { padding: 6px 16px; min-height: 30px; border: 1px solid #ccc; border-radius: 4px; background: #f5f5f5; color: #333; cursor: pointer; font-family: inherit; font-size: 13px; }
    .wizard-button:hover { background: #e5e5e5; }
    .wizard-button:disabled { opacity: 0.5; cursor: not-allowed; }
    .wizard-button.primary { background: #0066cc; color: #fff; border-color: #0066cc; }
    .wizard-button.primary:hover { background: #0052a3; }
    .wizard-button.primary:disabled { opacity: 0.5; cursor: not-allowed; background: #0066cc; }
    #wizard-service-keys-container .setting-row { display: flex; align-items: center; margin: 8px 0; gap: 8px; }
    #wizard-service-keys-container .setting-row label { min-width: 140px; font-weight: 500; }
    #wizard-service-keys-container .setting-input { flex: 1; padding: 6px 10px; font-size: 13px; border: 1px solid #ccc; border-radius: 4px; font-family: inherit; }
    #wizard-service-keys-container .setting-description { font-size: 12px; color: #666; margin: 4px 0 8px; }
  </style>
</head>

<body>
  <div class="wizard-container">

    <!-- Step 1: Server -->
    <div class="wizard-step active" id="wizard-step-server">
      <h3>Server</h3>
      <p>Enter the address of your Zotero RAG backend server.</p>
      <div class="wizard-row">
        <label for="wizard-server-url">Server URL:</label>
        <input id="wizard-server-url" type="text" class="wizard-input" placeholder="http://localhost:8119"/>
      </div>
      <div class="wizard-status" id="wizard-server-status"></div>
    </div>

    <!-- Step 2: Zotero identity -->
    <div class="wizard-step" id="wizard-step-identity">
      <h3>Zotero Account</h3>
      <p id="wizard-identity-intro">
        This server requires your personal Zotero API key to authenticate you and determine
        which libraries you can access.
      </p>
      <div class="wizard-row">
        <button type="button" class="wizard-button" id="wizard-create-key-btn">Create key on zotero.org</button>
      </div>
      <div class="wizard-row">
        <label for="wizard-zotero-key">Zotero API Key:</label>
        <input id="wizard-zotero-key" type="password" class="wizard-input" placeholder="Paste your key here"/>
        <button type="button" class="wizard-button" id="wizard-validate-btn">Validate</button>
      </div>
      <div class="wizard-status" id="wizard-identity-status"></div>
      <div class="wizard-row" id="wizard-autoindex-row" style="display:none;">
        <input type="checkbox" id="wizard-autoindex-checkbox"/>
        <label for="wizard-autoindex-checkbox" style="min-width:auto;cursor:pointer;">Also enable automatic indexing of these libraries</label>
      </div>
    </div>

    <!-- Step 3: Service API Keys -->
    <div class="wizard-step" id="wizard-step-keys">
      <h3>Service API Keys</h3>
      <p id="wizard-keys-intro">These API keys are required by the backend's current configuration.</p>
      <div id="wizard-service-keys-container">
        <div class="setting-description" id="wizard-service-keys-placeholder">
          No API keys required for this backend configuration.
        </div>
      </div>
    </div>

    <div class="wizard-buttons">
      <button type="button" class="wizard-button" id="wizard-cancel-btn">Cancel</button>
      <button type="button" class="wizard-button" id="wizard-back-btn" disabled="true">Back</button>
      <button type="button" class="wizard-button primary" id="wizard-next-btn">Next</button>
    </div>

  </div>
</body>
</html>
```

- [ ] **Step 2: Create `plugin/src/setup-wizard.js`**

```js
// @ts-check
// Dialog controller for the Zotero RAG setup wizard (Server -> Zotero identity -> Service API keys).

;(function() {
	/** @type {Record<string, number>} */
	const nsFlags = { warn: 0x1, error: 0x0 };
	const makeLogger = (/** @type {string} */ level) => (/** @type {any[]} */ ...args) => {
		const msg = "[Zotero RAG / setup-wizard] " + args.join(" ");
		if (level === "log" || level === "info") {
			// @ts-ignore
			Services.console.logStringMessage(msg);
		} else {
			// @ts-ignore
			const e = Cc["@mozilla.org/scripterror;1"].createInstance(Ci.nsIScriptError);
			// @ts-ignore
			e.init(msg, "", null, 0, 0, nsFlags[level], "chrome javascript");
			// @ts-ignore
			Services.console.logMessage(e);
		}
	};
	// @ts-ignore
	if (typeof console === "undefined") {
		// @ts-ignore
		globalThis.console = { log: makeLogger("log"), info: makeLogger("info"), warn: makeLogger("warn"), error: makeLogger("error") };
	} else {
		["log", "info", "warn", "error"].forEach(level => { /** @type {any} */ (console)[level] = makeLogger(level); });
	}
})();

var ZoteroSetupWizard = {
	/** @type {any} */
	plugin: null,

	/** @type {string[]} */
	steps: ['wizard-step-server', 'wizard-step-identity', 'wizard-step-keys'],

	/** @type {number} */
	currentStep: 0,

	/** @type {boolean} */
	identityValidated: false,

	/**
	 * Initialise the dialog. Called automatically after DOMContentLoaded loads this script.
	 * @returns {void}
	 */
	init() {
		// @ts-ignore - window.arguments is available in XUL/Firefox extension context
		if (!window.arguments || !window.arguments[0]) {
			console.error("No arguments passed to setup wizard dialog");
			return;
		}
		// @ts-ignore
		const args = window.arguments[0];
		this.plugin = args.plugin;

		const backendUrlInput = /** @type {HTMLInputElement} */ (document.getElementById('wizard-server-url'));
		backendUrlInput.value = this.plugin.backendURL || '';

		document.getElementById('wizard-cancel-btn').addEventListener('click', () => window.close());
		document.getElementById('wizard-back-btn').addEventListener('click', () => this.goToStep(this.currentStep - 1));
		document.getElementById('wizard-next-btn').addEventListener('click', () => this.onNext());
		document.getElementById('wizard-create-key-btn').addEventListener('click', () => {
			Zotero.launchURL('https://www.zotero.org/settings/keys/new');
		});
		document.getElementById('wizard-validate-btn').addEventListener('click', () => this.validateIdentity());

		this.goToStep(0);
	},

	/**
	 * @param {number} index
	 * @returns {void}
	 */
	goToStep(index) {
		if (index < 0 || index >= this.steps.length) return;
		this.currentStep = index;
		for (let i = 0; i < this.steps.length; i++) {
			document.getElementById(this.steps[i]).classList.toggle('active', i === index);
		}
		const backBtn = /** @type {HTMLButtonElement} */ (document.getElementById('wizard-back-btn'));
		const nextBtn = /** @type {HTMLButtonElement} */ (document.getElementById('wizard-next-btn'));
		backBtn.disabled = index === 0;
		nextBtn.textContent = index === this.steps.length - 1 ? 'Finish' : 'Next';
	},

	/**
	 * Handle the Next/Finish button for the current step.
	 * @returns {Promise<void>}
	 */
	async onNext() {
		if (this.currentStep === 0) {
			await this.confirmServer();
			return;
		}
		if (this.currentStep === 1) {
			if (!this.plugin.isLoopbackBackend() && !this.identityValidated) {
				const status = document.getElementById('wizard-identity-status');
				status.textContent = 'Please validate your Zotero API key first.';
				status.className = 'wizard-status status-error';
				return;
			}
			await this.enterKeysStep();
			this.goToStep(2);
			return;
		}
		// Step 3 (keys): Finish
		window.close();
	},

	/**
	 * Save + ping the server URL, then decide whether Step 2 (Zotero identity)
	 * is needed, based on whether the URL points at a loopback address.
	 * @returns {Promise<void>}
	 */
	async confirmServer() {
		const input = /** @type {HTMLInputElement} */ (document.getElementById('wizard-server-url'));
		const status = document.getElementById('wizard-server-status');
		const url = input.value.trim().replace(/\/+$/, '');
		if (!url) {
			status.textContent = 'Please enter a server URL.';
			status.className = 'wizard-status status-error';
			return;
		}
		try {
			new URL(url);
		} catch (_) {
			status.textContent = 'That does not look like a valid URL.';
			status.className = 'wizard-status status-error';
			return;
		}
		status.textContent = 'Checking server...';
		status.className = 'wizard-status';
		Zotero.Prefs.set('extensions.zotero-rag.backendURL', url, true);
		this.plugin.backendURL = url;
		try {
			await this.plugin.checkBackendVersion();
			status.textContent = '';
		} catch (e) {
			// @ts-ignore
			if (e && (e.status === 401 || e.status === 403)) {
				// Reachable, just not authenticated yet — expected, continue to Step 2.
				status.textContent = '';
			} else {
				status.textContent = `Could not reach server: ${e instanceof Error ? e.message : String(e)}`;
				status.className = 'wizard-status status-error';
				return;
			}
		}

		const identityIntro = document.getElementById('wizard-identity-intro');
		const autoindexRow = document.getElementById('wizard-autoindex-row');
		if (this.plugin.isLoopbackBackend()) {
			identityIntro.textContent = 'This server runs on your own machine — no Zotero API key is required.';
			document.getElementById('wizard-create-key-btn').setAttribute('hidden', 'true');
			document.getElementById('wizard-zotero-key').setAttribute('hidden', 'true');
			document.getElementById('wizard-validate-btn').setAttribute('hidden', 'true');
			autoindexRow.style.display = 'none';
			this.identityValidated = true;
			this.goToStep(1);
			await this.enterKeysStep();
			this.goToStep(2);
			return;
		}
		this.goToStep(1);
	},

	/**
	 * Validate the pasted key against the backend without saving it first.
	 * @returns {Promise<void>}
	 */
	async validateIdentity() {
		const input = /** @type {HTMLInputElement} */ (document.getElementById('wizard-zotero-key'));
		const status = document.getElementById('wizard-identity-status');
		const key = input.value.trim();
		if (!key) {
			status.textContent = 'Please paste your Zotero API key.';
			status.className = 'wizard-status status-error';
			return;
		}
		status.textContent = 'Validating...';
		status.className = 'wizard-status';
		try {
			const result = await this.plugin.checkZoteroIdentity(key);
			Zotero.Prefs.set('extensions.zotero-rag.zoteroApiKey', key, true);
			this.plugin.zoteroApiKey = key;
			this.identityValidated = true;
			const count = Array.isArray(result.targets) ? result.targets.length : 0;
			status.textContent = `✓ Authenticated as ${result.username} — ${count} librar${count === 1 ? 'y' : 'ies'} accessible.`;
			status.className = 'wizard-status status-ok';
			document.getElementById('wizard-autoindex-row').style.display = 'flex';
		} catch (e) {
			this.identityValidated = false;
			status.textContent = e instanceof Error ? e.message : String(e);
			status.className = 'wizard-status status-error';
		}
	},

	/**
	 * Populate Step 3 (Service API Keys) and, if requested, submit the
	 * auto-indexing checkbox state before moving on.
	 * @returns {Promise<void>}
	 */
	async enterKeysStep() {
		const autoindexCheckbox = /** @type {HTMLInputElement} */ (document.getElementById('wizard-autoindex-checkbox'));
		if (autoindexCheckbox && autoindexCheckbox.checked && this.plugin.zoteroApiKey) {
			try {
				await fetch(`${this.plugin.backendURL}/api/autoindex/keys`, {
					method: 'POST',
					headers: this.plugin.getAuthHeaders({ 'Content-Type': 'application/json' }),
					body: JSON.stringify({ api_key: this.plugin.zoteroApiKey }),
				});
			} catch (e) {
				console.warn('Failed to enable auto-indexing from wizard: ' + e);
			}
		}

		await this.plugin.fetchRequiredApiKeys();
		const container = document.getElementById('wizard-service-keys-container');
		const placeholder = document.getElementById('wizard-service-keys-placeholder');
		this.plugin.renderServiceApiKeyFields(document, container, placeholder, this.plugin.requiredApiKeys);
	},
};

ZoteroSetupWizard.init();
```

- [ ] **Step 3: Add `openSetupWizard()` to `zotero-rag.js`**

Add this method to the `ZoteroRAGPlugin` class in `plugin/src/zotero-rag.js`, right after
`openFixUnavailableDialog()` (around line 1600, after its closing `}`):

```js
	/**
	 * Open the setup wizard (Server -> Zotero identity -> Service API keys).
	 * Focuses the existing dialog instead of opening a second one if already open.
	 * @param {Window} win - Parent window
	 * @returns {void}
	 */
	openSetupWizard(win) {
		if (this._setupWizardWindow && !this._setupWizardWindow.closed) {
			this._setupWizardWindow.focus();
			return;
		}
		// @ts-ignore - openDialog is available in XUL/Firefox extension context
		this._setupWizardWindow = win.openDialog(
			'chrome://zotero-rag/content/setup-wizard.xhtml',
			'zotero-rag-setup-wizard',
			'chrome,centerscreen,resizable=yes,width=520,height=480',
			{ plugin: this }
		);
	}
```

Also add the corresponding property to the constructor, next to `_fixUnavailableWindow`'s
declaration pattern — add near `this._dialogWindow` in the constructor (`plugin/src/zotero-rag.js`,
inside the constructor block):

```js
		/** @type {Window|null} */
		this._setupWizardWindow = null;
```

- [ ] **Step 4: Syntax check both new files and the modified one**

Run: `node --check plugin/src/setup-wizard.js && node --check plugin/src/zotero-rag.js && python3 -c "import xml.dom.minidom as m; m.parse('plugin/src/setup-wizard.xhtml')"`

Expected: no output from any of the three commands.

- [ ] **Step 5: Commit**

```bash
git add plugin/src/setup-wizard.xhtml plugin/src/setup-wizard.js plugin/src/zotero-rag.js
git commit -m "feat: add setup wizard dialog (server, Zotero identity, service API keys)"
```

---

## Task 8: Plugin — auto-launch the wizard on a 401/403 startup failure

**Files:**
- Modify: `plugin/src/zotero-rag.js:411-433` (`main()`)

- [ ] **Step 1: Update `main()` to auto-launch the wizard once per session**

Replace `main()` (`plugin/src/zotero-rag.js:411-433`):

```js
	/**
	 * Main plugin entry point.
	 * @returns {Promise<void>}
	 */
	async main() {
		this.log(`Plugin initialized with backend URL: ${this.backendURL}`);

		// Check backend version compatibility
		try {
			await this.checkBackendVersion();
		} catch (e) {
			const errorMessage = e instanceof Error ? e.message : String(e);
			this.log(`Backend not available: ${errorMessage}`);
			return;
		}

		// Log backend service configuration and DB statistics
		try {
			await this.logServerInfo();
		} catch (e) {
			this.log(`Could not fetch server info: ${e instanceof Error ? e.message : String(e)}`);
		}
	}
```

with:

```js
	/**
	 * Main plugin entry point.
	 * @returns {Promise<void>}
	 */
	async main() {
		this.log(`Plugin initialized with backend URL: ${this.backendURL}`);

		// Check backend version compatibility
		try {
			await this.checkBackendVersion();
		} catch (e) {
			const errorMessage = e instanceof Error ? e.message : String(e);
			this.log(`Backend not available: ${errorMessage}`);
			// @ts-ignore
			const status = e && e.status;
			if ((status === 401 || status === 403) && !this.isLoopbackBackend() && !this._wizardAutoLaunchedThisSession) {
				this._wizardAutoLaunchedThisSession = true;
				const win = Zotero.getMainWindow();
				if (win) this.openSetupWizard(win);
			}
			return;
		}

		// Log backend service configuration and DB statistics
		try {
			await this.logServerInfo();
		} catch (e) {
			this.log(`Could not fetch server info: ${e instanceof Error ? e.message : String(e)}`);
		}
	}
```

- [ ] **Step 2: Syntax check**

Run: `node --check plugin/src/zotero-rag.js`

Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add plugin/src/zotero-rag.js
git commit -m "feat: auto-launch setup wizard on 401/403 startup failure"
```

---

## Task 9: Documentation updates

**Files:**
- Modify: `CLAUDE.md` (cron indexer section)
- Modify: `docs/history/implementation/master.md` (append summary)

- [ ] **Step 1: Update the cron-indexer section of `CLAUDE.md`**

In `CLAUDE.md`, find the paragraph under "### Debugging the cron indexer" that begins
"**Key flow — auto-index keys (not `--slugs-file` / `ZOTERO_API_KEY`):**" and add one sentence
directly after it, before the `uv run python bin/autoindex_add_key.py` example:

```markdown
As of the zotero-key-auth migration, the same personal Zotero API key a user enters in the
plugin's setup wizard (or Preferences) also authenticates their normal plugin use — auto-indexing
is just an on/off toggle reusing that key, not a separate credential.
```

- [ ] **Step 2: Append a phase summary to `docs/history/implementation/master.md`**

Add this section at the end of `docs/history/implementation/master.md`:

```markdown

## Zotero-Key Authentication Migration (2026-07)

Replaced the single shared `X-API-Key` secret with per-user Zotero.org authentication, closing an
IDOR where any user with the shared key could read/delete any other user's library.

- **Backend (Parts 1–2):** identity-derived authorization — every request's Zotero API key
  resolves to `(user_id, username, targets)` via `api.zotero.org`, cached with a TTL; every
  library-scoped endpoint checks `library_id ∈ targets`. A Part 2 access gate (group membership
  and/or an explicit user-id allowlist) controls who may use the instance at all. See
  `docs/history/plan-zotero-key-auth.md`.
- **Plugin + cutover (Part 3 + Part 5):** the plugin now sends `X-Zotero-API-Key` instead of the
  shared secret; a 3-step setup wizard (Server → Zotero identity → Service API keys) obtains and
  validates the key via the new `GET /api/auth/whoami`; auto-indexing collapsed to a single on/off
  toggle reusing that same key. The backend's transitional shared-secret fallback was removed in
  the same change (no staged migration window — single-operator deployment). See
  `docs/superpowers/specs/2026-07-06-zotero-key-auth-plugin-wizard-design.md`.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/history/implementation/master.md
git commit -m "docs: document the zotero-key-auth migration's plugin/cutover phase"
```

---

## Task 10: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full backend test suite**

Run: `uv run pytest backend/tests/ -v`

Expected: all tests PASS.

- [ ] **Step 2: Grep for any remaining dead references across both backend and plugin**

Run: `grep -rn "settings\.api_key\b\|X-API-Key\|extensions\.zotero-rag\.apiKey\b\|addApiKeyParam" backend plugin/src`

Expected: no output (all removed/renamed). Note this intentionally does not match
`extensions.zotero-rag.serviceApiKey.*` or `request.api_key` (the `KeyRequest.api_key` field in
`backend/api/autoindex.py`, a different concept — the submitted auto-index key's field name).

- [ ] **Step 3: Invoke the `verify` skill for manual end-to-end verification**

Use the project's `verify` skill to drive the plugin against a local dev backend
(`npm start`, loopback — `http://localhost:8119`) and confirm:
- Preferences pane loads with the new "Zotero API Key" field and shows "Not required for a local
  server." in the status line.
- The "Automatic indexing" checkbox is enabled (loopback allows it without a key) and toggling it
  calls the backend without error.
- Clicking "Run Setup Wizard…" opens the dialog, Step 1 pings the local server successfully, and
  because it's loopback, Steps 2 is skipped straight to Step 3 (Service API Keys).
- Manually testing the non-loopback 401/403 → auto-launch path requires a remote/gated deployment
  and is not feasible in local dev — note this as a follow-up manual check to run once a
  non-loopback test server is available, rather than blocking on it here.

- [ ] **Step 4: Report results to the user**

Summarize what was verified automatically (backend suite) versus manually (plugin, loopback path
only) and flag the non-loopback wizard auto-launch path as unverified pending a real remote
deployment.
