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
| `kisski` | KISSKI / SAIA gateway | `user` | Live model list with demand status; hour, day and month request meters. |
| `openai` | OpenAI | `user` | Key documentation link, OpenAI-style meters. |
| `google` | Google Gemini (OpenAI-compatible endpoint) | `user` or `managed` | Key documentation link; live list of Flash models (newest first, further filtered by the preset's `model_names` patterns); OpenAI-style meters. |
| `anthropic` | Anthropic (Claude) | `user`, LLM side only | Uses the Anthropic wire protocol (`llm_api = "anthropic"`). A preset that names a Claude model without this provider is warned about. |
| `mpcdf` | MPCDF LLM Inference Service | `shared` (embedding, LLM) or `user` | `shared`: ephemeral job URL and key set by the admin; health asks `/v1/models`. `user` (re-ranking side): each user logs in with their own MPCDF account and the endpoint runs as a job on it (see below). |
| `runpod` | RunPod serverless endpoints | `user` or `managed` | Provisions, pauses (`workersMax` 0) and resumes endpoints; readiness from the data-plane `/health`, paused state from the management API. A re-ranking side (`zotero-rag-rerank`, scope `user`) runs vLLM's `/v1/rerank` on `worker-v1-vllm`; the platform's OpenAI route does not pass `/rerank` through, so calls go to `/runsync` with the worker's route proxy, and a call during a cold start counts as "not ready" (the query runs without re-ranking and the call wakes the worker). |
| `huggingface` | Hugging Face Inference Endpoints | `user` or `managed` | One endpoint per side in the token owner's namespace; readiness from the endpoint's `status.state`; pause/resume via the REST API; the URL is derived from the token. |

## Credential scopes

A class lists the scopes it allows (`key_scopes`) and a default (`default_scope`);
a preset picks one with `provider.scope`.

- `user`: each user's own key, sent with their requests. Usage is per key; the user operates the resource (provision, pause).
- `managed`: the admin's key; the institution pays. Only admins operate the resource.
- `shared`: set once by the admin for everyone; free to the user, nothing to operate (for example MPCDF).

A remote re-ranking side (`rerank`) always uses `user`.

The scope decides the kind of key a side asks for: `user` declares `api_key_env` (the caller's own key, sent as a request header); `managed` and `shared` declare `shared_api_key_env` (the admin-set key). A preset whose key fields contradict its scope is rejected.

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
`provision(ctx, progress)`, `suspend(ctx, progress)`, `rerank_credentials(request, refresh)` and
`rerank_activity(request)` (the re-ranking side's endpoint for the calling user, and a report that it
was used), `session_login(user_id, values)`, `session_logout(user_id)` and `session_status(user_id)`
(for a provider whose endpoints run in the user's own login session), `is_paused(api_key)` (a cheap, non-raising check used to fail fast on a paused endpoint) and `teardown(ctx)`. A provider that supports pausing must also support provisioning, because resuming is provisioning again.
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

Who may provision follows the credential scope. For `user` any signed-in user may, on their own key and in their own job slot (two users can run at once, the same user gets HTTP 409 only for a side that already has a job running; the two sides are independent). For `managed` only an admin may, with the stored admin key and one global slot. `GET /api/config/provision/status` and `GET /api/config/health` answer for the caller's own slot and key. A provider whose endpoints live in the key owner's account sets `derives_endpoint_url` and implements `endpoint_url(key)`; the services then need no URL in the preset (lookups are cached per key in `backend/services/endpoint_cache.py`).

`POST /api/config/provision` runs each requested side's `provision()` as an
independent job (a failure on one side does not stop the other; a failed side can
be retried alone) and `bin/provision.py --preset <name>` does the same from the
command line, including `--pause` and `--teardown`. Pausing keeps the resources
and their URLs but stops billing and wake-ups; provisioning again resumes. A
key typed in the plugin is sent with each management request, kept only in memory in the running Zotero and never stored by the backend.

## Per-user sessions (MPCDF)

Some services allow neither a shared admin key nor a long-lived personal key: the MPCDF LLM
Inference Service needs each user's own login (username, password, one-time code) and runs each
endpoint as a Slurm job of at most 8 hours on that user's account. A provider of this kind sets
`provisioning_needs_key = False` and describes a login form (`provisioning.login` in the descriptor:
a title and the fields with their HTML `type` and `autocomplete`), which the plugin renders in the
side's section instead of a key field. The form posts to `POST /api/config/session/login`
(`{side, values}`, five attempts per user per ten minutes); `GET /api/config/session?side=` and
`POST /api/config/session/logout` answer for the caller only.

For MPCDF this goes through the `mpcdf-llm` library, a dependency installed from
its git repository (pinned in `pyproject.toml` under `[tool.uv.sources]`). `backend/services/mpcdf_sessions.py` keeps one manager per Zotero
user in `<data_path>/system/mpcdf/<user-hash>/`; the session token and the user's Hugging Face
token (every vLLM job there needs one) are sealed with `AUTOINDEX_SECRET`, and the password and
one-time code are never stored. The library's supervisor renews a job shortly before it ends while
it is used and cancels it after `idle_minutes` without a query; each re-ranking call reports use.
The session lasts about 12 hours, and logging in to the MPCDF web interface ends it. Options of the
`mpcdf` provider for a per-user side: `machine` (`Viper`), `gpu_type` (`mi300a`), `gpus` (1-2),
`time_hours` (1-8), `idle_minutes`, `build_timeout_minutes`, `image` and `extra_args` (vLLM
arguments without quotes, default `--enforce-eager`).

A per-user side has no fixed URL or key: the provider sets `uses_session_endpoints` and answers
`session_endpoint(user_id)` with the caller's running endpoint (or None) and `session_activity(user_id)`
when it is used. The embedding and LLM services ask for it before every call, because a renewal changes
both URL and key, and report use at most once a minute; the caller is the Zotero user of the request
(`backend/services/caller_context.py`, bound by the auth middleware). A side whose endpoint is not running
fails with "endpoint unavailable" and the provider's hint, and never falls back to another URL.
Automatic indexing skips a user whose embedding side is such a session (issue kind `session`), since
nobody is there to log in.

## Usage meters

Each response's rate-limit headers are recorded per side and per key
fingerprint (`backend/services/usage_meters.py`); the side's provider turns them
into meters (`unit`, `period`, `limit`, `remaining`, `resets_at`). The plugin
draws one bar per meter from `GET /api/rate-limits`, the autoindex status
`rate_limits.meters` and the `rate_limit_meters` of an upload response. Meters
show quota only; costs are read on the provider's own dashboard.
