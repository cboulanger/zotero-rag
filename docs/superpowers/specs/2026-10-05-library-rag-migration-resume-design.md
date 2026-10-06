# Library RAG Migration — Resume-From-Cursor Design

Addendum to [2026-10-05-library-rag-migration-design.md](2026-10-05-library-rag-migration-design.md).
That spec deliberately accepted "a failed run must restart from scratch" as a
known gap for a first version. A live migration of `users/39226`
(~2M chunks, ~5+ hour transfer) made that gap unacceptable in practice: a
transient network failure partway through would have discarded hours of
transferred data. This spec adds resume support to `bin/migrate_library.py`
only — no backend/API changes are needed, since cursor-based pagination
(`next_offset`) already exists server-side.

## 1. Goal

If `bin/migrate_library.py` is interrupted (crash, network outage exceeding
the existing per-request retry budget, Ctrl-C, killed process), a subsequent
invocation for the same `(slug, source-url, dest-url)` can continue from
where it left off instead of re-clearing the destination and starting over.

## 2. State file

**Location:** `<data_path>/system/migration_state/<key>.json`, where
`data_path` is `backend.config.settings.get_settings().data_path` (the same
base directory already used for `system/autoindex_keys.json` and
`system/registrations.json` — persistent, gitignored, survives reboots,
not subject to OS temp-directory cleanup). This is the operator machine's
own directory (the one running the script), not either backend instance's
data directory — the script only ever talks to source/dest over HTTP.

`<key>` is `hashlib.sha1(f"{slug}|{source_url}|{dest_url}".encode()).hexdigest()`.
Keying by all three values means a different source/dest pair for the same
slug gets its own independent state file — no collision, no cross-validation
needed.

**Contents:**

```json
{
  "slug": "users/39226",
  "source_url": "https://rag.example.com",
  "dest_url": "http://localhost:8119",
  "library_id": "...",
  "embedding_model_name": "...",
  "embedding_dim": 1024,
  "begin_done": true,
  "collections": {
    "chunks": {"cursor": "<opaque offset or null>", "transferred": 155000, "done": false},
    "dedup":  {"cursor": null, "transferred": 0, "done": false}
  },
  "metadata_done": false,
  "started_at": "2026-10-05T14:03:11Z",
  "updated_at": "2026-10-05T14:28:40Z"
}
```

State is written after **every** successful batch (one export + its
matching import), not just at collection or run boundaries — so a crash
loses at most the one batch in flight. Since `/api/migration/import` is a
Qdrant `upsert` keyed by the source's own point IDs, replaying the last
batch on resume just overwrites it identically — safe, no dedup/corruption
logic needed.

On full success (all collections `done` and `metadata_done`), the state
file is deleted. A leftover file on disk always means the last run for that
key did not finish.

## 3. CLI behavior

```bash
uv run python bin/migrate_library.py <slug> <source-url> <dest-url> \
  --source-key ... --dest-key ... [--mode clean|resume]
```

- **No state file for this key** → behaves exactly as today: embedding
  check, metadata fetch, `import/begin`, full transfer, writing state as it
  goes. `--mode` is accepted but has no effect when there's nothing to
  resume or clean.
- **State file exists, `--mode` omitted** → print a summary (started/updated
  timestamps, per-collection transferred counts) and prompt on stdin:
  `Resume, Clean, or Abort? [r/c/a]`. Loop until a valid single-letter
  answer; `a` (or EOF/Ctrl-C) exits 1 without touching anything.
- **`--mode=clean`** → delete any existing state file for this key, then run
  exactly as a fresh migration (calls `import/begin`, clears destination,
  starts both collections from `offset=None`).
- **`--mode=resume`** → requires an existing state file for this key (error
  and exit 1 if none exists — "nothing to resume"); skips `import/begin`
  entirely; for each collection not yet `done`, resumes `export` pagination
  from the stored `cursor`; skips `metadata` import if `metadata_done`.

**Always re-checked on resume, regardless of mode:** the embedding-info
compatibility check and the source metadata fetch (both cheap, read-only,
idempotent). If the recorded `embedding_model_name`/`embedding_dim` in the
state file don't match a fresh `embedding-info` call on resume, abort — the
source or destination config changed between runs, and continuing would
mix incompatible vectors.

## 4. Module structure

New module `backend/utils/migration_state.py` (pure stdlib — `json`,
`hashlib`, `dataclasses`, `pathlib` — no Qdrant/VectorStore dependency, so
it's trivially unit-testable without spinning up a backend):

- `state_path(slug, source_url, dest_url, data_path: Path) -> Path`
- `load_state(path: Path) -> Optional[dict]`
- `save_state(path: Path, state: dict) -> None` (atomic write: write to
  `path.with_suffix(".json.tmp")` then `os.replace` — avoids a half-written
  JSON file if the process is killed mid-write)
- `new_state(slug, source_url, dest_url, library_id, embedding_model_name, embedding_dim) -> dict`
- `delete_state(path: Path) -> None` (no-op if missing)

`bin/migrate_library.py` imports this module and:
- Resolves `data_path` via `backend.config.settings.get_settings()`
  (already an established import pattern for `bin/*.py` scripts in this
  project).
- Adds `--mode` to argparse (`choices=["clean", "resume"]`, default `None`).
- `run_migration()` gains the load/prompt/resume-vs-clean branching described
  in §3, and calls `save_state()` after each batch and `delete_state()` on
  full success.
- The interactive prompt itself lives in `main()` (not `run_migration()`),
  since `run_migration()` is also called directly by tests, which must stay
  non-interactive — tests pass `mode="resume"`/`"clean"` explicitly rather
  than exercising the stdin prompt.

## 5. Error handling additions

- `--mode=resume` with no matching state file → `MigrationError: "No
  incomplete migration found for {slug} ({source_url} -> {dest_url}); use
  --mode=clean or omit --mode to start fresh."`
- Resume with an embedding mismatch against the state file's recorded
  values → same `MigrationError` message style as the existing fresh-run
  mismatch check, prefixed with "Embedding config changed since the
  interrupted run: ...".
- A corrupt/unparseable state file (manually edited, truncated by a crash
  during write — though the atomic `os.replace` should prevent this) is
  treated as "no state file found" after logging a warning to stderr, so
  the tool degrades to a fresh-run prompt rather than crashing.

## 6. Testing

- `backend/tests/test_migration_state.py` (new): round-trip save/load,
  atomic write doesn't leave a `.tmp` file behind, `state_path` is stable
  for the same inputs and differs across any changed input, `delete_state`
  is a no-op when the file doesn't exist, corrupt-file handling.
- `backend/tests/test_migrate_library.py` (extend): `run_migration` with
  `mode="clean"` ignores existing state and clears destination (existing
  behavior, now explicit); `mode="resume"` skips `import/begin` and resumes
  a collection from a non-null stored cursor, verified via a fake state
  file and mock HTTP calls recording that `/import/begin` was never called;
  a batch failure persists state up to the last successful batch only;
  full success deletes the state file; `--mode=resume` with no state file
  raises `MigrationError`; `main()`'s interactive-prompt branch (mock
  `input()` for `r`/`c`/`a`, including the loop-until-valid-answer case).

## 7. Out of scope

- No change to the backend API (`backend/api/migration.py`) — cursors are
  already server-returned opaque values the script just needs to persist
  and replay.
- No locking against two concurrent migrations for the same key — this is
  a manually-run operator tool; running the same migration twice
  concurrently is a user error, not a case to guard against.
- No automatic retry/resume *within* a single process run beyond what
  already exists (`_MAX_ATTEMPTS` per-request retries) — resume is for
  *between* process invocations only.
