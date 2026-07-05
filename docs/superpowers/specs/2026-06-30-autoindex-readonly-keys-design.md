# Per-library read-only auto-indexing keys — Design

**Date:** 2026-06-30
**Status:** Approved (design)
**Branch:** `feature/autoindex-readonly-keys`

## Problem

The cron indexer ([bin/index_libraries.py](../../../bin/index_libraries.py),
[backend/services/cron_indexer.py](../../../backend/services/cron_indexer.py))
uses a single global `ZOTERO_API_KEY` for every slug. A Zotero API key can only
access **one** user's library, so the cron job can auto-index exactly one user
library. To auto-index several user libraries, each user must be able to
contribute a key scoped to the libraries they want indexed.

To keep this safe, the backend must reject any submitted key that is **not
read-only**, validated against `https://api.zotero.org/keys/<key>`.

## Decisions

| Decision | Choice |
|---|---|
| Key storage at rest | **Encrypted** (Fernet, secret from env `AUTOINDEX_SECRET`) |
| Library scope | **User library + accessible groups**, each indexed once (dedup) |
| Relationship to global key / static slugs.conf | **Replace entirely** (with a CLI migration path) |
| Revocation / downgrade handling | **Re-validate every run + auto-prune**, surface reasons |
| Group shared by several keys | **Index once**, using any currently-valid key that grants read |

## Architecture overview

```
Plugin prefs ──POST key──▶ /api/autoindex/keys ──validate(api.zotero.org/keys)──▶ reject if write-scoped
                                     │ accept (read-only)
                                     ▼
                          AutoIndexKeyStore (Fernet-encrypted, data/system/autoindex_keys.json)
                                     │
   cron run ──load all──▶ re-validate + prune ──resolve+dedup──▶ {slug: key} ──▶ CronIndexer
```

The single global `ZOTERO_API_KEY` + static `cron-indexing-slugs.conf` are
replaced by a set of user-submitted, read-only, validated keys. Each key
resolves to one or more target library slugs (`users/<id>` plus accessible
`groups/<id>`). Every cron run re-validates the keys, deduplicates targets, and
indexes each library once using a key that grants it read access.

## Component 1 — Key validation (`backend/zotero/key_validator.py`)

Validates a key via `GET https://api.zotero.org/keys/<key>`:

- **404 / expired** → invalid (revoked); reject.
- **Any write flag true** — `access.user.write`, or any `access.groups.*.write`
  — → rejected as not read-only, with an actionable message
  ("Create a read-only key at https://www.zotero.org/settings/keys").
- **Valid read-only** → returns `user_id`, `username`, and the resolved target
  slugs:
  - `users/<userID>` when `access.user.library` is true.
  - Groups: for `access.groups.all.library` enumerate the user's groups via
    `GET /users/<userID>/groups` (using the key) and add each as `groups/<id>`;
    for specific `access.groups.<id>.library` entries, add `groups/<id>`
    directly.

Returns a small result object, e.g.
`KeyValidation(user_id, username, targets: list[str], read_only: bool, reason: str | None)`.

### Read-only rule (explicit)

A key is accepted iff: it grants read access to at least one library
(`access.user.library` or some `access.groups.*.library` is true) **and** no
write flag is true anywhere in `access`.

## Component 2 — Encrypted key store (`backend/services/autoindex_key_store.py`)

Dedicated JSON file `data/system/autoindex_keys.json` (created with `0600`
permissions), protected by a `filelock` exactly like
[RegistrationService](../../../backend/services/registration_service.py) so
multi-worker uvicorn cannot corrupt it.

Entry keyed by a **non-secret fingerprint** = `sha256(key)[:12]`:

```json
{
  "<fingerprint>": {
    "ciphertext": "gAAAAA...",
    "user_id": 39226,
    "username": "cboulanger",
    "targets": ["users/39226", "groups/456"],
    "validated_at": "2026-06-30T10:00:00+00:00",
    "last_status": "ok"
  }
}
```

- Key value encrypted with **Fernet**; the symmetric secret comes from env
  `AUTOINDEX_SECRET` (new setting in
  [backend/config/settings.py](../../../backend/config/settings.py)).
- If `AUTOINDEX_SECRET` is unset, the feature is **disabled**: the API endpoints
  return 503 and the cron job logs that no keys can be decrypted. There is **no
  plaintext fallback**.
- Plaintext key values are **never** returned by any endpoint. The admin list
  exposes only fingerprint + user + targets + status.

Store API (sketch):

- `add(api_key, validation) -> fingerprint`
- `get_decrypted(fingerprint) -> str`
- `remove(fingerprint) -> bool` / `remove_by_key(api_key) -> bool`
- `list_metadata() -> list[dict]` (no ciphertext, no plaintext)
- `iter_decrypted() -> Iterator[(fingerprint, api_key, entry)]` (cron only)

## Component 3 — Backend API (`backend/api/autoindex.py`)

Mounted behind the existing API-key auth used by all other routes.

- `POST /api/autoindex/keys` `{ api_key }` → validate; if write-scoped, return
  **400** with the actionable message; on success store encrypted and return
  `{ user_id, username, targets }`.
- `DELETE /api/autoindex/keys` `{ api_key }` (or by fingerprint) → remove the
  user's key. Returns `{ removed: bool }`.
- `GET /api/autoindex/keys` (admin) → list metadata only (fingerprint, user_id,
  username, targets, last_status, validated_at). Never plaintext.

When `AUTOINDEX_SECRET` is unset, all three return **503** with a clear message.

## Component 4 — Plugin UX (`plugin/src/preferences.js`, `preferences.xhtml`)

New "Automatic indexing" section in preferences:

- A read-only API key field + an **Enable** button → `POST /api/autoindex/keys`
  using the existing auth headers (see
  [registerLibrary](../../../plugin/src/zotero-rag.js#L555)).
- On success, show the resolved list of libraries that will be auto-indexed.
- On `400`, show the validation error ("This key has write access; create a
  read-only key at zotero.org/settings/keys").
- A **Remove** button → `DELETE /api/autoindex/keys` to delete the user's key.
- Surface any `key_issues` reported by the backend (e.g. "your key was pruned —
  it now has write access or was revoked; please re-submit").
- Include a link to https://www.zotero.org/settings/keys with read-only setup
  guidance.

The existing HTTP→HTTPS guard in `registerLibrary` should be reused so keys are
never sent over plain HTTP.

## Component 5 — Cron indexer changes

`bin/index_libraries.py`:

1. Drop `ZOTERO_API_KEY` and `--slugs-file` as the primary source.
2. Load all keys from `AutoIndexKeyStore`.
3. **Re-validate each key** against `api.zotero.org/keys` (one cheap GET per
   key — keys are few). Prune keys that are revoked, expired, or now
   write-scoped; record the reason and fingerprint in
   `cron_status.json` under a new `key_issues` list.
4. **Resolve + dedup** surviving keys into `dict[slug → key]`: each group is
   indexed exactly once, using any currently-valid key that grants it read
   access.
5. Pass the mapping to `CronIndexer`.

`CronIndexer` ([backend/services/cron_indexer.py](../../../backend/services/cron_indexer.py)):

- Replace the constructor's `slugs: list[str]` + `api_key: str` with
  `targets: dict[str, str]` (slug → key).
- `_index_slug` builds `ZoteroWebAPI(api_key=self.targets[slug_info.slug])`
  instead of the shared `self.api_key`
  ([cron_indexer.py:401](../../../backend/services/cron_indexer.py#L401)).
- The slug list to iterate becomes `self.targets.keys()`.

## Migration (replace entirely)

1. Provide `bin/autoindex_add_key.py <read-only-key>` — a thin CLI that validates
   and stores a key through the same path as the API, so the existing single
   library can be onboarded without the plugin.
2. Once the current library is re-onboarded, remove `ZOTERO_API_KEY` and
   `cron-indexing-slugs.conf` from the cron/deploy setup.
3. Update the "Debugging the cron indexer" section of
   [CLAUDE.md](../../../CLAUDE.md) to document the new flow (no `--slugs-file`,
   keys live in `autoindex_keys.json`, `AUTOINDEX_SECRET` required).

## Error handling & status

- Write-scoped / revoked keys never index; reasons land in
  `cron_status.json.key_issues` and are surfaced in plugin prefs and the root
  `/` endpoint.
- Missing `AUTOINDEX_SECRET` → feature disabled (503 on API, logged at startup,
  cron logs "no decryptable keys").
- A 403/401 from Zotero mid-run for a given target → prune that key, record the
  reason, and continue indexing other targets.

## Testing

- **Validator**: accept read-only (user-only; `groups.all`; specific group);
  reject write-scoped (`user.write`, `groups.*.write`); handle 404/revoked.
- **Store**: Fernet round-trip; admin `list_metadata` never leaks ciphertext or
  plaintext; concurrent-write safety; disabled when `AUTOINDEX_SECRET` unset.
- **Resolution/dedup**: two keys granting the same group → exactly one target;
  user lib + groups both resolved.
- **Cron**: re-validation prunes downgraded/revoked keys with correct reasons
  recorded in status; `_index_slug` uses the per-slug key.
- **API**: 400 on write-scoped submission; 503 when secret unset; no plaintext
  in any response.

## Out of scope (YAGNI)

- Per-key rate-limit accounting beyond what the existing web API client does.
- A UI for admins to manage other users' keys (admin endpoint is read-only list).
- Rotating `AUTOINDEX_SECRET` / re-encryption tooling (manual: users re-submit).
