# Hugging Face preset and a provisioner-adapter layer (high-level spec)

Status: proposal, not implemented. High level on purpose: it fixes the
architecture and the decision points, not the code.

## Problem

The `runpod` preset (see `2026-10-09-runpod-preset-design.md` and
`2026-10-09-endpoint-health-provisioning-design.md`) is pure JSON plus one
provider script, but the provider is already hard-wired into core in four places:

| Coupling | Where | Effect |
|---|---|---|
| `health_check_provider: Literal["runpod"]` | `backend/config/presets.py` | adding a provider means editing the schema |
| `HEALTH_CHECKS = {"runpod": ...}` | `backend/utils/endpoint_health.py`, imported by `backend/api/config.py` | adding a provider means editing core |
| `provisioning_script` path + the `PROVISION_RESULT:` stdout contract | `backend/services/provisioning.py` | works for any provider, but is not typed or testable in-process |
| RunPod URL and key shapes (`shared_*_pattern`), model-name basename hack | preset JSON, `_embedding_model_identity` in `api/config.py` | provider quirks repeated per preset |

Hugging Face Inference Endpoints (dedicated) differ from RunPod in ways that make
"copy `runpod.json`" insufficient:

- One object per endpoint (image and env are inline), not template then endpoint.
- Status is a first-class API value (`pending`, `initializing`, `running`,
  `scaledToZero`, `paused`, `failed`), not a `/health` worker count.
- Each endpoint gets its own hostname (`*.endpoints.huggingface.cloud`), not a
  shared `api.runpod.ai/v2/<id>` host. Cold-start behaviour also differs (502
  until a replica is up).
- The control plane and data plane share one HF token, and creation needs a
  namespace (user or org) and a billing method on the account.
- Cost model is hourly with a longer idle tail, versus RunPod's per-second billing
  with a 60s idle timeout.

The task is therefore two things: a `huggingface` preset, and a decision on how
provider-specific logic is isolated so that the next provider is additive.

## Goals

- A `huggingface` preset with embedding (TEI) and LLM (vLLM or TGI) endpoints.
- Adding or swapping a provisioner touches no core file: no schema edit, no
  registry edit in `api/config.py`, no change to `embeddings.py` or `llm.py`.
- User-editable JSON presets under `data/presets/` stay as they are.
- RunPod keeps working with unchanged user-visible behaviour.

## Non-goals

- Automatic provider selection or cost optimisation.
- Third-party plugin distribution (out-of-tree packages). In-tree discovery is
  enough now, and a later entry-points hook is not precluded.
- Query-time cold-start retry policy. This stays as specified in the
  health/provisioning spec, except for the small hook below.

## Options for isolating provider logic

### Option A: presets become classes

A `Preset` subclass per provider (`RunPodPreset`, `HuggingFacePreset`) owns its
config, health check, and provisioning.

- (+) Maximal isolation, since everything for one provider lives in one class.
- (+) Full type safety.
- (-) Presets stop being data. `2026-10-08-file-based-presets-design.md` made
  `data/presets/*.json` admin-editable and seeded from bundled defaults. Classes
  would need a JSON-to-class factory anyway, which is Option B with extra steps.
- (-) A pure-data preset (KISSKI, OpenAI, CPU-only) has nothing to put in a class,
  so there would be two kinds of preset.
- (-) Presets would mix concerns: model and RAG tuning (data) with provider
  lifecycle (code), so the same code is re-instantiated per preset variant.
- (-) Loading code from `data/presets/` is an execution risk. If classes live only
  in-tree, admins lose the ability to define a preset.

Verdict: rejected. The preset's job (what to run, which models, which RAG
parameters) is data and should stay data. Provisioning is behaviour, but it is a
property of the provider, not of the preset.

### Option B: JSON presets plus in-process provisioner adapters (recommended)

Presets stay JSON. They gain one optional block naming a provisioner and passing
it opaque options. A small `Provisioner` protocol is implemented once per
provider as a module under `backend/provisioners/`. Core talks only to the
protocol, and adapters are discovered by scanning the package.

- (+) Presets stay data and stay admin-editable.
- (+) New provider = one file (plus tests and a preset JSON). Core is untouched.
- (+) In-process, so unit-testable with the existing `FakeClient` convention, and
  it can return structured results and progress instead of scraping stdout.
- (+) Subsumes the `HEALTH_CHECKS` registry and the `Literal["runpod"]` schema
  field, so those two core couplings go away.
- (-) Needs a one-time refactor of the RunPod script and health module into an
  adapter.
- (-) Long-running work must not block the event loop. It runs via
  `asyncio.to_thread`, as with other sync I/O in this codebase.

### Option C: keep JSON, generalise the existing script contract (out-of-process)

Keep `provisioning_script` and extend the stdout contract with
`--health --json` and `--teardown`. Each provider is a standalone script.

- (+) Zero core change for a new provider, language-agnostic, and already
  half-built.
- (+) Natural process isolation: a provider crash or a heavy SDK import cannot
  affect the backend.
- (-) Health is called on every Preferences refresh. A subprocess per poll is slow
  and untestable without process mocking.
- (-) Credentials must be passed through argv or env, which sits uneasily with the
  "never leak secrets via `ps`" rule.
- (-) Contract is stringly-typed (a stdout line), with no schema evolution.
- (-) Still needs a core-side switch to know which health to call, so core is not
  actually provider-blind.

Verdict: good as the escape hatch, not as the main design.

### Recommendation

Option B, with C kept as a fallback. The adapter protocol has one built-in
adapter, `external_script`, that wraps the existing
`provisioning_script`/`PROVISION_RESULT:` contract. That means RunPod could stay a
script, ad-hoc or private providers need no in-tree Python, and the migration of
RunPod can be incremental. Core never needs to know which kind it is talking to.

## Design (Option B)

### 1. Preset JSON

`health_check_provider` and `provisioning_script` are replaced by one block.
Adapter-specific settings live under `options`, which core never interprets:

```json
{
  "description": "Fully remote via your own Hugging Face Inference Endpoints ...",
  "embedding": {
    "model_type": "remote",
    "model_name": "intfloat/multilingual-e5-large-instruct",
    "batch_size": 64,
    "model_kwargs": {
      "shared_base_url_env": "HF_EMBEDDING_BASE_URL",
      "shared_api_key_env": "HF_TOKEN"
    }
  },
  "llm": { "model_type": "remote", "model_names": ["Qwen/Qwen2.5-7B-Instruct"], "...": "...",
    "model_kwargs": { "shared_base_url_env": "HF_LLM_BASE_URL", "shared_api_key_env": "HF_TOKEN" } },
  "provisioner": {
    "name": "huggingface",
    "options": {
      "namespace": null,
      "vendor": "aws",
      "region": "eu-west-1",
      "embedding": { "engine": "tei", "instance": "nvidia-l4", "scale_to_zero_timeout_min": 15 },
      "llm":       { "engine": "vllm", "instance": "nvidia-l4", "scale_to_zero_timeout_min": 15 }
    }
  }
}
```

The service-facing interface is unchanged: `embeddings.py` and `llm.py` still read
`shared_base_url_env` / `shared_api_key_env` through
`admin_settings_store.resolve_shared_value()`. The provisioner's only job is to
make those values exist and to report readiness.

Backward compatibility inside 1.x: existing `runpod.json` files on disk that use
`health_check_provider` / `provisioning_script` must still validate. The schema
keeps both as deprecated optional fields and normalises them into an implicit
`provisioner` (`{"name": "runpod"}` or `{"name": "external_script", ...}`) at load
time. Remove them at 2.0.

### 2. The protocol (`backend/provisioners/base.py`)

```python
class Provisioner(Protocol):
    name: ClassVar[str]
    options_model: ClassVar[type[BaseModel]]       # validates preset.provisioner.options
    credential_specs: ClassVar[list[CredentialSpec]]   # env name, regex, human label
    output_specs: ClassVar[list[OutputSpec]]       # base-url env names it produces + regex

    def validate_credentials(self, creds: Mapping[str, str]) -> None: ...
    def health(self, side: Literal["embedding", "llm"], ctx: Ctx) -> Health: ...
    def provision(self, ctx: Ctx, progress: Callable[[str], None]) -> dict[str, str]: ...
    def teardown(self, ctx: Ctx) -> None: ...
    def classify_http_error(self, status: int, body: str) -> ErrorKind | None: ...  # optional
```

- `Health` is `{"status": "ready"|"cold"|"unreachable", "detail": str}`, the shape
  the plugin already renders.
- `provision()` returns `{base_url_env_name: url}`. Core feeds it to the existing
  `update_remote_config()`, exactly as `PROVISION_RESULT:` is consumed today.
- `credential_specs` and `output_specs` replace the per-preset `shared_*_pattern`
  copies in `runpod.json`. Core uses them for the same format validation, and
  presets no longer repeat provider URL regexes.
- `classify_http_error` is the one hook for the cold-endpoint messages currently
  special-cased in `embeddings.py` and `llm.py` (RunPod's openresty 405 page, an
  HF 502 while a replica starts). It lets an adapter say "this response means
  cold, not broken" without a provider name check in the service.
- Registry: `backend/provisioners/__init__.py` imports every module in the package
  and builds `PROVISIONERS: dict[str, Provisioner]`. No central list to edit.
  `external_script` is one of them.

### 3. Core changes (done once, then provider-blind)

- `presets.py`: add `provisioner: Optional[ProvisionerConfig]` (`name`, opaque
  `options`). Validate `options` lazily against the adapter's `options_model`, so
  an unknown adapter is a clear error at preset load, not a schema edit.
- `api/config.py`: `GET /api/config/health` and `POST /api/config/provision` call
  the protocol via a registry lookup. `provisionable` becomes `provisioner is not
  None`.
- `services/provisioning.py`: run `provision()` through `asyncio.to_thread`,
  keeping the same job state machine (`idle`/`running`/`succeeded`/`failed`) and
  409/400 behaviour.
- A generic `bin/provision.py [--teardown] [--preset NAME]` replaces
  `bin/provision_runpod_endpoints.py`. The RunPod script stays as a thin shim for
  existing docs and cron.
- The model-name basename hack (`_embedding_model_identity`) is left as is. It is
  provider-agnostic.

### 4. The `huggingface` adapter

Behaviour, in the order the user sees it:

1. Credentials: one `HF_TOKEN` (fine-grained, "Inference Endpoints" scope). The
   same token is the data-plane key, so `shared_api_key_env` points at it for both
   sides, as in the RunPod preset.
2. `provision()` is idempotent by endpoint name (`zotero-rag-embedding`,
   `zotero-rag-llm`): `get_inference_endpoint(name)`; if absent, create it with
   `min_replica=0`, `max_replica=1`, and the configured `scale_to_zero_timeout`;
   if present and `scaledToZero`/`paused`, `.resume()`; then `.wait()` with a bound
   and a warm-up request. Differing config warns unless `--recreate`, mirroring the
   RunPod script.
3. Embedding uses the TEI engine; LLM uses vLLM or TGI. Both expose an
   OpenAI-compatible `/v1`. Base URL = the endpoint's `url` + `/v1`.
4. `health()` maps the HF status: `running` is ready, `scaledToZero`/`paused`/
   `pending`/`initializing` is cold, `failed` or an API error is unreachable.
   Unlike RunPod this is one cheap management-API call, not a data-plane probe.
5. `teardown()` deletes both endpoints.

### 5. Cost framing for the user (shown in the preset description)

- HF bills per instance-hour, by the minute, while initializing or running, and
  not while paused or scaled to zero. Indicative AWS single-GPU rates: L4 $0.80/h,
  A10G $1.00/h, L40S $1.80/h, A100-80G $2.50/h.
- RunPod flex serverless bills per second from worker start to full stop. For
  bursty use (a few queries a day), RunPod is probably cheaper because HF's idle
  tail is longer. For sustained indexing runs, HF's hourly rates are competitive.
- Figures are third-party-sourced and must be re-verified before the preset
  description states numbers.

## Migration plan

1. Introduce `backend/provisioners/` with the protocol, registry, and
   `external_script` adapter. Normalise the legacy fields. No behaviour change
   (existing tests must pass untouched).
2. Port RunPod into `runpod.py` (health and provisioning from the existing
   modules), delete `HEALTH_CHECKS`, and drop the `Literal["runpod"]` field.
3. Add `huggingface.py`, `huggingface.json`, an adapter contract test suite that
   every adapter must pass, and docs in `docs/presets.md`.
4. Only then consider entry-point discovery for out-of-tree adapters.

## Testing

- A shared contract test parametrised over every registered adapter: options
  validation, credential regex validation, idempotent `provision()` against a
  fake client, `health()` classification, `teardown()` no-op on absent resources.
- Unit tests for the HF adapter with a faked `huggingface_hub` client (no live
  calls; endpoints cost money).
- One manual smoke checklist (provision, re-run, wake from `scaledToZero`,
  teardown) run once against a real HF account.

## Open questions

- Does TEI's `/v1/embeddings` accept batched input and echo the model name in the
  way `RemoteEmbeddingService` expects? (Same open item the RunPod spec had.)
- What does vLLM on HF Endpoints report as the served model name? The vLLM
  container loads from `/repository`, so the preset's `model_names` may not match
  what must be sent in `model`. The adapter may need to supply the on-the-wire name.
- Real default for `scale_to_zero_timeout` and its minimum, since the 15-minute
  figure comes from one unverified source.
- Which AWS/GCP regions offer the chosen GPU in the EU, and whether GDPR hosting
  should be the default.
- Do reports of endpoints stuck in "Initializing" after a scale-from-zero wake-up
  (forum, unconfirmed) require the adapter to implement a resume-and-recheck loop?
- Is the sunk cost of the stdout contract worth keeping in `external_script` once
  both in-tree providers are native adapters, or should it be dropped (YAGNI)?
