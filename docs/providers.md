# Providers

A *provider* is a small Python class that owns everything vendor-specific about
one side of a preset: where its credentials come from, what its endpoint looks
like, how usage is metered, whether it can be provisioned or paused, and what to
tell the user when it is not ready. The core code never branches on a vendor
name; it asks the side's provider. Presets stay plain JSON and select a provider
by id, separately for each side, so a preset can mix them (local embeddings with
a frontier LLM, or one vendor for embeddings and another for answers).

```json
"embedding": { "provider": { "id": "runpod", "scope": "managed",
                             "options": { "gpu": "NVIDIA RTX A5000" } }, "...": "..." },
"llm":       { "provider": { "id": "anthropic" }, "...": "..." }
```

Code lives in `backend/providers/`; the registry resolves a preset with
`get_providers(preset)`, which validates it and returns one instance per side.

## Bundled providers

| id | For | Credential scope | Notes |
|---|---|---|---|
| `generic` (default) | **Any OpenAI-compatible API** (OpenAI-style `base_url`, optional API key) | `user` | Usage meters from the OpenAI-style `x-ratelimit-*-{requests,tokens}` headers (Go-style reset durations such as `6m0s`) and the IETF `RateLimit-*` / `RateLimit-Policy` headers. |
| `kisski` | KISSKI / SAIA gateway | `user` | Live model list with demand status; hour and day request meters. |
| `openai` | OpenAI | `user` | Key documentation link, OpenAI-style meters. |
| `anthropic` | Anthropic (Claude) | `user`, LLM side only | Uses the Anthropic wire protocol (`llm_api = "anthropic"`). A preset that names a Claude model without this provider is warned about. |
| `mpcdf` | MPCDF LLM Inference Service | `shared` | Ephemeral job URL and key set by the admin; health asks `/v1/models`. |
| `runpod` | RunPod serverless endpoints | `user` or `managed` | Provisions, pauses (`workersMax` 0) and resumes endpoints; readiness from the data-plane `/health`. |

## Credential scopes

A class lists the scopes it allows (`key_scopes`) and a default (`default_scope`);
a preset picks one with `provider.scope`.

- `user`: each user's own key, sent with their requests. Usage is per key; the user operates the resource (provision, pause).
- `managed`: the admin's key; the institution pays. Only admins operate the resource.
- `shared`: set once by the admin for everyone; free to the user, nothing to operate (for example MPCDF).

## What a provider can define

Class attributes: `id`, `label`, `Options` (a `ProviderOptions` model with
`extra="forbid"`, so a misspelled option is rejected), `supported_sides`,
`key_scopes`, `default_scope`, `supports_provisioning`, `supports_suspend`,
`has_live_models`, `llm_api` (`"openai"` or `"anthropic"`), `http_timeout`.

Hooks, all optional: `describe()` (the provider-agnostic descriptor the plugin
renders), `endpoint_url()`, `default_key_env()`, `key_docs_url()`,
`unavailable_hint()` (remedy appended to "endpoint unavailable" errors),
`live_models()`, `parse_usage(headers)` (quota meters, no billing),
`health(creds)`, `classify_http_error(status, body)` (`"cold"` or `"paused"`;
called before an HTTP 400 is treated as a per-item problem),
`provision(ctx, progress)`, `suspend(ctx, progress)` and `teardown(ctx)`.
Providers never raise from display-only hooks (`health`, `parse_usage`).

## Adding a provider

1. Create `backend/providers/<name>.py` with a `Provider` subclass and a unique `id`; importing it registers it (`__init_subclass__`), so add it to `backend/providers/__init__.py`.
2. Override only what differs from a plain OpenAI-compatible API.
3. Register a `ContractFixture` for it with `register_fixture` in `backend/tests/provider_fakes.py`. `test_provider_contract.py` then checks every registered provider against the same rules (descriptor shape, validation, display hooks never raising, scope handling). Add vendor tests next to `test_provider_runpod.py` using `FakeHTTP`.
4. Point a preset at it with `"provider": {"id": "<name>"}`.

## Invalid configurations

`get_providers` raises `ProviderConfigError` (the preset is then not listed and
cannot be activated) for: an unknown provider id, options the class does not
define, a provider on a side it does not support, a scope it does not allow, a
non-`generic` provider on a local side, and one key environment variable used by
two different providers.

## Provisioning and cost control

`POST /api/config/provision` runs each requested side's `provision()` as an
independent job (a failure on one side does not stop the other; a failed side can
be retried alone) and `bin/provision.py --preset <name>` does the same from the
command line, including `--pause` and `--teardown`. Pausing keeps the resources
and their URLs but stops billing and wake-ups; provisioning again resumes. A
key typed for a run is used for that run only and never stored.

## Usage meters

Each response's rate-limit headers are recorded per side and per key
fingerprint (`backend/services/usage_meters.py`); the side's provider turns them
into meters (`unit`, `period`, `limit`, `remaining`, `resets_at`). The plugin
draws one bar per meter from `GET /api/rate-limits`, the autoindex status
`rate_limits.meters` and the `rate_limit_meters` of an upload response. Meters
show quota only; costs are read on the provider's own dashboard.
