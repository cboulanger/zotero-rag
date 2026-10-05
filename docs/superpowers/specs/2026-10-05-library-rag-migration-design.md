# Cross-Instance Library RAG Data Migration — Design Spec

## 1. Goal

A tool to copy one Zotero library's indexed RAG data (vectors + metadata) from
one zotero-rag backend instance to another (e.g. production → local dev), so
retrieval/routing changes can be tested against real indexed data without
re-indexing or copying the entire shared vector database.

The destination's existing data for that library is **fully overwritten** —
this is a first-version, operator-run maintenance tool, not an
incremental/merge sync.

Both instances are reached only through their HTTP APIs — never direct Qdrant
access — because Qdrant has no published port on at least the one production
instance checked (it's only reachable inside the container network), and a
uniform API-based approach works the same whether the destination is local
dev or another remote server.

## 2. Scope: which data moves

Zotero library data isn't partitioned per library in Qdrant — all libraries
share three collections, filtered by a `library_id` payload field:

- `document_chunks` (`VectorStore.CHUNKS_COLLECTION`) — the actual RAG
  content: one point per chunk, with a real embedding vector (1024-dim,
  cosine distance in the current config).
- `deduplication` (`VectorStore.DEDUP_COLLECTION`) — content-hash records
  used to skip re-embedding unchanged content. Uses a dummy `[0.0]` vector;
  not a real embedding.
- `library_metadata` (`VectorStore.METADATA_COLLECTION`) — one point per
  library (keyed by a UUID derived from `library_id`), holding
  `LibraryIndexMetadata` (last-indexed version, item counts, etc.). Also
  uses a dummy `[0.0]` vector.

All three move together. Transferring only `document_chunks` would leave the
destination's dedup/metadata bookkeeping for that library absent or stale
until a full reindex — defeating the point of a quick, realistic test
dataset.

**Embedding compatibility:** only `document_chunks` vectors are real
embeddings, so only they are sensitive to a model/dimension mismatch between
source and destination. The script checks this once, up front, and refuses
to proceed on a mismatch (see §5) — dedup/metadata have no such constraint.

## 3. New backend endpoints: `backend/api/migration.py`

Mounted under `/api/migration`. Every endpoint requires
`require_authorized_group_admin` (`backend/dependencies.py`) — the same
dependency already gating the autoindex scheduler's pause/resume/run-now/abort
endpoints. This is evaluated against *that instance's own*
`AUTHORIZED_GROUP_ID`: an admin of the source's group can export; an admin of
the destination's group can import. The two accounts don't need to match, and
neither needs to be a member of the *library's* owning group specifically —
this is an instance-admin action, not a per-library-access action (unlike
`assert_can_access`, which gates normal query/read endpoints).

### `GET /api/migration/embedding-info`

No parameters. Returns `VectorStore.get_collection_info()`'s
`embedding_model_name` and `embedding_dim` fields:

```json
{"embedding_model_name": "...", "embedding_dim": 1024}
```

### `GET /api/migration/export/metadata?library_id=<backend_id>`

Returns the library's `LibraryIndexMetadata` payload (`vector_store.get_library_metadata(library_id)`).
`404` if the library has never been indexed on this instance.

### `GET /api/migration/export?library_id=<backend_id>&collection={chunks|dedup}&offset=<cursor>&limit=<n>`

One page of a `client.scroll(...)` call against `CHUNKS_COLLECTION` or
`DEDUP_COLLECTION`, filtered by `FieldCondition(key="library_id", match=MatchValue(value=library_id))`
— mirrors the scroll-pagination pattern already used throughout
`vector_store.py` (e.g. `get_items_by_metadata`, `get_item_states_bulk`).
`limit` defaults to the script's batch size; cursor is Qdrant's own
`next_offset` (an opaque ID or `null`), passed through unmodified by the
script, never parsed. Response:

```json
{"points": [{"id": "...", "vector": [...], "payload": {...}}, ...], "next_offset": "..." | null}
```

### `POST /api/migration/import/begin`

Body: `{"library_id": "..."}`. Calls the three *existing* deletion methods
already used by `DELETE /libraries/{library_id}/index`
(`backend/api/libraries.py`) — `delete_library_chunks`,
`delete_library_deduplication_records`, `delete_library_metadata` — rather
than reimplementing deletion. Returns the counts deleted. This is the
"overwrite" step and is always called before any import batch.

### `POST /api/migration/import?collection={chunks|dedup}`

Body: `{"library_id": "...", "points": [{"id": "...", "vector": [...], "payload": {...}}, ...]}`.
Upserts the batch into the matching collection (`PointStruct` per point,
`client.upsert`). No transformation of `id`, `vector`, or `payload` — the
destination inherits the source's point IDs and payload contents verbatim.

### `POST /api/migration/import/metadata`

Body: `{"library_id": "...", "payload": {...}}` — the exact payload returned
by `export/metadata`. Calls `vector_store.update_library_metadata(LibraryIndexMetadata(**payload))`
directly — same UUID-keyed point the existing method already writes to.

## 4. New script: `bin/migrate_library.py`

```bash
uv run python bin/migrate_library.py <slug> <source-url> <dest-url> \
  --source-key <source-admin-zotero-api-key> \
  --dest-key <dest-admin-zotero-api-key> \
  [--batch-size 200] [--dry-run]
```

Follows `bin/index_libraries.py`'s style (argparse, `sys.path` bootstrap
before importing `backend.*`). `<slug>` is `users/<id>` or `groups/<id>`,
converted via `slug_to_backend_id` (`backend/api/public_query.py`) — no
second conversion scheme.

**Flow:**

1. Resolve `library_id = slug_to_backend_id(slug)`.
2. `GET /api/migration/embedding-info` on both instances. If
   `embedding_model_name` or `embedding_dim` differ, abort with:
   > `Embedding mismatch: source uses {model_a} ({dim_a}-dim), destination
   > uses {model_b} ({dim_b}-dim). Re-index on the destination instead of
   > migrating vectors between incompatible models.`
3. `GET /api/migration/export/metadata?library_id=...` on source. `404` →
   abort: `Library {slug} has never been indexed on {source-url}.`
4. If `--dry-run`: print the metadata summary (item count, last indexed
   version, etc.) and the embedding-info match, then exit 0 without writing
   anything.
5. `POST /api/migration/import/begin` on destination with `{library_id}`.
   Print counts deleted.
6. For `collection` in `("chunks", "dedup")`: page through
   `GET /api/migration/export?...&offset=...` on source (starting
   `offset=null`), forwarding each page's `points` to
   `POST /api/migration/import?collection=...` on destination. Retry each
   failed batch (export or import call) with exponential backoff, matching
   `VectorStore._upsert_batched`'s retry pattern (4 attempts,
   `2**(attempt-1)`s delay) before giving up. Print running progress
   (`N points transferred` per collection; an initial `count()` call on
   source gives a denominator for a `N/total` display).
7. `POST /api/migration/import/metadata` on destination with the metadata
   payload fetched in step 3.
8. Print a summary: counts transferred per collection, elapsed time.

**Failure recovery:** if a run fails partway (network error, exhausted
retries), the destination is left with only the chunks/dedup already
transferred before the failure — a known, accepted gap for this
operator-run tool (see §6). Recovery is simply re-running the script; step 5
re-clears the destination and the run starts over. There is no
resume-from-cursor logic.

## 5. Auth and credentials

Both `--source-key` and `--dest-key` are read-write-irrelevant Zotero API
keys for accounts that are owner/admin of the respective instance's
`AUTHORIZED_GROUP_ID` — passed as the `X-Zotero-API-Key` header on every
request to that instance, same as any other authenticated endpoint. The
script never stores these keys; they exist only as CLI args / in-memory for
the run. (Unlike the live-query debug tooling, this script talks to a
*remote* source instance's own key store, not the local instance's — there
is no decrypt-from-`autoindex_keys.json` shortcut here; the operator supplies
both admin keys directly.)

No new secret type (env var, shared token) is introduced — this reuses the
existing Zotero-identity + group-admin trust boundary rather than adding a
parallel one.

## 6. Error handling

- **Embedding mismatch** → abort before any destination write (step 2).
- **Library never indexed on source** → abort before any destination write
  (step 3).
- **Transient network/Qdrant errors during a batch** → retry with backoff
  (4 attempts); exhausted retries abort the whole run. Destination may be
  left partially populated for that library — acceptable; re-run to fix.
- **Auth failure (403) on either instance** → abort immediately with the
  server's error detail (e.g. "not an admin of the authorizing group").
- **`import/begin` on a destination with no AUTHORIZED_GROUP_ID configured
  (e.g. a loopback-mode local dev instance)** → `require_authorized_group_admin`
  already has a documented loopback bypass (same as other admin endpoints),
  so this works without extra handling — a local dev instance run in
  loopback mode needs no admin key for `--dest-key` at all (pass any
  placeholder string; it's unused on that code path).

## 7. Testing

- **Python unit tests** (`backend/tests/test_migration_api.py` or similar):
  `GET embedding-info` returns the running config; `export/metadata` 404s
  for an unindexed library and returns the right payload otherwise;
  `export` pagination (chunks and dedup) returns correct filtered pages and
  terminates (`next_offset: null`) at the end; `import/begin` deletes
  exactly the target library's data in all three collections, leaving
  other libraries' data untouched; `import`/`import/metadata` round-trip a
  point through export → import on two separate in-memory `VectorStore`
  instances and confirm the destination matches the source byte-for-byte
  (vector + payload).
- **Script test**: a small in-process test spinning up two local backend
  instances (or two `VectorStore`s against separate embedded Qdrant temp
  dirs) with a few indexed chunks on one and verifying
  `bin/migrate_library.py` reproduces them on the other, including the
  `--dry-run` no-op path and the embedding-mismatch abort path.
- Manual verification against the `test-rag-plugin` group library
  (`groups/6297749`) between the local dev instance and a second local
  instance pointed at a different `DATA_PATH`, per this project's existing
  live-query-debugging conventions.
