# Provider abstraction layer, RunPod migration, and Hugging Face provider

Status: proposal, not implemented. High level on purpose: it fixes the
architecture, the contracts between the parts, and the decision points, not the
code.

Three parts, to be implemented in order:

- **A. Provider abstraction layer.** A `Provider` base class bound to one side
  (embedding or LLM), an id-based registry, preset versioning, a credential-scope
  model (user, shared and managed scopes; no keys in `.env`), a server default
  preset with per-user preset choice, the core changes that make the backend
  provider-blind, and a provider-agnostic provisioning UI in the plugin's settings.
- **B. Migrate existing presets.** RunPod becomes `RunPodProvider`, the KISSKI
  behaviour becomes `KisskiProvider`, MPCDF becomes `MpcdfProvider`, `remote-openai`
  becomes `OpenAIProvider`, and `AnthropicProvider` is added. Everything else runs
  on `GenericProvider`, the documented fallback for any OpenAI-compatible API
  (including the standard OpenAI and IETF rate-limit headers), which contains no
  vendor-specific code. Bundled presets are always re-seeded; custom presets are
  version-checked.
- **C. Hugging Face.** A new `HuggingFaceProvider` and a `huggingface` preset. This
  is the proof that a new provider is additive, with no core and no plugin change.

## Decision

Presets stay JSON (data, admin-editable under `data/presets/`). Provider-specific
behaviour lives in Python classes, one per provider, and the **embedding side and
the LLM side each select their class by an id string** in the JSON:

```json
"embedding": { "...": "...", "provider": { "id": "huggingface", "options": { "...": "opaque to core" } } },
"llm":       { "...": "...", "provider": { "id": "anthropic" } }
```

So a preset can pair a local embedding model with a frontier LLM, or Hugging Face
embeddings with Anthropic answering. A side without a `provider` block uses
`generic`.

**Credentials follow cost and funding.** A provider key is one of three kinds
(A10), and the provider class declares which kinds it allows:

- `user`: each user brings their own key and pays for and operates their own
  resources (KISSKI today; a user's own HF or RunPod account).
- `managed`: the admin holds one key for a resource that costs money but is funded
  by an institution; only the admin provisions, pauses and resumes it, and all
  users simply use it.
- `shared`: the admin sets one key and URL once for a service that costs nothing
  (institutionally provided, for example MPCDF); there is nothing to operate.

Provider keys are no longer read from `.env` or the process environment by the
main app or by the tools in `bin/`.

**One preset is the server default.** A user who installs the plugin and connects
gets it with no choice to make (out of the box: the rate-limited `remote-kisski`).
They may then choose another preset with the same embedding model for themselves,
for example one backed by their own paid provider to speed things up (A11).

Rejected alternatives: presets-as-classes (undoes the file-based preset design in
`2026-10-08-file-based-presets-design.md` and makes data-only presets awkward),
and out-of-process provisioning scripts as the main mechanism (a subprocess per
health poll, credentials via env/argv, core still has to know which health check
to call).

**No backward compatibility.** There is one production instance and the plugin and
backend ship together, so API shapes, preset schema, credential storage and plugin
code change in place with no fallbacks for older peers. Two mechanisms replace
compatibility code:

- Bundled presets are overwritten from the shipped copies on every start (B4), so
  the old RunPod schema disappears from the deployment on its first start after
  upgrade.
- Custom presets carry an integer `version`; a mismatch is logged as a warning, not
  repaired (B4).

Invalid configurations are rejected, not tolerated: a preset that breaks a rule in
A1 or A10 is unavailable, with a message saying why, and never partly works.

## Why this is needed (current coupling)

| Coupling | Where |
|---|---|
| `health_check_provider: Literal["runpod"]` on both `EmbeddingConfig` and `LLMConfig` | `backend/config/presets.py` |
| `HEALTH_CHECKS = {"runpod": ...}`, imported by `api/config.py` | `backend/utils/endpoint_health.py` |
| `provisioning_script` path (one per preset, covering both sides), the `PROVISION_RESULT:` stdout contract, and the `PROVISIONING_API_KEY` env hand-off | `presets.py`, `services/provisioning.py`, `api/config.py` |
| Provider URL and key regexes repeated in each preset's `model_kwargs` | `runpod.json` |
| Cold-endpoint messages special-casing RunPod's gateway; "endpoint unavailable" messages that name RunPod | `services/embeddings.py`, `services/llm.py` |
| Provisioning panel is fixed markup: one optional "Provisioning key" field, fixed help text, progress shown only as "Provisioning…" | `plugin/src/preferences.xhtml`, `plugin/src/preferences.js` |
| KISSKI model-list fetching, RAG-suitability filtering and demand labels, used by `GET /api/config` and `GET /api/models/status`, gated by the KISSKI-shaped `LLMConfig.models_status_url`; `query.py` also tests that field | `backend/utils/kisski.py`, `api/config.py`, `api/query.py`, `presets.py` |
| Vendor key-portal URLs in a shared table, and a header-name table whose entries only differ from the computed name in letter case (HTTP header names are case-insensitive) | `_KNOWN_DOCS_URLS`, `_KNOWN_HEADERS` in `services/embeddings.py` |
| Default key env var chosen by sniffing the model name for "claude"/"anthropic", and the LLM call routed to the Anthropic SDK the same way | `services/llm.py` (`_get_anthropic_client`, `generate`), `services/embeddings.py` (`OPENAI_API_KEY` default) |
| Rate-limit bars understand only KISSKI's `x-ratelimit-{limit,remaining}-{hour,day}` headers and the label "requests left/hour"; a single global snapshot, embedding side only | `plugin/src/rate-limit-widget.js`, `services/embeddings.py`, `services/rate_limit_info.py` |
| Provider keys and base URLs fall back to `os.environ` (so to `.env`) in many places, and RunPod uses one admin-wide key although it carries cost | `admin_settings_store.resolve_shared_value`, `services/embeddings.py`, `services/llm.py`, `dependencies.py`, `api/config.py`, `config/settings.py` |
| Seeding never overwrites, so a schema change cannot reach an existing deployment | `ensure_default_presets` in `presets.py` |
| Pydantic ignores unknown keys, so an outdated custom preset silently loses fields | `HardwarePreset` (no `extra` policy, no version) |

---

## A. Provider abstraction layer

### A1. Preset schema and versioning

`EmbeddingConfig` and `LLMConfig` each gain `provider`; `HardwarePreset` gains
`version` and loses `provisioning_script`:

```python
PRESET_SCHEMA_VERSION = 2          # bump on any breaking change to the file schema

class ProviderConfig(BaseModel):
    id: str = "generic"            # registry key, e.g. "runpod"
    scope: Optional[Literal["user", "shared", "managed"]] = None   # None == the class default
    options: dict = {}             # validated by the provider class, not by core

class EmbeddingConfig(BaseModel):
    ...
    provider: ProviderConfig = ProviderConfig()

class LLMConfig(BaseModel):
    ...
    provider: ProviderConfig = ProviderConfig()

class HardwarePreset(BaseModel):
    ...
    version: int = 1               # absent in a file means the pre-provider schema (1)
```

Removed: `EmbeddingConfig.health_check_provider`, `LLMConfig.health_check_provider`,
`HardwarePreset.provisioning_script`, and `LLMConfig.models_status_url`. The live
model list and demand indicator it switched on become a provider capability
(`live_models()`, A2); `KisskiProvider` derives the URL from the LLM `base_url`,
with an optional `models_url` override in its `options`.

**Load-time validation.** A violation makes that one preset unavailable, with a
message naming the rule, in the log and in `GET /api/config` listings. It never
fails backend startup or the other presets.

- The provider id exists and its `options` validate.
- A provider only serves the sides it declares (`supported_sides`); Anthropic is
  LLM-only.
- A side with `model_type: "local"` has no remote endpoint and needs no key; a
  non-generic provider on it is a configuration error.
- The effective scope (`provider.scope`, else the class default) is one of the
  scopes the class allows (`key_scopes`, A2). KISSKI allows only `user`; MPCDF only
  `shared`; RunPod, HF, OpenAI and Anthropic allow `user` and `managed`; generic
  allows all three and defaults to `user`.
- The key kinds a side uses match its effective scope (A10): `user` uses
  `api_key_env`; `managed` and `shared` use `shared_api_key_env` (and
  `shared_base_url_env` only for a provider that cannot derive its endpoint URL,
  such as MPCDF). Mixing them is rejected.
- Two sides that use the same key env var must use the same provider id. One
  header cannot carry two vendors' keys.

Illustrative mixed presets (not bundled by this spec):

```json
{ "version": 2, "description": "Hugging Face embeddings, Anthropic answers",
  "embedding": { "model_type": "remote", "model_name": "intfloat/multilingual-e5-large-instruct",
                 "model_kwargs": { "api_key_env": "HF_TOKEN" },
                 "provider": { "id": "huggingface", "options": { "instance": "nvidia-l4" } } },
  "llm": { "model_type": "remote", "model_names": ["claude-sonnet-5-5"],
           "provider": { "id": "anthropic" } },
  "...": "rag, memory_budget_gb, platform as usual" }
```

and a "local embeddings, Anthropic answers" preset is the same with a local
embedding config and no provider block on the embedding side.

Rules for `version`:

- It is an integer, bumped only for a breaking change to the preset file format
  (a key removed, renamed, or changed in meaning). Additive optional keys do not
  bump it.
- Every bundled preset ships `"version": 2`; this whole change set is version 2.
- A short changelog table in `docs/presets.md` lists what each version changed, so
  a warning can point at it.

### A2. The `Provider` base class (`backend/providers/base.py`)

One instance serves **one side** of one preset. The base class is concrete and
implements the generic behaviour; subclasses override only what their provider
needs, so `GenericProvider` is simply the base class registered under id `generic`.

```python
class Provider:
    id: ClassVar[str]
    label: ClassVar[str]                                  # human name for the UI
    Options: ClassVar[type[BaseModel]] = NoOptions        # validates the side's provider.options
    supported_sides: ClassVar[set[Side]] = {"embedding", "llm"}
    key_scopes: ClassVar[set[Scope]] = {"user"}           # scopes a preset may select, see A10
    default_scope: ClassVar[Scope] = "user"               # used when the preset names none
    supports_provisioning: ClassVar[bool] = False
    supports_suspend: ClassVar[bool] = False              # cost-control pause, see A8
    llm_api: ClassVar[Literal["openai", "anthropic"]] = "openai"   # wire protocol of the LLM side

    def __init__(self, side: Side, preset: HardwarePreset, options: BaseModel): ...

    # Load-time hook: fill provider defaults into this side's model_kwargs
    # (e.g. key regex) so embeddings.py / llm.py keep reading model_kwargs only.
    def apply_defaults(self) -> None: ...

    # What the client needs to render UI for this side. No secrets. See A7.
    def describe(self) -> ProviderDescriptor: ...

    # Base URL of a provisioned endpoint, derived from the caller's own key by
    # looking the endpoint up by name; None when it does not exist. Only for
    # providers whose endpoints live in the user's account. See A10.
    def endpoint_url(self, api_key: str) -> str | None: ...

    # Live, availability-ordered model list (LLM side), or None when the provider
    # has no such endpoint. Replaces the models_status_url preset field.
    def live_models(self, base_url: str, api_key: str) -> list[ModelInfo] | None: ...

    # Personal-key metadata for Preferences. No vendor table lives in core.
    def default_key_env(self) -> str | None: ...                  # e.g. "OPENAI_API_KEY"
    def key_docs_url(self, env_var: str) -> str | None: ...       # where the user manages it

    # Provider-specific advice appended to "endpoint unavailable" errors and shown in
    # Preferences under a side that is not ready. Core text stays vendor-neutral.
    def unavailable_hint(self) -> str | None: ...

    # Turn captured response headers into display meters (quota only, see A9).
    def parse_usage(self, headers: Mapping[str, str]) -> list[Meter]: ...

    # Readiness of this side. base_url is None when no endpoint exists yet.
    # None return means "this provider has no health concept".
    def health(self, creds: Credentials) -> Health | None: ...

    # Create or wake this side's remote resources. Returns {shared_base_url_env: url}
    # only for a provider that cannot derive its URL from the key; {} otherwise (the
    # URL comes from endpoint_url()). Must report steps through `progress` and
    # respect ctx.deadline.
    def provision(self, ctx: ProvisionContext, progress: Callable[[str], None]) -> dict[str, str]:
        raise NotImplementedError

    # Delete this side's remote resources (decommissioning). CLI only, never in the UI.
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
- `Credentials` is `{api_key, base_url | None}`: the key of whoever owns the
  resource (the caller for `user` scope, the admin-set key for `managed` and
  `shared`) and the base URL, which core fills from the preset, the shared store or
  `endpoint_url()`.
- `ProvisionContext` carries the preset, the side, the caller's credential, the data
  path, flags (`recreate`, `skip_warmup`) and a `deadline`. Credentials arrive
  in-process, so the `PROVISIONING_API_KEY` env hand-off disappears.
- `ModelInfo` is `{id, demand: int | None, availability: str | None}`; the first
  list entry is the preferred model, matching what `GET /api/config` does today.
- The base class is `GenericProvider`, documented as "any OpenAI-compatible HTTP
  API" (`/v1/embeddings`, `/v1/chat/completions`, bearer-key auth). It contains no
  vendor names: `key_scopes` allows all three scopes and `default_scope` is `"user"`
  (cost is unknown, so a remote generic side is per-user unless the preset says
  otherwise); `parse_usage()` understands the
  OpenAI-style `x-ratelimit-{limit,remaining,reset}-{requests,tokens}` headers and
  the IETF `RateLimit-*` headers (A9); `llm_api` is `"openai"`; `health()`,
  `endpoint_url()`, `live_models()`, `default_key_env()`, `key_docs_url()` and
  `unavailable_hint()` return `None`; `supports_provisioning` and `supports_suspend`
  are `False`; `classify_http_error()` returns `None`; `apply_defaults()` is a
  no-op; and `describe()` returns the provider id and label with no provisioning
  section. Vendor providers subclass it and override what differs, calling
  `super()` for the rest.

### A3. Registry and discovery (`backend/providers/__init__.py`)

- The package imports all of its submodules on first use. Each `Provider`
  subclass registers itself by its `id` (via `__init_subclass__`). There is no
  central list to edit, so adding a provider is adding a module.
- `get_providers(preset) -> {"embedding": Provider, "llm": Provider}` resolves each
  side's `provider.id` (default `generic`), runs the load-time validation of A1,
  validates `options` with the class's `Options` model, and caches the instances
  with the preset (presets are already cached per process; see the `presets.py`
  module docstring).
- Heavy or optional dependencies (an SDK) are imported lazily inside the provider
  module, so a deployment that does not use that provider never needs them.

### A4. Core call sites become provider calls

Wherever core used to look at the preset's provider, it now asks the provider of
the relevant side.

| Today | After |
|---|---|
| `_check_side()` looks up `HEALTH_CHECKS[provider]` and reads shared values | `providers[side].health(creds)` for the **caller's effective preset** (A11) with the key's owner's credentials (the caller's header key for `user`, the stored admin key for `managed` and `shared`); the URL comes from the preset, `endpoint_url()` or the shared store; `None` stays `null` in the response, a missing key or endpoint reads "not configured" / "not provisioned" |
| `provisionable = bool(preset.provisioning_script)` | Removed; the plugin reads `supports_provisioning` per side from the descriptors (A7) |
| `POST /api/config/provision` spawns `sys.executable script --json`, admin only, one global job | One job runs `provision()` for each requested provisioning-capable side in turn (embedding, then LLM) via `asyncio.to_thread`. `user` scope: any authenticated user, one job slot per caller, on the caller's own account. `managed` scope: admin only (`require_authorized_group_admin`), one global job slot, on the admin's account. `shared` scope: nothing to provision. Same job states (`idle`/`running`/`succeeded`/`failed`) and 409/400 behaviour; the job fails when `ctx.deadline` passes |
| Request body `{api_key}` | `{ "keys": { "<credential env>": "<one-time value>" }, "sides": ["llm"] }`; both fields optional. Without `keys` the caller's own service key is used; without `sides` every provisioning-capable side runs |
| Job state is `{status, message, started_at, finished_at}` | Adds `progress: list[str]` (bounded, newest last, each line prefixed with its side) and `sides: {embedding?: {status, message}, llm?: {status, message}}`, so a failed side can be retried alone with `sides: [that side]` |
| Result parsed from a `PROVISION_RESULT:` stdout line, then `update_remote_config()` | Providers that derive the URL from the key (RunPod, HF) return nothing to store, and the endpoint-URL cache entry is invalidated; a provider that cannot (a hypothetical one) returns `{shared_base_url_env: url}`, applied via `update_remote_config()` as soon as that side succeeds |
| `_provisioning_key()` reads the key pattern from `model_kwargs`; a supplied key is kept as the shared key when none is stored | Pattern comes from `apply_defaults()`; a supplied key is used for this run only and never stored by the backend |
| `_merge` skips `shared_base_url` keys when `provisioning_script` is set | Shared URL and key fields are reported for `managed` and `shared` scopes; a `managed` side whose provider derives its URL reports only the key field |
| `get_config()` and `get_models_status()` call `fetch_kisski_rag_models()` when `models_status_url` is set | Call `providers["llm"].live_models(base_url, api_key)`; `None` means use the preset's static `model_names` and return an empty status list, as today |
| `query.py` accepts an unlisted `llm_model` only when `models_status_url` is set | Accepts it when the LLM provider supports `live_models` (the provider's list is authoritative) |
| `GET /api/rate-limits` returns raw captured headers | Returns `meters` from each side's `parse_usage()` (A9) instead of raw headers |
| `docs_url_for_key()` reads a shared table; `required_client_fields()` defaults to `OPENAI_API_KEY`, or `ANTHROPIC_API_KEY` by sniffing the model name | `provider.default_key_env()` and `provider.key_docs_url(env_var)` of that side; the table, the model-name sniffing and `_KNOWN_HEADERS` are deleted (the computed header name is equivalent, since header names are case-insensitive) |
| `LLMService` routes to the Anthropic SDK when the model name contains "claude" or "anthropic" | Routes on the LLM provider's `llm_api`; core implements the two wire protocols and knows no vendor |
| Services take the base URL from `base_url` or `shared_base_url_env` | Also from `provider.endpoint_url(key)` when the side has neither; no URL means a clear "not provisioned" error with the provider's hint |
| "Endpoint unavailable" messages mention RunPod | Neutral core text plus that side's `provider.unavailable_hint()` |
| (new) | `POST /api/config/suspend`, same authentication rules as provisioning, runs `provider.suspend()` for the requested sides (default: every side that supports it) through the caller's job slot (409 while another of the caller's jobs runs); resume is `POST /api/config/provision` (A8) |

`embeddings.py` and `llm.py` otherwise keep reading `model_kwargs`. Their
cold-endpoint branches also ask `provider.classify_http_error()` instead of matching
a RunPod-specific response.

### A5. CLI

A generic `bin/provision.py [--preset NAME] [--side embedding|llm|both] [--pause]
[--teardown] [--recreate] [--yes] [--skip-warmup]` drives every side whose
provider supports provisioning. It replaces the per-provider script as the manual
entry point and is subject to the same "run inside the container so the data
volume is shared" rule from CLAUDE.md. Like every tool in `bin/` it does not let
environment variables supply or override provider keys: the key comes from a stdin
prompt or from the auto-index key store (user scope) or the shared store (managed
scope), and never from argv, so it cannot leak through `ps`.

The scripts in `scripts/` are developer and maintainer tooling and are not covered
by this rule: they may keep taking keys from CLI parameters or environment
variables, as they do today.

### A6. Provider contract tests

One test module parametrised over every registered provider and each side it
supports:

- `Options` validation (valid, invalid, defaults).
- `apply_defaults()` is idempotent and does not clobber explicit preset values.
- `describe()` returns a well-formed descriptor with no secret material, including
  its effective scope and allowed scopes.
- `health()` classification against a fake client, never raising.
- `endpoint_url()`, when supported, returns the URL of an existing endpoint, `None`
  for an absent one, and never raises.
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
- Using a provider on a side outside its `supported_sides`, or with key kinds that
  do not match its effective scope, is rejected at load.

A new provider must pass this suite. That is the definition of "easy to add".

### A7. Client UI: provider-agnostic provisioning in Preferences

Today the "Active Model Preset" group in `preferences.xhtml` has fixed markup for
provisioning: a single optional one-time key field, one fixed help sentence, and a
"Provisioning…" label with no detail while the job runs. `preferences.js` already
does the generic parts correctly (health rows per side, colour by status, button
shown only when a side is `unreachable`/`throttled` or a job runs, polling status
every 5 s). What it cannot do is describe a provider: what credential to ask for,
what that credential is for, how long it takes, or what step the job is on. With
providers per side it must also cope with two different providers in one preset.

**Principle:** the plugin never contains a provider name or provider-specific text.
Everything provider-specific arrives as data from the backend.

**Backend contract.** New read-only `GET /api/config/providers` (no admin gate, no
secrets, same posture as `GET /api/config`) returns one descriptor per side of the
active preset:

```json
{
  "embedding": {
    "id": "huggingface",
    "label": "Hugging Face",
    "key_scope": "user",
    "operable_by_caller": true,
    "supports_provisioning": true,
    "supports_suspend": true,
    "provisioning": {
      "credential": {
        "env": "HF_TOKEN",
        "label": "Hugging Face token",
        "help": "Creating endpoints needs a token with write access to Inference Endpoints. Leave empty to use your saved token; a value entered here is used for this run only.",
        "pattern": "^hf_[A-Za-z0-9]+$",
        "optional": true
      },
      "hint": "A first start can take several minutes while the model loads."
    },
    "unavailable_hint": null
  },
  "llm": {
    "id": "anthropic",
    "label": "Anthropic",
    "key_scope": "user",
    "operable_by_caller": false,
    "supports_provisioning": false,
    "supports_suspend": false,
    "provisioning": null,
    "unavailable_hint": null
  }
}
```

`key_scope` is the effective scope, and `operable_by_caller` is computed by the
backend for the requesting user: true for a `user` scope side, true for a
`managed` side only when the caller is an admin, false for `shared`. The plugin
never decides who may operate a resource; it renders what the backend says.

`GET /api/config/provision/status` returns the caller's job: `status`, `message`,
`progress`, and the per-side `sides` results (A4). There is no `provisionable` flag
any more; the plugin derives it from the descriptors. The plugin renders one
configuration section per side from these descriptors.

**Plugin behaviour.** All in `plugin/src/preferences.js`, built dynamically like
`renderServiceApiKeyFields`, replacing the fixed provisioning rows in the xhtml.
The "Active Model Preset" group keeps only the preset dropdown, its description and
the preset status line. Below it, **each side gets its own configuration section**
(a fieldset titled "Embedding" and one titled "LLM"), rendered from that side's
descriptor. The two sections have identical structure, so a mixed preset (for
example HF embeddings and an Anthropic LLM) simply shows two different sections,
and a side with a `generic` provider shows a minimal one.

Each section contains, top to bottom:

| Element | Source | Notes |
|---|---|---|
| Heading and provider | side name and `descriptor.label` | "Embedding: Hugging Face"; for a local model "Embedding: local model" with no further controls |
| Health row | `GET /api/config/health`, this side's entry | Unchanged colours and statuses; omitted when the side has no health concept; shown to every user, and for a `managed` side it is the only thing a non-admin sees, with the line "Managed by your administrator" |
| Hint under a side that is not ready | `descriptor.unavailable_hint` | For providers that cannot provision (for example MPCDF: start a new job and paste its URL and key) |
| Service key field(s) | `GET /api/required-keys`, entries for this side | `user` scope: the user's personal key (stored in the plugin's preferences, sent as a header, as `KISSKI_API_KEY` is today). `managed` and `shared` scope: the shared key (and URL where needed), admin only; other users see no field. If both sides use the same key env var they show the same preference, so editing it in one section updates the other |
| Provision credential | `descriptor.provisioning.credential` | Only when the side can provision. Label, help and placeholder from the descriptor; pattern checked client-side, enforced server-side regardless. Left empty, the user's saved service key is used; a value entered here is used for this run only, is never stored by the backend, and the plugin does not offer to save it as the service key |
| Provision / Resume button | `operable_by_caller`, `supports_provisioning` and health | "Provision" when the side is `unreachable`/`throttled`, "Resume" when `paused`; shown only when `operable_by_caller`. Posts `sides: [this side]` |
| Pause button | `operable_by_caller`, `supports_suspend` and health | Shown whenever the side is `ready` or `cold`, not only when something is wrong; one click, no confirmation (reversible) |
| Retry button | this side's entry in `status.sides` | Appears after a failed job on this side and posts `sides: [this side]` again |
| Hint line | `descriptor.provisioning.hint` | Shown while idle and while this side's job runs |
| Progress and result | `status.progress` filtered to this side, `status.sides[side]` | Newest line shown live, whole list as a tooltip; the result or failure reason is shown inside the section it belongs to |
| Paused notice | health `paused` | Row text "paused, no cost, will not wake on requests", in a neutral colour (not red) |
| Usage bars | `GET /api/rate-limits` meters for this side | The existing rate-limit widget, shown in the section it belongs to (A9) |

Rules across the two sections:

- Actions are per side. There is no combined "provision everything" button in v1;
  the backend still accepts a request without `sides` (CLI and API use that), and a
  "Provision all" convenience can be added later without any backend change.
- The job slot is per caller (A4), so while one side's job runs the other section's
  action buttons are disabled with the reason "another job is running". Progress
  lines carry their side, so each section shows only its own.
- Resume after reopening Preferences: the pane calls
  `GET /api/config/provision/status` on every refresh and, if a job is `running`,
  resumes polling and shows it in the section of the side it runs on. Today the
  in-memory `provisioning` flag is lost when the pane closes, so a long-running job
  (HF can take minutes) looks idle.
- Not authenticated or not allowed: `POST` returns 401/403, shown inline in the
  section where the click happened.
- The sections are rendered from descriptors only; the plugin contains no provider
  name or provider-specific text.

**Out of scope for v1:** per-provider free-form provisioning parameters entered in
the UI (for example choosing a region at click time), and a teardown button.
Provider-level settings stay in the preset JSON `options`. Teardown stays on the
CLI: for both RunPod and HF an idle, scaled-to-zero endpoint already costs nothing,
so deleting it saves no money, and on RunPod it changes the endpoint ID, which
breaks endpoint-restricted keys. The cost-control action is Pause (A8). The
descriptor can grow a `fields` list later without changing the plugin's structure.

**Tests.** Plugin tests render the two sections from fake descriptor pairs: same
provider on both sides, two different provisioning providers, a provisioning
provider plus a non-provisioning one (HF plus Anthropic), a local side plus a
remote one, and an MPCDF-like non-provisioning pair with `unavailable_hint`; they
assert that no provider name appears in plugin code. A job-resume test starts the
pane while status is `running` and checks the job appears in the right section; a
retry test posts only the failed side; a busy-slot test checks that the other
section's buttons are disabled. Backend tests cover `GET /api/config/providers` for
every registered provider via the contract suite. Pause, Resume and Provision
button visibility is tested for each combination of descriptor flag and per-side
health status.

### A8. Pause and Resume (cost control)

**Why not teardown.** Scale-to-zero already makes an idle endpoint free on both
RunPod and HF, so deleting it saves nothing. What costs money is waking it: any
request starts a worker and bills it for the run plus the idle tail (HF reportedly
about 15 minutes, to be verified). The hourly auto-indexer would wake the
endpoints every hour and keep them effectively always on, and a stray query, a
retry loop, or a misconfigured minimum replica count does the same. Pause is a
reversible switch that blocks every wake-up until the owner resumes.

**Semantics.** `suspend()` stops all billing for its side, keeps the resources and
their URLs, and is idempotent. Sides are independent: each side's provider decides
whether it can pause, and the API takes an optional list of sides (default: every
side that can). Resume is the existing idempotent `provision()`, which restores the
configured scaling and wakes the endpoint. The paused state is not stored by the
backend: `health()` derives it from the provider (A2), so a pause made from the CLI
or the provider's own console is seen the same way. Under `user`
scope the endpoints belong to the caller's own account (A10), so pause and resume
act on the caller's endpoints only; under `managed` scope they act on the shared
endpoints for everyone, and only an admin may do it.

| Provider | Pause | Resume |
|---|---|---|
| HF | `endpoint.pause()` (not billed, does not auto-wake) | `provision()` calls `.resume()` |
| RunPod | no native pause; set `workersMax` to 0 so no worker can start, and optionally purge queued jobs (whether the REST update accepts 0 must be verified) | `provision()` restores `workersMax` from `options` |
| Others | not supported | not applicable |

**Who respects the paused state.** All of these must avoid waking a paused
endpoint and report why, instead of failing obscurely or hanging. They check only
the sides they use, and for the user whose key they use:

- The auto-index scheduler and `bin/index_libraries.py` (the cron path): skip a
  user's libraries when that user's **embedding** side is `paused`, with a reason
  such as "Embedding endpoint is paused; resume it in Preferences" surfaced
  through the existing per-library auto-index status reasons. This is the main
  reason the feature exists. A paused LLM side does not block indexing.
- On-demand and deferred server-side indexing: same check, same reason.
- Interactive queries: `RemoteEmbeddingService` and the LLM service each consult a
  short-TTL cached `health()` (about 30 seconds) of their own side, for the
  requesting user, when that provider `supports_suspend`, and raise the existing
  "endpoint unavailable" error with a paused-specific message. This matters on
  RunPod, where a request to an endpoint with `workersMax=0` would otherwise queue
  and time out. `classify_http_error()` may return `paused` for providers whose
  paused endpoints answer with a recognisable error (HF).
- Health and the plugin: `paused` is shown in a neutral colour and offers Resume
  (A7).

**Access and concurrency.** `POST /api/config/suspend` follows the provisioning
rules (A4): any authenticated user for `user` scope with one job slot per caller,
an admin for `managed` scope with one global slot, and 409 while a job in the
relevant slot runs. It returns 400 when none of the
requested sides supports suspend.

**Tests.** Contract tests (A6); API tests for authentication, 409 and 400;
scheduler tests that a paused embedding side is skipped with the reason while a
paused LLM side is not, and that one user's pause does not affect another user's
libraries; query-path tests that a paused state yields the paused message without
a network call to the inference URL.

### A9. Usage meters (rate limits) per provider

Billing and spend are out of scope: cost stays a matter of inspecting the
provider's own dashboard. This section only generalises the quota display that
exists today for KISSKI's rate limits.

**Today.** Capture is already header-driven and vendor-neutral (any
`x-ratelimit*` / `ratelimit*` header from an embedding response, plus the
`retry-after` handling and the per-key `rate_limited` status), but display is not:
the plugin widget reads only KISSKI's `hour` and `day` header names. Capture also
covers the embedding side only and is one process-wide snapshot.

**Contract.** Each side's provider turns captured headers into a list of meters.
The plugin renders whatever it receives:

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

- `GET /api/rate-limits` returns `meters` only (the raw header dict is dropped; the
  plugin is the only consumer). The widget draws one bar per meter with the
  existing 75% amber and 95% red thresholds and the label "`remaining` `unit`
  left/`period`", and shows nothing when there are no meters.
- `GenericProvider.parse_usage()` understands the two standard dialects, so any
  OpenAI-compatible service gets bars with no provider class:
  - OpenAI-style: `x-ratelimit-limit-requests` / `-remaining-requests` /
    `-reset-requests`, and the same with `-tokens`. The reset value is a duration
    string such as `6m0s` or `1s`; it fills `resets_at`, and the period stays
    unknown unless a header states it.
  - IETF `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` (seconds), and
    the optional `RateLimit-Policy` window (`w=` seconds), which gives the period.
  A header set that matches neither yields no meters, never an error.
- `KisskiProvider` extends it with KISSKI's own `x-ratelimit-{limit,remaining}-
  {hour,day}` names (B2a), calling `super()` for the standard dialects.
- The snapshot becomes per side and per key fingerprint (quotas are per user under
  A10), and the LLM service uses the same capture helper. The cron status file
  persists the same per-side data. This also fixes the case where one user's key
  quota would be shown to another user.
- Retry-after handling and `EmbeddingRateLimitExhaustedError` stay as they are:
  they key off the OpenAI client's `RateLimitError`, not a provider.

### A10. Credential scope: who supplies a key, and where keys live

**Rule.** The key behind any cost or rate limit belongs to whoever pays. There are
three kinds, and a provider class declares which ones it allows (`key_scopes`,
`default_scope`). The preset may pick one of the allowed kinds with
`provider.scope`; the default is the class default, which is `user` for everything
that costs money. Choosing `managed` or `shared` is therefore always a deliberate
line in the preset, never an accident.

| `scope` | Who supplies the key | Costs | Who provisions, pauses, resumes | Where the key lives |
|---|---|---|---|---|
| `user` | Each user, their own | Theirs | The user, on their own account | The user's Zotero preferences (sent as a request header); for auto-indexing, the encrypted auto-index key store, as for KISSKI today |
| `managed` | The admin, once | An institution pays | The admin only; users just use it | `admin_settings.json`, Fernet-encrypted by `backend/services/secret_store.py`, set through `POST /api/config/remote-fields` |
| `shared` | The admin, once | Nothing (institutionally provided) | Nobody; the admin pastes a URL and key, as with MPCDF | Same as `managed` |

Allowed scopes per class: `kisski` `{user}`; `mpcdf` `{shared}`; `openai`,
`anthropic`, `runpod` and `huggingface` `{user, managed}`; `generic` all three.
Local sides need no key and have no scope. A self-hosted or institutional
OpenAI-compatible server the admin wants to configure once uses `generic` with
`"scope": "shared"`.

**No keys in `.env` or the environment for the app.** The main app and the tools in
`bin/` never read provider credentials from `.env` or from the process environment,
for any provider and any scope. Every site that currently falls back to
`os.environ` for an API key or endpoint URL loses that fallback:
`admin_settings_store.resolve_shared_value`, the remote embedding and LLM
services, `dependencies.get_client_api_keys`, `Settings.get_api_key`, the
"is it set" checks in `api/config.py`, and the key-name defaults in `.env.dist`
and the deploy env files. `.env` keeps non-credential settings and
`AUTOINDEX_SECRET`, which is the encryption secret for the stores, not a provider
key. The scripts in `scripts/` are developer tooling and may keep using CLI
parameters or environment variables for keys. `docs/presets.md`, `CLAUDE.md` and
`.env.dist` are updated to say so.

**Endpoints and their URLs.** For providers that provision (RunPod, HF), the
endpoints live in the key owner's own provider account (the user's under `user`, the
admin's under `managed`), named `zotero-rag-embedding` and `zotero-rag-llm`. Nothing
stores the endpoint URL: `endpoint_url(api_key)` finds the endpoint by name with the
owner's key. Core caches the answer in memory, keyed by provider, side and key
fingerprint, with a short TTL, and drops the entry on provision, suspend, teardown,
or a connection failure. The preset therefore has no `*_BASE_URL` fields for these
providers, only the key field. The embedding model is still fixed by the preset, so
all users share one vector space and one vector database whichever scope or
account serves them; only the compute behind it differs.

**Consequences.**

- Under `user`, provisioning, health, pause and resume are per user and need only a
  valid user identity. Under `managed` they are admin-only and affect everyone, and
  non-admins see health but no controls (A7). `shared` has nothing to operate.
- Job slots follow the owner: per caller for `user`, one global slot for `managed`.
- Auto-indexing and the cron indexer resolve the endpoint from the key that owns it
  (each user's stored key, or the admin-set key) and gate on that owner's health
  (A8).
- Usage meters and rate-limit state are keyed by key fingerprint, so a `managed` or
  `shared` key shows one pool for everyone and a `user` key shows that user's own.
- Under `user` the admin pays for and holds no one's compute; under `managed` the
  institution pays through the admin's account. Spend is inspected on the provider's
  dashboard in both cases.
- Many users each running their own GPU endpoints is more total infrastructure than
  one shared pair. That is the intended cost model for `user`, and it makes Pause
  (A8) more important.

### A11. Default preset and per-user preset choice

Today the active preset is global: `POST /api/config` switches it for every caller
and for the cron indexer, and only an admin may do that. With per-user credentials
a user can reasonably want something different from the server's default (for
example their own paid provider for speed) while sharing the same index.

**Server default.** The admin designates one preset as the default. It is stored in
`admin_settings.json` as `default_preset`, set through the existing admin-only
`POST /api/config` (whose meaning becomes "set the server default"). Resolution
order: the stored value, then the `MODEL_PRESET` setting, then the built-in
`remote-kisski`, so a fresh install works with the rate-limited KISSKI service. A
user who installs the plugin and connects is on the default with nothing to
choose; the setup wizard asks only for the keys the default needs (for KISSKI, the
user's own key).

**Personal choice.** A user may select another preset for themselves, from the
presets compatible with the default's embedding model (the existing
`_compatible_presets` rule, kept strict: both sides remote and the same embedding
model identity, so the one vector store stays valid), and whose credentials they
can supply. Relaxing the rule, for example to let a user change only the LLM
provider while keeping a local embedding model, is deliberately left for later. It is stored server-side per
user identity, not in a header, because the cron indexer and auto-index need it
too: `GET` and `PUT /api/config/my-preset`, keyed by the Zotero identity
fingerprint in a small `user_settings.json` (a preset name is not secret). In
loopback and public-query modes there is no identity, so only the default preset
applies and the personal-choice control is not shown. A
choice that later becomes invalid (the preset was removed, or it stopped being
compatible after the default changed) falls back to the default, and
`GET /api/config` says so.

**Effective preset.** Every request path that today calls
`settings.get_hardware_preset()` resolves `get_effective_preset(identity)`: the
user's choice if valid, else the default. Consequences:

- Health, provisioning, pause, usage meters and key requirements all describe the
  caller's effective preset, so the two per-side Preferences sections (A7) show the
  provider the user actually uses.
- The vector store is untouched: compatibility guarantees the same embedding model.
- The cron indexer and on-demand indexing use each user's effective preset, hence
  that user's embedding provider and key. The auto-index key store therefore holds
  keys per env var name per user (a map), rather than a single embedding key, so a
  user can have a KISSKI key and an HF token at once. Keys for presets the user no
  longer selects are kept (the user may switch back); only keys found permanently
  invalid are pruned, as today.
- `switchable_presets` becomes a per-user `selectable_presets` list (compatible,
  and usable for that user: keys present or, for `managed`, an admin-provisioned
  resource that is ready).

**Typical journeys.**

- *Free tier, then upgrade.* The default is `remote-kisski`. A user adds their HF
  token under the `huggingface` preset (`user` scope), provisions their own
  endpoints from their Embedding and LLM sections, and selects that preset for
  themselves. Their queries and indexing now use their own endpoints; everyone else
  is unaffected.
- *Institution-funded GPUs.* The admin makes a `managed` preset the default,
  provisions it once, and every user uses it with no key of their own.
- *Mixed.* A preset with a `shared` or `managed` embedding side and a `user` LLM
  side, for example institutional embeddings and each user's own Anthropic key.

**UI.** The "Active Model Preset" group becomes "Model preset" with a dropdown for
the user's own choice (the default is marked "(server default)"), and, for admins
only, a second control "Server default preset". Loopback and single-user
deployments show one control. Switching either triggers the same refresh of the
per-side sections that a preset change triggers today.

---

## B. Migrate existing presets

Which provider each bundled preset's sides get (an omitted `provider` block means
`generic`), and who supplies the key:

| Preset | Embedding | LLM | Key scope |
|---|---|---|---|
| `cpu-only`, `high-memory`, `apple-silicon-32gb` | generic (local) | generic (local) | none |
| `remote-kisski`, `apple-silicon-kisski`, `windows-test` | kisski | kisski | user (`remote-kisski` is the built-in server default, A11) |
| `cloud-server-kisski` | generic (local) | kisski | user (LLM) |
| `remote-mpcdf` | mpcdf | mpcdf | shared |
| `remote-openai` | openai | openai | user |
| `runpod` | runpod | runpod | user (a custom copy can set `"scope": "managed"`) |
| `huggingface` (new, Part C) | huggingface | huggingface | user (a custom copy can set `"scope": "managed"`) |

### B1. `GenericProvider` (any OpenAI-compatible API)

`GenericProvider` is the base class and is documented in `docs/presets.md` as the
provider for any OpenAI-compatible HTTP API, and as the provider of every local
side. The presets that use it everywhere only gain `"version": 2`.

- `GET /api/config/health` returns `null` for its sides.
- No provisioning panel and no provider line in the plugin.
- URL and key handling is `model_kwargs`-driven as before, with the key coming from
  the user (A10).
- No live model list and no demand indicator.
- New for sides on generic, and additive: rate-limit meters appear whenever the
  service sends OpenAI-style or IETF headers (A9). Nothing is removed.

### B2. Vendor providers

#### B2a. `KisskiProvider` (`backend/providers/kisski.py`)

Applies per side to `remote-kisski`, `apple-silicon-kisski`, `windows-test`
(both sides) and `cloud-server-kisski` (LLM side only; its local embedding side
stays generic). `key_scopes = {"user"}`, which is how KISSKI already works: rate
limits and keys are per user.

Everything KISSKI-specific moves here, and nothing is left behind in generic code:

| Moves from | To | Notes |
|---|---|---|
| `backend/utils/kisski.py` (`fetch_kisski_rag_models`, RAG-suitability filter, `coder`/`devstral` exclusion, demand to availability labels) | `KisskiProvider.live_models()` (LLM side) | The file is deleted; its tests move with it |
| `LLMConfig.models_status_url` in four preset files | `options.models_url` (optional, default `{llm base_url}/models`) | The schema field is removed (A1) |
| `"KISSKI_API_KEY": "https://saia.gwdg.de/dashboard"` in `_KNOWN_DOCS_URLS` | `KisskiProvider.key_docs_url()` and `default_key_env()` | The `KISSKI_API_KEY` entry in `_KNOWN_HEADERS` is dropped; the computed header name is equivalent |
| KISSKI `hour`/`day` header parsing in `rate-limit-widget.js` | `KisskiProvider.parse_usage()` (extends the generic parser) | The widget becomes provider-agnostic (A9) |
| `get_models_status()` demand labels (`available`, `busy`, `very busy`) | `KisskiProvider.live_models()` fills `demand` and `availability` | `GET /api/models/status` is a thin wrapper over the provider |

What deliberately stays out of the provider: `extra_body` (for example
`enable_thinking: false`) and `base_url`/`api_key_env` remain plain preset data in
`model_kwargs`, because they are generic OpenAI-compatible-client settings.

Behaviour preservation is checked for these presets (B6): the live ordered model
list in `GET /api/config`, the demand dots from `GET /api/models/status`, the
hour and day bars, and the Preferences key link.

A custom preset copied from a KISSKI one and left without `provider` blocks loses
those four features. The version warning in B4 names the removed
`models_status_url` key and says to add `"provider": {"id": "kisski"}` on the
sides concerned.

#### B2b. `OpenAIProvider` (`backend/providers/openai.py`)

Applies to both sides of `remote-openai`; `key_scopes = {"user", "managed"}`,
default `user`. It subclasses
`GenericProvider` and adds only `default_key_env()` (`OPENAI_API_KEY`) and
`key_docs_url()` (the OpenAI key page). The OpenAI embedding sentinel
`model_name: "openai"` and the matching entries in the known-dimensions table stay
model data in core, keyed by model name, not by provider. The `remote-openai`
description loses "Anthropic".

#### B2c. `AnthropicProvider` (`backend/providers/anthropic.py`)

`supported_sides = {"llm"}`: Anthropic has no embeddings API, so it is meant to be
combined with another provider's embedding side. `key_scopes = {"user",
"managed"}`, default `user`. Sets
`llm_api = "anthropic"`, `default_key_env()` (`ANTHROPIC_API_KEY`) and
`key_docs_url()`. This replaces sniffing "claude" or "anthropic" in the model
name, so the LLM service no longer contains a vendor check, only the two
wire-protocol implementations. No bundled preset uses it; the mixed presets in A1
are the intended use.

Consequence for existing files: a custom preset that relied on a Claude model name
being auto-routed now goes through the OpenAI protocol and fails. The preset
loader (B4) therefore logs a WARNING for a custom preset whose LLM model name
contains "claude" but whose LLM provider is not `anthropic`, naming the fix.

#### B2d. `MpcdfProvider` (`backend/providers/mpcdf.py`)

Applies to both sides of `remote-mpcdf`; **`key_scopes = {"shared"}`**, because the
service is institutionally provided and costs the users nothing, so the admin sets
the job URL and key once for everyone (`shared_base_url_env`, `shared_api_key_env`,
`POST /api/config/remote-fields`, encrypted storage, admin only). This is the one
bundled provider that keeps those fields.

The MPCDF LLM Inference Service has no MPCDF-specific code in the backend today:
the shared-field mechanism, `normalize_base_url` appending `/v1`, and the handling
of a vanished endpoint (HTTP 404/405) are all generic. What is specific is that
each endpoint is an ephemeral Slurm job of up to 8 hours that is started by hand in
the service's web UI. The provider turns that knowledge into behaviour, with no
core change:

- **`health()`:** a cheap authenticated `GET {base_url}/models` (the endpoints are
  vLLM). 200 is `ready`; 401 or 403 is `unreachable` with "key rejected"; 404, 405,
  a timeout or a connection error is `unreachable` with "job expired or not
  started". It never reports `cold`, since a job either runs or does not. An expired
  job now shows in Preferences instead of failing an indexing run.
- **`unavailable_hint()`:** "Start a new job in the MPCDF LLM Inference Service and
  paste its URL and key into Service API Keys." Shown in Preferences under a side
  that is not ready, and appended to the "endpoint unavailable" error.
- **`key_docs_url()`:** the service's web UI address, for the shared URL and key
  fields.
- `supports_provisioning` stays `False`: jobs are started by hand, and no job API is
  assumed. If MPCDF ever offers one, provisioning would be added to this class only.

Whether the service answers `/v1/models` without extra permissions must be checked
against a live job.

### B3. `RunPodProvider` (`backend/providers/runpod.py`)

Applies to both sides of `runpod`; `key_scopes = {"user", "managed"}`, default
`user`. Each side is its own
provider instance and owns one template and one endpoint in the user's RunPod
account, as the script already does per endpoint. Move, with no behaviour change
apart from the credential model:

- `check_runpod_health()` from `backend/utils/endpoint_health.py` becomes
  `RunPodProvider.health()`.
- The ensure-template, ensure-endpoint, warm-up and teardown logic from
  `bin/provision_runpod_endpoints.py` (~600 lines, `httpx` REST) becomes
  `provision()` / `teardown()`, with progress steps added ("Creating endpoint",
  "Waiting for warm-up", and so on). The served model is the side's own model name
  from the preset, so the former `--llm-model` option is no longer needed.
- New: `endpoint_url(api_key)` finds the endpoint by name in `GET /v1/endpoints` and
  returns `https://api.runpod.ai/v2/<id>/openai/v1`. The preset's
  `RUNPOD_EMBEDDING_BASE_URL` / `RUNPOD_LLM_BASE_URL` / shared-key fields disappear;
  `provision()` returns `{}`.
- The key regex (`^rpa_[A-Za-z0-9]+$`) moves into `apply_defaults()`, which writes
  it into `model_kwargs` as `api_key_pattern`. `runpod.json` stops repeating it.
- RunPod's gateway cold response (the openresty 405 page the services special-case
  today) becomes `classify_http_error()`, and the RunPod wording in the
  "endpoint unavailable" messages becomes `unavailable_hint()`.
- New: `supports_suspend = True`. `suspend()` sets the endpoint's `workersMax` to 0
  (and purges the queue); `health()` reports `paused` when `workersMax` is 0;
  `provision()` restores `workersMax` from `options`. The `GET /v1/endpoints`
  response already used for lookup carries `workersMax`, so no extra call is
  needed. Whether the REST update accepts 0 is the one thing to verify first; if it
  does not, the fallback is to lower `workersMax` to the minimum allowed and keep
  `workersMin=0`, which stops idle cost but cannot block a wake-up, and the spec
  then marks RunPod as `supports_suspend = False`.
- The provisioning panel text that is RunPod-specific in effect today ("a key with
  broader rights than the one used for queries") moves into `describe()`.

`Options` replaces the CLI flags (the CLI can still override them), per side:

```json
"embedding": { "...": "...", "model_kwargs": { "api_key_env": "RUNPOD_API_KEY" },
  "provider": { "id": "runpod",
  "options": { "gpu": "NVIDIA RTX A5000", "workers_max": 1, "idle_timeout": 60, "data_centers": null } } },
"llm":       { "...": "...", "model_kwargs": { "api_key_env": "RUNPOD_API_KEY" },
  "provider": { "id": "runpod",
  "options": { "gpu": "NVIDIA RTX A5000", "workers_max": 1, "idle_timeout": 60, "data_centers": null } } }
```

Both sides use `RUNPOD_API_KEY`, so the plugin shows one credential field for the
pair (A7). An institution-funded variant is a custom copy with
`"scope": "managed"` in both `provider` blocks and `shared_api_key_env:
"RUNPOD_API_KEY"` instead of `api_key_env`; the admin then provisions once from the
Preferences sections and users see health only. `runpod.json` after migration: `"version": 2`; drops
`health_check_provider` (x2), `provisioning_script`, all `shared_*` entries and the
key patterns; gains `api_key_env` and the two `provider` blocks above.

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
  docs/presets.md." The preset still loads, unless it breaks a rule in A1, in which
  case it is rejected with that rule named.
- If its LLM model name contains "claude" and its LLM provider is not `anthropic`,
  the same warning pass says so, because model-name routing to Anthropic was removed
  (B2c).
- If it contains keys removed in a later version (`health_check_provider`,
  `provisioning_script`, `models_status_url`), the warning names them and says they
  are ignored. The list of removed keys per version is a small table in
  `presets.py`, extended when a future version removes more.
- A custom copy of the old RunPod preset keeps working as a plain, non-provisioning
  remote preset only if it still passes validation; its old shared-key fields do
  not match the `user` key scope of the new providers, so adding the per-side
  `provider` blocks and switching to `api_key_env` is required, and the rejection
  message says so.

### B5. Implementation order (each step keeps the suite green)

1. Add `backend/providers/` (base, registry, `GenericProvider`), `version` and the
   per-side `provider` field on the schema, the A1 load-time validation, the B4
   seeding and version warnings, and bump every bundled preset to `"version": 2`.
   Core call sites in A4 switch to the per-side providers. Behaviour is identical
   for every preset except that `runpod` temporarily loses its health and
   provisioning, the KISSKI sides lose the live model list, demand dots, rate-limit
   bars and key link, and `remote-mpcdf` and `remote-openai` lose their (small)
   vendor extras. All of these are ported in step 2, so steps 1 and 2 must land
   together in one change set.
2. Port RunPod into `RunPodProvider` and the vendor behaviour into
   `KisskiProvider`, `OpenAIProvider`, `AnthropicProvider` and `MpcdfProvider` (B2),
   including the generic rate-limit widget and `meters` (A9) and deleting
   `backend/utils/kisski.py`. Delete `HEALTH_CHECKS`, the `Literal` fields,
   `models_status_url`, and the subprocess job path. Keep
   `bin/provision_runpod_endpoints.py` as a thin shim over `bin/provision.py`.
3. Credential model (A10): the three scopes, removal of every `os.environ` / `.env`
   credential fallback from the app and `bin/`, `endpoint_url()` and the per-user endpoint cache, per-user
   job slots and caller-credential health, plus the doc, `.env.dist` and
   `CLAUDE.md` updates. This touches the same call sites as step 1, so it can land
   with it or immediately after; it must land before RunPod and HF are used by more
   than one user.
4. Default preset and per-user choice (A11): `default_preset` setting,
   `get_effective_preset(identity)` on the request paths, `my-preset` endpoints,
   `user_settings.json`, the key store keyed by env var name, per-user cron
   resolution, and the preset-group UI. Needs step 3 first.
5. Backend descriptor endpoint, the multi-side provision job with per-side results
   and retry, and `progress` in job state (A4, A7).
6. Plugin: one dynamic configuration section per side (health, key fields,
   provision, pause, retry, usage bars), job resume, and the plugin tests from A7.
7. Pause and Resume (A8): `suspend()` on the base class and `RunPodProvider`
   (after verifying `workersMax=0`), the `paused` status, `POST /api/config/suspend`,
   the scheduler, indexing and query-path checks, and the two buttons. This can land
   after the HF provider if RunPod's `workersMax=0` check turns out negative, since
   HF alone already supports it.
8. Rewrite `runpod.json`, update `docs/presets.md` (storage rules, version
   changelog, provider ids, key scopes and the per-side `provider` block), and mark
   the older RunPod/health specs as superseded where they describe the script
   contract.

### B6. Behaviour-preservation checklist

For each bundled preset, before and after: `GET /api/config`, `GET
/api/config/health`, `GET /api/required-keys`, `POST /api/config/provision` status
code, and a preset switch. For the KISSKI presets, additionally: the live
ordered `llm_models` in `GET /api/config`, `GET /api/models/status` (demand and
labels), `GET /api/rate-limits` (the hour and day bars render identically from
`meters`), the Preferences key link, and a query that names a model missing from
the static list. For `remote-openai`: the key requirement and its portal link. For
`remote-mpcdf`: the shared fields still save and apply (admin only), and an
unreachable job now reports `unreachable` with the hint. For `runpod`: health
classification, provision end-to-end against a fake client for both sides with a
user's own key, and the plugin's provisioning flow (button, one-time key, polling,
health refresh), unchanged apart from the new progress line, per-side results and
the descriptor-driven text. Deliberately changed: a key present only in the
environment or `.env` is no longer used for any provider.

### B7. Seeding, versioning and schema tests

- A modified bundled file is overwritten and a WARNING naming it is logged.
- An identical bundled file is not rewritten (modification time unchanged).
- A deleted bundled file is recreated.
- A custom file is never touched.
- A custom file with a missing or old `version` warns exactly once per process;
  with the current version it does not warn.
- A custom file containing removed keys names them in the warning and still loads
  when otherwise valid.
- A custom KISSKI-derived file with `models_status_url` and no `provider` warns,
  loads as generic, and does not call the KISSKI endpoint.
- A custom file whose LLM model name contains "claude" and whose LLM provider is not
  `anthropic` warns once and names the fix.
- Mixed presets load: local embedding with an `anthropic` LLM, and `huggingface`
  embedding with an `anthropic` LLM.
- Rejections, each with its rule named and other presets untouched: `anthropic` on
  the embedding side; `shared_*_env` fields with a `user`-scope provider and
  `api_key_env` with a `managed` or `shared` one; a scope the class does not allow
  (for example `managed` on KISSKI, `user` on MPCDF); two sides using one key env var with
  different provider ids; a non-generic provider on a local side; an unknown
  provider id; invalid `options`.
- A provision job on a mixed preset runs only the provisioning-capable side, keeps a
  succeeded side's result when the other fails, and a retry with `sides: [failed]`
  runs only that side.
- Scopes: a `managed` preset can be provisioned, paused and resumed only by an
  admin and reports `operable_by_caller` accordingly; a `user` preset by any
  authenticated user, with separate job slots for two users; `shared` offers no
  controls.
- Default and per-user preset: a new user gets the default; choosing a compatible
  preset changes only that user's requests, health and cron indexing; an
  incompatible or removed choice falls back to the default; two users on different
  presets query the same vector store concurrently; the default resolves from the
  stored value, then `MODEL_PRESET`, then `remote-kisski`.
- Credentials: a key set only in the environment or `.env` is ignored by the
  embedding service, the LLM service, health, models status, the "is it set"
  checks and the `bin/` tools; `AUTOINDEX_SECRET` is still read.

### B8. Vendor provider tests

- Existing `kisski.py` unit tests are ported to `KisskiProvider.live_models()`
  (filtering, ordering, demand labels, network failure returns `None`).
- `parse_usage()` fixtures: the KISSKI hour and day headers, partial headers,
  non-numeric values, and unrelated headers.
- `GenericProvider.parse_usage()` fixtures: OpenAI-style requests and tokens
  headers with duration-string resets, IETF headers with and without
  `RateLimit-Policy`, mixed and malformed values.
- `AnthropicProvider` routes the LLM through the Anthropic protocol, and the LLM
  service contains no model-name check (a test that a Claude-named model on an
  `openai` provider stays on the OpenAI protocol).
- `MpcdfProvider.health()` against a fake client: 200, 401, 404, 405, timeout and
  connection error, with the expected status and detail for each.
- `RunPodProvider.endpoint_url()` and the endpoint-URL cache: found, absent,
  invalidation after provision, and two different keys resolving to two different
  URLs.
- The plugin widget test (`rate-limit-widget.test.js`) is rewritten to feed
  `meters`, with a case for a side that supplies none.

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
nothing requires a plugin change: the UI is driven by the descriptors (A7), which
is the test that the abstraction holds.

### C2. `HuggingFaceProvider` (`backend/providers/huggingface.py`)

Both sides are supported, and either side can be used on its own (for example HF
embeddings with an Anthropic LLM). `key_scopes = {"user", "managed"}`, default
`user`: each user provisions and pays for their own endpoints with their own token;
with `"scope": "managed"` an admin provisions one shared pair funded by the
institution. Each side is its own provider
instance and owns one endpoint.

- **Options (per side):** `namespace` (default: the token's own user), `vendor`,
  `region`, `engine` (`tei` for embeddings, `vllm` or `tgi` for the LLM),
  `instance`, `scale_to_zero_timeout_min`, `min_replica` (0), `max_replica` (1).
- **`apply_defaults()`:** adds the token regex (`^hf_[A-Za-z0-9]+$`) to
  `model_kwargs` as `api_key_pattern`. It must be verified against real tokens.
- **`endpoint_url()`:** looks the endpoint up by name with the user's token and
  returns its URL plus `/v1`, or `None` when absent. No URL is stored (A10).
- **`describe()`:** label "Hugging Face"; a credential (`HF_TOKEN`) description
  saying that creating endpoints needs a token with write access to Inference
  Endpoints and that a billing method must be on the account; a hint that a first
  start can take several minutes.
- **`key_docs_url()`:** returns the HF token page; the `HF_TOKEN` entries in the shared
  header and key-portal tables in `embeddings.py` move here.
- **`provision()`:** idempotent by name (`zotero-rag-embedding` or `zotero-rag-llm`,
  depending on the side). Absent: create. `scaledToZero` or `paused`: resume.
  Existing with a differing config: warn, or recreate with `--recreate`. Then wait
  with a bound, send a warm-up request, and return `{}` (the URL is derived).
  Emits progress steps for each phase (creating, initializing, warming up).
- **`health()`:** one management-API call. `running` maps to `ready`;
  `scaledToZero`, `pending`, `initializing` map to `cold`; `paused` maps to
  `paused`; `failed` or an API error maps to `unreachable`; an absent endpoint is
  `unreachable` with "not provisioned". No data-plane probe is needed, unlike
  RunPod.
- **`classify_http_error()`:** treats the 502 observed during scale-from-zero as
  `cold`, and the error a paused endpoint returns (to be confirmed) as `paused`.
- **`suspend()`:** `supports_suspend = True`; calls `.pause()` on the endpoint.
  Idempotent. `provision()` resumes a paused endpoint, so Resume needs no extra code.
- **`teardown()`:** deletes the endpoint; an absent endpoint is a no-op. CLI only.
- **Dependency:** use `huggingface_hub` if its endpoint API is stable enough to
  justify it, imported lazily inside this module only. Otherwise plain `httpx`
  REST, as RunPod does. Decide in the spike.

### C3. Preset (`backend/config/default_presets/huggingface.json`)

```json
{
  "version": 2,
  "description": "Fully remote via your own Hugging Face Inference Endpoints (embedding + LLM, pay-per-use, scale-to-zero, billed to your own Hugging Face account). Enter your HF token, then click \"Provision endpoints\" to create or wake both endpoints.",
  "embedding": {
    "model_type": "remote",
    "model_name": "intfloat/multilingual-e5-large-instruct",
    "batch_size": 64,
    "model_kwargs": { "api_key_env": "HF_TOKEN" },
    "provider": { "id": "huggingface", "options": {
      "vendor": "aws", "region": "eu-west-1", "engine": "tei",
      "instance": "nvidia-l4", "scale_to_zero_timeout_min": 15 } }
  },
  "llm": {
    "model_type": "remote",
    "model_names": ["Qwen/Qwen2.5-7B-Instruct"],
    "max_context_length": 32768,
    "max_answer_tokens": 2048,
    "temperature": 0.7,
    "model_kwargs": { "api_key_env": "HF_TOKEN" },
    "provider": { "id": "huggingface", "options": {
      "vendor": "aws", "region": "eu-west-1", "engine": "vllm",
      "instance": "nvidia-l4", "scale_to_zero_timeout_min": 15 } }
  },
  "rag": { "top_k": 10, "score_threshold": 0.35, "max_chunk_size": 800 },
  "memory_budget_gb": 0.5,
  "platform": "any"
}
```

Region, instance names and the scale-to-zero timeout are placeholders to confirm
against the live service. Both sides use the user's one `HF_TOKEN`, so the plugin
shows a single credential field.

### C4. Cost note for the preset description

HF bills per instance-hour; indicative AWS single-GPU rates are L4 $0.80/h, A10G
$1.00/h, L40S $1.80/h, A100-80G $2.50/h. RunPod serverless bills per second, so
for bursty use (a few queries a day) it is likely cheaper, and HF's longer idle
tail is the main cost risk. For sustained indexing runs HF's hourly rates are
competitive. These figures come from third-party summaries and must be
re-verified before any number appears in user-facing text. Under A10 the cost lands
on each user's own account.

### C5. Tests

- Provider contract suite (A6) with `HuggingFaceProvider` on each side and a faked
  client. No live calls (endpoints cost money).
- Status-to-health mapping table test; idempotent `provision()` for the
  absent / scaled-to-zero / running / failed starting states; 502 classification;
  `endpoint_url()` found and absent.
- Plugin: the descriptor-driven tests (A7) are extended with the real HF descriptor
  and an HF plus Anthropic pair, with no plugin code change.
- Manual smoke checklist, run once against a real account: provision from
  scratch via the Preferences pane (progress lines visible, closing and reopening
  the pane mid-run resumes), re-run (reuse and wake), scale to zero and wake via a
  query, pause and resume, and teardown via the CLI. Repeat once with the mixed
  HF plus Anthropic preset to confirm only the HF side is provisioned, and once
  with a second user's token to confirm the endpoints are separate.

---

## Open questions

None of these needs a product decision; the decisions taken so far are in the
sections above (including: the strict compatibility rule for personal preset
choice, default-preset-only when there is no identity, keeping keys of unselected
presets, and one-time provisioning keys that are never saved).

### To verify in a spike before the implementation plan

- Pause (A8): does RunPod's REST update accept `workersMax=0`? If not, RunPod
  loses Pause (the spec's fallback). Does a paused HF endpoint answer requests with
  a distinguishable error?
- Per-user endpoint lookup (A10): can an endpoint-restricted RunPod key list
  endpoints? If not, `endpoint_url()` fails for such keys and the key needs list
  permission (or the URL would have to be stored per user).

### To check during implementation

- Does TEI's `/v1/embeddings` accept batched input and echo the model name the way
  `RemoteEmbeddingService` expects?
- What served model name does vLLM on HF Endpoints report (the container loads from
  `/repository`)? The provider may need to supply the on-the-wire model name
  separately from the preset's `model_names`.
- Real default and minimum for HF's scale-to-zero timeout, whether the idle tail is
  really 15 minutes, and whether the reported "stuck in Initializing after wake-up"
  issue needs a resume-and-recheck loop in `provision()`.
- EU region and GPU availability, and whether GDPR hosting should be the default.
- `huggingface_hub` versus plain REST for the HF provider (C2).
- MPCDF (B2d): does a live job answer an authenticated `GET {base}/models`, and does
  a rejected key return 401 or 403? Is there any job API worth building
  provisioning on?
- Job deadline: what is a sane default (HF cold starts may take well over the RunPod
  3-minute warm-up bound), and should it be a provider option?

### Deferred unless a need shows up

- Scheduled automatic pause: scale-to-zero already covers idle time.
- Whether the plugin names the admin on `managed` presets, or "Managed by your
  administrator" is enough.
- Whether "newest progress line plus tooltip" is enough, and whether a descriptor
  `fields` list is the right extension for click-time parameters.
- A one-generation backup of an overwritten bundled preset; the WARNING log line is
  the current decision.
- Relaxing the personal-choice compatibility rule (for example LLM-only changes
  with a local embedding model).
