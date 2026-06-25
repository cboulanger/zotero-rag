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

## Prerequisites

1. **Zotero API key** — create one at <https://www.zotero.org/settings/keys>.
   The key must have read access to the libraries you want to index.

2. **`.env` configuration** — add the key to `.env` in the project root:
   ```
   ZOTERO_API_KEY=your_key_here
   ```

3. The backend dependencies must be installed (`uv sync`).

## Running Manually (local installation)

```bash
# Index one user library and one group library
uv run python bin/index_libraries.py users/12345 groups/678

# Force a full sync (ignore incremental state)
uv run python bin/index_libraries.py users/12345 --mode full

# Index from a file listing slugs
uv run python bin/index_libraries.py --slugs-file my_libraries.txt

# Limit to 100 items per library (useful for testing)
uv run python bin/index_libraries.py users/12345 --max-items 100

# Override the log file location
uv run python bin/index_libraries.py users/12345 --log-file /var/log/zotero_index.log
```

### All Options

| Flag | Default | Description |
|---|---|---|
| `slugs` (positional) | — | One or more `users/{id}` or `groups/{id}` slugs |
| `--slugs-file FILE` | — | Text file with slugs, one per line or whitespace-separated |
| `--mode auto\|incremental\|full` | `auto` | Indexing mode (see below) |
| `--max-items N` | unlimited | Cap items per library (for testing) |
| `--log-file PATH` | `data/logs/cron_indexer.log` | Log file path |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `--force` | off | Remove a stale lock file and proceed |

### Slugs File Format

```
# One per line, comments are ignored
users/12345
groups/678  
groups/999
```

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
podman exec zotero-rag uv run python bin/index_libraries.py users/12345 groups/678
```

The container already has the right data path (`DATA_PATH=/data`) and all
backend dependencies. No host Python or `uv` installation is needed.

### Passing ZOTERO_API_KEY to the container

Add the key to your `.env.deploy-<target>` file so it is injected at
container start:

```env
# .env.deploy-myserver  (or .env.deploy-localhost)
ZOTERO_API_KEY=your_key_here
```

Then re-deploy or restart the container:

```bash
node bin/deploy.mjs .env.deploy-myserver
# or just restart if the env file already has the key:
node bin/container.mjs restart
```

Alternatively, pass it once at exec time (no restart required):

```bash
podman exec -e ZOTERO_API_KEY=your_key_here zotero-rag \
  uv run python bin/index_libraries.py users/12345
```

### Scheduling inside the container host

Add a cron entry on the host that calls `podman exec`:

```cron
# /etc/cron.d/zotero-rag-index  (or crontab -e)
0 2 * * * podman exec zotero-rag uv run python bin/index_libraries.py users/12345 groups/678
```

Log output goes to `/data/logs/cron_indexer.log` inside the container
(i.e. `<DATA_DIR>/logs/cron_indexer.log` on the host).

---

## Setting Up a Scheduled Job (local installation)

### Linux / macOS (cron)

```cron
# Edit with: crontab -e
# Index every night at 2 AM
0 2 * * * cd /path/to/zotero-rag && uv run python bin/index_libraries.py users/12345 groups/678 >> data/logs/cron_indexer.log 2>&1
```

## Reading Progress

While indexing is running (or after it finishes), the `/` root endpoint includes
a `cron_indexing` key:

```json
{
  "service": "Zotero RAG API",
  ...
  "cron_indexing": {
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
    }
  }
}
```

Slug statuses: `pending` → `indexing` → `done` (or `error`).

The status file persists at `data/system/cron_status.json` between runs, so
`cron_indexing` shows the result of the last run even when nothing is currently
running.

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
uv run python bin/index_libraries.py users/12345 --force
```

### "ZOTERO_API_KEY is not set"

Check that `.env` in the project root contains `ZOTERO_API_KEY=...` and that
`uv run` is being used (it loads `.env` via pydantic-settings).

### "Invalid slug"

Slugs must be exactly `users/{numericId}` or `groups/{numericId}`.  The
numeric Zotero user ID can be found at <https://www.zotero.org/settings/keys>
(shown as "Your userID for use in API calls").
