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
| Hugging Face: can an endpoint be created? | **Not with this account.** `POST /v2/endpoint/cmboulanger` returns `403 {"error":"Forbidden: Payment method required for namespace: cmboulanger","code":"FORBIDDEN"}`; `whoami` shows `canPay: false`. The token itself is fine (fine-grained, includes `inference.endpoints.write`). The three organisations the account belongs to answer 403 on the endpoints API (the token is scoped to the user entity), and creating endpoints there would bill someone else, so nothing was created |
| Hugging Face: `huggingface_hub` versus plain REST | **Decision: plain `httpx` REST.** See "Hugging Face findings" |
| Hugging Face: TEI batching and echoed model name, vLLM served model name, data-plane error of a paused endpoint, scale-to-zero minimum, "stuck in Initializing" | **Not tested.** Needs a running endpoint (blocked by the payment method) and `*.endpoints.huggingface.cloud` is not unblocked, so the data plane is unreachable from this session anyway |
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

## Needs you (cannot be done from this environment)

1. **Restricted-key listing.** Create a RunPod API key restricted to one endpoint, then run
   `curl -sS -H "Authorization: Bearer $RESTRICTED_KEY" https://rest.runpod.io/v1/endpoints`
   and report: HTTP status and whether the list contains that endpoint only, all endpoints,
   or nothing. This decides whether `endpoint_url()` can work for restricted keys.
2. **Hugging Face live checks.** Add a payment method to the account (or provide a token for a
   namespace that has one) and unblock `*.endpoints.huggingface.cloud`; then the remaining checks
   (TEI batching and echoed model name, the model name a vLLM image serves, the data-plane error of
   a paused endpoint, the scale-to-zero minimum, cold-start behaviour) take about ten minutes on a
   US$0.50/h T4 and cost cents. A token restricted to `inference.endpoints.infer.write` would also
   settle the lookup question.
3. **MPCDF.** With a live job, `curl -H "Authorization: Bearer $KEY" "$BASE/models"` for a
   valid key, a wrong key, and after the job has expired.
