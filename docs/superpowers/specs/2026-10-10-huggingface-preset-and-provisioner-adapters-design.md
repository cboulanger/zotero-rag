# Provider abstraction layer, RunPod migration, and Hugging Face provider

Status: proposal, not implemented. High level on purpose: it fixes the
architecture, the contracts between the parts, and the decision points, not the
code.

Three parts, to be implemented in order:

- **A. Provider abstraction layer.** A `Provider` base class, an id-based
  registry, and the small core changes that make core provider-blind.
- **B. Migrate existing presets.** RunPod becomes `RunPodProvider`. Every other
  bundled preset runs on a fallback `GenericProvider` that preserves today's
  behaviour exactly.
- **C. Hugging Face.** A new `HuggingFaceProvider` and a `huggingface` preset.
  This is the proof that a new provider is additive.

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
to call). Both were weighed in the previous revision of this document.

## Why this is needed (current coupling in core)

| Coupling | Where |
|---|---|
| `health_check_provider: Literal["runpod"]` on both `EmbeddingConfig` and `LLMConfig` | `backend/config/presets.py` |
| `HEALTH_CHECKS = {"runpod": ...}`, imported by `api/config.py` | `backend/utils/endpoint_health.py` |
| `provisioning_script` path, the `PROVISION_RESULT:` stdout contract, and the `PROVISIONING_API_KEY` env hand-off | `presets.py`, `services/provisioning.py`, `api/config.py` |
| Provider URL and key regexes repeated in each preset's `model_kwargs` | `runpod.json` |
| Cold-endpoint messages special-casing RunPod's gateway | `services/embeddings.py`, `services/llm.py` |
| Shared URL and key lookup pushed through `provisionable` in `_merge` | `api/config.py` |

---

## A. Provider abstraction layer

### A1. Preset schema

`HardwarePreset` gains one optional field and loses three:

```python
class ProviderConfig(BaseModel):
    id: str                       # registry key, e.g. "runpod"
    options: dict = {}            # validated by the provider class, not by core

class HardwarePreset(BaseModel):
    ...
    provider: Optional[ProviderConfig] = None   # None == {"id": "generic"}
```

Removed from the schema: `EmbeddingConfig.health_check_provider`,
`LLMConfig.health_check_provider`, `HardwarePreset.provisioning_script`.
`LLMConfig.models_status_url` (the KISSKI demand indicator) is a different feature
and is not touched.

### A2. The `Provider` base class (`backend/providers/base.py`)

The base class is concrete and implements the generic behaviour. Subclasses
override only what their provider needs, so `GenericProvider` is simply the base
class registered under id `generic`.

```python
class Provider:
    id: ClassVar[str]
    Options: ClassVar[type[BaseModel]] = NoOptions      # validates preset.provider.options
    supports_provisioning: ClassVar[bool] = False

    def __init__(self, preset: HardwarePreset, options: BaseModel): ...

    # Load-time hook: fill provider defaults into the preset's model_kwargs
    # (e.g. URL/key regexes) so embeddings.py / llm.py keep reading model_kwargs only.
    def apply_defaults(self, preset: HardwarePreset) -> None: ...

    # Readiness of one side. None means "this provider has no health concept".
    def health(self, side: Side, base_url: str, api_key: str) -> Health | None: ...

    # Create or wake the remote resources. Returns {shared_base_url_env: url}.
    def provision(self, ctx: ProvisionContext, progress: Callable[[str], None]) -> dict[str, str]:
        raise NotImplementedError

    def teardown(self, ctx: ProvisionContext) -> None:
        raise NotImplementedError

    # Optional: tell the query path that an HTTP error means "endpoint is cold".
    def classify_http_error(self, status: int, body: str) -> Literal["cold"] | None: ...
```

- `Health` keeps the existing shape: `{"status": "ready"|"cold"|"throttled"|"unreachable", "detail": str}`.
- `ProvisionContext` carries the preset, the resolved credentials, the data path,
  and flags (`recreate`, `skip_warmup`). Credentials arrive in-process, so the
  `PROVISIONING_API_KEY` env hand-off disappears.
- Defaults implement `GenericProvider`: `health()` returns `None`,
  `supports_provisioning` is `False`, `classify_http_error()` returns `None`, and
  `apply_defaults()` is a no-op.

### A3. Registry and discovery (`backend/providers/__init__.py`)

- The package imports all of its submodules on first use. Each `Provider`
  subclass registers itself by its `id` (via `__init_subclass__`). There is no
  central list to edit, so adding a provider is adding a module.
- `get_provider(preset) -> Provider` resolves `preset.provider.id` (default
  `generic`), validates `options` with the class's `Options` model, and caches
  the instance with the preset (presets are already cached per process; see the
  `presets.py` module docstring).
- An unknown id or invalid options marks that one preset unavailable with a
  warning in the log and in `GET /api/config` listings. It never fails backend
  startup or the other presets.
- Heavy or optional dependencies (an SDK) are imported lazily inside the provider
  module, so a deployment that does not use that provider never needs them.

### A4. Core call sites become provider calls

| Today | After |
|---|---|
| `_check_side()` looks up `HEALTH_CHECKS[provider]` | `get_provider(preset).health(side, url, key)`; `None` stays `null` in the response, unresolvable URL/key stays "not configured" |
| `provisionable = bool(preset.provisioning_script)` | `provider.supports_provisioning` |
| `POST /api/config/provision` spawns `sys.executable script --json` | `await asyncio.to_thread(provider.provision, ctx, progress)`; same job states (`idle`/`running`/`succeeded`/`failed`), same 409 and 400 behaviour |
| Result parsed from a `PROVISION_RESULT:` stdout line | Returned dict goes straight to `update_remote_config()` |
| `_merge` skips `shared_base_url` keys when `provisioning_script` is set | Skips them when `provider.supports_provisioning` is set |

`embeddings.py` and `llm.py` keep reading `shared_base_url_env` /
`shared_api_key_env` via `resolve_shared_value()`. The only addition is that
their cold-endpoint branches may also ask `provider.classify_http_error()`
instead of matching a RunPod-specific response. The hook is optional, and the
current RunPod detection stays in place until Part B moves it.

### A5. CLI

A generic `bin/provision.py [--preset NAME] [--teardown] [--recreate] [--yes]
[--skip-warmup]` drives any provider that supports provisioning. It replaces the
per-provider script as the manual entry point and is subject to the same
"run inside the container so the data volume is shared" rule from CLAUDE.md.

### A6. Provider contract tests

One test module parametrised over every registered provider:

- `Options` validation (valid, invalid, defaults).
- `apply_defaults()` is idempotent and does not clobber explicit preset values.
- `health()` classification against a fake client, never raising.
- `provision()` idempotency against a fake client (second run creates nothing).
- `teardown()` is a no-op for absent resources.
- Providers with `supports_provisioning = False` raise `NotImplementedError` and
  are never offered a provision button.

A new provider must pass this suite. That is the definition of "easy to add".

---

## B. Migrate existing presets

### B1. `GenericProvider` (every preset except `runpod`)

Applies to `cpu-only`, `high-memory`, `windows-test`, `remote-kisski`,
`remote-mpcdf`, and `remote-openai`. These presets need **no JSON change**:
`provider` is omitted and resolves to `generic`.

The base class defaults reproduce today's behaviour exactly:

- `GET /api/config/health` returns `null` for both sides.
- `provisionable` is `false`, and `POST /api/config/provision` returns 400.
- URL and key handling is untouched: `required_client_fields()` still reads
  `base_url`/`api_key_env` or `shared_*_env` from `model_kwargs`.
- KISSKI's `models_status_url` demand indicator is unaffected.
- `remote-mpcdf` keeps its manual `POST /api/config/remote-fields` workflow. An
  MPCDF Slurm provider is a possible future provider and is out of scope here.

### B2. `RunPodProvider` (`backend/providers/runpod.py`)

Move, with no behaviour change:

- `check_runpod_health()` from `backend/utils/endpoint_health.py` becomes
  `RunPodProvider.health()`.
- The ensure-template, ensure-endpoint, warm-up and teardown logic from
  `bin/provision_runpod_endpoints.py` (~600 lines, `httpx` REST) becomes
  `provision()` / `teardown()`.
- The URL regex (`^https://api\.runpod\.ai/v2/[A-Za-z0-9]+/openai/v1$`) and key
  regex (`^rpa_[A-Za-z0-9]+$`) move into `apply_defaults()`, which writes them
  into `model_kwargs` as `shared_*_pattern`. `runpod.json` stops repeating them,
  and the services still read them from `model_kwargs`.
- RunPod's gateway cold response (the openresty 405 page the services special-case
  today) becomes `classify_http_error()`.

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

`runpod.json` after migration drops `health_check_provider` (x2),
`provisioning_script`, and the four `shared_*_pattern` entries, and gains the
`provider` block above.

### B3. Compatibility for already-seeded files

`data/presets/runpod.json` is seeded once and never overwritten
(`ensure_default_presets`), so admins' existing copies still carry the old fields.
Load-time normalisation handles that:

- `health_check_provider == "runpod"` or `provisioning_script` ending in
  `provision_runpod_endpoints.py` becomes `provider = {"id": "runpod"}`.
- Any other `provisioning_script` value logs a warning and is ignored (no bundled
  preset uses one, and `GenericProvider` does not run scripts).
- The old keys are accepted on input and never written back. The shim is removed
  at 2.0.

### B4. Implementation order (each step keeps the suite green)

1. Add `backend/providers/` (base, registry, `GenericProvider`), the schema field,
   and the B3 normalisation. Core call sites in A4 switch to the provider.
   Behaviour is identical because only `generic` and the shimmed `runpod` exist.
   Tests: existing `test_endpoint_health`, `test_provision_job`, `test_config`,
   `test_embeddings`, `test_llm` pass with only their patch targets updated
   (e.g. `HEALTH_CHECKS`).
2. Port RunPod into `RunPodProvider`. Delete `HEALTH_CHECKS`, the `Literal`
   fields, and the subprocess job path. Keep `bin/provision_runpod_endpoints.py`
   as a thin shim over `bin/provision.py`.
3. Rewrite `runpod.json`, update `docs/presets.md`, and mark the older
   RunPod/health specs as superseded where they describe the script contract.

### B5. Behaviour-preservation checklist

For each bundled preset, before and after: `GET /api/config`, `GET
/api/config/health`, `GET /api/config/api-keys` (the key requirements listing),
`POST /api/config/provision` status code, and a preset switch. For `runpod`:
health classification, provision end-to-end against a fake client, and the
plugin's "Provision endpoints" button flow, unchanged.

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

These all fit behind the A2 interface, which is the point of the exercise. Nothing
here requires a core change.

### C2. `HuggingFaceProvider` (`backend/providers/huggingface.py`)

- **Options:** `namespace` (default: the token's own user), `vendor`, `region`, and
  per side `engine` (`tei` for embeddings, `vllm` or `tgi` for the LLM),
  `instance`, `scale_to_zero_timeout_min`, `min_replica` (0), `max_replica` (1).
- **`apply_defaults()`:** adds a key regex (`^hf_[A-Za-z0-9]+$`) and a base-URL
  regex for the endpoint hostname shape to `model_kwargs`. Both patterns must be
  verified against real endpoints.
- **`provision()`:** idempotent by name (`zotero-rag-embedding`,
  `zotero-rag-llm`). Absent: create. `scaledToZero` or `paused`: resume. Existing
  with a differing config: warn, or recreate with `--recreate`. Then wait with a
  bound, send a warm-up request, and return `{HF_EMBEDDING_BASE_URL: ..., HF_LLM_BASE_URL: ...}`
  (endpoint URL plus `/v1`).
- **`health()`:** one management-API call per side. `running` maps to `ready`;
  `scaledToZero`, `paused`, `pending`, `initializing` map to `cold`; `failed` or an
  API error maps to `unreachable`. No data-plane probe is needed, unlike RunPod.
- **`classify_http_error()`:** treats the 502 observed during scale-from-zero as
  `cold`.
- **`teardown()`:** deletes both endpoints; absent endpoints are a no-op.
- **Dependency:** use `huggingface_hub` if its endpoint API is stable enough to
  justify it, imported lazily inside this module only. Otherwise plain `httpx`
  REST, as RunPod does. Decide in the spike.

### C3. Preset (`backend/config/default_presets/huggingface.json`)

```json
{
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
- Manual smoke checklist, run once against a real account: provision from
  scratch, re-run (reuse and wake), scale to zero and wake via a query, teardown.

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
- Should a provider be able to declare extra Preferences-pane fields (for example
  HF `namespace`) so the plugin can render them without plugin changes, or is
  `options` in the preset JSON enough for now?
- When to remove the B3 compatibility shim: at 2.0, per the repository's
  documentation policy.
