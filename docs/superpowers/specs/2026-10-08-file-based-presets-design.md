# File-based hardware/model presets

## Problem

`backend/config/presets.py` hardcodes all 9 hardware/model presets
(`cpu-only`, `remote-kisski`, `remote-mpcdf`, etc.) as a module-level
`PRESETS` dict of `HardwarePreset` instances. Changing a preset — tuning
`top_k`, swapping a model name, adding a new preset for a new deployment
target — requires editing source code and redeploying/restarting. There is
no way for an operator to add a local or site-specific preset, or tweak an
existing one, without a code change.

## Goals

- Move preset *content* out of source code into user data, so a preset can
  be added, edited, or removed without touching `presets.py` or
  redeploying.
- Ship the current 9 presets as defaults that a fresh deployment gets
  automatically, without losing any admin's local edits on upgrade.
- Keep the existing typed, validated `HardwarePreset`/`EmbeddingConfig`/
  `LLMConfig`/`RAGConfig` Pydantic models as the in-memory representation —
  only the *source* of the data changes, not how it's modeled once loaded.

## Non-goals

- No live file-watcher — editing a preset file takes effect on the next
  call that reads it, not pushed to already-open connections.
- No schema-migration tooling for future required-field additions to
  `HardwarePreset`.
- No admin UI for editing presets (a separate future feature; this spec
  only makes the files exist and be loaded).
- No shared/templated model lists or `$ref`-style includes across preset
  files — each preset file is fully self-contained.

## Design

### Storage layout

- `<data_path>/presets/<name>.json` — one file per preset, e.g.
  `data/presets/cpu-only.json`. The filename stem is the preset's
  canonical identifier, used by `get_preset(name)`/`list_presets()` the
  same way the current dict key is.
- `backend/config/default_presets/<name>.json` — the 9 current presets,
  shipped in-repo as bundled defaults. One JSON file per current
  `PRESETS` dict entry, translated verbatim (see "Two behavior changes"
  below for the two spots that aren't a direct translation).

### Seeding: copy-if-missing, never overwrite

A new `ensure_default_presets(data_path: Path)` in `presets.py` is called
from `Settings.ensure_directories()` (the same method that already creates
`model_weights_path`, `vector_db_path`, etc. on every startup). For each
bundled default file, it copies that file into `<data_path>/presets/`
**only if no file of that name exists there yet**. An admin's edited or
deleted preset file is never touched or resurrected — this mirrors the
explicit requirement that local modifications persist across upgrades.

### Schema stays as-is; filename is authoritative over content

`EmbeddingConfig`, `LLMConfig`, `RAGConfig`, `HardwarePreset` are
unchanged. A preset file's JSON content matches `HardwarePreset`'s field
shape, except the `name` field is not read from the file — it's always
set from the filename stem:

```python
data = json.loads(filepath.read_text())
data["name"] = filepath.stem  # overrides any "name" key the file might contain
preset = HardwarePreset.model_validate(data)
```

This prevents the filename and an internal `name` field from silently
drifting apart after a rename or copy.

### Loader API (`backend/config/presets.py`)

- `get_preset(name: str, data_path: Optional[Path] = None) -> HardwarePreset`
  — when `data_path` is omitted, it's resolved via a deferred
  `get_settings().data_path` import (the same pattern
  `Settings.get_hardware_preset()` already uses to reach
  `admin_settings_store` without a circular import against `settings.py`).
  Unknown name raises the same `ValueError("Unknown preset '<name>'. Available: ...")`
  as today. A file that fails to parse or fails Pydantic validation raises
  `ValueError` wrapping the underlying error and naming the file path, so
  whoever edited it knows exactly what to fix.
- `list_presets(data_path: Optional[Path] = None) -> list[str]` — lists
  the valid preset names found under `<data_path>/presets/`. A file that
  fails to parse/validate is skipped with a logged warning rather than
  raising — one broken custom preset shouldn't break the preset dropdown
  for every other preset.
- The module-level `PRESETS` dict is removed — there's no sound way to
  keep a static, eagerly-populated dict once content is file-backed and
  resolved per `data_path`. Its three current call sites switch to
  `list_presets()`/`get_preset()`:
  - `backend/api/config.py` (`GET /config`, `POST /config` validation,
    `available_presets`)
  - `scripts/check_embedding_compat.py` (`pick_preset()`)
  - `scripts/eval_embeddings.py`
- No caching: each call re-reads the (small) JSON file from disk. This is
  also what makes "edit a preset file, see it take effect" work with zero
  extra code — the admin-controlled runtime preset *switch*
  (`admin_settings_store.active_preset_override`) already re-reads the
  preset on every `get_hardware_preset()` call; now its *content* is live
  too.

### Two behavior changes from today, called out explicitly

1. **`KISSKI_RAG_MODELS`** is today a shared Python list constant reused
   by 4 presets (`apple-silicon-kisski`, `remote-kisski`,
   `cloud-server-kisski`, `windows-test`). It becomes a literal
   `llm.model_names` array duplicated in each of those 4 default JSON
   files. Confirmed via grep that nothing outside `presets.py` imports
   this constant, so nothing else breaks. Presets are now independent,
   user-editable files by design — accepted duplication, not a
   regression.
2. **`remote-kisski`'s env-driven batch size** —
   `batch_size=int(os.environ.get("EMBEDDING_BATCH_SIZE", "256"))` — is
   **generalized, not dropped**. `EMBEDDING_BATCH_SIZE` is documented,
   production-critical memory-tuning guidance (`docs/cron-indexing.md`,
   `docs/presets.md`, `.env.dist`, `.env.deploy.example`), added
   specifically to prevent the hourly cron indexer from OOM-killing a
   low-RAM host — it must keep working exactly as documented, for every
   remote-embedding preset, not just `remote-kisski`. The JSON file
   stores a plain literal default (`256` for `remote-kisski`, unchanged
   per-preset defaults for the others). `Settings.get_hardware_preset()`
   applies the override once, after loading whichever preset is active:
   if `EMBEDDING_BATCH_SIZE` is set in the environment, it overwrites
   `preset.embedding.batch_size` on the returned object, regardless of
   which preset that is. This is strictly more general than today (every
   remote preset becomes tunable this way, not only `remote-kisski`) and
   requires no production `.env` migration — an already-set
   `EMBEDDING_BATCH_SIZE` keeps working unchanged.

### Error handling

| Situation | Behavior |
|---|---|
| `get_preset("unknown-name")` | `ValueError("Unknown preset 'unknown-name'. Available: ...")` — unchanged message shape |
| `get_preset("broken")` where `broken.json` is malformed/invalid | `ValueError` wrapping the parse/validation error, naming the file path |
| `list_presets()` encounters a malformed file | Logged warning, file excluded from the returned list, other presets unaffected |
| Preset file content includes a `"name"` key | Ignored; `name` is always set from the filename stem |

## Testing

- `backend/tests/test_config.py`'s preset tests currently call
  `get_preset("cpu-only")`/`list_presets()` with no `data_path`, which
  would otherwise read the real project's `data/` directory. Update them
  to use an isolated `tempfile.TemporaryDirectory()` with an explicit
  `data_path`, seeded via `ensure_default_presets()` where a test needs
  real preset content.
- New tests:
  - `ensure_default_presets` copies every bundled default into an empty
    directory.
  - `ensure_default_presets` does not overwrite an existing file, even one
    whose content differs from the bundled default (simulating a user
    edit).
  - `get_preset` on a malformed JSON file and on a file that fails
    `HardwarePreset` validation each raise `ValueError` naming the file
    path.
  - `list_presets` skips a malformed file (with a warning) instead of
    raising, and still returns the other valid presets.
  - A `"name"` field inside a preset file's JSON content is ignored in
    favor of the filename stem.
  - `Settings.get_hardware_preset()` applies `EMBEDDING_BATCH_SIZE` (when
    set) to whichever preset is active, not just `remote-kisski`.
  - `backend/api/config.py`, `scripts/check_embedding_compat.py`,
    `scripts/eval_embeddings.py` still work end-to-end against
    `list_presets()`/`get_preset()` (no more `PRESETS` import).

## Open items for the implementation plan

- Confirm no other script or test besides the three identified call sites
  imports `PRESETS` directly (a repo-wide grep at implementation time,
  since new code may have landed since this spec was written).
