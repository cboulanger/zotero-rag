# Per-user embedding API key storage for cron auto-indexing

## Problem

`backend/services/cron_indexer.py` currently indexes every auto-index user's
library through a single, server-wide `EmbeddingService` instance, whose API
key comes from an environment variable (`bin/index_libraries.py:120`,
`backend/dependencies.py:92-108`). This is wrong for remote embedding
providers like KISSKI, where the API key is issued per-user and its token
quota should be billed to that user, not shared across every library the
cron job touches. The plugin already lets a user configure their own
embedding API key (`extensions.zotero-rag.serviceApiKey.<key_name>`,
`plugin/src/zotero-rag.js:562-613`), but that key only ever lives in local
Zotero prefs and is attached as a per-request header for interactive
(non-cron) queries (`backend/dependencies.py:73-89`) — the cron process,
which has no live client connection, cannot see it.

Separately, `backend/services/autoindex_key_store.py` already solves an
equivalent problem for the Zotero API key: it Fernet-encrypts a per-user key
server-side, keyed by a non-secret fingerprint, using the `AUTOINDEX_SECRET`
symmetric key. This design reuses that same store and encryption
infrastructure to also hold each user's embedding API key.

## Goals

- Cron auto-indexing uses each user's own embedding API key, not a shared
  server-wide one.
- No new opt-in UI: enabling "Automatic indexing" also stores whatever
  embedding key the user has already configured locally, with no extra
  consent step.
- One user's invalid or rate-limited embedding key never blocks or aborts
  indexing for any other user.
- No fallback to a server-wide embedding key for auto-indexing: if a user's
  entry has no valid embedding key, that user's libraries are skipped for
  the run and the gap is recorded, not silently paid for by someone else's
  credential.

## Non-goals

- No change to how interactive (non-cron) queries obtain the embedding key
  (still a per-request header from the plugin, unchanged).
- No "admin" view of other users' key status — the existing per-identity
  filtering in `GET /api/autoindex/status` (`backend/api/autoindex.py:125-136`)
  already scopes `slugs`/`key_issues` to the caller's own Zotero identity;
  this design's new `key_issues` entries plug into that same filter and
  need no new access-control work.
- No browser-viewable (query-param) authentication for status endpoints;
  auth remains header-only (`X-Zotero-API-Key`), consistent with the rest
  of this backend.

## 1. Data model (`backend/services/autoindex_key_store.py`)

Each entry in `autoindex_keys.json`, still keyed by the Zotero-key
fingerprint, gains new optional fields alongside the existing Zotero-key
ciphertext:

```json
{
  "<fingerprint>": {
    "ciphertext": "...",
    "user_id": 39226, "username": "...", "targets": [...],
    "validated_at": "...", "last_status": "ok",

    "embedding_key_ciphertext": "gAAAAA...",
    "embedding_key_name": "KISSKI_API_KEY",
    "embedding_key_status": "ok",
    "embedding_key_rate_limit_until": null
  }
}
```

- `embedding_key_name` records which `api_key_env` (e.g. `KISSKI_API_KEY`)
  the stored key satisfies, so a later server-side provider change doesn't
  silently misapply a stale key.
- `embedding_key_status` is one of `"ok"`, `"invalid"`, `"unverified"`
  (stored despite a transient validation failure — see §2),
  `"rate_limited"`.
- `embedding_key_rate_limit_until` is a per-entry replacement for the
  current global `embedding_rate_limit_until` field on the status file
  (`cron_indexer.py:235-259`).

New/changed `AutoIndexKeyStore` methods:

- `set_embedding_key(fp, api_key, key_name) -> None` — encrypts and stores,
  resetting status to `"ok"` (or `"unverified"`, passed in by the caller
  after validation).
- `get_decrypted_embedding_key(fp) -> Optional[tuple[str, str]]` — returns
  `(key_name, plaintext)` or `None`.
- `set_embedding_key_status(fp, status, rate_limit_until=None) -> None`.
- `iter_decrypted()` (used by `autoindex_resolver.resolve_targets`) also
  yields the decrypted embedding key (or `None`) and its status/rate-limit
  fields from the same entry.

Removing an entry (`remove()` / `remove_by_key()`, unchanged) still deletes
both keys together — disabling auto-indexing drops the embedding key too;
no separate deletion path is needed.

## 2. Validation

Validation happens at submission time (matching the existing Zotero-key
read-only check). A throwaway `RemoteEmbeddingService` is constructed with
the candidate key (via `create_embedding_service(preset.embedding,
api_key=candidate)`) and a single `embed_text("test")` call is made:

- `EmbeddingAuthenticationError` → the key is rejected outright (HTTP 400
  from the endpoint); nothing is stored.
- Any other exception (network error, timeout, rate limit, 5xx) → treated
  as transient. The key is still stored, with `embedding_key_status =
  "unverified"` rather than blocking the toggle-on action.

This lives in a small new helper, e.g.
`backend/services/embedding_key_validator.py::validate_embedding_key(api_key)
-> EmbeddingKeyValidation` (mirrors the shape of
`backend/zotero/key_validator.py`'s `KeyValidation`).

## 3. Backend endpoints (`backend/api/autoindex.py`)

**`POST /api/autoindex/keys`** — `KeyRequest` gains
`embedding_api_key: Optional[str] = None`. The Zotero-key
validate-and-store flow is unchanged. If `embedding_api_key` is present,
`validate_embedding_key()` runs and the result is stored via
`set_embedding_key()` / `set_embedding_key_status()` on the *same* entry.
The response gains `embedding_key_status` (and a `reason` string on
rejection) so the caller can surface a warning without the whole request
failing when only the embedding key is bad.

This same endpoint is reused, unchanged, for the "re-sync on edit" flow
(§5) — the plugin just POSTs the same shape again whenever the local
embedding key value changes while auto-indexing is enabled.

**`GET /api/autoindex/keys`** — `list_metadata()` adds `has_embedding_key`
and `embedding_key_status` per entry.

**`DELETE /api/autoindex/keys`** — unchanged; removes the whole entry.

**`GET /api/autoindex/status`** — no code change needed. The existing
per-identity filtering (lines 125-136) already scopes `key_issues` to
`issue.get("user") == identity.username` and `slugs` to
`identity.targets`; the new embedding-key issues (§4) use the same `user`
field and are filtered by the same code path.

## 4. `autoindex_resolver.resolve_targets()`

Return type changes from `dict[str, str]` (slug → Zotero key) to
`dict[str, dict]`:

```python
{"users/123": {"zotero_key": "...", "embedding_key": "...", "embedding_key_name": "KISSKI_API_KEY"}}
```

For each stored entry (via the extended `iter_decrypted()`):

- Zotero-key validation/pruning logic is unchanged.
- If the entry has no embedding key, or `embedding_key_status` is not
  `"ok"`/`"unverified"`, or it's still inside its own
  `embedding_key_rate_limit_until` window: **exclude its slugs from
  `targets`** and append an issue:
  `{"fingerprint": fp, "user": username, "reason": "No valid embedding API key configured; auto-indexing skipped.", "pruned": False, "kind": "embedding_key"}`.
- Otherwise the slug is added to `targets` with both keys attached.

## 5. `CronIndexer` restructuring (`backend/services/cron_indexer.py`)

- Constructor drops the shared `embedding_service` parameter entirely.
  `_index_slug()` now builds a per-slug `RemoteEmbeddingService` from
  `self.targets[slug_info.slug]["embedding_key"]`.
- The global rate-limit short-circuit at the top of `run()` (currently
  reading a single `embedding_rate_limit_until` off the status file,
  lines 234-259) is removed — replaced by the per-entry check in the
  resolver (§4) plus the per-slug handling below.
- In `_index_slug()`, the existing
  `except (EmbeddingRateLimitExhaustedError, EmbeddingAuthenticationError): raise`
  (line 462) changes from *re-raise* to *handle locally*:
  - `EmbeddingAuthenticationError` → `store.set_embedding_key_status(fp, "invalid")`;
    this slug's status becomes `"error"`; the run continues to the next slug.
  - `EmbeddingRateLimitExhaustedError` → `store.set_embedding_key_status(fp, "rate_limited", rate_limit_until=exc.available_at)`;
    this slug's status becomes `"skipped"` with `skip_reason: "embedding_rate_limit"`;
    the run continues.
- The two now-unreachable outer `except` blocks in `run()` for these two
  exception types (lines 321-354) are deleted, along with the
  `status["embedding_rate_limit_until"]` / `status["embedding_auth_error"]`
  status-file fields they wrote (superseded by the per-entry fields
  surfaced through `key_issues`).
- `bin/index_libraries.py` drops its `embedding_service =
  make_embedding_service()` call (line 120) and the corresponding
  constructor argument — `CronIndexer` builds embedding services internally
  now, one per slug.

## 6. Plugin UI (`plugin/src/preferences.js`, `plugin/src/zotero-rag.js`)

- **Toggle-on bundling**: the `autoindexToggle` change handler
  (`preferences.js:294-326`) finds the required-key entry whose
  `required_for` includes `"indexing"`, reads its current value from
  `Zotero.Prefs.get('extensions.zotero-rag.serviceApiKey.<key_name>', true)`,
  and includes it as `embedding_api_key` in the existing POST body. If the
  response's `embedding_key_status !== "ok"`, the status text warns the
  user their embedding key was rejected/unverified rather than claiming
  plain success.
- **Re-sync on edit**: `renderServiceApiKeyFields()`
  (`zotero-rag.js:562-613`, shared with `setup-wizard.js`) gets an optional
  `onKeyChange(keyInfo, value)` callback, invoked from the existing input
  `change` listener after the pref is set. `setup-wizard.js` passes nothing
  (no behavior change there). `preferences.js` passes a callback that, if
  the edited key is the indexing key and `autoindexToggle.checked` is
  true, re-POSTs `{api_key, embedding_api_key}` to keep the server copy in
  sync.
- **Reflecting state on load**: `refreshAutoindexToggle()`
  (`preferences.js:262-292`) already GETs `/api/autoindex/keys`; it now
  also reads `has_embedding_key` / `embedding_key_status` from that
  response to set the correct initial status text (e.g. flagging a
  missing/invalid embedding key) without waiting for a toggle interaction.
- No new `/api/autoindex/status` polling is added — `key_issues` remains a
  diagnostic surface, consistent with today (nothing in the plugin
  currently reads that endpoint's `key_issues`).

## Testing

- `backend/tests/`: extend the existing `autoindex_key_store` /
  `autoindex_resolver` / `autoindex` API test coverage for the new fields,
  submission-time validation (auth-reject vs. transient-accept), and the
  resolver's per-entry skip-with-issue behavior.
- `backend/tests/`: `cron_indexer` tests covering per-slug isolation — one
  slug's `EmbeddingAuthenticationError` or
  `EmbeddingRateLimitExhaustedError` must not affect other slugs in the
  same run.
- Plugin: `plugin/test/` coverage for the bundled POST body on toggle-on,
  the re-sync-on-edit callback wiring, and the initial status text derived
  from `has_embedding_key`/`embedding_key_status`.
