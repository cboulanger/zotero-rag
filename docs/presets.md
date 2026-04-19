# Hardware Presets

Configuration presets optimized for different hardware scenarios. Each preset defines the models and settings for embeddings, LLM inference, and RAG retrieval.

## Dependency overview

| Preset | `sentence-transformers` / `torch` required? | API keys |
| ------ | ------------------------------------------- | -------- |
| `apple-silicon-32gb` | Yes (~1-2 GB) | — |
| `high-memory` | Yes (~1-2 GB) | — |
| `cpu-only` | Yes (~1-2 GB) | — |
| `apple-silicon-kisski` | **No** | `KISSKI_API_KEY` |
| `remote-kisski` | **No** | `KISSKI_API_KEY` |
| `remote-openai` | **No** | `OPENAI_API_KEY` |
| `cloud-server-kisski` | Yes (~500 MB) | `KISSKI_API_KEY` |
| `windows-test` | **No** | `KISSKI_API_KEY` |

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
```

See [backend/config/presets.py](../backend/config/presets.py) for complete configuration details.
