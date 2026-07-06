# Plan: Replace the shared API key with per-user Zotero.org authentication

## Problem this fixes

Today every `/api/*` request is authenticated against a **single global shared secret**
(`settings.api_key`, compared in `backend/main.py:158-168`). On the documented multi-tenant
deployment (`rag.panya.de`), every user configures the plugin with the *same* key, and there is
**no per-caller authorization** on the read/delete paths:

- `POST /api/query` returns verbatim chunk content for any `library_ids` in the body
  (`backend/api/query.py:58`).
- `GET /api/libraries` enumerates **all** libraries + registered usernames/user IDs
  (`backend/api/libraries.py:80`).
- `DELETE /api/libraries/{id}/index` (and sync-deletions, item-chunk delete, batch metadata)
  wipes any library (`backend/api/libraries.py:152`).

Any legitimate user can therefore read or destroy any other user's library. The ownership
primitive exists (`RegistrationService`) but is wired only into the write/index paths, and even
there the `user_id` is self-asserted from the request body (`document_upload.py:980`).

## Core idea

Stop using a shared secret as the credential. Make the credential the user's own **read-only
Zotero API key**. The backend already has everything needed to turn that key into a trustworthy
identity: `backend/zotero/key_validator.py::validate_key()` calls
`GET https://api.zotero.org/keys/<key>` and returns:

```
KeyValidation(user_id, username, targets=["users/<id>", "groups/<id>", ...], read_only, ...)
```

`targets` is exactly the set of libraries that key is allowed to read. So:

- **Authentication** = "the key resolves to a real Zotero identity."
- **Authorization** = "every `library_id` in the request is in that key's `targets`."

This binds every operation to Zotero's own permission model and closes the IDOR: you cannot name
a library your key cannot read. It also removes the need to build and maintain our own access
registry — we lean entirely on zotero.org.

---

## Part 1 — Backend: identity-derived authorization

### 1.1 Auth dependency

Replace the binary middleware check with a FastAPI dependency (`require_zotero_identity`) that:

1. Reads the Zotero key from a dedicated header, `X-Zotero-API-Key` (keep it distinct from the
   LLM/embedding provider keys already sent in `getAuthHeaders()`).
2. Validates it via a **cached** wrapper around `validate_key()` (see 1.3).
3. Rejects with 401 if the key is missing/invalid; 503 if zotero.org is unreachable and there is
   no cached result.
4. Applies the **access gate** (Part 2). Rejects with 403 if the identity is not authorized.
5. Attaches `ZoteroIdentity(user_id, username, targets)` to `request.state` for handlers.

The existing global `api_key` middleware is removed for `/api/*` (kept only for the optional
local/self-host mode, Part 4).

### 1.2 Per-handler enforcement (`target ∈ identity.targets`)

Add a small helper `assert_can_access(identity, library_id)` and apply it at every point a
library is named:

| Endpoint | Enforcement |
| --- | --- |
| `POST /api/query` | every `library_ids[i] ∈ targets`, else 403 |
| `GET /api/libraries` | **filter** the response to `targets` (each user sees only their own) |
| `DELETE /api/libraries/{id}/index` | `id ∈ targets` |
| `POST .../sync-deletions`, item-chunk delete, batch-metadata | `library_id ∈ targets` |
| upload/index endpoints (`document_upload.py`) | `library_id ∈ targets`; derive `user_id` from the **validated key**, never from the request body |

This makes `RegistrationService` redundant for authorization. Keep it, if at all, only as a
display/bookkeeping record populated from the validated identity — not as a security boundary.

### 1.3 Validation cache

Per-request calls to zotero.org add latency and a hard dependency. Add an in-memory TTL cache
keyed by `fingerprint(key)` (reuse `autoindex_key_store.fingerprint`) → `KeyValidation`, TTL
~10 min. On a transient zotero.org failure (`KeyValidation.transient`), serve the cached value if
present; otherwise 503. Group enumeration (`/users/<id>/groups`) is part of the cached result, so
it is paid once per key per TTL window, not per request.

### 1.4 Remove self-asserted identity

Delete `user_id` from `AbstractIndexRequest` and the multipart metadata parsing
(`document_upload.py:239, 980`). Everywhere it was read from the body, read
`request.state.zotero_identity.user_id` instead.

---

## Part 2 — The access gate: which Zotero users may use the server

Any Zotero user has a valid key, so identity alone is not authorization to *use this instance*.
The key payload exposes only `userID` and `username` — no email or org affiliation — so an email
domain rule is impossible. Options, best to worst:

### Decision: Option A, with Option B as a fallback/override

### Option A: membership in a designated "gatekeeper" Zotero group

- The operator creates a **private Zotero group** (e.g. "RAG Users") and sets
  `AUTHORIZED_GROUP_ID=<id>` in the deploy env.
- A user is authorized **iff** their validated key's `targets` include `groups/<AUTHORIZED_GROUP_ID>`
  — i.e. they are a member of that group and their key grants group read.
- **Self-service membership**: the operator invites/approves/removes members in Zotero's own group
  admin UI. No server-side list, no redeploy to grant or revoke access. Revocation takes effect on
  the next cache-TTL expiry.
- The group needs no content; membership is the only signal.
- **Cost**: the operator runs one Zotero group; each user accepts the invite and creates a key with
  "all groups — read" (the validator already enumerates these). Onboarding is one extra click.

### Option B: explicit `user_id` allowlist

- `AUTHORIZED_USER_IDS` env var or a small JSON file the server reads.
- Allowlist by **`user_id`** (stable), not `username` (user-changeable on zotero.org).
- Simple and explicit, but every add/remove is a manual ops action. Good as a fallback or for a
  tiny fixed team, and as an admin override.

### Option C: one-time invite tokens

- Operator issues codes; first successful auth with a code binds `user_id` into a persisted
  allowlist. Re-introduces a (small) server-side registry — the thing we are trying to avoid.
  Not recommended.

### Decided

Primary **Option A**, with **Option B as an optional OR-override** (authorized if in the group
*or* in the explicit `user_id` allowlist). **Fail closed**: if neither `AUTHORIZED_GROUP_ID` nor
`AUTHORIZED_USER_IDS` is configured, the server refuses to start in remote mode, so no one can
accidentally run a wide-open instance.

---

## Part 3 — Plugin: mandatory key + setup wizard

### 3.1 Send the key

- Add a `zoteroApiKey` pref; in `getAuthHeaders()` (`plugin/src/zotero-rag.js:201`) send it as
  `X-Zotero-API-Key`. Drop the shared `X-API-Key` once migration completes (Part 5).

### 3.2 Setup wizard (first run, or when key is missing/invalid/ungated)

1. Explain why a Zotero key is required (identity + access control).
2. **"Create key on zotero.org"** button → open `https://www.zotero.org/settings/keys/new` in the
   external browser. Instruct: *Personal library — read only*; and *All groups — read only*
   (needed for the group gate). (Verify whether the new-key page accepts prefill query params such
   as `name`/`library_access`; if so, deep-link with them to reduce clicks.)
3. User pastes the key back into the wizard.
4. Plugin calls `POST /api/auth/validate` (new lightweight endpoint that runs the same validation +
   gate and returns `{authorized, user_id, username, targets, gate_reason?}` — never echoes the
   key).
5. On success: store the key, show which libraries are accessible. On **gate failure with the group
   option**: show a "Request access — join the RAG Users group" message with a deep link to the
   group's join page.
6. Optional unification: offer *"also enable server-side automatic indexing"*, which simply submits
   the **same** key to the existing `POST /api/autoindex/keys`. One key, both features.

### 3.3 Reference touch points

- Key/header logic: `plugin/src/zotero-rag.js:169-217`.
- Preferences UI: `plugin/src/preferences.xhtml` / `preferences.js`.
- Existing auto-index submission UI already exists — reuse its validation UX for the wizard.

---

## Part 4 — Keeping purely local / single-user setups (explicitly out of scope as a priority)

### Decision

**Loopback-bound setups skip Zotero-key authentication (and the Part 2 gate) completely.**
Everywhere else, Zotero.org authentication is mandatory — there is no opt-in shared-key fallback
for remote deployments (Option 4.2 below is rejected, not just deprioritized). This is Option 4.1,
formalized as the only supported non-remote mode; see rationale and guardrails below.

This change makes zotero.org sync **mandatory for the shared/remote mode**: a purely local Zotero
(local API only, no account, no sync) has no zotero.org key and therefore no identity and no way to
pass the gate. That is acceptable for the primary use case, but here are the ways to keep supporting
local-only users and their costs, so the decision is explicit.

**Why the IDOR does not actually affect a true single-user deploy:** the vulnerability requires
*multiple* users sharing one key on one instance. A single self-hoster using only their own
libraries has no second tenant to attack. So local support is about *not breaking* those users, not
about them needing the new gate.

### Option 4.1 (decided): loopback-bound, no-auth local mode

- The code already special-cases `api_host ∈ {localhost, 127.0.0.1}` (`document_upload.py:254`).
  Extend that: when bound to loopback, **skip Zotero-key auth and the gate entirely**. Single
  trusted local user, port not exposed, IDOR irrelevant.
- **Guardrail**: refuse to start no-auth mode on a non-loopback host, so an operator cannot expose
  local mode on `0.0.0.0`.
- **Cost**: near-zero. Two code paths (remote hardened vs. local trusted), both already implied by
  the existing localhost branch.

### Option 4.2: keep the legacy shared-key mode as an opt-in for self-hosters (rejected)

- A single user self-hosting for themselves sets `API_KEY` and only ever touches their own
  libraries. Safe **as long as the key is never shared across users**.
- **Cost**: we retain the vulnerable-by-design code path and must document loudly "single-tenant
  only." Two auth systems to maintain and test.
- **Rejected**: the decision above removes this as an option for non-loopback deployments entirely,
  not just after a migration window — see Part 5, which now has no long-term legacy fallback.

### Option 4.3: Zotero-key identity without the gate (rejected)

- Use Zotero-key auth (closes the IDOR) but leave `AUTHORIZED_*` unset for a small trusted group.
- **Cost**: still requires Zotero accounts (so not truly "local"), and is open to anyone with the
  URL if exposed — only safe on a private network. Conflicts with the fail-closed default, so it
  would need an explicit `ALLOW_ANY_ZOTERO_USER=true` opt-in.
- **Rejected**: "zotero.org auth is mandatory everywhere except loopback" leaves no room for a
  gate-less-but-remote mode.

### Consequence

Exactly one hardened remote path (Zotero-key + Part 2 gate) and one clearly-scoped local path
(loopback, no auth, one user), consistent with today's localhost special-casing. Truly local,
*multi-user*, no-zotero.org setups are an accepted non-goal.

---

## Part 5 — Migration / rollout

Given the Part 4 decision (no long-term legacy fallback for remote deployments), the transitional
phase exists only to avoid breaking already-deployed plugins during rollout — it is not a
permanent alternative mode.

1. Ship the plugin update with the setup wizard and `X-Zotero-API-Key` first.
2. Backend transitional phase (short window, security fix — days not months): accept **either** a
   valid Zotero key **or** the legacy `API_KEY`, so un-upgraded plugins keep working while users
   migrate. Log usage of the legacy path so the operator can see when it's safe to cut over.
3. Announce the change and the cutover date to all known users (e.g. via the plugin's update
   notes and/or the existing registered-user list).
4. Flip the server to Zotero-key-only for non-loopback hosts (remove the legacy `api_key`
   middleware for `/api/*` entirely; loopback keeps the no-auth local mode from Part 4).

---

## Post-fix threat model

- A user can read/delete only libraries their Zotero key can read → **IDOR closed**.
- Only group members (or allowlisted `user_id`s) can use the instance at all → arbitrary Zotero
  users blocked; **fail-closed** default prevents an accidentally-open instance.
- Live query auth never persists the user's key (validate + in-memory cache only). The auto-index
  store continues to encrypt keys at rest (Fernet) for cron.
- Compromise of one user's key exposes only that user's libraries, not the whole instance (unlike
  the shared key today).

## Decisions log

- **Part 2 (access gate)**: Option A (group membership) is primary, Option B (`user_id` allowlist)
  is an OR-override/fallback. Fail-closed if neither is configured in remote mode. — *Decided.*
- **Part 4 (local setups)**: loopback-bound deployments skip Zotero-key auth and the gate
  completely; every other deployment requires zotero.org auth, with no shared-key opt-out.
  Options 4.2 and 4.3 are rejected as permanent modes. — *Decided.*

## Open decisions still to confirm before implementation

1. **Group-library destructive ops**: any group member can currently pass the read gate. Should
   `DELETE`/sync-deletions on a *group* library be allowed for any member, or restricted (e.g. to
   the first indexer, or require write scope on that group)? Read (query) for any member is fine.
2. **Migration window length** for the short transitional dual-auth phase in Part 5.
3. Whether `https://www.zotero.org/settings/keys/new` accepts prefill query params to streamline
   the wizard (needs a quick check).

---

## Part 6 — Documentation and tests to update

### Documentation

| Doc | Update needed |
| --- | --- |
| `CLAUDE.md` (root) | The "Debugging the cron indexer" section documents `AUTOINDEX_SECRET` and the read-only-key submission flow (`bin/autoindex_add_key.py`) as a separate, optional feature from normal plugin use. Once the Zotero key becomes mandatory for all plugin use, fold this into a single "Zotero identity" story: the same key now gates login *and* (optionally) auto-indexing, so the doc should stop describing key submission as an auto-index-only side path. |
| `docs/cron-indexing.md` | Update the key-source description: keys now arrive via the mandatory login/setup-wizard flow, not only via manual opt-in for auto-indexing. Re-check any statement implying auto-index keys are the *only* key type stored/used by the server. |
| `docs/architecture.md` | Add/update the auth section: replace the "shared `X-API-Key` middleware" description with the per-request Zotero-identity dependency, the validation cache, and the Part 2 access gate. Document the loopback no-auth exception explicitly as an intentional, guarded special case (not a leftover). |
| `.env.dist` / deploy env template docs | Remove or relabel `API_KEY` as loopback/local-only; add `AUTHORIZED_GROUP_ID` and `AUTHORIZED_USER_IDS` (new, required in remote mode) with examples; note the fail-closed startup check. |
| Plugin-facing docs (`plugin/README.md` if present, or preferences help text) | Document the new mandatory setup wizard, the "Create key on zotero.org" step, required key scopes (personal library read + all-groups read), and what "access denied — request group membership" means for the end user. |
| New doc, e.g. `docs/zotero-auth.md` | Consider a dedicated reference doc for the new identity/gate model (validation cache TTL, `targets` semantics, group-gate vs allowlist precedence) so it isn't scattered across cron-indexing and architecture docs. |
| `docs/history/master.md` (per `CLAUDE.md`'s implementation-progress convention) | If this plan is executed as a phased implementation, add per-phase progress docs under `docs/history/` and link a short summary from `master.md`, per the project's documented convention for master implementation plans. |

### Tests

| Area | Update needed |
| --- | --- |
| Auth middleware / dependency | New unit tests for `require_zotero_identity`: valid key → identity attached; invalid/revoked key → 401; zotero.org unreachable with no cache → 503; zotero.org unreachable with a warm cache → served from cache. |
| Access gate (Part 2) | Unit tests: key in authorized group → pass; key not in group and not in allowlist → 403; key not in group but `user_id` in allowlist → pass (OR-override); neither `AUTHORIZED_GROUP_ID` nor `AUTHORIZED_USER_IDS` set in remote mode → server refuses to start (fail-closed), covered as a startup/config test. |
| `backend/api/query.py` | Test that `POST /api/query` 403s when a `library_ids` entry is outside the caller's `targets`, and succeeds when inside. Replace/extend any existing test that relied on the old shared-key-only auth. |
| `backend/api/libraries.py` | Test that `GET /api/libraries` returns only the caller's own libraries (filtered by `targets`), not the full registry. Test that `DELETE .../index`, sync-deletions, item-chunk delete, and batch-metadata all 403 for out-of-target library IDs. |
| `backend/api/document_upload.py` | Remove/replace tests that pass `user_id` in the request body as the trust boundary; add a test confirming `user_id` is now taken from the validated identity and that a body-supplied `user_id` is ignored. Test `_check_registration`'s (or its replacement's) localhost bypass still works together with the new loopback no-auth path — clarify whether `_check_registration` is superseded or kept as bookkeeping only. |
| Loopback no-auth mode (Part 4) | Test that a request to a non-loopback `api_host` with auth disabled is rejected at startup/config validation (the guardrail), and that loopback mode genuinely skips both the Zotero-key check and the Part 2 gate. |
| Validation cache (1.3) | Unit test for TTL expiry/refresh behavior and for serving a stale cache entry on a transient zotero.org failure vs. a hard failure (revoked key) which must not be masked by a stale "valid" cache entry. |
| Plugin (`plugin/test/` or equivalent) | Tests for `getAuthHeaders()` sending `X-Zotero-API-Key` instead of/in addition to `X-API-Key` during the transitional window; setup-wizard flow (mock `POST /api/auth/validate` success/gate-failure responses). |
| Migration/dual-auth window (Part 5 step 2) | Test that the transitional server accepts *either* a valid Zotero key *or* the legacy `API_KEY`, and that legacy-path usage is logged (so the operator can verify before cutover). Remove this test once the legacy path is removed in step 4. |
| Container/startup smoke test | If `docker-compose`/`container.mjs` startup behavior changes (e.g. fail-closed check reading `AUTHORIZED_GROUP_ID`), re-run `uv run pytest -m container -v -s` and update `scripts/test_startup_sequence.py` expectations per `CLAUDE.md`'s existing container-smoke-test guidance. |
