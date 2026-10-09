# Hardware Presets

Configuration presets optimized for different hardware scenarios. Each preset defines the models and settings for embeddings, LLM inference, and RAG retrieval.

## How presets are stored

Each preset is a JSON file under `<data_path>/presets/<name>.json` (e.g. `data/presets/remote-kisski.json` in a default local checkout) — not hardcoded in Python. The 10 presets documented below ship as bundled defaults in `backend/config/default_presets/`; the backend copies any of them that aren't already present in `<data_path>/presets/` on every startup, but **never overwrites an existing file** there. This means:

- **Editing a preset is just editing its JSON file** — change `top_k`, swap a model name, tune `batch_size`, etc. in `data/presets/<name>.json`. No code change or rebuild needed. An already-running process picks up the edit after a restart (an in-memory cache keeps a loaded preset's *content* fast to re-read for the rest of that process's life — see `backend/config/presets.py`'s module docstring); adding a *new* preset file is picked up immediately, no restart required.
- **Adding a custom preset** means creating a new `<data_path>/presets/<your-name>.json` matching the schema below. It shows up in `GET /api/config`'s `available_presets` (and the Zotero plugin's preset dropdown) right away.
- **Deleting or renaming a bundled default** is safe — it won't be silently recreated once you've touched `<data_path>/presets/`; only a file that's *missing entirely* gets reseeded.

A preset file's fields mirror `backend.config.presets.HardwarePreset` and its nested `embedding`/`llm`/`rag` sections (see that module for the full Pydantic schema and field descriptions). The file's own `name` key, if present, is ignored — a preset's identity always comes from its filename.

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
| `runpod` | **No** | `RUNPOD_API_KEY` (shared, admin-set — see below) | any |

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

**What's different about this preset:** MPCDF's embedding job and LLM job are two independent Slurm jobs, each lasting at most 8 hours, each handing out its own freshly-generated endpoint URL and API key. Rather than editing `.env` and restarting the backend every time a job rotates, these four values are set at runtime through the admin API — see "Admin: runtime preset switching & shared remote config" below.

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

**What's different about this preset:** unlike KISSKI's fixed shared gateway, these are two serverless endpoints you provision yourself by running `uv run python scripts/provision_runpod_endpoints.py` (it reads `RUNPOD_API_KEY` from `.env`; pass `--api-key` to override). The script is idempotent — re-running it finds existing endpoints by name and sends a lightweight warm-up request to wake them from scale-to-zero, rather than creating duplicates. Endpoint URLs are only known after provisioning, so (like `remote-mpcdf`) they're set at runtime through the admin API rather than being a fixed literal in the preset file — see "Admin: runtime preset switching & shared remote config" below.

**Advantages:**

- Full control over cost and data residency — no dependency on an external academic gateway's rate limits or availability.
- Pay only for active GPU-seconds; scales to zero between uses.
- No local GPU or large Python dependencies on the host running zotero-rag itself.

**Trade-offs:** Requires a RunPod account and the provisioning script to be run (and re-run to wake idle endpoints) before use; a cold start after idle time adds latency to the first request. Uses a smaller, self-hosted 7B LLM rather than KISSKI's 70B model — lower answer quality in exchange for independence from KISSKI. Hot-swappable at runtime with `remote-kisski`/`remote-mpcdf` — all three use the same underlying embedding model, just served under different literal API model-name strings (KISSKI/MPCDF use the short alias `multilingual-e5-large-instruct`; `runpod` uses the full HuggingFace repo id `intfloat/multilingual-e5-large-instruct`, required by the RunPod worker image). The runtime preset switcher's `compatible_presets` list (see "Admin: runtime preset switching & shared remote config" below) compares embedding models by basename, so this naming difference doesn't block the switch.

**Health and provisioning from the plugin:** a preset can declare `health_check_provider` (on its `embedding` and/or `llm` config; currently only `"runpod"`) and `provisioning_script`. For such a preset, `GET /api/config/health` reports each endpoint as `ready`, `cold` (scaled to zero, wakes normally on the next request), `throttled` (RunPod has no available GPU capacity for the endpoint right now — jobs sit queued and nothing will progress until capacity frees up or the endpoint's GPU type is changed) or `unreachable` (`null` for a side with no health check), and the Preferences pane shows a status row per side. If `provisioning_script` is set, an admin sees a "Provision endpoints" button that calls `POST /api/config/provision`; the backend runs the script as `uv run python <script> --json` in the background (poll `GET /api/config/provision/status`) and applies the base URLs from its single `PROVISION_RESULT: {...}` stdout line via the shared remote config. Design: `docs/superpowers/specs/2026-10-09-endpoint-health-provisioning-design.md`.

**Requires:** `RUNPOD_API_KEY`, `RUNPOD_EMBEDDING_BASE_URL`, `RUNPOD_LLM_BASE_URL` — set automatically in `.env` by `scripts/provision_runpod_endpoints.py`, or via `POST /api/config/remote-fields` (admin only, no restart) to apply to an already-running backend.

---

## Admin: runtime preset switching & shared remote config

Two admin-only capabilities exist alongside `MODEL_PRESET` (which still requires a restart to change):

**Switching the active preset without a restart.** `POST /api/config` with `{"preset_name": "..."}` switches immediately, for every caller and for the hourly cron auto-indexer — but only to a preset that shares the current one's embedding model (same vector space, so the already-open vector store stays valid). `GET /api/config`'s `compatible_presets` field lists which presets currently qualify; switching to anything else (a different embedding model, or a local-model preset) still requires `MODEL_PRESET` + a restart. The Zotero plugin's Preferences pane exposes this as the "Active Model Preset" dropdown.

**Setting shared remote endpoint/API-key values.** Presets like `remote-mpcdf`, whose endpoint URL and API key rotate with each short-lived job, declare this via `shared_base_url_env`/`shared_api_key_env` in their preset file (`data/presets/remote-mpcdf.json`) rather than a fixed `base_url`. `GET /api/required-keys` reports these fields with `kind: "shared_base_url"`/`"shared_api_key"` and an `is_set` flag (never the value itself); `POST /api/config/remote-fields` with `{"values": {"MPCDF_EMBEDDING_BASE_URL": "...", ...}}` sets them — persisted in `<data_path>/system/admin_settings.json` (plaintext; these are short-lived, sandboxed-job credentials, not long-term secrets) and picked up immediately by interactive queries and the cron indexer alike, no restart needed. The Preferences pane's "Service API Keys" section renders these as a text/password field per value, distinct from a personal API key field (e.g. `KISSKI_API_KEY`), which is never shared across users.

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

Set `MODEL_PRESET` in your `.env` file:

```bash
MODEL_PRESET=remote-kisski
```

For remote presets, also set the required API key:

```bash
# KISSKI (remote-kisski, apple-silicon-kisski, windows-test)
KISSKI_API_KEY=your_kisski_key_here

# OpenAI (remote-openai)
OPENAI_API_KEY=sk-...

# MPCDF (remote-mpcdf) — typically set at runtime via POST /api/config/remote-fields
# instead (see "Admin: runtime preset switching & shared remote config" above), since
# these rotate with each new MPCDF job; shown here only for the env-var fallback path.
# The trailing /v1 is optional — it's appended automatically if omitted.
MPCDF_EMBEDDING_BASE_URL=https://llm.mpcdf.mpg.de/<job-id>/v1
MPCDF_EMBEDDING_API_KEY=...
MPCDF_LLM_BASE_URL=https://llm.mpcdf.mpg.de/<job-id>/v1
MPCDF_LLM_API_KEY=...

# RunPod (runpod) — set automatically by scripts/provision_runpod_endpoints.py;
# shown here only for the env-var fallback path / manual editing.
RUNPOD_API_KEY=...
RUNPOD_EMBEDDING_BASE_URL=https://api.runpod.ai/v2/<embedding-endpoint-id>/openai/v1
RUNPOD_LLM_BASE_URL=https://api.runpod.ai/v2/<llm-endpoint-id>/openai/v1
```

See `data/presets/*.json` for each preset's actual configuration (or `backend/config/default_presets/*.json` for the bundled defaults before they're copied), and [backend/config/presets.py](../backend/config/presets.py) for the schema (`HardwarePreset`/`EmbeddingConfig`/`LLMConfig`/`RAGConfig`) and loader these files are validated against.
