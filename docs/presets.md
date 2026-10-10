# Hardware Presets

Configuration presets optimized for different hardware scenarios. Each preset defines the models and settings for embeddings, LLM inference, and RAG retrieval.

## How presets are stored

Each preset is a JSON file under `<data_path>/presets/<name>.json` (e.g. `data/presets/remote-kisski.json` in a default local checkout) — not hardcoded in Python. The presets documented below ship as bundled defaults in `backend/config/default_presets/`. On every startup the backend **overwrites** the copy in `<data_path>/presets/` of each bundled preset whose content differs from the bundled file (logging a WARNING when it discards a local edit), and creates the ones that are missing. This means:

- **Customise by copying.** To change `top_k`, swap a model name, tune `batch_size`, etc., copy a bundled preset to a new file name (`data/presets/my-kisski.json`) and edit the copy; edits to a bundled preset's own file are lost at the next start. The copy shows up in `GET /api/config`'s `available_presets` (and the Zotero plugin's preset dropdown) right away. An already-running process picks up an edit of an existing file after a restart (an in-memory cache keeps a loaded preset's *content* fast to re-read — see `backend/config/presets.py`'s module docstring).
- **Every preset carries an integer `version`** (the current schema version is `PRESET_SCHEMA_VERSION` in `backend/config/presets.py`; see the table below). A custom preset with a missing or older version, one that still uses a removed key, or one that selects a Claude model without the `anthropic` provider is loaded as usual but logs a WARNING once per process saying what to change.
- **Each side names its provider.** `embedding.provider` and `llm.provider` select the provider class that handles that side's credentials, endpoint behaviour, usage meters and provisioning (`{"id": "kisski"}`, `{"id": "runpod", "scope": "managed", "options": {...}}`; the default is `generic`, any OpenAI-compatible API). The two sides may use different providers, e.g. a local embedding model with a frontier LLM. See [providers.md](providers.md).
- **Invalid configurations are rejected**, not repaired: an unknown provider id, options the provider does not define, a provider on a side that cannot use it, a credential scope the provider does not allow, a non-`generic` provider on a local side, or one key environment variable shared by two different providers. A rejected preset is not listed and cannot be activated; the reason is logged.

A preset file's fields mirror `backend.config.presets.HardwarePreset` and its nested `embedding`/`llm`/`rag` sections (see that module for the full Pydantic schema and field descriptions). The file's own `name` key, if present, is ignored — a preset's identity always comes from its filename.

### Preset schema versions

| Version | Change |
|---|---|
| 1 | Original schema (no `version` key means 1). |
| 2 | Per-side `provider` blocks. Removed keys: `provisioning_script`, `embedding.health_check_provider`, `llm.health_check_provider`, `llm.models_status_url` (provider capabilities now). |

### The `platform` field

Every preset also declares a `platform` field: `"any"` (the default — visible everywhere), `"darwin"`, `"linux"`, or `"windows"`. A preset naming a specific platform is hidden from `GET /api/config`'s `available_presets`/`compatible_presets` (and rejected by `POST /api/config`) on any other host — e.g. the bundled `apple-silicon-32gb`/`apple-silicon-kisski` presets (`platform: "darwin"`) and `windows-test` (`platform: "windows"`) never appear in the preset list on the production Debian server, even though the files exist on disk. This only gates what's *listed to a client*: an operator can still explicitly run a platform-specific preset via `MODEL_PRESET` in `.env` on a matching host, and internal tooling (`scripts/eval_embeddings.py`, `scripts/check_embedding_compat.py`) lists every preset regardless of platform since it's meant for a developer comparing configs, not an end client.

## Dependency overview

| Preset | `sentence-transformers` / `torch` required? | API keys | Platform |
| ------ | ------------------------------------------- | -------- | -------- |
| `apple-silicon-32gb` | Yes (~1-2 GB) | — | `darwin` |
| `high-memory` | Yes (~1-2 GB) | — | any |
| `cpu-only` | Yes (~1-2 GB) | — | any |
| `apple-silicon-kisski` | **No** | `KISSKI_API_KEY` | `darwin` |
| `remote-kisski` | **No** | `KISSKI_API_KEY` | any |
| `remote-openai` | **No** | `OPENAI_API_KEY` | any |
| `cloud-server-kisski` | Yes (~500 MB) | `KISSKI_API_KEY` | any |
| `windows-test` | **No** | `KISSKI_API_KEY` | `windows` |
| `remote-mpcdf` | **No** | `MPCDF_EMBEDDING_API_KEY`, `MPCDF_LLM_API_KEY` (shared, admin-set — see below) | any |
| `runpod` | **No** | `RUNPOD_API_KEY` (each user's own) | any |
| `huggingface` | **No** | `HF_TOKEN` (each user's own) | any |

Presets marked **No** use only remote APIs for both embeddings and LLM inference. The Docker image can be built without Tesseract and without installing `sentence-transformers`/`torch` for these presets (see [container-deployment.md](container-deployment.md)).

---

## Available Presets

### `remote-openai` (OpenAI API)

**Best for:** Users with OpenAI API access

**Configuration:**

- Embedding: `text-embedding-3-small` (OpenAI remote, 1536-dim)
- LLM: `gpt-4o-mini` (OpenAI remote, 128k context)
- Memory: ~0.5 GB (no local models)
- Top-k: 10 chunks / Max chunk: 1024 tokens

**Advantages:**

- Excellent embedding and answer quality
- Large 128k context window
- No local GPU or `torch` required

**Requires:** `OPENAI_API_KEY` environment variable

---

### `apple-silicon-32gb` (Fully local / privacy)

**Best for:** Apple Silicon Macs with 32 GB RAM, offline or privacy-sensitive use

**Configuration:**

- Embedding: `intfloat/multilingual-e5-large-instruct` (local, MPS-accelerated, 1024-dim, multilingual)
- LLM: `mistralai/Mistral-7B-Instruct-v0.3` (local, 4-bit quantized)
- Memory: ~10 GB
- Top-k: 10 chunks / Max chunk: 800 tokens

**Note:** Uses the same model as `remote-kisski` and `apple-silicon-kisski`, so existing KISSKI-generated vectors are compatible (subject to the server applying no instruction prefix — verify with `scripts/check_embedding_compat.py`). MPS acceleration on Apple Silicon gives ~50–150 texts/sec, far faster than CPU-only inference.

**Requires:** `sentence-transformers`, `torch` (~1-2 GB extra dependencies — see [Optional local dependencies](#optional-local-dependencies))

**Platform:** `darwin` only — hidden from the preset list on any other host (see [How presets are stored](#how-presets-are-stored)).

---

### `high-memory` (Generic high-memory systems)

**Best for:** Systems with >24 GB RAM, dedicated GPU

**Configuration:**

- Embedding: `sentence-transformers/all-mpnet-base-v2` (local)
- LLM: `mistralai/Mistral-7B-Instruct-v0.3` (local, 8-bit quantized)
- Memory: ~16 GB
- Top-k: 10 chunks / Max chunk: 768 tokens

**Requires:** `sentence-transformers`, `torch`

---

### `cpu-only` (Minimal hardware)

**Best for:** CPU-only machines, low-memory environments, quick testing

**Configuration:**

- Embedding: `sentence-transformers/all-MiniLM-L6-v2` (local, lightweight)
- LLM: `TinyLlama/TinyLlama-1.1B-Chat-v1.0` (local, 4-bit quantized)
- Memory: ~3 GB
- Top-k: 5 chunks / Max chunk: 384 tokens

**Trade-offs:** Lower quality; limited 2k context window.

**Requires:** `sentence-transformers`, `torch`

---

### `cloud-server-kisski` (Cloud server with KISSKI LLM)

**Best for:** Cloud servers with 16 GB RAM, 4 vCPU, no GPU — avoids KISSKI embedding outages while keeping high-quality answers

**Configuration:**

- Embedding: `intfloat/multilingual-e5-small` (local, ~470 MB, multilingual)
- LLM: `llama-3.3-70b-instruct` (KISSKI remote, 128k context)
- Memory: ~2 GB
- Top-k: 10 chunks / Max chunk: 768 tokens

**Advantages:**

- Embedding is fully local — unaffected by KISSKI outages
- Multilingual support (comparable quality to e5-large for typical queries)
- High-quality 70B LLM answers via KISSKI

**Trade-offs:** CPU embedding is slower than GPU or remote API (~5–20 sentences/sec); initial indexing may take minutes to hours depending on library size.

**Requires:** `KISSKI_API_KEY` environment variable; `sentence-transformers`, `torch` (`uv sync --extra local-models`)

---

### `windows-test` (Windows development)

**Best for:** Windows machines — avoids PyTorch/CUDA setup entirely

**Configuration:**

- Embedding: `multilingual-e5-large-instruct` (KISSKI remote)
- LLM: `llama-3.3-70b-instruct` (KISSKI remote)
- Memory: ~0.5 GB (fully remote)
- Top-k: 10 chunks / Max chunk: 1024 tokens

**Requires:** `KISSKI_API_KEY` environment variable

**Platform:** `windows` only — hidden from the preset list on any other host (see [How presets are stored](#how-presets-are-stored)).

---

### `remote-kisski` (fully remote, no GPU needed, requires KISSKI/SAIA access)

**Best for:** Any machine with internet access and a KISSKI/SAIA Academic Cloud account

**Configuration:**

- Embedding: `multilingual-e5-large-instruct` (KISSKI remote, 1024-dim, multilingual)
- LLM: `llama-3.3-70b-instruct` (KISSKI remote, 128k context)
- Memory: ~0.5 GB (no local models)
- Top-k: 10 chunks / Max chunk: 1024 tokens

**Advantages:**

- Zero local GPU or large Python dependencies — `torch` / `sentence-transformers` are not loaded
- Excellent multilingual embedding quality, ideal for academic content
- High-quality 70B LLM answers with 128k context window
- Single API key for both embedding and LLM

**Memory tuning:** The embedding batch size defaults to 256 texts per API call.
On hosts with limited RAM (≤16 GB) set `EMBEDDING_BATCH_SIZE=64` in your environment
to reduce peak RSS during indexing.

**Requires:** `KISSKI_API_KEY` environment variable

---

### `apple-silicon-kisski` (Recommended for Apple Silicon + KISSKI)

**Best for:** Apple Silicon Macs (16-32 GB RAM) with KISSKI API access

**Configuration:**

- Embedding: `multilingual-e5-large-instruct` (KISSKI remote, 1024-dim)
- LLM: `llama-3.3-70b-instruct` (KISSKI remote, 128k context)
- Memory: ~0.5 GB (fully remote)
- Top-k: 10 chunks / Max chunk: 1024 tokens

**Note:** This preset is now fully remote (no local torch/sentence-transformers). It differs from `remote-kisski` only in its intended context; both presets are identical in configuration.

**Requires:** `KISSKI_API_KEY` environment variable

**Platform:** `darwin` only — hidden from the preset list on any other host (see [How presets are stored](#how-presets-are-stored)).

---

### `remote-mpcdf` (MPCDF LLM Inference Service — temporary KISSKI workaround)

**Best for:** Riding out an exhausted KISSKI rate limit, using a short-lived job on the MPCDF LLM Inference Service (`llm.mpcdf.mpg.de`) instead

**Configuration:**

- Embedding: `multilingual-e5-large-instruct` (MPCDF remote, vLLM, 1024-dim — same model and vector space as `remote-kisski`)
- LLM: `openai/gpt-oss-120b` (MPCDF remote, vLLM, 131k context)
- Memory: ~0.5 GB (fully remote)
- Top-k: 10 chunks / Max chunk: 1024 tokens

**What's different about this preset:** unlike KISSKI's per-user keys, this is a shared, institution-provided gateway: the `mpcdf` provider has credential scope `shared` (the admin sets the base URL and API key once for all users; it costs them nothing and there is nothing for them to operate). The job's endpoint and key rotate with each short-lived MPCDF job, so they are set at runtime via `POST /api/config/remote-fields` instead of being literals in the preset file. Its health row asks the gateway's `/v1/models`.

**Advantages:**

- Uses the same embedding model/vector space as `remote-kisski`, `apple-silicon-kisski`, and `windows-test` — switching to/from this preset at runtime (no restart) is supported for exactly this reason.
- No local GPU or large Python dependencies.

**Trade-offs:** Requires an active MPCDF HPC allocation and manually starting a job through the MPCDF LLM Inference Service UI; not a general-purpose recommendation — use `remote-kisski` under normal circumstances.

**Requires:** `MPCDF_EMBEDDING_BASE_URL`, `MPCDF_EMBEDDING_API_KEY`, `MPCDF_LLM_BASE_URL`, `MPCDF_LLM_API_KEY` — set via `POST /api/config/remote-fields` (admin only, no restart) or as environment variables. The base URL the MPCDF LLM Inference Service UI hands out for a job doesn't include the `/v1` API-version segment; it's appended automatically if missing (see "Admin: runtime preset switching & shared remote config" below), so pasting the bare job URL works either way.

---

### `runpod` (self-hosted RunPod serverless endpoints)

**Best for:** Self-hosting embedding + LLM inference on pay-per-use, scale-to-zero GPU endpoints you control, as an alternative to depending on KISSKI/MPCDF

**Configuration:**

- Embedding: `intfloat/multilingual-e5-large-instruct` (RunPod remote, `runpod/worker-v1-vllm` in pooling mode, 1024-dim)
- LLM: `Qwen/Qwen2.5-7B-Instruct` (RunPod remote, `runpod/worker-v1-vllm`, 32k context)
- Memory: ~0.5 GB (fully remote)
- Top-k: 10 chunks / Max chunk: 800 tokens

**What's different about this preset:** unlike KISSKI's fixed shared gateway, these are two serverless endpoints in a RunPod account, handled by the `runpod` provider. The bundled preset uses credential scope `user`: every user enters their own RunPod API key in the plugin's Preferences, the endpoints live in that user's account (named `zotero-rag-embedding` and `zotero-rag-llm`) and the backend finds their URLs from the key, so there are no URL fields to fill in. "Provision endpoints" creates, wakes or resumes both endpoints on the caller's own key and any signed-in user may run it (one job per side at a time; the two sides run independently). The optional "Provisioning API key" next to the button is a RunPod API key; once entered it is kept in memory in the running Zotero (so Resume, Retry and Pause need no re-entry) and is never saved or sent to the backend for storage. It is gone when Zotero quits. Each side is its own job: if one fails the other still completes, and a failed side can be retried alone. After provisioning you can use a key restricted to the two endpoints for day-to-day queries (a restricted key is tied to endpoint IDs, so update it if an endpoint is ever recreated — the health row then shows `HTTP 403: the API key has no access to endpoint <id>`); supply a full-access key again in the "Provisioning key" field whenever you provision.

**Institution-funded variant (scope `managed`).** To let an institution pay for one shared RunPod account that only admins operate, copy `runpod.json` to a new file (for example `runpod-managed.json`), set `"scope": "managed"` on both sides and replace each side's `model_kwargs` with `{"shared_base_url_env": "RUNPOD_EMBEDDING_BASE_URL" (or `RUNPOD_LLM_BASE_URL`), "shared_api_key_env": "RUNPOD_API_KEY"}`. The admin then sets `RUNPOD_API_KEY` under "Service API Keys", only admins can provision, pause or resume, and there is one global job slot.

Per-side tuning lives in the preset: `embedding.provider.options` / `llm.provider.options` accept `gpu`, `workers_max` (at least 1), `idle_timeout`, `data_centers`, `image` and `container_disk_gb`. A paused endpoint (`workersMax` 0, which RunPod answers with HTTP 409 `ENDPOINT_PAUSED`) stops all billing and wake-ups until it is provisioned again; provisioning resumes it.

Admins can also run `uv run python bin/provision.py --preset runpod` on the server (`--side llm` for one side, `--pause` to stop billing, `--teardown` to delete the resources, `--recreate`, `--yes`, `--skip-warmup`). It takes the key from the shared store, or asks for it at a hidden prompt (used for that run only); it writes the URLs to the shared store (`<data_path>/system/admin_settings.json`) and never touches `.env`. It is idempotent and finds existing endpoints by name. In a container, run it inside the container (`podman exec <container> python bin/provision.py --preset runpod`) so the data volume is shared with the backend.

**Advantages:**

- Full control over cost and data residency — no dependency on an external academic gateway's rate limits or availability.
- Pay only for active GPU-seconds; scales to zero between uses.
- No local GPU or large Python dependencies on the host running zotero-rag itself.

**Trade-offs:** Requires a RunPod account and provisioning (re-run it, or resume, to wake paused endpoints) before use; a cold start after idle time adds latency to the first request. Uses a smaller, self-hosted 7B LLM rather than KISSKI's 70B model — lower answer quality in exchange for independence from KISSKI. Hot-swappable at runtime with `remote-kisski`/`remote-mpcdf` — all three use the same underlying embedding model, just served under different literal API model-name strings (KISSKI/MPCDF use the short alias `multilingual-e5-large-instruct`; `runpod` uses the full HuggingFace repo id `intfloat/multilingual-e5-large-instruct`, required by the RunPod worker image). The runtime preset switcher's `compatible_presets` list (see "Admin: runtime preset switching & shared remote config" below) compares embedding models by basename, so this naming difference doesn't block the switch.

**Health and provisioning from the plugin:** a provider that can report readiness gives `GET /api/config/health` a status per side: `ready`, `cold` (scaled to zero, wakes normally on the next request), `paused`, `throttled` (the provider has no GPU capacity for the endpoint right now) or `unreachable` (`null` for a side with no health check). `GET /api/config/providers` describes each side's provider for the plugin (id, label, credential scope, capability flags, the one-time credential provisioning accepts, an `unavailable_hint` and `operable_by_caller`), and the Preferences pane renders one section per side from it, so the plugin contains no provider-specific code. A provider that supports provisioning (`supports_provisioning`) lets whoever may operate it run `POST /api/config/provision` (body `{"keys": {...}, "sides": [...]}`, both optional) as a background job per side (poll `GET /api/config/provision/status`, which reports a status and progress lines per side) and applies any base URL the provider returns via the shared remote config. Design: `docs/superpowers/specs/2026-10-10-huggingface-preset-and-provisioner-adapters-design.md`.

**Requires:** your personal `RUNPOD_API_KEY` (Preferences pane, or the "Provisioning API key" field). The endpoint URLs are looked up from the key and cached for a few minutes; provisioning refreshes them at once.

---

### `huggingface` (your own Hugging Face Inference Endpoints)

**Best for:** Pay-per-use, scale-to-zero GPU endpoints billed to your own Hugging Face account, as an alternative to RunPod.

**Configuration:**

- Embedding: `intfloat/multilingual-e5-large-instruct` on a text-embeddings-inference (`tei`) endpoint, `nvidia-t4` in `aws/eu-west-1`
- LLM: `Qwen/Qwen2.5-7B-Instruct` on a `vllm` endpoint, `nvidia-a10g` in `aws/eu-west-1`
- Memory: ~0.5 GB (fully remote); Top-k: 10 chunks / Max chunk: 800 tokens

**What's different about this preset:** like `runpod`, credential scope `user`: every user enters their own Hugging Face token, the endpoints (`zotero-rag-embedding`, `zotero-rag-llm`) live in that user's namespace and the backend finds their URL from the token. Two tokens are involved: the stored `HF_TOKEN` only calls endpoints (a fine-grained token with "Make calls to Inference Endpoints"), while the optional *Provisioning token* entered next to the Provision/Pause buttons is kept in memory in the running Zotero until it quits (so Resume, Retry and Pause need no re-entry) and is never saved or stored by the backend; it needs the "Manage Inference Endpoints" permission (or a classic Write token) and a billing method on the account, and it is also what Pause and Resume use (without one Hugging Face answers "Payment method required", which the plugin shows as a clear message). Per-side options in the preset's `provider.options`: `namespace` (default: the token's own user), `vendor`, `region`, `engine` (`tei`, `vllm` or `tgi`), `instance`, `instance_size`, `scale_to_zero_timeout_min` (15 to 2880), `min_replica`, `max_replica` and `image`. The text-embeddings-inference image tag depends on the GPU architecture; only the T4 tag is known to the provider, so another instance needs an explicit `image`. A scaled-to-zero or starting endpoint answers with HTTP 503 for about a minute (shown as "cold"; the first query after idle time can fail with an "endpoint unavailable" message and succeeds when retried), and a paused one answers 400 "endpoint is paused".

**Trade-offs:** Hugging Face bills per instance-hour with a 15-minute minimum idle tail, so for a few queries a day RunPod's per-second billing is likely cheaper; for sustained indexing the hourly rates are competitive. Costs are read on Hugging Face's own dashboard.

**Requires:** your personal `HF_TOKEN` (Preferences pane, or the one-time token for provisioning). This is a provider credential and unrelated to the server-side `HF_TOKEN` environment setting that downloads gated *local* model weights.

---

### Mixed presets

Each side names its own provider, so a preset can combine them, for example Hugging Face embeddings with an Anthropic LLM (`"llm": {"model_names": ["claude-sonnet-4"], "model_kwargs": {}, "provider": {"id": "anthropic"}}`), or a local embedding model with an Anthropic LLM. Only the sides whose provider can provision get provisioning controls, and each key is asked for once under the side that uses it (`HF_TOKEN` and `ANTHROPIC_API_KEY` in the first example). Copy a bundled preset to a new file name to build one.

### Pause and resume

A provider that can pause an endpoint (`runpod`, `huggingface`) gets a **Pause** button in its section of the Preferences pane while the endpoint is ready or cold, and **Resume** once it is paused. Pausing stops the billing and the wake-ups and keeps the URL; `POST /api/config/suspend` (same gating, job slots and body as `POST /api/config/provision`) pauses, and provisioning resumes. A paused embedding side makes automatic indexing skip that owner's libraries (reason `embedding_paused`, shown in the indexing status dialog) and makes queries fail at once with a "paused" message instead of calling the endpoint; this never counts toward quarantining an upload. One user's pause does not affect anyone else's libraries. `bin/provision.py --preset <name> --pause` does the same from the command line.

**Question dialog.** When the dialog opens it checks the endpoints (`GET /api/config/health`). While an endpoint it needs is cold, paused or unreachable, Submit/Index is disabled and a status message is shown left of the buttons (indexing needs the embedding side, a question needs both); the check repeats every few seconds until the endpoint is ready. The first time a cold endpoint is seen the plugin calls `POST /api/config/warmup`, which sends one minimal request to each cold side of the caller's preset (at most once every two minutes per user and side) so the endpoint is starting while the user types. A paused or unprovisioned endpoint is never woken this way; it needs Resume or Provision in Preferences.

---

## Admin: runtime preset switching & shared remote config

Two admin-only capabilities exist alongside `MODEL_PRESET` (which still requires a restart to change):

**Switching the active preset without a restart.** `POST /api/config` with `{"preset_name": "..."}` switches immediately, for every caller and for the hourly cron auto-indexer — but only to a preset that shares the current one's embedding model (same vector space, so the already-open vector store stays valid). `GET /api/config`'s `compatible_presets` field lists which presets currently qualify; switching to anything else (a different embedding model, or a local-model preset) still requires `MODEL_PRESET` + a restart. The Zotero plugin's Preferences pane exposes this as the "Active Model Preset" dropdown.

`GET /api/config` also returns `switchable_presets`: `[{"name", "active", "credentials"}]`, i.e. `compatible_presets` filtered to those with usable credentials (the active preset is always listed). A preset's credentials count as usable when every key its embedding and LLM sides require is present and not known to be invalid: a `shared_base_url`/`shared_api_key` value set via `POST /api/config/remote-fields`; a personal key (e.g. `KISSKI_API_KEY`) sent in the requesting admin's header or stored (non-invalid) in the auto-index key store. Keys are not live-validated, so listing never spends rate-limited quota. `POST /api/config` accepts a preset that lacks credentials: the server default can be switched first and the keys entered in that preset's sections afterwards (it is not ready until they are set). A successful switch also discards the cached rate-limit headers of the previous provider and clears the stored embedding-key rate-limit skips, so the next auto-index run retries libraries that were skipped for `embedding_rate_limit`. The plugin's auto-index status dialog offers the same switch to admins.

**Default preset and per-user choice.** The server has one *default* preset: the admin-stored one (set with `POST /api/config`), else `MODEL_PRESET`, else `remote-kisski`. Everyone runs on it until they choose their own. `PUT /api/config/my-preset` (`{"preset_name": "<name>"}` or `null` to return to the default) stores a signed-in user's choice server-side; `GET /api/config/my-preset` and `GET /api/config` report `default_preset`, the caller's effective `preset_name`, `selectable_presets` and, when a saved choice is no longer honoured, `preset_fell_back`. A choice is honoured only while the preset exists, passes validation and is compatible with the default (both sides remote and the same embedding model, since the vector store is shared); to select one the user must have usable credentials for it (their own key sent in its header for `user` scope, or the shared key set for `managed`/`shared`). Requests without an identity (a loopback server, the public query page) always run on the default, and the cron indexer indexes each owner's libraries on that owner's preset using that owner's key for it. Keys for presets a user is not currently on stay in their key store, so switching back needs no re-entry. `POST /api/config` changes only the default.

**Setting shared remote endpoint/API-key values.** Presets like `remote-mpcdf`, whose endpoint URL and API key rotate with each short-lived job, declare this via `shared_base_url_env`/`shared_api_key_env` in their preset file (`data/presets/remote-mpcdf.json`) rather than a fixed `base_url`. `GET /api/required-keys` reports these fields with `kind: "shared_base_url"`/`"shared_api_key"` and an `is_set` flag (never the value itself); `POST /api/config/remote-fields` with `{"values": {"MPCDF_EMBEDDING_BASE_URL": "...", ...}}` sets them — persisted in `<data_path>/system/admin_settings.json` and picked up immediately by interactive queries and the cron indexer alike, no restart needed. Base URLs are stored as plain text; every `*_API_KEY` value is Fernet-encrypted at rest (as `{"enc": "<token>"}`) with `AUTOINDEX_SECRET`, through the central module `backend/services/secret_store.py`. Without `AUTOINDEX_SECRET`, saving a key is refused (HTTP 503) while URLs can still be saved. A plaintext key already in the file keeps working and is encrypted on the next write or at backend startup. To move provisioned credentials between instances, re-send the values with `POST /api/config/remote-fields` (the target instance encrypts them with its own `AUTOINDEX_SECRET`). The Preferences pane's "Service API Keys" section renders these as a text/password field per value, distinct from a personal API key field (e.g. `KISSKI_API_KEY`), which is never shared across users.

A stored `shared_base_url`-kind value is normalized on every read — `backend.services.admin_settings_store.normalize_base_url` appends `/v1` if it's missing (leaving a URL that already ends in `/v1` untouched) — since the openai-compatible client appends `/embeddings`/`/chat/completions` directly to whatever base URL it's given, and a bare job URL without `/v1` would otherwise 404.

Both endpoints require an admin — an owner/admin of the server's `AUTHORIZED_GROUP_ID` (the same requirement as the autoindex scheduler's pause/resume controls, see [cron-indexing.md](cron-indexing.md)) — except on a loopback/personal deployment, where this check is skipped. See `docs/superpowers/specs/2026-10-08-dynamic-remote-preset-config-design.md` for the full design.

---

## Quick Selection Guide

| Your Setup | Recommended Preset |
| ---------- | ----------------- |
| Any machine + KISSKI access (recommended) | `remote-kisski` |
| Apple Silicon Mac + KISSKI access | `apple-silicon-kisski` |
| OpenAI API access | `remote-openai` |
| Windows (no GPU setup) | `windows-test` |
| Apple Silicon Mac (32 GB), offline/privacy | `apple-silicon-32gb` |
| High-memory GPU system (>24 GB), offline | `high-memory` |
| CPU-only or low memory, offline | `cpu-only` |
| Cloud server (no GPU) + KISSKI access | `cloud-server-kisski` |
| Want full cost/data control, willing to self-host on RunPod | `runpod` |

---

## Performance Comparison

### Indexing Speed

1. `remote-openai` / `remote-kisski` / `apple-silicon-kisski` / `windows-test` — fast (parallel API calls, no local model load)
2. `apple-silicon-32gb` — fast (M-series Neural Engine)
3. `high-memory` — good (GPU acceleration)
4. `cpu-only` — slowest (CPU-bound)

### Answer Quality

1. `remote-openai` / `remote-kisski` / `apple-silicon-kisski` / `windows-test` — excellent (large models, 128k context)
2. `apple-silicon-32gb` / `high-memory` — good (7B models, 8k context)
3. `cpu-only` — basic (1.1B model, 2k context)

### Privacy Level

1. `apple-silicon-32gb` / `cpu-only` / `high-memory` — fully local
2. `remote-kisski` / `apple-silicon-kisski` / `windows-test` — fully remote (KISSKI is an academic service hosted by GWDG)
3. `remote-openai` — fully remote (commercial)

---

## Optional local dependencies

The presets `cpu-only`, `high-memory`, and `apple-silicon-32gb` run embedding models locally and require `sentence-transformers` and `torch`, which add ~1-2 GB to the installation.

These are listed as optional in `pyproject.toml`. Install them when needed:

```bash
uv sync --extra local-models
```

Or manually:

```bash
uv add sentence-transformers torch
```

If you switch from a local preset to a fully-remote one, you can remove these packages to save disk space:

```bash
uv remove sentence-transformers torch transformers accelerate bitsandbytes
```

For Docker deployments, the image can be built without these packages using the `INSTALL_OCR=false` build argument (see [container-deployment.md](container-deployment.md)). The `sentence-transformers`/`torch` packages are never included in the Docker image — remote presets work without them by design.

---

## Choosing a preset by evaluating embedding performance

The quick-selection guide above is a good starting point, but embedding quality varies by language, domain, and query style. Use `scripts/eval_embeddings.py` to run a data-driven comparison before committing to a preset.

### Quick smoke test (no download required)

Runs the built-in multilingual pair corpus (22 English + German + cross-lingual pairs):

```bash
uv run python scripts/eval_embeddings.py \
  --preset-a remote-kisski \
  --preset-b cloud-server-kisski
```

This verifies basic functionality and measures throughput, but the corpus is too easy to reveal quality differences — all well-separated presets will score 1.0.

### Published IR benchmark (recommended)

Downloads a real retrieval dataset and evaluates nDCG@10, MRR@10, and Recall@{1,5,10} against ground-truth relevance judgments. Requires `uv sync --extra eval`.

```bash
# English academic content (scientific fact-checking, ~500 corpus docs)
uv run python scripts/eval_embeddings.py \
  --preset-a remote-kisski \
  --preset-b cloud-server-kisski \
  --mteb-task scifact --max-queries 50

```

| Task | Language | Corpus size | Best for |
| ---- | -------- | ----------- | -------- |
| `scifact` | English | ~5 K | Academic / scientific content |
| `nfcorpus` | English | ~3.6 K | Biomedical content |
| `fiqa` | English | ~57 K (sampled) | Financial / social-science content |

For multilingual quality, use the built-in pair corpus (`--data`) with your own German query/passage pairs extracted from your Zotero library.

### Checking vector compatibility before switching presets

If you already have an indexed library and want to switch presets, first check whether the existing vectors are reusable — re-indexing a large library can take hours:

```bash
uv run python scripts/check_embedding_compat.py \
  --preset-a remote-kisski \
  --preset-b apple-silicon-32gb
```

Compatibility requires cosine similarity ≥ 0.999 across all probe texts. A mismatch (different model, different dimension, or server-side preprocessing) means a full re-index is needed.

### Interpreting results

- **nDCG@10** is the primary MTEB metric — the most meaningful single number for ranking quality. Published scores for `multilingual-e5-large-instruct` on MIRACL-de are ~0.72; `multilingual-e5-small` scores ~0.66.
- **Throughput** measured on a 22-passage corpus is dominated by model load time and is not representative. Run on a larger corpus (≥ 500 docs) for reliable throughput numbers.
- **Embedding dim** mismatch is an instant incompatibility — vectors cannot be shared between Qdrant collections of different dimensions.

---

## Usage

Set `MODEL_PRESET` in your `.env` file to choose the preset the server starts with:

```bash
MODEL_PRESET=remote-kisski
```

### Credentials

Provider API keys are never read from `.env` or the server's environment. Where a key comes from depends on the provider's credential scope (see [providers.md](providers.md)):

- `user` (KISSKI, OpenAI, Anthropic, `runpod` with the default scope): each user enters their own key in the plugin's Preferences. It is sent with their requests, and for automatic indexing the user can opt in to storing it encrypted in the auto-index key store.
- `managed` (for example `runpod` as configured in the bundled preset) and `shared` (MPCDF): the admin sets the key, and the endpoint URL where the provider cannot derive it, with `POST /api/config/remote-fields` or the Preferences pane's "Service API Keys" section. They are stored encrypted in `<data_path>/system/admin_settings.json` using `AUTOINDEX_SECRET`.

The Hugging Face token for downloading gated *local* model weights (`HF_TOKEN`) is not a provider credential and stays an environment setting.

See `data/presets/*.json` for each preset's actual configuration (or `backend/config/default_presets/*.json` for the bundled defaults before they're copied), and [backend/config/presets.py](../backend/config/presets.py) for the schema (`HardwarePreset`/`EmbeddingConfig`/`LLMConfig`/`RAGConfig`) and loader these files are validated against.
