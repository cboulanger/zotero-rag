# Hardware Presets

Configuration presets optimized for different hardware scenarios. Each preset defines the optimal models and settings for embeddings, LLM, and RAG retrieval.

## Available Presets

### `apple-silicon-kisski` (Recommended for Apple Silicon + KISSKI)

**Best for:** Apple Silicon Macs (16-32GB RAM) with GWDG KISSKI API access

**Configuration:**
- Embedding: `nomic-ai/nomic-embed-text-v1.5` (local, fast on Neural Engine)
- LLM: `mistral-large-instruct` (KISSKI remote, 128k context)
- Memory: ~2GB
- Top-k: 10 chunks
- Max chunk: 1024 tokens

**Advantages:**
- Fast local embeddings leveraging M-series Neural Engine
- Highest quality answers from KISSKI's large model (120B+ parameters)
- Optimal hybrid: fast indexing + excellent inference
- Privacy-friendly (embeddings stay local)

**Requires:** `KISSKI_API_KEY` environment variable

---

### `apple-silicon-32gb` (Best for Offline/Privacy)

**Best for:** Apple Silicon Macs with 32GB RAM, fully local operation

**Configuration:**
- Embedding: `nomic-ai/nomic-embed-text-v1.5` (local)
- LLM: `mistralai/Mistral-7B-Instruct-v0.3` (local, 4-bit quantized)
- Memory: ~10GB
- Top-k: 10 chunks
- Max chunk: 768 tokens

**Advantages:**
- Fully offline, no internet required
- Complete privacy (all data stays local)
- Good quality answers from 7B model
- Fast inference on Apple Silicon

**Trade-offs:**
- Lower answer quality than large remote models
- Limited 8k context window vs 128k for remote

---

### `high-memory` (Generic High-Memory Systems)

**Best for:** Systems with >24GB RAM (GPU or Apple Silicon)

**Configuration:**
- Embedding: `sentence-transformers/all-mpnet-base-v2` (local)
- LLM: `mistralai/Mistral-7B-Instruct-v0.3` (local, 8-bit quantized)
- Memory: ~16GB
- Top-k: 10 chunks
- Max chunk: 768 tokens

**Use case:** Generic high-memory preset for non-Apple Silicon systems with dedicated GPU

---

### `cpu-only` (Minimal Hardware)

**Best for:** CPU-only systems, low-memory environments

**Configuration:**
- Embedding: `sentence-transformers/all-MiniLM-L6-v2` (local, lightweight)
- LLM: `TinyLlama/TinyLlama-1.1B-Chat-v1.0` (local, 4-bit quantized)
- Memory: ~3GB
- Top-k: 5 chunks
- Max chunk: 384 tokens

**Advantages:**
- Runs on minimal hardware
- Fully local

**Trade-offs:**
- Lower quality embeddings and answers
- Limited context (2k tokens)
- Best for testing or resource-constrained environments

---

### `remote-openai` (OpenAI API)

**Best for:** Users with OpenAI API access

**Configuration:**
- Embedding: OpenAI embeddings API (remote)
- LLM: `gpt-4o-mini` (remote)
- Memory: ~1GB (minimal local)
- Top-k: 10 chunks
- Max chunk: 1024 tokens

**Advantages:**
- Excellent quality embeddings and answers
- Large 128k context window
- Minimal local resource usage

**Requires:** `OPENAI_API_KEY` environment variable

---

### `remote-kisski` (Legacy KISSKI)

**Best for:** Systems without good local compute, using KISSKI API

**Configuration:**
- Embedding: `sentence-transformers/all-MiniLM-L6-v2` (local, basic)
- LLM: `mistral-large-instruct` (KISSKI remote)
- Memory: ~1GB
- Top-k: 10 chunks
- Max chunk: 1024 tokens

**Note:** For Apple Silicon systems, use `apple-silicon-kisski` instead for much faster indexing.

**Requires:** `KISSKI_API_KEY` environment variable

---

### `windows-test` (Windows Development)

**Best for:** Windows development/testing

**Configuration:**
- Embedding: OpenAI embeddings API (remote, avoids PyTorch issues)
- LLM: `mistral-large-instruct` (KISSKI remote)
- Memory: ~0.5GB (fully remote)
- Top-k: 10 chunks
- Max chunk: 1024 tokens

**Requires:** `OPENAI_API_KEY` and `KISSKI_API_KEY` environment variables

---

## Quick Selection Guide

| Your Setup | Recommended Preset |
|------------|-------------------|
| Apple Silicon Mac (16-32GB) + KISSKI access | `apple-silicon-kisski` |
| Apple Silicon Mac (32GB), offline/privacy priority | `apple-silicon-32gb` |
| High-memory GPU system (>24GB) | `high-memory` |
| CPU-only or low memory (<8GB) | `cpu-only` |
| OpenAI API access | `remote-openai` |
| Generic system + KISSKI access | `remote-kisski` |
| Windows development | `windows-test` |

## Performance Comparison

### Indexing Speed (Most Time-Intensive)
1. `apple-silicon-kisski` / `apple-silicon-32gb` - Fast (optimized for M-series)
2. `high-memory` - Good (GPU acceleration)
3. `remote-openai` / `windows-test` - Moderate (API limits)
4. `remote-kisski` - Slower (basic local model)
5. `cpu-only` - Slowest (CPU-bound)

### Answer Quality
1. `remote-openai` / `apple-silicon-kisski` / `remote-kisski` - Excellent (large models, 128k context)
2. `apple-silicon-32gb` / `high-memory` - Good (7B models, 8k context)
3. `cpu-only` - Basic (1.1B model, 2k context)

### Privacy Level
1. `apple-silicon-32gb` / `cpu-only` / `high-memory` - Fully local
2. `apple-silicon-kisski` / `remote-kisski` - Embeddings local, LLM remote
3. `remote-openai` / `windows-test` - Fully remote

## Usage

Set the `MODEL_PRESET` environment variable in your `.env` file:

```bash
# Example: Use optimized Apple Silicon + KISSKI preset
MODEL_PRESET=apple-silicon-kisski
```

For remote presets, also configure required API keys:

```bash
# For KISSKI presets
KISSKI_API_KEY=your_kisski_key_here

# For OpenAI presets
OPENAI_API_KEY=your_openai_key_here
```

## Technical Details

See [backend/config/presets.py](../backend/config/presets.py) for complete configuration details including:
- Exact model names and versions
- Quantization settings
- Batch sizes
- RAG retrieval parameters
- Memory budgets
