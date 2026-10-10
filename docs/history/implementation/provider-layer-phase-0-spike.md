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
| Hugging Face (TEI batching and model echo, vLLM model name, scale-to-zero default and minimum, error of a paused endpoint, `huggingface_hub` versus REST) | **Not tested.** No HF credentials in this environment, and `huggingface.co` / `api.endpoints.huggingface.cloud` are blocked by the session's network policy (CONNECT returns 403) |
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

## Needs you (cannot be done from this environment)

1. **Restricted-key listing.** Create a RunPod API key restricted to one endpoint, then run
   `curl -sS -H "Authorization: Bearer $RESTRICTED_KEY" https://rest.runpod.io/v1/endpoints`
   and report: HTTP status and whether the list contains that endpoint only, all endpoints,
   or nothing. This decides whether `endpoint_url()` can work for restricted keys.
2. **Hugging Face.** With a token and billing enabled (or after allowing the HF hosts in the
   environment's network policy and providing a token), run the checks in plan Task 0.2
   Step 1 and Step 2 against the smallest GPU, and delete everything afterwards.
3. **MPCDF.** With a live job, `curl -H "Authorization: Bearer $KEY" "$BASE/models"` for a
   valid key, a wrong key, and after the job has expired.
