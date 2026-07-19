# Cron Indexing

Index Zotero libraries via the web API without requiring the Zotero desktop
app or the browser plugin to be running.

## Overview

`bin/index_libraries.py` is a standalone script that connects directly to
`https://api.zotero.org` to download items and attachments, then indexes them
into the vector store using the same pipeline as the plugin-driven workflow.

Useful for:
- Automated nightly/weekly re-indexing on a headless server
- Initial bulk indexing before deploying the plugin to end users
- Incremental sync jobs triggered by CI or task schedulers

**Note on metadata edits:** when the plugin is running, title/author/tag/year/type
edits made in Zotero reach the backend within seconds via a separate live push
(see [docs/indexing.md](indexing.md#live-client-side-metadata-sync)), independent
of this script. This cron job remains the mechanism for everything that live push
doesn't cover: new/changed attachments, items indexed while the plugin wasn't
running, deleted-item cleanup, and cleared/blanked metadata fields — it's the
system of record and safety net, not the primary path for a metadata-only edit
made while the plugin is active.

## How keys are supplied

`index_libraries.py` does **not** take a single global `ZOTERO_API_KEY` or a list
of slugs. Instead it reads read-only Zotero keys from an **encrypted key store**
and resolves the libraries to index from those keys automatically.

1. **`AUTOINDEX_SECRET`** — a Fernet key that encrypts the store. It is required;
   without it the cron run exits early ("AUTOINDEX_SECRET is not set; nothing to
   index") and the auto-index API returns HTTP 503. Generate one with:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
   Set it in your `.env` (local) or deploy env file (container) as
   `AUTOINDEX_SECRET=...`.

2. **Read-only Zotero keys** — users add their own keys (each created at
   <https://www.zotero.org/settings/keys> with **read-only** access) either:
   - via the plugin's Preferences pane ("Automatic indexing"), or
   - on the host with:
     ```bash
     uv run python bin/autoindex_add_key.py <read-only-key>
     ```
   Each key is validated (must be read-only), its target libraries are resolved,
   and it is stored encrypted.

3. The backend dependencies must be installed (`uv sync`).

The store lives at `<data_path>/system/autoindex_keys.json` (i.e.
`data/system/autoindex_keys.json` for a local install, or `/data/system/...`
inside a container).

### Key re-validation and pruning

Every run re-validates each stored key against `api.zotero.org` and:

- **Confirms** keys that are still read-only (their `last_status` becomes `ok`).
- **Prunes** keys that are permanently invalid — revoked, expired, or downgraded
  to write scope — removing them from the store.
- **Keeps** keys through transient outages (network errors, HTTP 429/500/503),
  reusing their previously stored targets so indexing still attempts them this
  run (their `last_status` becomes `transient_error`).
- **Deduplicates** targets, so a library shared by several keys is indexed once.

Pruned- and kept-key details are surfaced under `key_issues` in
`cron_status.json` (see [Reading Progress](#reading-progress)).

## Running Manually (local installation)

Keys and targets come from the store, so no slugs are passed on the command line:

```bash
# Index everything resolvable from the stored keys (auto mode)
uv run python bin/index_libraries.py

# Force a full sync (ignore incremental state)
uv run python bin/index_libraries.py --mode full

# Limit to 100 items per library (useful for testing)
uv run python bin/index_libraries.py --max-items 100

# Override the log file location
uv run python bin/index_libraries.py --log-file /var/log/zotero_index.log
```

### All Options

| Flag | Default | Description |
|---|---|---|
| `--mode auto\|incremental\|full` | `auto` | Indexing mode (see below) |
| `--max-items N` | unlimited | Cap items per library (for testing) |
| `--log-file PATH` | `data/logs/cron_indexer.log` | Log file path |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `--force` | off | Remove a stale lock file and proceed |

## Indexing Modes

### `auto` (default)

Selects the best mode automatically for each library:

- **First run** (library has never been indexed): runs a full sync.
- **Subsequent runs**: runs an incremental sync.
- **Under-indexed library**: if fewer than 25 % of the library's Zotero items
  are reflected in the index, a full sync is forced to repair the gap.
- **Interrupted previous run**: if the lock file shows the previous process
  died mid-run, a full sync is forced to ensure consistency.

### `incremental`

Fetches only items modified since the last indexed version (using Zotero's
`?since=version` parameter). For each modified item:

- **New item**: indexed.
- **Updated item** (higher Zotero version number): old chunks are deleted and
  the item is re-indexed.
- **Unchanged item**: skipped.

After processing modified items, the script calls the Zotero `/deleted`
endpoint to fetch item keys that have been removed from the library since the
last indexed version.  Chunks for those deleted items are purged from the
vector store, along with their deduplication records.

### `full`

Performs a complete sync of the entire library against the vector store:

1. **Fetches all current items** from Zotero.
2. **Compares against the index**: items present in the vector store but absent
   from the Zotero response are treated as deleted — their chunks and
   deduplication records are removed.
3. **Processes each current item**:
   - Already indexed at the current Zotero version → skipped.
   - Indexed at an older version → old chunks deleted, item re-indexed.
   - Not yet indexed → indexed.

Existing chunks are never deleted upfront.  An interrupted full run leaves
already-indexed items intact and searchable; the next run picks up where it
left off.

When `--max-items` is set the orphan-deletion step is skipped, because only
a subset of items is fetched so missing keys cannot reliably be classified as
deleted.

## Running Inside a Container

When the backend runs as a container (via `container.mjs`), run the script
with `podman exec` against the `zotero-rag` container:

```bash
podman exec zotero-rag python bin/index_libraries.py
```

The container already has the right data path (`DATA_PATH=/data`) and all
backend dependencies. No host Python or `uv` installation is needed.

### Configuring AUTOINDEX_SECRET for the container

Add the Fernet secret to your `.env.deploy-<target>` file so it is injected at
container start (the encrypted key store lives under the mounted data volume at
`/data/system/autoindex_keys.json`):

```env
# .env.deploy-myserver  (or .env.deploy-localhost)
AUTOINDEX_SECRET=your_generated_fernet_key
```

Then re-deploy or restart the container:

```bash
node bin/deploy.mjs .env.deploy-myserver
# or just restart if the env file already has the secret:
node bin/container.mjs restart
```

Users then add their read-only keys via the plugin's "Automatic indexing"
preferences, or you can add one directly inside the container:

```bash
podman exec zotero-rag python bin/autoindex_add_key.py <read-only-key>
```

### Scheduling inside the container host

Add a cron entry on the host that calls `podman exec` (no slugs — targets come
from the stored keys):

```cron
# /etc/cron.d/zotero-rag-indexer  (or crontab -e)
0 * * * * root podman exec zotero-rag python bin/index_libraries.py > /dev/null 2>> /path/to/data/logs/cron_indexer.log
```

Log output goes to `/data/logs/cron_indexer.log` inside the container
(i.e. `<DATA_DIR>/logs/cron_indexer.log` on the host). Timestamps in the log
are **UTC** regardless of the host timezone.

### Memory and performance tuning

Two container environment variables control memory use during indexing:

| Variable | Default | Description |
|---|---|---|
| `INDEX_BATCH_SIZE` | `300` | Items per subprocess batch during full sync. Each batch runs in an isolated process; memory is freed when it exits. Set to `0` to disable subprocess isolation. |
| `EMBEDDING_BATCH_SIZE` | `256` | Texts sent per embedding API call. Lower values reduce peak RSS at the cost of more round-trips. |

Set these in your deploy env file (`.local/.env.deploy.<target>`):

```env
INDEX_BATCH_SIZE=300
EMBEDDING_BATCH_SIZE=64   # recommended for hosts with ≤16 GB RAM
```

---

## Setting Up a Scheduled Job

The simplest way to schedule indexing — for both local installs and
containers — is the backend's own built-in scheduler: set
`AUTOINDEX_INTERVAL_MINUTES` (see `.env.dist`) and restart. No crontab entry,
no `podman exec` cron line, nothing living outside version control:

```env
AUTOINDEX_INTERVAL_MINUTES=60   # index every 60 minutes
```

The scheduler starts 60 seconds after the backend does, then ticks on the
configured interval indefinitely, triggering the same unscoped indexing run
the admin "run full index now" control (see [Admin Controls](#admin-controls))
triggers on demand. `GET /` and `GET /api/autoindex/status` both report the
scheduler's state under `cron_indexing.scheduler` / `scheduler`:
`active` (whether `AUTOINDEX_INTERVAL_MINUTES` is set), `interval_minutes`,
and `paused` (see [Admin Controls](#admin-controls)).

### Alternative: external scheduler

If you need per-slug control (`--fingerprint`, `--force`) outside the
built-in scheduler's unscoped ticks, an external cron job still works exactly
as before — the built-in scheduler and an external one can coexist, since
both ultimately call the same `bin/index_libraries.py` guarded by the same
lock file:

```cron
# Edit with: crontab -e
# Index every night at 2 AM (targets come from the stored keys)
0 2 * * * cd /path/to/zotero-rag && uv run python bin/index_libraries.py >> data/logs/cron_indexer.log 2>&1
```

## Reading Progress

The `/` root endpoint carries only whether the feature is enabled — the single
unauthenticated auto-index detail:

```json
{
  "service": "Zotero RAG API",
  ...
  "cron_indexing": {
    "enabled": true,
    "scheduler": { "active": true, "interval_minutes": 60, "paused": false }
  }
}
```

`enabled` reflects whether `AUTOINDEX_SECRET` is configured (when `false` no keys
can be decrypted). `scheduler` reports the built-in scheduler's state:
`active` (whether `AUTOINDEX_INTERVAL_MINUTES` is set), `interval_minutes`, and
`paused` (set via the admin pause/resume endpoints — see
[Admin Controls](#admin-controls)).

Everything else — the registered-key count and live per-run progress — is served
by the **authenticated** `GET /api/autoindex/status` endpoint (requires
`X-Zotero-API-Key` on non-loopback deployments). It returns `keys_registered` (the number of read-only keys in the
store, `0` when disabled), a `disabled_reason` when `enabled` is `false`, plus the
last run's full status once a run has produced a status file:

```json
{
  "enabled": true,
  "keys_registered": 3,
  "scheduler": { "active": true, "interval_minutes": 60, "paused": false },
  "running": true,
  "started_at": "2026-06-23T02:00:01Z",
  "pid": 12345,
  "slugs": {
    "users/12345": {
      "status": "indexing",
      "items_processed": 150,
      "items_total": 500,
      "chunks_added": 1200,
      "started_at": "2026-06-23T02:00:02Z"
    },
    "groups/678": { "status": "pending" }
  },
  "key_issues": [
    {
      "fingerprint": "ab12cd34",
      "user": "alice",
      "reason": "Key not found (revoked or expired).",
      "pruned": true
    }
  ]
}
```

The run-specific fields (`running`, `slugs`, `key_issues`, …) are absent until a
run has produced a status file. If a run recorded `running: true` but its process
is no longer alive, the endpoint reports `running: false` with `crashed: true`.

Per-library totals for the vector store (items and chunks currently indexed,
across **all** libraries, not just cron targets) are available at
`GET /api/libraries`; `vector_db.libraries_count` on `/` is the scalar library
count.

Slug statuses: `pending` → `indexing` → `done` (or `error`).

`key_issues` lists keys that failed re-validation this run. `pruned: true` means
the key was permanently invalid and removed from the store; `pruned: false` means
it failed transiently and was kept for retry. The list is empty when all keys
validated cleanly.

The status file persists at `data/system/cron_status.json` between runs, so
`GET /api/autoindex/status` shows the result of the last run even when nothing is
currently running.

## Admin Controls

Owners/admins of the server's authorizing Zotero group (`AUTHORIZED_GROUP_ID`)
get five additional endpoints, all requiring `X-Zotero-API-Key` from an
account Zotero itself reports as an owner or admin of that group (checked
live against `GET https://api.zotero.org/groups/<id>`, cached 5 minutes).
Loopback deployments (`API_HOST=localhost`) bypass this check entirely, same
as the rest of the Zotero-key auth gate. All five are also reachable from the
plugin's auto-index status dialog, hidden unless the backend reports
`is_admin: true`.

| Endpoint | Effect |
|---|---|
| `POST /api/autoindex/scheduler/pause` | Pauses the built-in scheduler (persists across restarts) |
| `POST /api/autoindex/scheduler/resume` | Resumes it |
| `POST /api/autoindex/scheduler/run-now` | Triggers an immediate unscoped run of every registered library, without waiting for the next tick |
| `POST /api/autoindex/abort` | Kills the entire running indexing process (last resort — use when it's genuinely stuck) |
| `POST /api/autoindex/scheduler/skip-slug` `{"slug": "users/12345"}` | Cooperatively skips one job in the active run, without killing the process — the running subprocess notices the request at its next progress-callback checkpoint (or immediately, if the slug hasn't started yet) and moves on to the next library |

Admins can also pass `?scope=all` to `GET /api/autoindex/status` to see every
job in the run (not just their own), with each job labeled
`library_name`/`owner_id` joined from `registrations.json` rather than a raw
slug. A slug with no matching registration falls back to the raw slug string.

`abort` vs. `skip-slug`: `abort` kills the whole subprocess and relies on the
existing crash-recovery path (the next run detects the dead PID and forces a
full re-index of whatever was mid-flight). `skip-slug` only ever affects the
one named job — every other library in the run keeps indexing uninterrupted.
Prefer `skip-slug` unless the process itself is unresponsive.

## Troubleshooting

### Log file location

Default: `data/logs/cron_indexer.log` (relative to project root).
Override with `--log-file`.

### Lock file / stuck process

The script uses a PID-based lock at `data/system/cron_indexer.lock`.  If the
previous run crashed without cleaning up, the next run automatically detects
the dead PID and takes over.  Any library that was mid-index when the previous
run died is forced to run a full sync on the next invocation.

To force-clear a lock manually (e.g. if PID wrap-around caused a false
positive):
```bash
uv run python bin/index_libraries.py --force
```

### "AUTOINDEX_SECRET is not set; nothing to index"

The key store cannot be decrypted without the Fernet secret. Set
`AUTOINDEX_SECRET` in your `.env` (local) or deploy env file (container) and
ensure `uv run` is used (it loads `.env` via pydantic-settings). Without it the
cron run exits early and the auto-index API returns HTTP 503. Generate a secret
with:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### "Nothing to index" (no targets resolved)

The store has no usable keys. Add at least one read-only Zotero key via the
plugin's "Automatic indexing" preferences or:
```bash
uv run python bin/autoindex_add_key.py <read-only-key>
```
If keys were recently pruned, check `key_issues` in `cron_status.json` for the
reason (revoked, expired, or downgraded to write scope).
