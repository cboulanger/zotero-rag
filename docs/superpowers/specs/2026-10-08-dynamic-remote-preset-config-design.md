# Dynamic remote-endpoint config + runtime preset switching

## Problem

`backend/config/presets.py` was just extended with a `remote-mpcdf` preset
(MPCDF LLM Inference Service, `llm.mpcdf.mpg.de`) to work around exhausted
KISSKI rate limits. Unlike KISSKI's fixed `base_url`
(`https://chat-ai.academiccloud.de/v1`), MPCDF hands out a fresh endpoint
(`https://llm.mpcdf.mpg.de/<job-id>`) and a fresh API key **every time an
≤8h Slurm job is (re)started** — for both the embedding job and the LLM
job independently. Today, `base_url` for a preset is either a literal
string in `presets.py` or resolved once from an env var at module-import
time (`os.environ.get(...)`, evaluated when the process starts). Picking
up a new MPCDF job today means editing `.env` and restarting the whole
backend (`npm start`) — disruptive, and happens as often as every few
hours during active use.

There's also no way to switch which preset is active without restarting
the backend (`MODEL_PRESET` env var, read once at startup), and no
mechanism at all for a field that needs a fresh value supplied **after**
the backend is already running.

The plugin already solves an adjacent problem — per-user API keys for
providers like KISSKI (`extensions.zotero-rag.serviceApiKey.<key_name>`,
`plugin/src/zotero-rag.js:699-764`, discovered via
`GET /api/required-keys`, injected as a per-request header by
`getAuthHeaders()`, `zotero-rag.js:347-359`). That mechanism is correct
for a genuinely *personal* secret (each user's own KISSKI quota), but
doesn't fit MPCDF's case: there is exactly **one** shared endpoint/key per
job, used by every caller of this backend (interactive queries *and* the
hourly cron auto-indexer, `backend/services/cron_indexer.py:711`) — not
one per user.

## Goals

- Enter a freshly-issued MPCDF endpoint/key once, without restarting the
  backend, and have it immediately apply to both interactive requests and
  the cron auto-indexer.
- Generalize this so any future preset can declare a field (`base_url` or
  `api_key`) as "shared, admin-set, no restart needed" — not something
  MPCDF-specific baked into the code.
- Let an admin switch the active preset at runtime (no restart), scoped to
  presets that are safe to hot-swap between.
- Keep the existing per-user API key mechanism (KISSKI-style,
  `api_key_env`, header-based) completely unchanged — it solves a
  different problem and already works, including for cron (see
  `docs/superpowers/specs/2026-07-07-per-user-embedding-key-design.md`).

## Non-goals

- No support for switching to/from a **local-model** preset
  (`apple-silicon-32gb`, `high-memory`, `cpu-only`) at runtime — those load
  multi-GB torch models into memory/VRAM at startup; switching to/from
  them still requires a restart (`MODEL_PRESET` + restart, unchanged).
- No encryption for the new shared store. The values it holds are valid
  for at most 8 hours and scoped to a sandboxed HPC job; the ciphertext
  machinery used for long-lived personal keys
  (`backend/services/autoindex_key_store.py`, `AUTOINDEX_SECRET`) is not
  warranted here.
- No change to how a genuinely per-user secret (KISSKI `api_key_env`)
  flows through the system, interactively or via cron.
- No persistence of the runtime preset-switch across a backend restart —
  it reverts to the `MODEL_PRESET` env var on restart, same as today.

## Design

### 1. Two kinds of "required field"

`required_api_keys()` (in `backend/services/embeddings.py` and
`backend/services/llm.py`) becomes `required_client_fields()`, returning
entries of two kinds, both read from a preset's `model_kwargs`:

| `model_kwargs` key      | `kind`             | Who sets it | How it's resolved |
|--------------------------|---------------------|-------------|--------------------|
| `api_key_env` (existing) | `"api_key"`         | Each user, in their own plugin install | Per-request header → process env var → error. **Unchanged.** |
| `shared_base_url_env` (new) | `"shared_base_url"` | One admin, once | Shared plaintext store → process env var → error |
| `shared_api_key_env` (new)  | `"shared_api_key"`  | One admin, once | Shared plaintext store → process env var → error |

`remote-mpcdf`'s `model_kwargs` (currently using a single `api_key_env:
"MPCDF_API_KEY"` and an import-time-resolved `base_url` — both wrong, see
Correction below) becomes:

```python
embedding=EmbeddingConfig(
    ...,
    model_kwargs={
        "shared_base_url_env": "MPCDF_EMBEDDING_BASE_URL",
        "shared_api_key_env": "MPCDF_EMBEDDING_API_KEY",
    },
),
llm=LLMConfig(
    ...,
    model_kwargs={
        "shared_base_url_env": "MPCDF_LLM_BASE_URL",
        "shared_api_key_env": "MPCDF_LLM_API_KEY",
    },
),
```

**Correction to the already-committed preset:** it currently declares a
single `MPCDF_API_KEY` for both embedding and LLM. MPCDF's embedding job
and LLM job are two independent Slurm jobs; each almost certainly gets its
own distinct generated key. Split into `MPCDF_EMBEDDING_API_KEY` /
`MPCDF_LLM_API_KEY`, mirroring the existing `MPCDF_EMBEDDING_BASE_URL` /
`MPCDF_LLM_BASE_URL` split. Also drop the current
`"base_url": os.environ.get("MPCDF_EMBEDDING_BASE_URL")` literal — no
`base_url` key at all once `shared_base_url_env` is declared.

### 2. Shared plaintext store

New module `backend/services/remote_config_store.py`, modeled on the
*structure* (not the encryption) of `autoindex_key_store.py`:

- File: `<data_path>/system/dynamic_remote_config.json` — flat
  `{key_name: value}`, e.g. `{"MPCDF_EMBEDDING_BASE_URL": "https://llm.mpcdf.mpg.de/5fe69nkr66ldwf4l/v1", "MPCDF_EMBEDDING_API_KEY": "..."}`.
- Plaintext, atomic write (temp file + `os.replace`), `chmod 0600` on
  write — cheap defense-in-depth on a multi-user host even though the
  threat model doesn't require encryption.
- API: `read_value(key_name) -> Optional[str]`, `write_values(values:
  dict[str, str]) -> None` (merge/update, preserving unrelated keys),
  `list_keys() -> dict[str, bool]` (presence only — never returns values
  in bulk, so a status endpoint can't accidentally leak them).

### 3. Resolution lives in the service factories, not the HTTP layer

`cron_indexer.py:711` calls `create_embedding_service(preset.embedding,
api_key=target["embedding_key"])` **directly** — it does not go through
`backend/dependencies.py::make_embedding_service()`, which only exists for
the HTTP request path. This means the shared-store fallback must be added
inside `create_embedding_service`/`create_llm_service` (or the
`RemoteEmbeddingService`/`RemoteLLMService` classes they build) in
`embeddings.py`/`llm.py` themselves — the single place both the
interactive-request path and the cron path already converge. Concretely,
wherever these currently do:

```python
base_url = self.config.model_kwargs.get("base_url")
...
api_key = self._api_key or os.getenv(api_key_env)
```

add, before falling through to env/error:

```python
if shared_url_env := self.config.model_kwargs.get("shared_base_url_env"):
    base_url = remote_config_store.read_value(shared_url_env) or os.getenv(shared_url_env)
if shared_key_env := self.config.model_kwargs.get("shared_api_key_env"):
    api_key = self._api_key or remote_config_store.read_value(shared_key_env) or os.getenv(shared_key_env)
```

If nothing resolves a required shared field, raise the same style of clear
`ValueError` already used for a missing `api_key_env` value, naming the
missing field.

Because `cron_indexer.py:710` reads the preset via
`get_settings().get_hardware_preset()` on every run (not a cached
reference), it automatically follows whatever preset is currently active
— including after a runtime switch (§5) — with no cron-specific code
change needed there at all.

### 4. New admin endpoint to write the shared store

`POST /api/config/remote-fields` in `backend/api/config.py`, gated by
`require_authorized_group_admin` (same dependency as the autoindex
scheduler's pause/resume/run-now controls, bypassed on loopback/personal
deployments):

- Body: `{"values": {"MPCDF_EMBEDDING_BASE_URL": "...", ...}}`.
- Validates every key in `values` is one of the *active* preset's declared
  `shared_base_url_env`/`shared_api_key_env` names (400 listing the
  offending key otherwise — prevents writing arbitrary env-var-shaped
  keys into the store).
- Writes via `remote_config_store.write_values(...)`, returns the
  resulting presence map (`list_keys()`), never the values.

### 5. Runtime preset switching

- A small in-memory runtime override (not the cached `Settings` object —
  avoid fighting pydantic immutability) holds the current preset name.
  `Settings.get_hardware_preset()` checks it before falling back to
  `MODEL_PRESET`. Resets to the env-var default on restart; not persisted
  to disk.
- "Safe to hot-swap" = `embedding.model_type == "remote"` and
  `llm.model_type == "remote"` **and** `embedding.model_name` matches the
  *currently active* preset's embedding model name — same embedding model
  ⇒ same vector space ⇒ the existing `VectorStore` singleton
  (`backend/dependencies.py:176-203`, opened once at startup) stays valid
  with zero changes to its lifecycle. No re-opening/re-keying logic is
  needed.
  - Today's qualifying set, all using `"multilingual-e5-large-instruct"`:
    `remote-kisski`, `windows-test`, `apple-silicon-kisski`,
    `remote-mpcdf`. `remote-openai` (different embedding model) and
    `cloud-server-kisski` (local embedding) are excluded.
- `ConfigResponse` (`backend/api/config.py:21-35`) gains
  `compatible_presets: List[str]` — the qualifying set relative to the
  current preset.
- `POST /api/config` (`backend/api/config.py:116-150`, currently a
  documented no-op) starts actually taking effect: admin-gated, validates
  `preset_name ∈ compatible_presets` (400 otherwise, with a message
  pointing at the restart-based path for an incompatible target), flips
  the runtime override, returns the fresh `ConfigResponse`.

### 6. Plugin changes

- `renderServiceApiKeyFields` (`plugin/src/zotero-rag.js:699-764`)
  branches on the new `kind` field from `/api/required-keys`:
  - `"api_key"` → unchanged: password input → local pref
    (`extensions.zotero-rag.serviceApiKey.<key_name>`) → per-request
    header.
  - `"shared_base_url"` → text input; `"shared_api_key"` → password
    input. Neither writes a local pref — on change, both POST to
    `/api/config/remote-fields`. Rendered with a visible "shared — admin
    only, affects every user" annotation. Shown as "configured ✓"
    (from `is_set`) rather than ever reflecting a previous value back.
  - Surfaced only in the Preferences pane (`plugin/src/preferences.js`),
    not the setup wizard — this is ongoing admin maintenance, not
    first-run onboarding.
- New Preferences control: a preset dropdown populated from
  `compatible_presets`. On change: `POST /api/config` with
  `{preset_name}`; on success, refresh the displayed config and re-fetch
  + re-render `/api/required-keys` (the newly active preset's dynamic
  fields need to be filled in right away).

## Error handling

- Missing shared field at resolution time (store empty, env var unset):
  `ValueError` naming the field, surfaced as today's existing missing-key
  error path does for `api_key_env`.
- `POST /api/config/remote-fields` with a key not declared by the active
  preset: 400.
- `POST /api/config` with a `preset_name` outside `compatible_presets`:
  400. Unknown `preset_name`: 400 (existing check, unchanged). Non-admin
  caller on a non-loopback deployment: 403 (existing dependency).

## Testing

- Backend (pytest): `required_client_fields()` emits correct `kind` per
  declared `model_kwargs` key; `remote_config_store` read/write/merge and
  file permissions; service-factory resolution precedence (store → env →
  error) for both embedding and LLM; `cron_indexer.py`'s direct
  `create_embedding_service(...)` call picks up a store value with no
  cron-specific code touched; `POST /api/config/remote-fields`
  validation + admin gating; `POST /api/config` compatibility filtering +
  admin gating + runtime effect (subsequent `GET /api/config` reflects
  the switch).
- Plugin (Node test runner): a `shared_base_url`/`shared_api_key` field
  renders with the correct input type and POSTs to the new endpoint
  instead of writing a local pref; preset dropdown reflects
  `compatible_presets` and re-renders required fields after a successful
  switch.

## Open items for the implementation plan

- Confirm exact MPCDF endpoint URL format once an admin has a live job
  (the current `remote-mpcdf` preset comment doesn't assume one — no code
  depends on its shape beyond "an OpenAI-compatible base URL").
- Confirm whether any other existing caller besides `cron_indexer.py` and
  the HTTP request path constructs `RemoteEmbeddingService`/
  `RemoteLLMService` directly (so the shared-store fallback placed inside
  the factories covers every caller, not just the two identified here).
