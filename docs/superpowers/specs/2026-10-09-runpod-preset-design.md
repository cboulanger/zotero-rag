> **Status: superseded** by `2026-10-10-huggingface-preset-and-provisioner-adapters-design.md` (provider layer). Kept as a record of the original RunPod design.

# RunPod preset + endpoint-provisioning script

## Problem

The production host (`cloud@141.5.110.62`, 4 vCPU AMD EPYC, no GPU) cannot
run `intfloat/multilingual-e5-large-instruct` locally at usable speed:
measured throughput is ~0.65 passages/sec regardless of batch size (CPU
compute-bound, no AVX-512, no GPU), which means ~4.3 hours to index a
10,000-chunk library and ~1.5s of added latency per query embedding. The
project currently depends on KISSKI (GWDG academic cloud) for this model
remotely, with MPCDF as a documented fallback for when KISSKI's rate
limit is exhausted (`remote-mpcdf` preset). Both are external services
outside the project owner's control.

RunPod offers on-demand serverless GPU endpoints (REST API, pay-per-second,
scale-to-zero when idle) as a third option: self-provisioned, billed only
for actual usage, and — unlike MPCDF's ≤8h ephemeral Slurm jobs — endpoints
persist until explicitly deleted. There is currently no preset or tooling
for this provider.

## Goals

- A new `runpod` hardware preset, following the existing file-based preset
  convention (`data/presets/<name>.json` + bundled default under
  `backend/config/default_presets/`), using remote embedding
  (`intfloat/multilingual-e5-large-instruct`) and remote LLM inference via
  two independently-provisioned RunPod serverless endpoints.
- A script, `scripts/provision_runpod_endpoints.py`, that given a RunPod
  API key creates both endpoints (and their backing templates) if they
  don't already exist, and otherwise verifies them and sends a warm-up
  request to each — so re-running the script against an idle (scaled-to-
  zero) deployment is the normal way to "wake it up" before a work session.
- Hand the resulting endpoint URLs to the rest of the system through the
  same `shared_base_url_env`/`shared_api_key_env` mechanism already built
  for `remote-mpcdf` (`.env` + `POST /api/config/remote-fields`), so no new
  config-resolution code path is needed.

## Non-goals

- No RunPod SDK dependency — plain REST via `httpx` (already a project
  dependency), consistent with how other `scripts/*.py` talk to external
  APIs.
- No automatic model/GPU-tier recommendation engine — sensible defaults
  (below), overridable via CLI flags.
- No autoscaling policy beyond a single always-same worker count
  (`workersMin=0`, `workersMax=1` by default) — this is a small
  single-deployment academic tool, not a multi-tenant service.
- No change to `backend/services/embeddings.py` / `llm.py` resolution
  logic — the existing `shared_base_url_env`/`shared_api_key_env` handling
  (built for `remote-mpcdf`) already covers this preset's needs unchanged.
- No CI/automated recurring provisioning — this is a manually-invoked admin
  script, run locally by whoever operates the deployment.

## Design

### 1. The `runpod` preset

New files, mirroring `remote-mpcdf`'s shape exactly:

- `backend/config/default_presets/runpod.json` (bundled default)
- `data/presets/runpod.json` (seeded copy an admin can edit; never
  overwritten once it exists — existing `ensure_default_presets` behavior)

```json
{
  "description": "Fully remote via self-hosted RunPod serverless endpoints (embedding + LLM, pay-per-use, scale-to-zero). Provision/wake both endpoints with scripts/provision_runpod_endpoints.py before use.",
  "embedding": {
    "model_type": "remote",
    "model_name": "intfloat/multilingual-e5-large-instruct",
    "batch_size": 64,
    "model_kwargs": {
      "shared_base_url_env": "RUNPOD_EMBEDDING_BASE_URL",
      "shared_api_key_env": "RUNPOD_API_KEY"
    }
  },
  "llm": {
    "model_type": "remote",
    "model_names": ["Qwen/Qwen2.5-7B-Instruct"],
    "max_context_length": 32768,
    "max_answer_tokens": 2048,
    "temperature": 0.7,
    "model_kwargs": {
      "shared_base_url_env": "RUNPOD_LLM_BASE_URL",
      "shared_api_key_env": "RUNPOD_API_KEY"
    }
  },
  "rag": {
    "top_k": 10,
    "score_threshold": 0.35,
    "max_chunk_size": 800
  },
  "memory_budget_gb": 0.5
}
```

Both `shared_api_key_env` fields name the **same** env var
(`RUNPOD_API_KEY`) — unlike MPCDF's embedding/LLM jobs, which are
independent Slurm allocations with distinct generated keys, a RunPod
account has one stable API key used for every endpoint it owns. Base URLs
differ (`RUNPOD_EMBEDDING_BASE_URL` / `RUNPOD_LLM_BASE_URL`) because each
endpoint gets its own endpoint ID baked into its URL
(`https://api.runpod.ai/v2/<endpoint-id>/openai/v1`).

`max_context_length: 32768` matches Qwen2.5-7B-Instruct's native context
window (unlike KISSKI/MPCDF's 128k+ models — this is a smaller
self-hosted model, so the preset's context budget is set accordingly).

No code changes needed in `backend/services/embeddings.py` /
`backend/services/llm.py` — the `shared_base_url_env`/`shared_api_key_env`
resolution path (store → env var → error) built for `remote-mpcdf` already
handles this preset identically.

### 2. RunPod resource model

Each of the two endpoints (embedding, LLM) requires two RunPod objects,
created in order:

1. **Template** (`POST /v1/templates`) — Docker image + env vars + disk
   size. RunPod's endpoint-creation API requires a `templateId`; there is
   no inline image/env option on endpoint creation itself.
2. **Endpoint** (`POST /v1/endpoints`) — references the template, adds
   `gpuTypeIds`, `workersMin`/`workersMax`, `idleTimeout`, optional
   `dataCenterIds`.

| | Embedding | LLM |
|---|---|---|
| Template name | `zotero-rag-embedding` | `zotero-rag-llm` |
| Image | `runpod/worker-infinity-embedding:stable-cuda12.1.0` | `runpod/worker-vllm:stable-cuda12.1.0` |
| Key env var | `MODEL_NAMES=intfloat/multilingual-e5-large-instruct` | `MODEL_NAME=<--llm-model, default Qwen/Qwen2.5-7B-Instruct>` |
| Endpoint name | `zotero-rag-embedding` | `zotero-rag-llm` |
| Default GPU (`--embedding-gpu`/`--llm-gpu`) | `NVIDIA RTX A4000` (16GB) | `NVIDIA RTX A5000` (24GB) |
| `workersMin` / `workersMax` | 0 / 1 (`--workers-max`) | 0 / 1 (`--workers-max`) |
| `idleTimeout` | 60s (`--idle-timeout`) | 60s (`--idle-timeout`) |

The embedding worker (`worker-infinity-embedding`, built on the Infinity
embedding server) is used instead of `worker-vllm` for embeddings — vLLM's
pooling/embed mode is not what RunPod's prebuilt `worker-vllm` image
exposes; `worker-infinity-embedding` is RunPod's purpose-built,
OpenAI-`/v1/embeddings`-compatible image for this.

GPU type IDs and datacenter IDs are strings that drift as RunPod's fleet
changes; defaults here are a starting point, overridable via
`--embedding-gpu`/`--llm-gpu`/`--data-centers`, and the script's `--help`
text points at `GET /v1/gpuTypes` for the current valid list if a default
ever starts failing.

### 3. Script behavior (`scripts/provision_runpod_endpoints.py`)

```text
uv run python scripts/provision_runpod_endpoints.py [options]

  --api-key TEXT           RunPod API key (default: RUNPOD_API_KEY from .env)
  --llm-model TEXT         HF repo id for the LLM endpoint (default: Qwen/Qwen2.5-7B-Instruct)
  --embedding-gpu TEXT     GPU type ID for the embedding endpoint (default: NVIDIA RTX A4000)
  --llm-gpu TEXT           GPU type ID for the LLM endpoint (default: NVIDIA RTX A5000)
  --workers-max INT        Max workers per endpoint (default: 1)
  --idle-timeout INT       Seconds idle before scale-to-zero (default: 60)
  --data-centers TEXT      Comma-separated RunPod datacenter IDs (default: unset, RunPod chooses)
  --recreate               Delete and recreate an endpoint/template whose config differs from requested
  --teardown               Delete both endpoints and templates by name (prompts for confirmation)
  --yes, -y                Skip the --teardown confirmation prompt (required when running non-interactively)
  --skip-warmup            Create/verify only; don't send the warm-up request
```

**Idempotent ensure-and-wake flow**, run once per endpoint (embedding,
then LLM):

1. `GET /v1/templates`, find by name. If absent, `POST /v1/templates` to
   create it. If present and its `imageName`/`env` differ from what was
   requested: log a warning and continue using the existing template
   unless `--recreate` was passed (in which case: delete, recreate).
2. `GET /v1/endpoints`, find by name. Same absent/present/`--recreate`
   logic, referencing the (possibly just-created) template's id.
3. Unless `--skip-warmup`: send one lightweight request to the endpoint's
   OpenAI-compatible URL (embedding: embed the string `"ping"`; LLM: a
   1-max-token chat completion) using the resolved API key. This is what
   makes a re-run against an idle, scaled-to-zero deployment "wake it up"
   — RunPod spins up a worker on the first inbound request regardless of
   whether the script just created the endpoint or found it already
   existing idle.
4. Print the endpoint's base URL
   (`https://api.runpod.ai/v2/<endpoint-id>/openai/v1`).

After both endpoints are ensured and awake, the script:

- Reads the existing `.env` file (if present), updates/inserts
  `RUNPOD_API_KEY`, `RUNPOD_EMBEDDING_BASE_URL`, `RUNPOD_LLM_BASE_URL` by
  parsing it into lines and replacing/appending each key individually
  (never a blind `>>` append — avoids the same newline-concatenation
  hazard documented in this project's CLAUDE.md for worktree `.env`
  setup), and writes it back.
- Prints the exact command to push the same three values into an
  already-running backend without a restart:

  ```bash
  curl -X POST https://<host>/api/config/remote-fields \
    -H "X-Zotero-API-Key: <admin-key>" \
    -H "Content-Type: application/json" \
    -d '{"values": {"RUNPOD_EMBEDDING_BASE_URL": "...", "RUNPOD_LLM_BASE_URL": "...", "RUNPOD_API_KEY": "..."}}'
  ```

**`--teardown`**: looks up both endpoints and templates by name; prints
what will be deleted and asks for a y/N confirmation (unless running
non-interactively, in which case it refuses and exits 1 — a `--teardown`
pointed at a stdin-less CI context should never silently delete
infrastructure); deletes endpoints first, then templates. A name that
doesn't exist is a no-op, not an error — a partial teardown (e.g. a
previous run deleted the endpoint but not the template) can be re-run
safely.

### 4. Error handling

| Situation | Behavior |
|---|---|
| No API key resolvable (`--api-key` absent, `RUNPOD_API_KEY` not in `.env`/env) | Exit 1 before any network call, message points at `.env` |
| RunPod API returns 4xx/5xx on create | Abort immediately with the response body printed; nothing written to `.env` for either endpoint if either half fails |
| Warm-up request times out (first-ever cold start can take 30-60s+ while the worker pulls model weights) | Retried with backoff up to ~3 minutes total, then a warning (not a failure) — the endpoint exists and will warm on the next real request either way |
| `--teardown` on a name that doesn't exist | No-op, not an error |
| `--recreate` requested but the existing resource is mid-use (active worker) | RunPod's own delete call is the authority here; the script surfaces whatever error RunPod returns rather than guessing at in-use state itself |

### 5. Testing

- Unit tests (`backend/tests/` or a new `scripts/tests/`, mocking `httpx`
  responses — no live RunPod calls, which cost money and need a real
  account):
  - Name-based idempotency: existing template/endpoint found → reused, not
    recreated; mismatched config → warning logged, existing kept (default)
    vs. deleted+recreated (`--recreate`).
  - Fresh-create flow: template created, then endpoint created referencing
    its id, in that order.
  - `.env` update inserts new keys and replaces existing ones in place
    without corrupting surrounding content (including the no-trailing-
    newline edge case).
  - `--teardown`: deletes endpoint then template; no-ops cleanly on
    already-absent names; refuses non-interactively without `--yes`
    (open item below — see note).
  - Warm-up retry/backoff gives up after its ceiling and warns rather than
    raising.
- Manual smoke-test checklist (documented in the script's own docstring,
  run once against a real RunPod account): provision both endpoints from
  scratch, confirm both respond correctly to a real embedding/chat
  request, re-run the script and confirm it reuses (not recreates) both
  and still wakes them, run `--teardown` and confirm both are gone.

## Open items for the implementation plan

- `--teardown`'s non-interactive refusal needs a `--yes`/`-y` escape
  hatch for scripted use (e.g. a future CI teardown of a test account) —
  confirm whether that's wanted now or left for later, since today's ask
  only requires an interactive confirmation.
- Confirm current valid `gpuTypeIds` strings and, if GDPR/EU hosting
  matters enough to default `--data-centers`, which RunPod EU datacenter
  IDs are currently live — both drift over time; the defaults in this spec
  are a starting point to validate against the real API at implementation
  time (`GET /v1/gpuTypes`, `GET /v1/datacenters` or equivalent).
- Confirm `runpod/worker-infinity-embedding`'s exact request/response
  shape matches what `RemoteEmbeddingService` (`backend/services/
  embeddings.py`) expects from an OpenAI-compatible `/v1/embeddings`
  endpoint (model name echoed in responses, batch input support) — the
  design assumes OpenAI-API parity based on RunPod's own documentation,
  not a live test against the image yet.
