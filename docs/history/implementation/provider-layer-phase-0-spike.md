# Provider layer, Phase 0: verification spike results

Date: 2026-10-10. Plan: `docs/superpowers/plans/2026-10-10-provider-layer.md` (Phase 0).
Spec: `docs/superpowers/specs/2026-10-10-huggingface-preset-and-provisioner-adapters-design.md`.

Run from the cloud session with the RunPod credentials injected by the session proxy
(no key was read, printed or stored). Only throwaway resources named `zotero-rag-spike*`
were created, at `workersMin=0` so no worker ever started and nothing was billed; all of
them were deleted, and the account was re-listed afterwards (the two existing endpoints
`zotero-rag-llm` and `zotero-rag-embedding` and their templates were never touched).

## Summary

| Question | Result |
|---|---|
| RunPod: does the REST update accept `workersMax=0`? | **Yes.** `PATCH /v1/endpoints/{id}` with `{"workersMax":0}` returns 200 and reads back as 0 |
| Does it actually block a wake-up? | **Yes, and explicitly.** The endpoint becomes a paused state: `POST /v2/{id}/run` and `POST /v2/{id}/openai/v1/embeddings` both return **HTTP 409** with `{"code":"ENDPOINT_PAUSED","detail":"Endpoint is paused (max_workers=0). Set max_workers > 0 to accept work."}` in about 0.1 s; `/health` showed 0 workers and 0 queued jobs after 50 s |
| Can an endpoint be created with `workersMax=0`? | **No.** `POST /v1/endpoints` with `workersMax:0` returns HTTP 500 with an empty body. Create with at least 1, then PATCH |
| Restore | `PATCH {"workersMax":1}` returns 200 (not re-tested against a live request, to avoid starting a worker) |
| RunPod: can an endpoint-restricted key list endpoints? | **Not tested.** Needs a restricted key from the user (see "Needs you") |
| Hugging Face: can an endpoint be created? | **Yes, after a payment method was added** (first attempt: `403 Payment method required for namespace`, with `canPay: false`). Live runs below. The three organisations the account belongs to answer 403 on the endpoints API (the token is scoped to the user entity) and were not used |
| Hugging Face: `huggingface_hub` versus plain REST | **Decision: plain `httpx` REST.** See "Hugging Face findings" |
| Hugging Face: TEI batching and echoed model name, vLLM served model name, data-plane error of a paused endpoint, scale-to-zero minimum, "stuck in Initializing" | **All answered live**, see "Hugging Face live results". No endpoint got stuck in `initializing` in six deploys/resumes (a small sample) |
| MPCDF `GET {base}/models` | **Not tested.** No job available, and `llm.mpcdf.mpg.de` is blocked by the session's network policy |

## Other facts recorded

- `GET /v1/endpoints` returns, per endpoint: `id`, `name`, `templateId`, `gpuTypeIds`,
  `gpuCount`, `idleTimeout`, `workersMin`, `workersMax`, `workersStandby`, `scalerType`,
  `scalerValue`, `flashboot`, `networkVolumeId`, `minCudaVersion`, `version`, `userId`,
  `createdAt`. The list does not embed the template; create and PATCH responses do.
- `workersStandby` appears in every response (equal to `workersMax` at creation on the
  existing endpoints) and was unchanged by the PATCH; its meaning is not documented here
  and no behaviour depends on it.
- `GET /v1/endpoints/{id}` works and returns the same shape without the embedded template.
- `POST /v2/{id}/purge-queue` returns 200; `DELETE` of endpoint and template return 204.
- The data-plane `/health` payload is `{"jobs":{completed,failed,inProgress,inQueue,retried},
  "workers":{idle,initializing,ready,running,throttled,unhealthy}}`. It does **not** mark a
  paused endpoint, so paused detection must read `workersMax` from the management API.
- Every create and update call succeeded on the first try apart from the `workersMax:0`
  creation.

## Hugging Face findings (read-only, plus SDK source)

Management API base `https://api.endpoints.huggingface.cloud/v2`; the session proxy injects the
token for that host and for `huggingface.co`. Everything below was read from the live API or from
the `huggingface_hub` 2.2.0 source (installed in a scratch venv outside the repo).

- **REST routes** (all under `/endpoint/{namespace}`): `POST` create; `GET` list; `GET /{name}`
  fetch; `PUT /{name}` update; `DELETE /{name}`; `POST /{name}/pause`; `POST /{name}/resume`;
  `POST /{name}/scale-to-zero`. Also `GET /provider` for the instance catalog.
- **Create payload:** `{"name", "type", "provider":{"vendor","region"}, "compute":{"accelerator",
  "instanceType","instanceSize","scaling":{"minReplica","maxReplica","scaleToZeroTimeout"}},
  "model":{"repository","revision","framework","task","image":{...},"env","secrets"}}`.
  `scaleToZeroTimeout` is in minutes. Image is a variant dict forwarded as-is: `{"tei":{...}}`,
  `{"tgi":{...}}`, `{"vLLM":{"url","port"}}`, `sGLang`, `llamacpp`, `hfServe`, or a flat custom image.
- **Endpoint `type`** is now `authenticated` (default), `public` or `private`; the old `protected`
  is rejected by the SDK.
- **Scale to zero is opt-in:** `min_replica` defaults to 1 and `scale_to_zero_timeout` to none, so
  the provider must set `minReplica: 0` and a timeout explicitly. There is also a manual
  `scale-to-zero` call, distinct from `pause`: a scaled-to-zero endpoint restarts on the next request,
  a paused one needs an explicit resume and is not billed.
- **Statuses (SDK enum):** `pending`, `initializing`, `updating`, `updateFailed`, `running`,
  `paused`, `failed`, `scaledToZero`. The spec's mapping lacked `updating` and `updateFailed`.
- **Status object** (read from the account's two old paused endpoints): `state`, `message`, `url`,
  `readyReplica`, `targetReplica`, `lastUsedAt`, `createdAt/By`, `updatedAt/By`. A paused endpoint
  keeps its `url`, so `endpoint_url()` also works while paused.
- **Catalog and prices** (`GET /provider`, hourly): `eu-west-1` offers only GPUs `nvidia-t4` x1
  (16 GB, US$0.50) and `nvidia-a10g` x1 (24 GB, US$1.00), plus Intel Sapphire Rapids CPU instances
  from US$0.033/h. **`nvidia-l4` is not available in `eu-west-1`** (it exists in `us-east-1`, US$0.80,
  and GCP `us-east4`, US$0.70). Other: `us-east-1` L40S US$1.80, A100 US$2.50; GCP `us-east4` A100
  US$3.60, H100 US$10. Every instance reports `quota.maxAccelerators: 0`; whether that is an enforced
  quota or just "not set" could not be told without a payment method.
- **Why REST, not the SDK:** the repo's lockfile pins `huggingface-hub` 0.36.0 only transitively via
  the optional local-model extras (transformers requires `<1.0`), so adopting the 2.x SDK would add a
  direct dependency that conflicts with them; the API has already changed under the SDK (`protected`
  removed, image variants added); the SDK needs a locally available token even to resolve the namespace
  (`whoami`); and the REST surface needed is eight routes. `httpx` is already a dependency.
- **Token permissions:** a fine-grained token distinguishes `inference.endpoints.write` (manage
  endpoints) from `inference.endpoints.infer.write` (call them). A token with only the second may be
  unable to look an endpoint up by name, which is the same restricted-key question as for RunPod and
  could not be tested without a second token.
- **Not reachable from the session:** `*.endpoints.huggingface.cloud` (the data plane) is blocked by
  the network policy, so no request could be sent to an endpoint, even a paused one.

Other idea noted, not pursued: the catalog lists cheap Sapphire Rapids CPU instances; the original
problem was slow CPU embedding on a no-AVX-512 host, so TEI on a CPU endpoint is worth measuring later
as a very cheap embedding option.

## Hugging Face live results (2026-10-10, ~US$0.3 total)

Endpoints were created through the REST contract above (plain `httpx`), on `aws/eu-west-1`,
type `public` (the data-plane hosts do not get the session's injected token, so an
`authenticated` endpoint cannot be called from the session), and deleted by the scripts'
`finally` blocks; the account was re-listed afterwards and holds only the two old paused endpoints.

**Embeddings: TEI 1.8.3 on `nvidia-t4`, image `ghcr.io/huggingface/text-embeddings-inference:turing-1.8`,
repository `intfloat/multilingual-e5-large-instruct`, image variant `{"tei": {...}}`**

| Check | Result |
|---|---|
| Deploy time (create to `running`) | 157 s first time, 51 to 62 s afterwards (model cached) |
| `/info` | `model_id: "/repository"`, `max_input_length: 512`, dtype float16, mean pooling |
| `POST /v1/embeddings` | HTTP 200; the request `model` is **ignored**; the response `model` is `"/repository"`; 1024 dimensions; `usage.prompt_tokens`/`total_tokens` present; duplicate inputs give identical vectors and `index` preserves order |
| **Client batch limit** | Default **32**: a batch of 33 gets HTTP 413 `{"message":"batch size 33 > maximum allowed batch size 32","code":413,"type":"Validation"}`. The image-variant field `maxClientBatchSize` was ignored. Setting **`model.env: {"MAX_CLIENT_BATCH_SIZE": "128"}`** works (`/info` then reports 128; 128 is accepted, 129 gets 413). The preset batch size is 64, so the provider must set this |
| Throughput (800-character passages) | about 106 passages/s at batch 128, about 64/s at batch 32, versus about 0.65/s measured on the production CPU host |
| `scaleToZeroTimeout` | Accepted range is **15 to 2880 minutes** (`400 ... must be between 15 and 2880 minutes`); the minimum idle tail is therefore 15 minutes (about US$0.125 on a T4, US$0.25 on an A10G, per wake) |
| `POST /scale-to-zero` | State becomes `scaledToZero` at once, but the replica keeps serving for a short moment |
| **Cold start** (request to a scaled-to-zero endpoint) | The first request is answered immediately (0.05 s) with **HTTP 503** `{"error":"503 Service Unavailable","code":"SERVICE_UNAVAILABLE"}`, no `Retry-After`; the state moves to `initializing`; requests keep getting 503 (they do not hang) and the first 200 came after about 42 s |
| **Paused endpoint** | `POST /pause` gives state `paused` at once. A data-plane request is answered with **HTTP 400** `{"error":"Bad Request: The endpoint is paused, ask a maintainer to restart it","code":"BAD_REQUEST"}`, repeatedly, and the endpoint stays paused (requests do not wake it). `POST /resume` took 84 s to `running` |
| Authenticated endpoint | A request with no token or a wrong token gets HTTP 401 `{"error":"401 Unauthorized","code":"UNAUTHORIZED"}` on every route including `/health`, and **does not wake** a scaled-to-zero endpoint |
| Management API after PUT with a too-small timeout | Validation error as above; the endpoint stayed `running` and unchanged |

**LLM: vLLM on `nvidia-a10g`, image `{"vLLM": {"url": "vllm/vllm-openai:latest", "port": 8000, "healthRoute": "/health"}}`,
repository `Qwen/Qwen2.5-7B-Instruct`, no env or args**

| Check | Result |
|---|---|
| Deploy time | 458 s (about 3.5 minutes waiting for hardware, then model download and load) |
| `GET /v1/models` | one model whose `id` is the **repository id** `Qwen/Qwen2.5-7B-Instruct` (`root: "/repository"`, `max_model_len: 32768`) |
| `POST /v1/chat/completions` | With `model` = the repo id: HTTP 200, response `model` equal to it. With `"/repository"` or any other name: **HTTP 404** `{"error":{"message":"The model \`X\` does not exist.","type":"NotFoundError",...}}`. So the preset's model name must be the repo id, as it already is |
| Rate-limit headers | none sent; `usage` present in the body |
| Latency | 0.2 to 0.9 s for short completions |

## Consequences for the design

1. **RunPod keeps Pause** (`supports_suspend = True`), implemented as `PATCH workersMax=0`.
   The fallback in spec B3 is not needed.
2. **`classify_http_error()` can return `paused` for RunPod**: HTTP 409 with body code
   `ENDPOINT_PAUSED`. The OpenAI SDK raises this as a status error, so it lands in the
   "unexpected status" branch that already becomes `EmbeddingEndpointUnavailableError`
   (and therefore never counts toward the #70 upload quarantine).
3. **The query path needs no health pre-check for RunPod.** A paused endpoint answers
   immediately with a recognisable error, so the cached `health()` lookup in spec A8 is only
   needed for providers without such an error.
4. **`provision()` for RunPod must create with `workersMax >= 1` and then PATCH**, and its
   resume path restores `workersMax` from the options with a PATCH.

5. **Hugging Face creation needs a payment method** on the namespace (HTTP 403 with that text).
   The provider must turn this into a clear message ("add a payment method to your Hugging Face
   account"), and the descriptor help already tells users billing is required.
6. **The spec's preset placeholders were wrong for the EU:** `nvidia-l4` does not exist in
   `eu-west-1`. The embedding side should use `nvidia-t4` and the 7B LLM side `nvidia-a10g`.

7. **Hugging Face provider rules from the live runs:** set `MAX_CLIENT_BATCH_SIZE` in `model.env`
   to at least the preset batch size; the TEI image tag depends on the GPU architecture
   (`turing-1.8` for the T4; other architectures need their own tags, to be confirmed when added);
   `classify_http_error()` maps **HTTP 400 whose body contains "endpoint is paused"** to `paused`
   and HTTP 503 `SERVICE_UNAVAILABLE` to `cold`; a 401 never wakes an endpoint.
8. **The paused response is a 400.** The embedding service treats a `BadRequestError` as a
   per-item content problem, so the paused (and any provider-classified) error must be classified
   **before** that branch, otherwise a paused endpoint would be read as a bad document.
9. **Cold starts return 503 for about 40 to 60 s.** The existing retry loop treats 5xx as transient and
   gives up after its last attempt with `EmbeddingEndpointUnavailableError`, which is the right
   class (systemic, never counts toward the #70 quarantine); the hint should say the endpoint is
   starting.

## Needs you (cannot be done from this environment)

1. **Restricted-key listing.** Create a RunPod API key restricted to one endpoint, then run
   `curl -sS -H "Authorization: Bearer $RESTRICTED_KEY" https://rest.runpod.io/v1/endpoints`
   and report: HTTP status and whether the list contains that endpoint only, all endpoints,
   or nothing. This decides whether `endpoint_url()` can work for restricted keys.
2. **Hugging Face restricted-token lookup.** A token limited to `inference.endpoints.infer.write`
   (call only) cannot be made from this session. Whether such a token can still `GET` an endpoint by
   name decides if `endpoint_url()` works for it.
3. **MPCDF.** With a live job, `curl -H "Authorization: Bearer $KEY" "$BASE/models"` for a
   valid key, a wrong key, and after the job has expired.
