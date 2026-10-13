> **Status: superseded** by `2026-10-10-huggingface-preset-and-provisioner-adapters-design.md` (provider layer). Kept as a record of the original RunPod design.

# Cross-preset endpoint health check + "Provision endpoints" button

## Problem

The `runpod` preset (see `docs/superpowers/specs/2026-10-09-runpod-preset-design.md`)
backs onto two serverless endpoints that scale to zero when idle. Switching
the active preset to `runpod` (now hot-swappable — see the
`_compatible_presets()` basename fix) does not mean the endpoints are
actually warm: a query issued right after the switch can time out or fail
while a cold worker spins up, and there is currently no way for an admin
to know whether the endpoints are ready, or to wake them, from the Zotero
plugin's Preferences pane. Today, warming them up means running
`scripts/provision_runpod_endpoints.py` by hand on a machine with shell
access to the backend host — not something a remote admin using only the
plugin UI can do.

This is a deliberately narrow follow-on to the existing per-model demand
indicator (`LLMConfig.models_status_url`, `GET /api/models/status` — see
memory `feature-model-demand-indicator`), which answers a different
question ("how busy is this always-on gateway") for a different preset
shape (KISSKI's fixed shared endpoint). `runpod`-style presets need "is
this specific scale-to-zero worker up at all," plus a way to trigger
provisioning, neither of which the existing mechanism covers.

## Goals

- A generic, per-preset-declared health-check mechanism: a preset's
  embedding and/or LLM config can optionally name a `health_check_provider`
  (today: `"runpod"` only), and the backend exposes `GET /api/config/health`
  reporting each side's readiness (`ready` / `cold` / `unreachable`), or
  omitting a side entirely when its config declares no health check.
- A generic `provisioning_script` field on a preset, and a `POST
  /api/config/provision` (+ status-polling `GET`) admin endpoint that runs
  that script as a background subprocess and, on success, automatically
  applies the resulting shared config (base URLs) via the existing
  `update_remote_config` mechanism — no manual curl/`.env` step.
- A "Provision endpoints" button in the plugin's Preferences pane, shown
  only for a preset that declares `provisioning_script`, disabled while a
  job is running or while the endpoint has just been provisioned and
  already reports `ready`, re-enabled on completion (success or failure).
- Two independent status rows (embedding, LLM) rather than one combined
  indicator, since RunPod's two endpoints can be ready/cold independently.

## Non-goals

- No change to the existing KISSKI demand indicator (`models_status_url`,
  `/api/models/status`) — it answers a different question and keeps its
  own code path.
- No support for a second serverless health-check provider yet — the
  provider registry is designed to extend, but only `"runpod"` is
  implemented now (YAGNI).
- No automatic re-provisioning or scheduled warm-up — this is an
  admin-triggered, on-demand action from the Preferences pane only.
- Does not change what happens if a real RAG query hits a cold endpoint
  mid-request (still subject to whatever timeout/retry behavior the
  embedding/LLM service already has) — this feature only gives visibility
  and a manual way to pre-warm, not query-time cold-start handling.
- Health/provisioning only ever target the **currently active** preset —
  there is no UI to pre-warm a preset before switching to it. Provisioning
  endpoint changes don't change `POST /api/config`'s existing switch logic.

## Architecture

### 1. Preset schema additions (`backend/config/presets.py`)

```python
class EmbeddingConfig(BaseModel):
    ...
    health_check_provider: Optional[Literal["runpod"]] = Field(
        default=None,
        description="If set, GET /api/config/health checks this config's "
                     "shared_base_url_env endpoint via this provider's health API.",
    )

class LLMConfig(BaseModel):
    ...
    health_check_provider: Optional[Literal["runpod"]] = Field(default=None, ...)  # same meaning

class HardwarePreset(BaseModel):
    ...
    provisioning_script: Optional[str] = Field(
        default=None,
        description="Repo-relative path to a script that provisions/wakes this "
                     "preset's remote endpoint(s) (see docs/superpowers/specs/"
                     "2026-10-09-endpoint-health-provisioning-design.md for the "
                     "script's --json output contract). Presence of this field "
                     "is what makes the Preferences pane show a 'Provision "
                     "endpoints' button.",
    )
```

Only `backend/config/default_presets/runpod.json` sets these:
`embedding.health_check_provider = "runpod"`, `llm.health_check_provider =
"runpod"`, `provisioning_script = "scripts/provision_runpod_endpoints.py"`.
Every other bundled preset leaves all three unset (`None`), and their
schema default means existing preset JSON files on disk (including any
user's already-customized ones) keep validating unchanged.

### 2. Health-check provider registry (`backend/utils/endpoint_health.py`, new file)

```python
def check_runpod_health(base_url: str, api_key: str) -> dict:
    """Returns {"status": "ready"|"cold"|"unreachable", "detail": str}."""
```

- Extracts the endpoint id from `base_url` via the fixed RunPod URL shape
  `https://api.runpod.ai/v2/<id>/...` (regex `r"/v2/([^/]+)/"`; a base_url
  that doesn't match is `"unreachable"` with a descriptive detail — this
  can only happen if an admin hand-edits `RUNPOD_*_BASE_URL` to something
  malformed).
- Calls `GET https://api.runpod.ai/v2/<id>/health` with the configured API
  key, 10s timeout.
- Classification from the response body's `workers` object (same shape
  already seen live during the Task 10 smoke test):
  - `workers.ready > 0` or `workers.running > 0` → `"ready"`
  - endpoint reachable, no ready/running workers → `"cold"`
  - request error, timeout, or non-2xx → `"unreachable"` (detail includes
    the status code or exception message)
- A small dict dispatches `provider_name -> check_fn`, so adding a second
  provider later is a one-function, one-registry-entry change.

### 3. `GET /api/config/health` (`backend/api/config.py`)

No admin gate (read-only, no secrets in the response — same posture as
`GET /api/config`). For each of the active preset's embedding/LLM configs:

- `health_check_provider` unset → that key is `null` in the response.
- Otherwise resolve `base_url`/`api_key` via the **same** precedence
  `RemoteEmbeddingService._get_client()` already uses (admin
  `remote_config` override, then `os.environ`) and call the matching
  `check_*_health` function.

To avoid a third copy of that resolution logic (it already exists once in
`embeddings.py` and once in `llm.py`), extract it into
`admin_settings_store.resolve_shared_value(data_path, env_var_name) ->
Optional[str]` (wraps `get_remote_config_value(...) or
os.getenv(env_var_name)`), and have both existing services call it instead
of re-implementing the `or` chain inline. Small, mechanical refactor with
no behavior change — covered by each existing test suite still passing.

Response shape:

```json
{"embedding": {"status": "ready", "detail": "..."} | null,
 "llm": {"status": "cold", "detail": "..."} | null}
```

If `base_url`/`api_key` can't be resolved at all (not yet provisioned),
that side reports `{"status": "unreachable", "detail": "not configured"}`
rather than `null` — `null` means "this preset has no health concept,"
while `"unreachable"` means "it has one, but there's nothing to check yet."

### 4. Provisioning job (`backend/api/config.py` + new `backend/services/provisioning.py`)

**Script contract** (documents what any future `provisioning_script` must
do to integrate, not just RunPod's): invoked as `uv run python
<provisioning_script> --json`. On success (exit code 0), somewhere in its
stdout it prints exactly one line of the form:

```text
PROVISION_RESULT: {"RUNPOD_EMBEDDING_BASE_URL": "...", "RUNPOD_LLM_BASE_URL": "..."}
```

`scripts/provision_runpod_endpoints.py` gains a `--json` flag that adds
this line (its existing human-readable output — the "Wrote ... to .env"
message and curl reminder — is unchanged and still printed; `--json` only
adds the one extra machine-readable line, so running the script by hand
exactly as documented in `docs/presets.md` still works identically). The
keys in that JSON object are exactly the preset's declared
`shared_base_url_env` values, so the backend can feed them straight into
`update_remote_config` without any provider-specific translation.

**Job tracking**: a single module-level in-memory state (not persisted —
a provisioning run is a few minutes at most, the script is already
idempotent, and losing track of an in-flight job on a backend restart just
means the admin clicks the button again):

```python
_job_state = {"status": "idle", "message": None, "started_at": None, "finished_at": None}
```

`POST /api/config/provision` (admin-gated, same `require_authorized_group_admin`
dependency as `POST /api/config`):

- 409 if `_job_state["status"] == "running"`.
- 400 if the active preset has no `provisioning_script`.
- Otherwise sets `status = "running"` and returns 202 immediately. The
  handler is `async def` (not `def`, unlike most of this file's
  synchronous-I/O handlers — see the project's event-loop rule): it
  `await`s only `asyncio.create_subprocess_exec("uv", "run", "python",
  script_path, "--json", stdout=PIPE, stderr=PIPE)` to start the
  subprocess, then hands the resulting process off to
  `asyncio.create_task(_await_job(proc, preset))` without awaiting it —
  that task runs `await proc.communicate()` and updates `_job_state` in
  the background, after the response has already been sent.
- The background task: on exit code 0 with a `PROVISION_RESULT:` line
  found, calls `update_remote_config(data_path, parsed_values)`, sets
  `status = "succeeded"`. On exit code 0 with no such line (script
  contract violation) or non-zero exit, sets `status = "failed"` with
  `message` set from the script's stderr tail (last ~500 chars) or a
  "script did not report a result" message.

`GET /api/config/provision/status` (no admin gate — read-only, lets any
caller see an in-progress job started by an admin): returns the current
`_job_state` dict as-is.

### 5. Plugin UI (`plugin/src/preferences.js`, `preferences.xhtml`)

In `refreshPresetState()`, after populating the preset `<select>`, also
call a new `refreshEndpointHealth()`:

- `GET /api/config/health`. For each non-`null` side, render a status row
  (`Embedding: ● ready`, using the same traffic-light-style convention as
  the existing demand indicator: green=ready, yellow=cold, red=unreachable).
  Both rows omitted entirely if both sides are `null` (no health concept
  for this preset) — matches the silent-degrade convention already used
  for `models_status_url`.
- If the active preset's config also reports `provisionable: true` (a new
  boolean added to `ConfigResponse` — exposes *whether* a script exists,
  not the script path itself) and at least one status is not `"ready"`,
  show a "Provision endpoints" button.
- Button click: `POST /api/config/provision`, disable the button
  immediately, show a spinner/"Provisioning…" label, then poll both
  `GET /api/config/provision/status` and `GET /api/config/health` every 5s.
  - `status === "running"` → keep polling.
  - `status === "succeeded"` → re-render health rows from the fresh
    `/health` response, re-enable the button only if a row still isn't
    `"ready"` (e.g. the embedding warm-up timed out as seen live in Task
    10 — exit 0 but still cold; the admin can just click again).
  - `status === "failed"` → show the error message inline, re-enable the
    button.
- `refreshEndpointHealth()` is also called once, independently, whenever
  the preset `<select>`'s `change` handler finishes switching presets
  (already calls `refreshPresetState()`, which will now include this).

## Error handling

- Every network/parsing failure in `refreshEndpointHealth()` degrades to
  "no status rows shown" (caught and logged, like the existing
  `refreshPresetState()` catch block) — never blocks the rest of the
  Preferences pane from rendering.
- `check_runpod_health()` never raises — any exception becomes
  `{"status": "unreachable", "detail": str(exc)}`, since this is a
  display-only signal, not something that should 500 `GET /api/config/health`.
- A provisioning job's subprocess failing, timing out, or printing
  malformed JSON in its `PROVISION_RESULT:` line all land in `"failed"`
  with a human-readable `message` — never left in `"running"` forever
  (the subprocess's own `WARMUP_MAX_SECONDS`-bounded retry loop already
  guarantees the script itself exits in bounded time).

## Testing plan

- `backend/tests/test_endpoint_health.py` (new): unit tests for
  `check_runpod_health()` against a `FakeClient` (reusing the
  `FakeClient`/`FakeResponse` convention from
  `test_provision_runpod_endpoints.py`) — ready/cold/unreachable
  classification, malformed base_url, HTTP error, timeout.
- `backend/tests/test_config.py` additions: `GET /api/config/health`
  returns `null`/`null` for `remote-kisski`; returns real status objects
  for `runpod` (mocking `check_runpod_health`); `provisionable` flag
  correct per preset.
- `backend/tests/test_provision_job.py` (new, or appended to
  `test_config.py`): `POST /api/config/provision` admin-gating, 409 on
  concurrent call, 400 when active preset has no `provisioning_script`,
  success path applies `update_remote_config` from a mocked subprocess's
  `PROVISION_RESULT:` stdout, failure path on non-zero exit.
- `scripts/test_provision_runpod_endpoints.py` additions: `--json` flag
  prints a well-formed `PROVISION_RESULT:` line with the correct keys on
  the existing success path; existing human-readable output unchanged.
- Plugin: manual verification only (per project convention — no existing
  automated test harness drives the Preferences pane's network calls),
  using the already-provisioned live RunPod endpoints from Task 10.
