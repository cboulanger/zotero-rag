# Indexed-status tags

Attachments that are indexed in the backend carry the Zotero tag `📇 rag-indexed`.
Zotero's item list renders any tag containing an emoji as a small icon before the
item title (this works on attachment rows too), so no custom column is needed and the
tree re-renders through Zotero's normal notifier flow when the tag changes.

The tag is a plain *automatic* tag (type 1) — not a colored tag, which would consume
one of the nine library-wide, synced colored-tag slots. Consequences to be aware of:

- Zotero's "Delete Automatic Tags in This Library" also deletes this tag. A Refresh
  (below) restores it.
- In a group library the tag is item data, so it syncs to, and is visible to, every
  member. It is only written where the local library is editable.
- The feature is opt-in: Preferences → *Indexed-status tags* → "Keep indexed-status
  tags up to date" (pref `extensions.zotero-rag.indexedTags.enabled`, default off).

## Ground truth

An attachment is *indexed* when at least one searchable chunk (`has_content` not
false) with its `attachment_key` exists in the chunks collection.
`VectorStore.get_indexed_attachment_keys(library_id, attachment_keys=None)` answers
that for a batch with one filtered scroll. Catalog-only stubs and legacy chunks
without an `attachment_key` do not count. (The `deduplication` collection is keyed by
content hash → item, not by attachment, so it is not used.)

Both update paths below converge on this one query.

## Real-time path

There is no backend → plugin push channel (the plugin polls
`/api/autoindex/status` for cron progress), so the smallest consistent addition is a
polled event log:

1. `VectorStore` records an event (`indexed`, `unindexed`, `library_unindexed`) in
   `IndexEventLog` (`<data>/system/index_events.jsonl`, `backend/services/index_event_log.py`)
   whenever an attachment gains its first chunks (`add_chunk`, `add_chunks_batch`,
   `copy_chunks_cross_library`) or loses them (`delete_item_chunks`,
   `delete_library_chunks`, snapshot purge). Because the hook lives in the store, it
   covers plugin uploads served by the API process and `bin/index_libraries.py` runs
   alike; an OS file lock serializes appends across those processes. The log keeps
   the last 5000 events; sequence numbers stay monotonic across rotation.
2. The plugin (`plugin/src/indexed-tags.js`) polls `GET /api/indexed-tags/events?since=<seq>`
   every 15 s, filtered to libraries the caller can read. First contact (no cursor)
   follows from the current log head; the cursor is stored in a pref so events from
   while Zotero was closed are applied on the next start. `gap: true` means events
   were rotated away — run Refresh.
3. `indexed` adds the tag at once. `unindexed` is held for two minutes and then
   re-confirmed with `POST /api/indexed-tags/check` before the tag is removed, so a
   re-index (which deletes an attachment's chunks before re-adding them) does not
   flap the tag. Tag writes use `saveTx({ skipDateModifiedUpdate: true })` and are
   skipped when the item already has the desired state.

Events not emitted: `VectorStore.delete_chunks_by_ids` called directly (used by
`bin/reindex_oversized_items.py`). Refresh covers those.

## Refresh path

Preferences → *Indexed-status tags* → **Refresh indexed-status tags**.

1. `POST /api/indexed-tags/refresh` spawns `bin/sync_indexed_tags.py --fingerprint <fp>`
   as a detached subprocess (as `POST /api/autoindex/run` does for the indexer), using
   the key the caller already registered for automatic indexing. The key is looked up
   in the encrypted store by fingerprint; it never appears on a command line. A caller
   with a run already in progress is attached to it instead of starting another.
2. The script resolves libraries with `autoindex_resolver.resolve_targets` — the
   resolver the cron indexer uses — restricted to that fingerprint and without the
   embedding-key gate (nothing is embedded). For each library it pages through the
   Zotero web API's attachments (with their tags), reads indexed state **fresh for each
   page**, and plans only the differences. It writes JSON lines; re-running over
   converged data emits no `ops` records.
3. The plugin tails `GET /api/indexed-tags/refresh/<run_id>?offset=<bytes>` once a
   second and applies each `ops` record as it arrives, showing libraries / attachments
   checked / tagged / untagged in the pane.

**The backend cannot write tags.** Stored keys are read-only (write-scoped keys are
rejected at submission), so the script plans and the plugin — which owns the local
library and syncs it — applies. Because the plan is computed against the web API's
view, a tag the plugin just wrote but Zotero has not yet synced is planned again on
the next Refresh; the plugin's apply step is idempotent, so that is a no-op.

### Not racing the real-time path

- Every `ops` record carries `as_of_seq`: the event-log head read *before* that page's
  indexed state. The plugin remembers the newest real-time event it applied per
  attachment and discards operations older than it.
- Removals (from either path) are re-confirmed against the backend immediately before
  being applied.

### Output format (`bin/sync_indexed_tags.py`)

| `type` | Fields |
| --- | --- |
| `start` | `run_id`, `pid`, `pid_create_time`, `tag`, `started_at` |
| `libraries` | `libraries` (slugs) |
| `applied` | `library`, `written` (keys), `failed` ({key: message}); only with `--write-api-key` and no `--dry-run` |
| `library_start` | `library` |
| `ops` | `library`, `as_of_seq`, `ops: [{op: "add"\|"remove", attachment_key, item_key}]` |
| `progress` | `library`, `attachments_checked`, `to_add`, `to_remove`, `already_correct` |
| `library_done` | same counters as `progress` |
| `library_error` | `library`, `error` (other libraries continue) |
| `done` | totals, `libraries_failed`, `finished_at` |
| `error` | `message` (fatal) |

Why JSON lines in an append-only file rather than `cron_status.json`-style snapshots or
`migrate_library.py`-style checkpoints: the output is an ordered stream of operations
the plugin must consume exactly once (potentially thousands), which a single
overwritten snapshot cannot carry, and a Refresh is cheap to repeat so it needs no
resume cursor. The file (`<data>/system/indexed_tag_sync/<fingerprint>/<run_id>.jsonl`,
newest 5 kept, one day max) survives a backend restart, and a run whose process
disappears without a terminal record is reported as failed (same PID + creation-time
liveness check as `read_live_status`).

### CLI

```bash
uv run python bin/sync_indexed_tags.py --api-key <read-only-key> [--library-ids users/1 groups/2]
```

Prints the JSON lines to stdout (`--output-file` appends to a file instead). With
`--api-key` the key is visible in `ps`; on servers prefer `--fingerprint <fp>` for a
key already in the auto-index store.

By default the script only plans (the client-only path: the plugin applies the tags).
For a manual run, add a separate write-scoped key to apply the plan to zotero.org
directly:

```bash
uv run python bin/sync_indexed_tags.py --api-key <read-only-key> --write-api-key <write-key>
```

The read-only key still enumerates libraries; the write key is used only for tag
writes (batches of 50, each with the item version it was planned against, so an item
edited in the meantime is reported in `applied.failed` rather than overwritten; other
tags on the item are preserved). Add `--dry-run` to plan and report without writing,
even with a write key. Tags written this way reach the plugin through normal Zotero
sync. The server never holds a write key, so the Refresh button always uses the
client-only path.

## Known cost: standalone attachments

Adding a tag changes the tagged item's Zotero version. The incremental indexer
compares versions of *indexed units*: for an attachment under a parent item that is the
parent's version (unaffected), but a standalone attachment is its own unit, so its
first tag (and any later tag change) can trigger one re-index on the next scheduled
run.
