# Provider abstraction layer, RunPod migration, and Hugging Face provider

Status: proposal, not implemented. High level on purpose: it fixes the
architecture, the contracts between the parts, and the decision points, not the
code.

Three parts, to be implemented in order:

- **A. Provider abstraction layer.** A `Provider` base class, an id-based
  registry, preset versioning, the small core changes that make the backend
  provider-blind, and a provider-agnostic provisioning UI in the plugin's settings.
- **B. Migrate existing presets.** RunPod becomes `RunPodProvider` and the four
  KISSKI presets become `KisskiProvider`, which takes over every KISSKI-specific
  behaviour (live model list, demand indicator, rate-limit meters, key portal).
  Every other bundled preset runs on a minimal fallback `GenericProvider` that
  contains no vendor-specific code and preserves the behaviour those presets have
  today. Bundled presets are always re-seeded; custom presets are version-checked.
- **C. Hugging Face.** A new `HuggingFaceProvider` and a `huggingface` preset. This
  is the proof that a new provider is additive, with no core and no plugin change.

## Decision

Presets stay JSON (data, admin-editable under `data/presets/`). Provider-specific
behaviour lives in Python classes, one per provider, and a preset selects its
class by an id string in the JSON:

```json
"provider": { "id": "runpod", "options": { "...": "provider-specific, opaque to core" } }
```

Rejected alternatives: presets-as-classes (undoes the file-based preset design in
`2026-10-08-file-based-presets-design.md` and makes data-only presets awkward),
and out-of-process provisioning scripts as the main mechanism (a subprocess per
health poll, credentials via env/argv, core still has to know which health check
to call).

Backward compatibility is deliberately not handled at run time. Instead:

- Bundled presets are overwritten from the shipped copies on every start (B4), so
  the old RunPod schema disappears from any deployment on its first start after
  upgrade.
- Custom presets carry an integer `version`; a mismatch is logged as a warning, not
  repaired (B4).

## Why this is needed (current coupling)

| Coupling | Where |
|---|---|
| `health_check_provider: Literal["runpod"]` on both `EmbeddingConfig` and `LLMConfig` | `backend/config/presets.py` |
| `HEALTH_CHECKS = {"runpod": ...}`, imported by `api/config.py` | `backend/utils/endpoint_health.py` |
| `provisioning_script` path, the `PROVISION_RESULT:` stdout contract, and the `PROVISIONING_API_KEY` env hand-off | `presets.py`, `services/provisioning.py`, `api/config.py` |
| Provider URL and key regexes repeated in each preset's `model_kwargs` | `runpod.json` |
| Cold-endpoint messages special-casing RunPod's gateway | `services/embeddings.py`, `services/llm.py` |
| Provisioning panel is fixed markup: one optional "Provisioning key" field, fixed help text, progress shown only as "Provisioning…" | `plugin/src/preferences.xhtml`, `plugin/src/preferences.js` |
| KISSKI model-list fetching, RAG-suitability filtering and demand labels, used by `GET /api/config` and `GET /api/models/status`, gated by the KISSKI-shaped `LLMConfig.models_status_url`; `query.py` also tests that field | `backend/utils/kisski.py`, `api/config.py`, `api/query.py`, `presets.py` |
| KISSKI key-portal URL and header-name entries in tables shared with OpenAI, Anthropic and HF | `_KNOWN_HEADERS`, `_KNOWN_DOCS_URLS` in `services/embeddings.py` |
| Rate-limit bars understand only KISSKI's `x-ratelimit-{limit,remaining}-{hour,day}` headers and the label "requests left/hour"; a single global snapshot, embedding side only | `plugin/src/rate-limit-widget.js`, `services/embeddings.py`, `services/rate_limit_info.py` |
| Seeding never overwrites, so a schema change cannot reach an existing deployment | `ensure_default_presets` in `presets.py` |
| Pydantic ignores unknown keys, so an outdated custom preset silently loses fields | `HardwarePreset` (no `extra` policy, no version) |

---

## A. Provider abstraction layer

### A1. Preset schema and versioning

`HardwarePreset` gains `provider` and `version`, and loses three fields:

```python
PRESET_SCHEMA_VERSION = 2          # bump on any breaking change to the file schema

class ProviderConfig(BaseModel):
    id: str                        # registry key, e.g. "runpod"
    options: dict = {}             # validated by the provider class, not by core

class HardwarePreset(BaseModel):
    ...
    version: int = 1               # absent in a file means the pre-provider schema (1)
    provider: Optional[ProviderConfig] = None   # None == {"id": "generic"}
```

Removed: `EmbeddingConfig.health_check_provider`, `LLMConfig.health_check_provider`,
`HardwarePreset.provisioning_script`, and `LLMConfig.models_status_url`. The live
model list and demand indicator it switched on become a provider capability
(`live_models()`, A2); `KisskiProvider` derives the URL from the LLM `base_url`,
with an optional `models_url` override in its `options`.

Rules for `version`:

- It is an integer, bumped only for a breaking change to the preset file format
  (a key removed, renamed, or changed in meaning). Additive optional keys do not
  bump it.
- Every bundled preset ships `"version": 2`.
- A short changelog table in `docs/presets.md` lists what each version changed, so
  a warning can point at it.

### A2. The `Provider` base class (`backend/providers/base.py`)

The base class is concrete and implements the generic behaviour. Subclasses
override only what their provider needs, so `GenericProvider` is simply the base
class registered under id `generic`.

```python
class Provider:
    id: ClassVar[str]
    label: ClassVar[str]                                  # human name for the UI
    Options: ClassVar[type[BaseModel]] = NoOptions        # validates preset.provider.options
    supports_provisioning: ClassVar[bool] = False
    supports_suspend: ClassVar[bool] = False              # cost-control pause, see A8

    def __init__(self, preset: HardwarePreset, options: BaseModel): ...

    # Load-time hook: fill provider defaults into the preset's model_kwargs
    # (e.g. URL/key regexes) so embeddings.py / llm.py keep reading model_kwargs only.
    def apply_defaults(self, preset: HardwarePreset) -> None: ...

    # What the client needs to render provisioning UI. No secrets. See A7.
    def describe(self) -> ProviderDescriptor: ...

    # Live, availability-ordered model list for the LLM side, or None when the
    # provider has no such endpoint. Replaces the models_status_url preset field.
    def live_models(self, base_url: str, api_key: str) -> list[ModelInfo] | None: ...

    # Where the user manages the personal key for this env var (shown in Preferences).
    def key_docs_url(self, env_var: str) -> str | None: ...

    # Turn captured response headers into display meters (quota only, see A9).
    def parse_usage(self, side: Side, headers: Mapping[str, str]) -> list[Meter]: ...

    # Readiness of one side. None means "this provider has no health concept".
    def health(self, side: Side, base_url: str, api_key: str) -> Health | None: ...

    # Create or wake the remote resources. Returns {shared_base_url_env: url}.
    # Must report steps through `progress` and respect ctx.deadline.
    def provision(self, ctx: ProvisionContext, progress: Callable[[str], None]) -> dict[str, str]:
        raise NotImplementedError

    # Delete the remote resources (decommissioning). CLI only, never in the UI.
    def teardown(self, ctx: ProvisionContext) -> None:
        raise NotImplementedError

    # Stop all billing and block any wake-up until resumed. Idempotent; keeps the
    # resources and their URLs. Resume is provision(). See A8.
    def suspend(self, ctx: ProvisionContext, progress: Callable[[str], None]) -> None:
        raise NotImplementedError

    # Optional: tell the query path what an HTTP error means.
    def classify_http_error(self, status: int, body: str) -> Literal["cold", "paused"] | None: ...
```

- `Health` keeps the existing shape: `{"status": "ready"|"cold"|"throttled"|"paused"|"unreachable", "detail": str}`.
  The five statuses are defined provider-neutrally: `cold` wakes on the next
  request by itself, `throttled` means the provider has no capacity right now,
  `paused` was stopped on purpose and will not wake until resumed (A8),
  `unreachable` includes "nothing provisioned yet" and provider-side failure.
  Pause state is read from the provider through `health()`, never stored locally,
  so there is one source of truth and nothing to drift.
- `ProvisionContext` carries the preset, the resolved credentials, the data path,
  flags (`recreate`, `skip_warmup`) and a `deadline`. Credentials arrive
  in-process, so the `PROVISIONING_API_KEY` env hand-off disappears.
- `ModelInfo` is `{id, demand: int | None, availability: str | None}`; the first
  list entry is the preferred model, matching what `GET /api/config` does today.
- Defaults implement `GenericProvider`, which contains no vendor-specific code:
  `health()` returns `None`, `live_models()` returns `None`, `key_docs_url()`
  returns `None`, `parse_usage()` returns `[]`, `supports_provisioning` and
  `supports_suspend` are `False`, `classify_http_error()` returns `None`,
  `apply_defaults()` is a no-op, and `describe()` returns the provider id and label
  with no provisioning section.

### A3. Registry and discovery (`backend/providers/__init__.py`)

- The package imports all of its submodules on first use. Each `Provider`
  subclass registers itself by its `id` (via `__init_subclass__`). There is no
  central list to edit, so adding a provider is adding a module.
- `get_provider(preset) -> Provider` resolves `preset.provider.id` (default
  `generic`), validates `options` with the class's `Options` model, and caches
  the instance with the preset (presets are already cached per process; see the
  `presets.py` module docstring).
- An unknown id or invalid options marks that one preset unavailable, with a
  warning in the log and in `GET /api/config` listings. It never fails backend
  startup or the other presets.
- Heavy or optional dependencies (an SDK) are imported lazily inside the provider
  module, so a deployment that does not use that provider never needs them.

### A4. Core call sites become provider calls

| Today | After |
|---|---|
| `_check_side()` looks up `HEALTH_CHECKS[provider]` | `get_provider(preset).health(side, url, key)`; `None` stays `null` in the response, unresolvable URL/key stays "not configured" |
| `provisionable = bool(preset.provisioning_script)` | `provider.supports_provisioning` |
| `POST /api/config/provision` spawns `sys.executable script --json` | `await asyncio.to_thread(provider.provision, ctx, progress)`; same job states (`idle`/`running`/`succeeded`/`failed`), same 409 and 400 behaviour; the job fails when `ctx.deadline` passes |
| Result parsed from a `PROVISION_RESULT:` stdout line | Returned dict goes straight to `update_remote_config()` |
| `_provisioning_key()` reads the key pattern from `model_kwargs` | Unchanged in logic; the pattern now arrives via `apply_defaults()` |
| `_merge` skips `shared_base_url` keys when `provisioning_script` is set | Skips them when `provider.supports_provisioning` is set |
| Job state is `{status, message, started_at, finished_at}` | Adds `progress: list[str]`, the provider's reported steps (bounded, newest last) |
| `get_config()` and `get_models_status()` call `fetch_kisski_rag_models()` when `models_status_url` is set | Call `provider.live_models(base_url, api_key)`; `None` means use the preset's static `model_names` and return an empty status list, as today |
| `query.py` accepts an unlisted `llm_model` only when `models_status_url` is set | Accepts it when `provider.live_models` is supported (the provider's list is authoritative) |
| `GET /api/rate-limits` returns raw captured headers | Also returns `meters` from `provider.parse_usage()` (A9); raw `limits` kept |
| `docs_url_for_key()` reads a shared table | `provider.key_docs_url(env_var)`, falling back to a small table of the generic OpenAI and Anthropic entries |
| (new) | `POST /api/config/suspend`, admin-gated, runs `provider.suspend()` through the same single job slot (409 while another job runs); resume is `POST /api/config/provision` (A8) |

`embeddings.py` and `llm.py` keep reading `shared_base_url_env` /
`shared_api_key_env` via `resolve_shared_value()`. Their cold-endpoint branches may
also ask `provider.classify_http_error()` instead of matching a RunPod-specific
response; until Part B moves it, the current RunPod detection stays in place.

### A5. CLI

A generic `bin/provision.py [--preset NAME] [--pause] [--teardown] [--recreate] [--yes]
[--skip-warmup]` drives any provider that supports provisioning. It replaces the
per-provider script as the manual entry point and is subject to the same
"run inside the container so the data volume is shared" rule from CLAUDE.md.

### A6. Provider contract tests

One test module parametrised over every registered provider:

- `Options` validation (valid, invalid, defaults).
- `apply_defaults()` is idempotent and does not clobber explicit preset values.
- `describe()` returns a well-formed descriptor with no secret material.
- `health()` classification against a fake client, never raising.
- `provision()` idempotency against a fake client (second run creates nothing),
  and it reports at least one progress step.
- `teardown()` is a no-op for absent resources.
- `parse_usage()` never raises, returns `[]` for headers it does not recognise, and
  returns well-formed meters (limit and remaining non-negative, `remaining` not
  above `limit`) for sample headers supplied by the provider's test fixture.
- `live_models()`, when supported, returns the preferred model first and never
  raises (network failure returns `None`).
- If `supports_suspend`: `suspend()` is idempotent, `health()` then reports
  `paused`, a following `provision()` reports `ready` or `cold` again, and the
  endpoint URLs are unchanged across the cycle.
- Providers with `supports_provisioning = False` or `supports_suspend = False`
  raise `NotImplementedError` and are never offered the corresponding button.

A new provider must pass this suite. That is the definition of "easy to add".

### A7. Client UI: provider-agnostic provisioning in Preferences

Today the "Active Model Preset" group in `preferences.xhtml` has fixed markup for
provisioning: a single optional one-time key field, one fixed help sentence, and a
"Provisioning…" label with no detail while the job runs. `preferences.js` already
does the generic parts correctly (health rows per side, colour by status, button
shown only when a side is `unreachable`/`throttled` or a job runs, polling status
every 5 s). What it cannot do is describe a provider: what credential to ask for,
what that credential is for, how long it takes, or what step the job is on.

**Principle:** the plugin never contains a provider name or provider-specific text.
Everything provider-specific arrives as data from the backend.

**Backend contract.** New read-only `GET /api/config/provider` (no admin gate, no
secrets, same posture as `GET /api/config`), returning the active preset's
`ProviderDescriptor`:

```json
{
  "id": "huggingface",
  "label": "Hugging Face",
  "supports_provisioning": true,
  "supports_suspend": true,
  "provisioning": {
    "credential": {
      "label": "Hugging Face token",
      "help": "Creating endpoints needs a token with write access to Inference Endpoints. Used for this run only unless no token is stored yet.",
      "pattern": "^hf_[A-Za-z0-9]+$",
      "optional": true
    },
    "hint": "A first start can take several minutes while the model loads."
  }
}
```

`GET /api/config/provision/status` additionally returns `progress` (A4). `POST
/api/config/provision` keeps its `{ "api_key": "..." }` body. `GET /api/config`
keeps `provisionable`, now derived from the provider.

**Plugin behaviour** (all in `plugin/src/preferences.js`, built dynamically like
`renderServiceApiKeyFields`, replacing the fixed provisioning rows in the xhtml
with one container):

| Element | Source | Notes |
|---|---|---|
| Provider line ("Provider: Hugging Face") | `descriptor.label` | Under the preset description; hidden for `generic` |
| Health rows per side | `GET /api/config/health` | Unchanged, same colours and statuses |
| Provision panel visibility | `descriptor.supports_provisioning` and (a side is `unreachable`/`throttled`/`paused`, or a job is running) | Same rule as today, plus `paused` |
| "Pause endpoints" button | `descriptor.supports_suspend` and no side `paused` | Shown next to the health rows whenever the endpoints are `ready` or `cold`, not only when something is wrong; one click, no confirmation (reversible) |
| "Resume endpoints" button | `descriptor.supports_suspend` and a side `paused` | Same action and job path as Provision (`POST /api/config/provision`), different label, so there is one code path |
| Paused notice | health `paused` | Row text "paused, no cost, will not wake on requests", in a neutral colour (not red) |
| Credential field | `descriptor.provisioning.credential` | Label, help text and placeholder from the descriptor; pattern checked client-side, enforced server-side regardless; omitted if the descriptor has none |
| Hint line | `descriptor.provisioning.hint` | Shown while idle and while running |
| Progress | `status.progress` | Newest line shown live under the button; whole list available as a tooltip |
| Result | `status.message` | Success or the failure reason, unchanged |
| Resume after reopening Preferences | `GET /api/config/provision/status` on every refresh | If `running`, resume the polling loop. Today the in-memory `provisioning` flag is lost when the pane closes, so a long-running job (HF can take minutes) looks idle |
| Non-admin | unchanged | `POST` returns 403, shown inline |

**Old-backend tolerance.** If `GET /api/config/provider` is missing (older
backend), the plugin falls back to a plain descriptor: generic labels, a single
optional one-time key field, no progress. It never hard-fails, matching the
existing "degrade to showing nothing" convention for health.

**Shared service-key fields stay as they are.** The "Service API Keys" section
still renders `HF_TOKEN` or `RUNPOD_API_KEY` from `GET /api/required-keys`
(`kind: "shared_api_key"`). The provisioning credential is a separate, one-time
override, exactly as today.

**Out of scope for v1:** per-provider free-form provisioning parameters entered in
the UI (for example choosing a region at click time), and a teardown button.
Provider-level settings stay in the preset JSON `options`. Teardown stays on the
CLI: for both RunPod and HF an idle, scaled-to-zero endpoint already costs nothing,
so deleting it saves no money, and on RunPod it changes the endpoint ID, which
breaks endpoint-restricted keys. The cost-control action is Pause (A8). The
descriptor can grow a `fields` list later without changing the plugin's structure.

**Tests.** Plugin tests render the panel from two fake descriptors (one RunPod-like
with the existing broader-key help text, one HF-like with a token pattern and a
hint) and from the old-backend fallback, and assert that no provider name appears
in plugin code. A job-resume test starts the pane while status is `running`.
Backend tests cover `GET /api/config/provider` for every registered provider via
the contract suite. Pause and Resume button visibility is tested for each
combination of descriptor flag and health status.

### A8. Pause and Resume (cost control)

**Why not teardown.** Scale-to-zero already makes an idle endpoint free on both
RunPod and HF, so deleting it saves nothing. What costs money is waking it: any
request starts a worker and bills it for the run plus the idle tail (HF reportedly
about 15 minutes, to be verified). The hourly auto-indexer would wake the
endpoints every hour and keep them effectively always on, and a stray query, a
retry loop, or a misconfigured minimum replica count does the same. Pause is a
reversible switch that blocks every wake-up until an admin resumes.

**Semantics.** `suspend()` stops all billing for both sides at once, keeps the
resources and their URLs, and is idempotent. Resume is the existing idempotent
`provision()`, which restores the configured scaling and wakes the endpoints. The
paused state is not stored by the backend: `health()` derives it from the
provider (A2), so a pause made from the CLI or the provider's own console is
seen the same way.

| Provider | Pause | Resume |
|---|---|---|
| HF | `endpoint.pause()` (not billed, does not auto-wake) | `provision()` calls `.resume()` |
| RunPod | no native pause; set `workersMax` to 0 so no worker can start, and optionally purge queued jobs (whether the REST update accepts 0 must be verified) | `provision()` restores `workersMax` from `options` |
| Generic | not supported | not applicable |

**Who respects the paused state.** All of these must avoid waking a paused
endpoint and report why, instead of failing obscurely or hanging:

- The auto-index scheduler and `bin/index_libraries.py` (the cron path): skip the
  run for a preset whose provider reports `paused`, with a reason such as
  "Endpoints are paused; resume them in Preferences" surfaced through the existing
  auto-index status reasons. This is the main reason the feature exists.
- On-demand and deferred server-side indexing: same check, same reason.
- Interactive queries: `RemoteEmbeddingService` and the LLM service consult a
  short-TTL cached `health()` (about 30 seconds) for providers with
  `supports_suspend`, and raise the existing "endpoint unavailable" error with a
  paused-specific message. This matters on RunPod, where a request to an endpoint
  with `workersMax=0` would otherwise queue and time out. `classify_http_error()`
  may return `paused` for providers whose paused endpoints answer with a
  recognisable error (HF).
- Health and the plugin: `paused` is shown in a neutral colour and offers Resume
  (A7).

**Access and concurrency.** `POST /api/config/suspend` uses the same admin
dependency as provisioning and the same single job slot, so pause cannot overlap a
provision run (409). It returns 400 for a provider without `supports_suspend`.

**Tests.** Contract tests (A6); API tests for admin gating, 409 and 400; scheduler
tests that a paused preset is skipped with the reason and that nothing is called on
the endpoint; query-path tests that a paused state yields the paused message
without a network call to the inference URL.

### A9. Usage meters (rate limits) per provider

Billing and spend are out of scope: cost stays a matter of inspecting the
provider's own dashboard. This section only generalises the quota display that
exists today for KISSKI's rate limits.

**Today.** Capture is already header-driven and vendor-neutral (any
`x-ratelimit*` / `ratelimit*` header from an embedding response, plus the
`retry-after` handling and the per-key `rate_limited` status), but display is not:
the plugin widget reads only KISSKI's `hour` and `day` header names. Capture also
covers the embedding side only and is one process-wide snapshot.

**Contract.** A provider turns captured headers into a list of meters. The plugin
renders whatever it receives:

```python
class Meter:
    id: str                      # e.g. "requests/hour"
    side: Literal["embedding", "llm"]
    unit: str                    # "requests", "tokens"
    period: str | None           # "minute", "hour", "day"
    limit: int
    remaining: int
    resets_at: str | None
    as_of: str
    source: Literal["run", "cache"]
```

- `GET /api/rate-limits` keeps its raw `limits` and gains `meters`. The plugin
  uses `meters` when present and draws one bar per meter with the existing 75%
  amber and 95% red thresholds and the label "`remaining` `unit` left/`period`".
  Without `meters` (older backend) it shows nothing, as it does now for any
  provider whose headers it does not understand.
- `GenericProvider.parse_usage()` returns `[]`: no vendor header names live in
  the fallback. `KisskiProvider` is the only provider that parses meters today
  (B2). A provider whose service emits the OpenAI-style `x-ratelimit-*-requests` /
  `-tokens` headers or the IETF `RateLimit-*` headers adds its own parser; a shared
  helper for those dialects can be factored out when a second provider needs it.
- The snapshot becomes per side and, where the quota is per key, per key
  fingerprint, and the LLM service uses the same capture helper. The cron status
  file persists the same per-side data. This also fixes the case where one user's
  key quota would be shown to another user.
- Retry-after handling and `EmbeddingRateLimitExhaustedError` stay as they are:
  they key off the OpenAI client's `RateLimitError`, not a provider.

---

## B. Migrate existing presets

### B1. `GenericProvider` (presets with no vendor integration)

Applies to `cpu-only`, `high-memory`, `apple-silicon-32gb`, `remote-mpcdf` and
`remote-openai`. Their JSON gains only `"version": 2`; `provider` is omitted and
resolves to `generic`.

The base class contains no vendor-specific code, and for these presets it
reproduces today's behaviour:

- `GET /api/config/health` returns `null` for both sides.
- `provisionable` is `false`, and `POST /api/config/provision` returns 400.
- `GET /api/config/provider` returns the generic descriptor, so the plugin shows no
  provisioning panel and no provider line.
- URL and key handling is untouched: `required_client_fields()` still reads
  `base_url`/`api_key_env` or `shared_*_env` from `model_kwargs`.
- No live model list, no demand indicator, and no rate-limit meters. None of these
  presets has a KISSKI endpoint, so none of them shows these today.
- `remote-mpcdf` keeps its manual `POST /api/config/remote-fields` workflow. An
  MPCDF Slurm provider is a possible future provider and is out of scope here.
- `remote-openai` stays generic. The OpenAI and Anthropic key-portal entries
  remain in a small fallback table consulted when a provider returns no URL; an
  `OpenAIProvider` could own them later.

### B2. `KisskiProvider` (`backend/providers/kisski.py`)

Applies to `remote-kisski`, `apple-silicon-kisski`, `cloud-server-kisski` and
`windows-test`, which get `"provider": {"id": "kisski"}`. The provider is per
preset, so for `cloud-server-kisski` (local embeddings, KISSKI LLM) the embedding
side simply has no remote usage to report.

Everything KISSKI-specific moves here, and nothing is left behind in generic code:

| Moves from | To | Notes |
|---|---|---|
| `backend/utils/kisski.py` (`fetch_kisski_rag_models`, RAG-suitability filter, `coder`/`devstral` exclusion, demand to availability labels) | `KisskiProvider.live_models()` | The file is deleted; its tests move with it |
| `LLMConfig.models_status_url` in four preset files | `options.models_url` (optional, default `{llm base_url}/models`) | The schema field is removed (A1) |
| `"KISSKI_API_KEY": "https://saia.gwdg.de/dashboard"` in `_KNOWN_DOCS_URLS` | `KisskiProvider.key_docs_url()` | The redundant `KISSKI_API_KEY` entry in `_KNOWN_HEADERS` is dropped; the default header derivation already yields `X-Kisski-Api-Key` |
| KISSKI `hour`/`day` header parsing in `rate-limit-widget.js` | `KisskiProvider.parse_usage()` | The widget becomes provider-agnostic (A9) |
| `get_models_status()` demand labels (`available`, `busy`, `very busy`) | `KisskiProvider.live_models()` fills `demand` and `availability` | `GET /api/models/status` is a thin wrapper over the provider |

What deliberately stays out of the provider: `extra_body` (for example
`enable_thinking: false`) and `base_url`/`api_key_env` remain plain preset data in
`model_kwargs`, because they are generic OpenAI-compatible-client settings.

Behaviour preservation is checked for the four presets (B6): the live ordered model
list in `GET /api/config`, the demand dots from `GET /api/models/status`, the
hour and day bars, and the Preferences key link.

A custom preset copied from a KISSKI one and left without a `provider` block loses
those four features. The version warning in B4 names the removed
`models_status_url` key and says to add `"provider": {"id": "kisski"}`.

### B3. `RunPodProvider` (`backend/providers/runpod.py`)

Move, with no behaviour change:

- `check_runpod_health()` from `backend/utils/endpoint_health.py` becomes
  `RunPodProvider.health()`.
- The ensure-template, ensure-endpoint, warm-up and teardown logic from
  `bin/provision_runpod_endpoints.py` (~600 lines, `httpx` REST) becomes
  `provision()` / `teardown()`, with progress steps added ("Creating embedding
  endpoint", "Waiting for warm-up", and so on).
- The URL regex (`^https://api\.runpod\.ai/v2/[A-Za-z0-9]+/openai/v1$`) and key
  regex (`^rpa_[A-Za-z0-9]+$`) move into `apply_defaults()`, which writes them
  into `model_kwargs` as `shared_*_pattern`. `runpod.json` stops repeating them.
- RunPod's gateway cold response (the openresty 405 page the services special-case
  today) becomes `classify_http_error()`.
- New: `supports_suspend = True`. `suspend()` sets each endpoint's `workersMax` to 0
  (and purges the queue); `health()` reports `paused` when `workersMax` is 0;
  `provision()` restores `workersMax` from `options`. The `GET /v1/endpoints`
  response already used for lookup carries `workersMax`, so no extra call is
  needed. Whether the REST update accepts 0 is the one thing to verify first; if it
  does not, the fallback is to lower `workersMax` to the minimum allowed and keep
  `workersMin=0`, which stops idle cost but cannot block a wake-up, and the spec
  then marks RunPod as `supports_suspend = False`.
- The provisioning panel text that is RunPod-specific in effect today ("a key with
  broader rights than the one used for queries") moves into `describe()`.

`Options` replaces the CLI flags (the CLI can still override them):

```json
"provider": {
  "id": "runpod",
  "options": {
    "llm_model": "Qwen/Qwen2.5-7B-Instruct",
    "embedding_gpu": "NVIDIA RTX A5000",
    "llm_gpu": "NVIDIA RTX A5000",
    "workers_max": 1,
    "idle_timeout": 60,
    "data_centers": null
  }
}
```

`runpod.json` after migration: `"version": 2`; drops `health_check_provider` (x2),
`provisioning_script`, and the four `shared_*_pattern` entries; gains the
`provider` block above.

### B4. Seeding and versioning (replaces any run-time compatibility handling)

**Bundled presets are always overwritten.** `ensure_default_presets` is changed so
that, once per process per data path (the existing cache), every file in
`backend/config/default_presets/` is written to `<data_path>/presets/<name>.json`
unless the on-disk bytes are already identical. The write keeps the existing
atomic temp-file-and-rename pattern, so a concurrent reader (the cron indexer
sharing the data path) never sees a partial file.

- If the on-disk file differed, the overwrite is logged at WARNING with the file
  name and a pointer: "Bundled preset X was modified locally; local changes were
  replaced. To customise a preset, copy it to a new file name."
- Consequences, to be documented in `docs/presets.md` in place of the current "never
  overwritten, deletions stick" text: editing a bundled file in place does not
  survive a restart; deleting a bundled file is undone at the next start; the way to
  customise is a new file name.
- The old RunPod schema therefore disappears from a deployment on its first start
  after upgrade, with no detection logic and no migration code to remove later.

**Custom presets are version-checked, not repaired.** A "custom" preset is any file
in `<data_path>/presets/` whose name is not a bundled file name. When one is loaded:

- If `version` is missing or differs from `PRESET_SCHEMA_VERSION`, log one WARNING
  per file per process: "Custom preset X declares version N, current is M; fields
  may be ignored or behave differently. See the preset changelog in
  docs/presets.md." The preset still loads.
- If it contains keys removed in a later version (`health_check_provider`,
  `provisioning_script`, `models_status_url`), the warning names them and says they are ignored. The
  list of removed keys per version is a small table in `presets.py`, extended when
  a future version removes more.
- A custom copy of the old RunPod preset keeps working as a plain, non-provisioning
  remote preset, and the warning tells the admin to move to the `provider` block.

### B5. Implementation order (each step keeps the suite green)

1. Add `backend/providers/` (base, registry, `GenericProvider`), `version` and
   `provider` on the schema, the B4 seeding and version warnings, and bump every
   bundled preset to `"version": 2`. Core call sites in A4 switch to the provider.
   Behaviour is identical for every preset except that `runpod` temporarily loses
   its health and provisioning, and the four KISSKI presets temporarily lose the
   live model list, demand dots, rate-limit bars and key link (both are ported in
   step 2), so steps 1 and 2 must land together in one change set.
2. Port RunPod into `RunPodProvider` and the KISSKI behaviour into
   `KisskiProvider` (B2), including the generic rate-limit widget and `meters`
   (A9) and deleting `backend/utils/kisski.py`. Delete `HEALTH_CHECKS`, the
   `Literal` fields, `models_status_url`, and the subprocess job path. Keep
   `bin/provision_runpod_endpoints.py` as a thin shim over `bin/provision.py`.
3. Backend descriptor endpoint and `progress` in job state (A4, A7).
4. Plugin: dynamic provisioning panel, job resume, old-backend fallback, and the
   plugin tests from A7.
5. Pause and Resume (A8): `suspend()` on the base class and `RunPodProvider`
   (after verifying `workersMax=0`), the `paused` status, `POST /api/config/suspend`,
   the scheduler, indexing and query-path checks, and the two buttons. This can land
   after the HF provider if RunPod's `workersMax=0` check turns out negative, since
   HF alone already supports it.
6. Rewrite `runpod.json`, update `docs/presets.md` (storage rules, version
   changelog, provider ids), and mark the older RunPod/health specs as superseded
   where they describe the script contract.

### B6. Behaviour-preservation checklist

For each bundled preset, before and after: `GET /api/config`, `GET
/api/config/health`, `GET /api/required-keys`, `POST /api/config/provision` status
code, and a preset switch. For the four KISSKI presets, additionally: the live
ordered `llm_models` in `GET /api/config`, `GET /api/models/status` (demand and
labels), `GET /api/rate-limits` (the hour and day bars render identically from
`meters`), the Preferences key link, and a query that names a model missing from
the static list. For `runpod`: health classification, provision
end-to-end against a fake client, and the plugin's provisioning flow (button,
one-time key, polling, health refresh), unchanged apart from the new progress line
and the descriptor-driven text.

### B7. Seeding and versioning tests

- A modified bundled file is overwritten and a WARNING naming it is logged.
- An identical bundled file is not rewritten (modification time unchanged).
- A deleted bundled file is recreated.
- A custom file is never touched.
- A custom file with a missing or old `version` warns exactly once per process;
  with the current version it does not warn.
- A custom file containing removed keys names them in the warning and still loads.
- A custom KISSKI-derived file with `models_status_url` and no `provider` warns,
  loads as generic, and does not call the KISSKI endpoint.

### B8. KISSKI provider tests

- Existing `kisski.py` unit tests are ported to `KisskiProvider.live_models()`
  (filtering, ordering, demand labels, network failure returns `None`).
- `parse_usage()` fixtures: the KISSKI hour and day headers, partial headers,
  non-numeric values, and unrelated headers.
- The plugin widget test (`rate-limit-widget.test.js`) is rewritten to feed
  `meters`, with a case for a provider that supplies none.

---

## C. Hugging Face provider and preset

### C1. Why it differs from RunPod

| | RunPod | Hugging Face Inference Endpoints (dedicated) |
|---|---|---|
| Objects | template, then endpoint | one endpoint (image and env inline) |
| Readiness | `/health` worker counts | status field: `pending`, `initializing`, `running`, `scaledToZero`, `paused`, `failed` |
| URL | shared host, per-endpoint path | per-endpoint hostname (`*.endpoints.huggingface.cloud`) |
| Credentials | one RunPod key | one HF token for both management and inference |
| Billing | per second, 60s idle default | per instance-hour by the minute, billed while initializing or running, not while paused or scaled to zero |
| Cold behaviour | gateway 405 page | reported 502 until a replica is up |

All of this fits behind the A2 interface. Nothing requires a core change, and
nothing requires a plugin change: the UI is driven by the descriptor (A7), which is
the test that the abstraction holds.

### C2. `HuggingFaceProvider` (`backend/providers/huggingface.py`)

- **Options:** `namespace` (default: the token's own user), `vendor`, `region`, and
  per side `engine` (`tei` for embeddings, `vllm` or `tgi` for the LLM),
  `instance`, `scale_to_zero_timeout_min`, `min_replica` (0), `max_replica` (1).
- **`apply_defaults()`:** adds a key regex (`^hf_[A-Za-z0-9]+$`) and a base-URL
  regex for the endpoint hostname shape to `model_kwargs`. Both patterns must be
  verified against real endpoints.
- **`describe()`:** label "Hugging Face"; a credential description saying that
  creating endpoints needs a token with write access to Inference Endpoints and
  that a billing method must be on the account; a hint that a first start can take
  several minutes.
- **`provision()`:** idempotent by name (`zotero-rag-embedding`,
  `zotero-rag-llm`). Absent: create. `scaledToZero` or `paused`: resume. Existing
  with a differing config: warn, or recreate with `--recreate`. Then wait with a
  bound, send a warm-up request, and return
  `{HF_EMBEDDING_BASE_URL: ..., HF_LLM_BASE_URL: ...}` (endpoint URL plus `/v1`).
  Emits progress steps for each phase (creating, initializing, warming up).
- **`health()`:** one management-API call per side. `running` maps to `ready`;
  `scaledToZero`, `pending`, `initializing` map to `cold`; `paused` maps to
  `paused`; `failed` or an API error maps to `unreachable`. No data-plane probe is
  needed, unlike RunPod.
- **`classify_http_error()`:** treats the 502 observed during scale-from-zero as
  `cold`, and the error a paused endpoint returns (to be confirmed) as `paused`.
- **`suspend()`:** `supports_suspend = True`; calls `.pause()` on both endpoints.
  Idempotent. `provision()` resumes a paused endpoint, so Resume needs no extra code.
- **`teardown()`:** deletes both endpoints; absent endpoints are a no-op. CLI only.
- **Dependency:** use `huggingface_hub` if its endpoint API is stable enough to
  justify it, imported lazily inside this module only. Otherwise plain `httpx`
  REST, as RunPod does. Decide in the spike.

### C3. Preset (`backend/config/default_presets/huggingface.json`)

```json
{
  "version": 2,
  "description": "Fully remote via your own Hugging Face Inference Endpoints (embedding + LLM, pay-per-use, scale-to-zero). Enter your HF token, then click \"Provision endpoints\" to create or wake both endpoints.",
  "embedding": {
    "model_type": "remote",
    "model_name": "intfloat/multilingual-e5-large-instruct",
    "batch_size": 64,
    "model_kwargs": { "shared_base_url_env": "HF_EMBEDDING_BASE_URL", "shared_api_key_env": "HF_TOKEN" }
  },
  "llm": {
    "model_type": "remote",
    "model_names": ["Qwen/Qwen2.5-7B-Instruct"],
    "max_context_length": 32768,
    "max_answer_tokens": 2048,
    "temperature": 0.7,
    "model_kwargs": { "shared_base_url_env": "HF_LLM_BASE_URL", "shared_api_key_env": "HF_TOKEN" }
  },
  "rag": { "top_k": 10, "score_threshold": 0.35, "max_chunk_size": 800 },
  "provider": {
    "id": "huggingface",
    "options": {
      "vendor": "aws",
      "region": "eu-west-1",
      "embedding": { "engine": "tei",  "instance": "nvidia-l4", "scale_to_zero_timeout_min": 15 },
      "llm":       { "engine": "vllm", "instance": "nvidia-l4", "scale_to_zero_timeout_min": 15 }
    }
  },
  "memory_budget_gb": 0.5,
  "platform": "any"
}
```

Region, instance names and the scale-to-zero timeout are placeholders to confirm
against the live service. The shared `HF_TOKEN` is the same key for both sides, as
`RUNPOD_API_KEY` is today.

### C4. Cost note for the preset description

HF bills per instance-hour; indicative AWS single-GPU rates are L4 $0.80/h, A10G
$1.00/h, L40S $1.80/h, A100-80G $2.50/h. RunPod serverless bills per second, so
for bursty use (a few queries a day) it is likely cheaper, and HF's longer idle
tail is the main cost risk. For sustained indexing runs HF's hourly rates are
competitive. These figures come from third-party summaries and must be
re-verified before any number appears in user-facing text.

### C5. Tests

- Provider contract suite (A6) with `HuggingFaceProvider` and a faked client. No
  live calls (endpoints cost money).
- Status-to-health mapping table test; idempotent `provision()` for the
  absent / scaled-to-zero / running / failed starting states; 502 classification.
- Plugin: the existing descriptor-driven tests (A7) are extended with the real HF
  descriptor, with no plugin code change.
- Manual smoke checklist, run once against a real account: provision from
  scratch via the Preferences pane (progress lines visible, closing and reopening
  the pane mid-run resumes), re-run (reuse and wake), scale to zero and wake via a
  query, teardown via the CLI.

---

## Open questions

- Does TEI's `/v1/embeddings` accept batched input and echo the model name the way
  `RemoteEmbeddingService` expects?
- What served model name does vLLM on HF Endpoints report (the container loads from
  `/repository`)? The provider may need to supply the on-the-wire model name
  separately from the preset's `model_names`.
- Real default and minimum for HF's scale-to-zero timeout, and whether the reported
  "stuck in Initializing after wake-up" issue needs a resume-and-recheck loop in
  `provision()`.
- EU region and GPU availability, and whether GDPR hosting should be the default.
- `huggingface_hub` versus plain REST for the HF provider (C2).
- Pause (A8): does RunPod's REST update accept `workersMax=0`? Does a paused HF
  endpoint answer requests with a distinguishable error? Is the HF idle tail
  really 15 minutes and is it configurable down?
- Pause scope: both sides together is assumed. Is there a case for pausing the LLM
  and keeping the embedding endpoint up (for example during indexing only)?
- Should an admin be able to schedule an automatic pause (for example after a
  period with no queries), or is a manual button enough? Scale-to-zero already
  covers idle time, so this is deferred unless the always-on cost shows up in
  practice.
- Should `GenericProvider.parse_usage()` understand the standard OpenAI-style and
  IETF `RateLimit-*` header dialects, so any compatible API gets bars without a
  provider class? It is left empty for now to keep vendor-specific assumptions out
  of the fallback.
- Where should the OpenAI and Anthropic key-portal URLs live long term: the small
  fallback table (current plan), or an `OpenAIProvider` for `remote-openai`?
- Job deadline: what is a sane default (HF cold starts may take well over the RunPod
  3-minute warm-up bound), and should it be a provider option?
- UI: is "newest progress line plus tooltip" enough, or should the full step list be
  visible? And when a provider later needs click-time parameters, is a descriptor
  `fields` list the right extension?
- Should a bundled-preset overwrite keep a one-generation backup (`<name>.json.bak`)
  as a safety net, or is the WARNING log line enough? (The current decision is no
  backup, since customising via a new file name is the supported path.)
