# File-based hardware/model presets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move hardware/model presets out of the hardcoded `PRESETS` dict in `backend/config/presets.py` into individual JSON files under `<data_path>/presets/`, seeded from bundled defaults on first run, so an operator can add or tune a preset without a code change or redeploy.

**Architecture:** `backend/config/presets.py` keeps its Pydantic schema classes (`EmbeddingConfig`, `LLMConfig`, `RAGConfig`, `HardwarePreset`) unchanged, but replaces the static `PRESETS` dict with a file loader: `get_preset(name, data_path=None)` and `list_presets(data_path=None)` read `<data_path>/presets/<name>.json` on every call (no caching), and `ensure_default_presets(data_path)` copies the 9 bundled defaults from `backend/config/default_presets/` into that directory for any name not already present there. `Settings.ensure_directories()` calls the seeder on every startup; `Settings.get_hardware_preset()` passes its own `data_path` through and applies the `EMBEDDING_BATCH_SIZE` env override generically. Three other call sites (`backend/api/config.py`, `scripts/check_embedding_compat.py`, `scripts/eval_embeddings.py`) switch from the removed `PRESETS` dict to the loader functions.

**Tech Stack:** Python, Pydantic, `pathlib`/`json`/`shutil` (stdlib only — no new dependency).

**Spec:** `docs/superpowers/specs/2026-10-08-file-based-presets-design.md`

---

### Task 1: Bundled default preset JSON files

**Files:**
- Create: `backend/config/default_presets/apple-silicon-32gb.json`
- Create: `backend/config/default_presets/high-memory.json`
- Create: `backend/config/default_presets/cpu-only.json`
- Create: `backend/config/default_presets/remote-openai.json`
- Create: `backend/config/default_presets/apple-silicon-kisski.json`
- Create: `backend/config/default_presets/remote-kisski.json`
- Create: `backend/config/default_presets/cloud-server-kisski.json`
- Create: `backend/config/default_presets/windows-test.json`
- Create: `backend/config/default_presets/remote-mpcdf.json`

These are verbatim translations of the current `PRESETS` dict entries in
`backend/config/presets.py:87-358`, with two deliberate deviations (per
the spec): the `KISSKI_RAG_MODELS` list is inlined literally into each of
the 4 presets that used it, and `remote-kisski`'s `batch_size` is the
plain literal `256` (the `EMBEDDING_BATCH_SIZE` env override becomes
generic in Task 3, not baked into this one file). None of these files
include a `"name"` key — the loader always derives it from the filename
(Task 2).

- [ ] **Step 1: Create `backend/config/default_presets/apple-silicon-32gb.json`**

```json
{
  "description": "Optimized for Apple Silicon Macs with 32GB RAM — local multilingual embeddings via MPS",
  "embedding": {
    "model_type": "local",
    "model_name": "intfloat/multilingual-e5-large-instruct",
    "model_kwargs": {"device": "mps"},
    "batch_size": 64
  },
  "llm": {
    "model_type": "local",
    "model_names": ["mistralai/Mistral-7B-Instruct-v0.3"],
    "quantization": "4bit",
    "max_context_length": 8192,
    "max_answer_tokens": 2048,
    "temperature": 0.7,
    "model_kwargs": {"device_map": "auto", "trust_remote_code": true}
  },
  "rag": {
    "top_k": 10,
    "score_threshold": 0.35,
    "max_chunk_size": 800
  },
  "memory_budget_gb": 10.0
}
```

- [ ] **Step 2: Create `backend/config/default_presets/high-memory.json`**

```json
{
  "description": "For systems with >24GB RAM (GPU or Apple Silicon)",
  "embedding": {
    "model_type": "local",
    "model_name": "sentence-transformers/all-mpnet-base-v2",
    "batch_size": 64
  },
  "llm": {
    "model_type": "local",
    "model_names": ["mistralai/Mistral-7B-Instruct-v0.3"],
    "quantization": "8bit",
    "max_context_length": 8192,
    "max_answer_tokens": 2048,
    "temperature": 0.7,
    "model_kwargs": {"device_map": "auto"}
  },
  "rag": {
    "top_k": 10,
    "score_threshold": 0.4,
    "max_chunk_size": 768
  },
  "memory_budget_gb": 16.0
}
```

- [ ] **Step 3: Create `backend/config/default_presets/cpu-only.json`**

```json
{
  "description": "CPU-optimized smaller models",
  "embedding": {
    "model_type": "local",
    "model_name": "sentence-transformers/all-MiniLM-L6-v2",
    "batch_size": 16
  },
  "llm": {
    "model_type": "local",
    "model_names": ["TinyLlama/TinyLlama-1.1B-Chat-v1.0"],
    "quantization": "4bit",
    "max_context_length": 2048,
    "max_answer_tokens": 512,
    "temperature": 0.7,
    "model_kwargs": {"device_map": "cpu"}
  },
  "rag": {
    "top_k": 5,
    "score_threshold": 0.3,
    "max_chunk_size": 384
  },
  "memory_budget_gb": 3.0
}
```

- [ ] **Step 4: Create `backend/config/default_presets/remote-openai.json`**

```json
{
  "description": "Using OpenAI/Anthropic remote inference endpoints",
  "embedding": {
    "model_type": "remote",
    "model_name": "openai",
    "batch_size": 100
  },
  "llm": {
    "model_type": "remote",
    "model_names": ["gpt-4o-mini"],
    "max_context_length": 128000,
    "max_answer_tokens": 4096,
    "temperature": 0.7
  },
  "rag": {
    "top_k": 10,
    "score_threshold": 0.5,
    "max_chunk_size": 1024
  },
  "memory_budget_gb": 1.0
}
```

- [ ] **Step 5: Create `backend/config/default_presets/apple-silicon-kisski.json`**

```json
{
  "description": "Apple Silicon (16-32GB) with KISSKI remote embeddings + LLM (fully remote)",
  "embedding": {
    "model_type": "remote",
    "model_name": "multilingual-e5-large-instruct",
    "batch_size": 64,
    "model_kwargs": {
      "base_url": "https://chat-ai.academiccloud.de/v1",
      "api_key_env": "KISSKI_API_KEY"
    }
  },
  "llm": {
    "model_type": "remote",
    "model_names": [
      "mistral-large-3-675b-instruct-2512",
      "qwen3.5-122b-a10b",
      "gemma-4-31b-it",
      "deepseek-r1-distill-llama-70b",
      "qwen3-30b-a3b-instruct-2507"
    ],
    "max_context_length": 128000,
    "max_answer_tokens": 4096,
    "temperature": 0.7,
    "model_kwargs": {
      "base_url": "https://chat-ai.academiccloud.de/v1",
      "api_key_env": "KISSKI_API_KEY",
      "extra_body": {"chat_template_kwargs": {"enable_thinking": false}}
    },
    "models_status_url": "https://chat-ai.academiccloud.de/v1/models"
  },
  "rag": {
    "top_k": 10,
    "score_threshold": 0.35,
    "max_chunk_size": 800
  },
  "memory_budget_gb": 0.5
}
```

- [ ] **Step 6: Create `backend/config/default_presets/remote-kisski.json`**

```json
{
  "description": "Fully remote via GWDG KISSKI/SAIA Academic Cloud",
  "embedding": {
    "model_type": "remote",
    "model_name": "multilingual-e5-large-instruct",
    "batch_size": 256,
    "model_kwargs": {
      "base_url": "https://chat-ai.academiccloud.de/v1",
      "api_key_env": "KISSKI_API_KEY"
    }
  },
  "llm": {
    "model_type": "remote",
    "model_names": [
      "mistral-large-3-675b-instruct-2512",
      "qwen3.5-122b-a10b",
      "gemma-4-31b-it",
      "deepseek-r1-distill-llama-70b",
      "qwen3-30b-a3b-instruct-2507"
    ],
    "max_context_length": 128000,
    "max_answer_tokens": 4096,
    "temperature": 0.7,
    "model_kwargs": {
      "base_url": "https://chat-ai.academiccloud.de/v1",
      "api_key_env": "KISSKI_API_KEY",
      "extra_body": {"chat_template_kwargs": {"enable_thinking": false}}
    },
    "models_status_url": "https://chat-ai.academiccloud.de/v1/models"
  },
  "rag": {
    "top_k": 10,
    "score_threshold": 0.35,
    "max_chunk_size": 800
  },
  "memory_budget_gb": 0.5
}
```

- [ ] **Step 7: Create `backend/config/default_presets/cloud-server-kisski.json`**

```json
{
  "description": "Cloud server (16GB RAM, 4 vCPU, no GPU): local multilingual embeddings + KISSKI LLM",
  "embedding": {
    "model_type": "local",
    "model_name": "intfloat/multilingual-e5-small",
    "batch_size": 16
  },
  "llm": {
    "model_type": "remote",
    "model_names": [
      "mistral-large-3-675b-instruct-2512",
      "qwen3.5-122b-a10b",
      "gemma-4-31b-it",
      "deepseek-r1-distill-llama-70b",
      "qwen3-30b-a3b-instruct-2507"
    ],
    "max_context_length": 128000,
    "max_answer_tokens": 4096,
    "temperature": 0.7,
    "model_kwargs": {
      "base_url": "https://chat-ai.academiccloud.de/v1",
      "api_key_env": "KISSKI_API_KEY",
      "extra_body": {"chat_template_kwargs": {"enable_thinking": false}}
    },
    "models_status_url": "https://chat-ai.academiccloud.de/v1/models"
  },
  "rag": {
    "top_k": 10,
    "score_threshold": 0.3,
    "max_chunk_size": 768
  },
  "memory_budget_gb": 2.0
}
```

- [ ] **Step 8: Create `backend/config/default_presets/windows-test.json`**

```json
{
  "description": "Windows-compatible: fully remote via KISSKI (avoids PyTorch/CUDA setup)",
  "embedding": {
    "model_type": "remote",
    "model_name": "multilingual-e5-large-instruct",
    "batch_size": 64,
    "model_kwargs": {
      "base_url": "https://chat-ai.academiccloud.de/v1",
      "api_key_env": "KISSKI_API_KEY"
    }
  },
  "llm": {
    "model_type": "remote",
    "model_names": [
      "mistral-large-3-675b-instruct-2512",
      "qwen3.5-122b-a10b",
      "gemma-4-31b-it",
      "deepseek-r1-distill-llama-70b",
      "qwen3-30b-a3b-instruct-2507"
    ],
    "max_context_length": 128000,
    "max_answer_tokens": 4096,
    "temperature": 0.7,
    "model_kwargs": {
      "base_url": "https://chat-ai.academiccloud.de/v1",
      "api_key_env": "KISSKI_API_KEY",
      "extra_body": {"chat_template_kwargs": {"enable_thinking": false}}
    },
    "models_status_url": "https://chat-ai.academiccloud.de/v1/models"
  },
  "rag": {
    "top_k": 10,
    "score_threshold": 0.35,
    "max_chunk_size": 800
  },
  "memory_budget_gb": 0.5
}
```

- [ ] **Step 9: Create `backend/config/default_presets/remote-mpcdf.json`**

```json
{
  "description": "Fully remote via MPCDF LLM Inference Service (llm.mpcdf.mpg.de) — Each endpoint is an ephemeral (<=8h) Slurm job; the endpoint URL and the API key need to be set for each job individually for both the embedding and the inference endpoint.",
  "embedding": {
    "model_type": "remote",
    "model_name": "multilingual-e5-large-instruct",
    "batch_size": 64,
    "model_kwargs": {
      "shared_base_url_env": "MPCDF_EMBEDDING_BASE_URL",
      "shared_api_key_env": "MPCDF_EMBEDDING_API_KEY"
    }
  },
  "llm": {
    "model_type": "remote",
    "model_names": ["openai/gpt-oss-120b"],
    "max_context_length": 131072,
    "max_answer_tokens": 4096,
    "temperature": 0.7,
    "model_kwargs": {
      "shared_base_url_env": "MPCDF_LLM_BASE_URL",
      "shared_api_key_env": "MPCDF_LLM_API_KEY"
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

- [ ] **Step 10: Verify all 9 files are valid JSON**

Run: `uv run python -c "import json, glob; files = sorted(glob.glob('backend/config/default_presets/*.json')); print(len(files)); [json.load(open(f)) for f in files]; print('all valid')"`
Expected: `9` then `all valid`

- [ ] **Step 11: Commit**

```bash
git add backend/config/default_presets/
git commit -m "feat(config): add bundled default preset JSON files"
```

---

### Task 2: File-based preset loader in `backend/config/presets.py`

**Files:**
- Modify: `backend/config/presets.py` (entire file — keep the 4 schema classes, replace everything from `KISSKI_RAG_MODELS` (line 50) through `list_presets()` (end of file))
- Test: `backend/tests/test_config.py:1-82` (the `TestPresets` class and its imports)

- [ ] **Step 1: Replace the `TestPresets` class and imports in `backend/tests/test_config.py`**

Replace lines 1-82 (everything up to but not including `class TestSettings`) with:

```python
"""
Unit tests for configuration system.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.config.presets import (
    DEFAULT_PRESETS_DIR,
    ensure_default_presets,
    get_preset,
    list_presets,
)
from backend.config.settings import Settings, get_settings, reset_settings


class TestPresets(unittest.TestCase):
    """Test hardware presets, loaded from JSON files under data_path/presets/."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self._tmp.name)
        ensure_default_presets(self.data_path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_get_preset_cpu_only(self):
        """Test getting cpu-only preset."""
        preset = get_preset("cpu-only", self.data_path)

        self.assertEqual(preset.name, "cpu-only")
        self.assertEqual(preset.embedding.model_name, "sentence-transformers/all-MiniLM-L6-v2")
        self.assertEqual(preset.llm.model_name, "TinyLlama/TinyLlama-1.1B-Chat-v1.0")

    def test_get_preset_remote_openai(self):
        """Test getting remote-openai preset."""
        preset = get_preset("remote-openai", self.data_path)

        self.assertEqual(preset.name, "remote-openai")
        self.assertEqual(preset.embedding.model_type, "remote")
        self.assertEqual(preset.llm.model_type, "remote")

    def test_get_preset_remote_kisski(self):
        """Test getting remote-kisski preset."""
        preset = get_preset("remote-kisski", self.data_path)

        self.assertEqual(preset.name, "remote-kisski")
        self.assertEqual(preset.embedding.model_type, "remote")  # Fully remote — no local torch
        self.assertEqual(preset.embedding.model_name, "multilingual-e5-large-instruct")
        self.assertEqual(preset.embedding.model_kwargs["base_url"], "https://chat-ai.academiccloud.de/v1")
        self.assertEqual(preset.embedding.model_kwargs["api_key_env"], "KISSKI_API_KEY")
        self.assertEqual(preset.llm.model_type, "remote")
        self.assertEqual(preset.llm.model_kwargs["base_url"], "https://chat-ai.academiccloud.de/v1")
        self.assertEqual(preset.llm.model_kwargs["api_key_env"], "KISSKI_API_KEY")

    def test_get_preset_remote_mpcdf_uses_shared_dynamic_fields(self):
        """remote-mpcdf has no static base_url — both base_url and API key are
        resolved at request time from the shared admin-set store (see
        backend.services.admin_settings_store), not baked into the preset."""
        preset = get_preset("remote-mpcdf", self.data_path)

        self.assertEqual(preset.name, "remote-mpcdf")
        self.assertEqual(preset.embedding.model_type, "remote")
        self.assertNotIn("base_url", preset.embedding.model_kwargs)
        self.assertEqual(preset.embedding.model_kwargs["shared_base_url_env"], "MPCDF_EMBEDDING_BASE_URL")
        self.assertEqual(preset.embedding.model_kwargs["shared_api_key_env"], "MPCDF_EMBEDDING_API_KEY")
        self.assertEqual(preset.llm.model_type, "remote")
        self.assertNotIn("base_url", preset.llm.model_kwargs)
        self.assertEqual(preset.llm.model_kwargs["shared_base_url_env"], "MPCDF_LLM_BASE_URL")
        self.assertEqual(preset.llm.model_kwargs["shared_api_key_env"], "MPCDF_LLM_API_KEY")
        # Same embedding model as remote-kisski — same vector space, so the two
        # presets are mutually hot-swappable at runtime.
        self.assertEqual(
            preset.embedding.model_name,
            get_preset("remote-kisski", self.data_path).embedding.model_name,
        )

    def test_get_preset_invalid(self):
        """Test getting invalid preset raises error."""
        with self.assertRaises(ValueError) as ctx:
            get_preset("invalid-preset", self.data_path)

        self.assertIn("Unknown preset", str(ctx.exception))

    def test_list_presets(self):
        """Test listing all presets."""
        presets = list_presets(self.data_path)

        self.assertIn("cpu-only", presets)
        self.assertIn("remote-openai", presets)
        self.assertIn("remote-kisski", presets)
        expected_count = len(list(DEFAULT_PRESETS_DIR.glob("*.json")))
        self.assertEqual(len(presets), expected_count)

    def test_ensure_default_presets_copies_every_bundled_default(self):
        """Every bundled default file ends up in the (empty) target directory."""
        presets_dir = self.data_path / "presets"
        bundled_names = {p.name for p in DEFAULT_PRESETS_DIR.glob("*.json")}
        copied_names = {p.name for p in presets_dir.glob("*.json")}
        self.assertEqual(bundled_names, copied_names)

    def test_ensure_default_presets_does_not_overwrite_existing_file(self):
        """A user's edited preset file survives re-running the seeder (e.g. on
        every backend startup)."""
        presets_dir = self.data_path / "presets"
        custom_content = (
            '{"description": "edited by user", '
            '"embedding": {"model_type": "local", "model_name": "x"}, '
            '"llm": {"model_type": "local", "model_names": ["y"]}, '
            '"rag": {}, "memory_budget_gb": 1.0}'
        )
        (presets_dir / "cpu-only.json").write_text(custom_content)

        ensure_default_presets(self.data_path)

        self.assertEqual((presets_dir / "cpu-only.json").read_text(), custom_content)

    def test_get_preset_raises_for_malformed_json(self):
        """A file that isn't valid JSON raises ValueError naming the file."""
        presets_dir = self.data_path / "presets"
        broken_path = presets_dir / "broken.json"
        broken_path.write_text("{not valid json")

        with self.assertRaises(ValueError) as ctx:
            get_preset("broken", self.data_path)

        self.assertIn(str(broken_path), str(ctx.exception))

    def test_get_preset_raises_for_schema_invalid_file(self):
        """A file missing required HardwarePreset fields raises ValueError
        naming the file."""
        presets_dir = self.data_path / "presets"
        invalid_path = presets_dir / "invalid.json"
        invalid_path.write_text(json.dumps({"description": "missing required fields"}))

        with self.assertRaises(ValueError) as ctx:
            get_preset("invalid", self.data_path)

        self.assertIn(str(invalid_path), str(ctx.exception))

    def test_list_presets_skips_malformed_file(self):
        """One broken custom preset doesn't hide the rest."""
        presets_dir = self.data_path / "presets"
        (presets_dir / "broken.json").write_text("{not valid json")

        presets = list_presets(self.data_path)

        self.assertNotIn("broken", presets)
        self.assertIn("cpu-only", presets)

    def test_name_field_in_file_is_ignored_in_favor_of_filename(self):
        """The filename is authoritative; a stray "name" key in the file content
        can't make the preset's name drift from its filename."""
        presets_dir = self.data_path / "presets"
        data = json.loads((presets_dir / "cpu-only.json").read_text())
        data["name"] = "something-else"
        (presets_dir / "cpu-only.json").write_text(json.dumps(data))

        preset = get_preset("cpu-only", self.data_path)

        self.assertEqual(preset.name, "cpu-only")

```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `uv run pytest backend/tests/test_config.py::TestPresets -v`
Expected: FAIL/ERROR — `ImportError: cannot import name 'ensure_default_presets' from 'backend.config.presets'` (the loader doesn't exist yet)

- [ ] **Step 3: Rewrite `backend/config/presets.py`**

Replace the entire file content with:

```python
"""
Configuration presets for different hardware scenarios.

Presets are loaded from JSON files under ``<data_path>/presets/`` rather
than defined in this module. Bundled defaults live in
``backend/config/default_presets/`` and are copied into the data
directory — for any preset name not already present there — by
``ensure_default_presets``, called from ``Settings.ensure_directories()``.
A user's edited or added preset file is never overwritten. See
docs/superpowers/specs/2026-10-08-file-based-presets-design.md.
"""

import json
import logging
import shutil
from pathlib import Path
from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

DEFAULT_PRESETS_DIR = Path(__file__).parent / "default_presets"


class EmbeddingConfig(BaseModel):
    """Configuration for embedding models."""

    model_type: Literal["local", "remote"] = "local"
    model_name: str = Field(..., description="Model identifier or API endpoint")
    model_kwargs: dict = Field(default_factory=dict, description="Additional model parameters")
    batch_size: int = Field(default=32, description="Batch size for embedding generation")
    cache_enabled: bool = Field(default=True, description="Enable content-hash based caching")


class LLMConfig(BaseModel):
    """Configuration for LLM models."""

    model_type: Literal["local", "remote"] = "local"
    model_names: List[str] = Field(..., description="Model identifier(s) — first entry is the default")
    quantization: Optional[Literal["4bit", "8bit", "none"]] = None
    max_context_length: int = Field(default=4096, description="Maximum context window size")
    max_answer_tokens: int = Field(default=2048, description="Maximum tokens for generated answers")
    temperature: float = Field(default=0.7, description="Sampling temperature")
    model_kwargs: dict = Field(default_factory=dict, description="Additional model parameters")
    models_status_url: Optional[str] = Field(
        default=None,
        description="URL to query for per-model availability metrics (KISSKI format: POST → data[].{id, demand, status})",
    )

    @field_validator("model_names", mode="before")
    @classmethod
    def coerce_to_list(cls, v: object) -> list:
        if isinstance(v, str):
            return [m.strip() for m in v.split(",") if m.strip()]
        return v  # type: ignore[return-value]

    @property
    def model_name(self) -> str:
        """Backward-compatible alias: returns the first (default) model name."""
        return self.model_names[0]


class RAGConfig(BaseModel):
    """Configuration for RAG retrieval."""

    top_k: int = Field(default=5, description="Number of chunks to retrieve")
    score_threshold: float = Field(default=0.3, description="Minimum similarity score (0.0-1.0)")
    max_chunk_size: int = Field(default=512, description="Maximum characters per chunk (passed to chunker as max_characters)")


class HardwarePreset(BaseModel):
    """Complete hardware-specific configuration preset."""

    name: str
    description: str
    embedding: EmbeddingConfig
    llm: LLMConfig
    rag: RAGConfig
    memory_budget_gb: float = Field(..., description="Estimated memory usage in GB")


def ensure_default_presets(data_path: Path) -> None:
    """
    Seed <data_path>/presets/ with the bundled default preset files.

    Copies each file under DEFAULT_PRESETS_DIR into the data directory only
    if no file of that name exists there yet — a user's edited or deleted
    preset is never touched or resurrected.
    """
    presets_dir = Path(data_path) / "presets"
    presets_dir.mkdir(parents=True, exist_ok=True)
    for default_file in DEFAULT_PRESETS_DIR.glob("*.json"):
        target = presets_dir / default_file.name
        if not target.exists():
            shutil.copy(default_file, target)


def _load_preset_file(path: Path) -> HardwarePreset:
    """
    Load and validate a single preset file.

    The file's own "name" key, if present, is ignored — the preset name
    always comes from the filename stem, so a rename or copy can't leave
    a preset's identity out of sync with its content.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read preset file '{path}': {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Preset file '{path}' must contain a JSON object")
    data["name"] = path.stem
    try:
        return HardwarePreset.model_validate(data)
    except Exception as exc:
        raise ValueError(f"Invalid preset file '{path}': {exc}") from exc


def get_preset(name: str, data_path: Optional[Path] = None) -> HardwarePreset:
    """
    Get a hardware preset by name, loading it from <data_path>/presets/<name>.json.

    Args:
        name: Preset name (e.g., "cpu-only")
        data_path: Base data directory. Defaults to the global Settings' data_path.

    Returns:
        HardwarePreset configuration

    Raises:
        ValueError: If preset name is not found, or its file is malformed/invalid.
    """
    if data_path is None:
        from backend.config.settings import get_settings
        data_path = get_settings().data_path

    path = Path(data_path) / "presets" / f"{name}.json"
    if not path.exists():
        available = ", ".join(list_presets(data_path))
        raise ValueError(f"Unknown preset '{name}'. Available: {available}")

    return _load_preset_file(path)


def list_presets(data_path: Optional[Path] = None) -> list[str]:
    """
    List all available preset names found under <data_path>/presets/.

    A file that fails to parse or validate is skipped (with a logged
    warning) rather than raising, so one broken custom preset doesn't hide
    every other valid preset.
    """
    if data_path is None:
        from backend.config.settings import get_settings
        data_path = get_settings().data_path

    presets_dir = Path(data_path) / "presets"
    if not presets_dir.is_dir():
        return []

    names = []
    for path in sorted(presets_dir.glob("*.json")):
        try:
            _load_preset_file(path)
        except ValueError as exc:
            logger.warning("Skipping invalid preset file: %s", exc)
            continue
        names.append(path.stem)
    return names
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_config.py::TestPresets -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/config/presets.py backend/tests/test_config.py
git commit -m "feat(config): load hardware presets from data_path/presets/*.json

Replaces the hardcoded PRESETS dict with a file-based loader
(get_preset/list_presets/ensure_default_presets). Schema classes
(EmbeddingConfig/LLMConfig/RAGConfig/HardwarePreset) are unchanged."
```

---

### Task 3: Wire seeding + EMBEDDING_BATCH_SIZE override into `backend/config/settings.py`

**Files:**
- Modify: `backend/config/settings.py:17` (import), `backend/config/settings.py:324-344` (`get_hardware_preset`), `backend/config/settings.py:346-356` (`ensure_directories`)
- Test: `backend/tests/test_config.py` (the 4 `get_hardware_preset` tests in `TestSettings`, plus one new test)

- [ ] **Step 1: Update the failing-first tests in `backend/tests/test_config.py`**

Find this block (the 4 `get_hardware_preset` tests inside `TestSettings`):

```python
    def test_get_hardware_preset(self):
        """Test getting hardware preset from settings."""
        settings = Settings(model_preset="cpu-only")
        preset = settings.get_hardware_preset()

        self.assertEqual(preset.name, "cpu-only")

    def test_get_hardware_preset_uses_active_preset_override_when_set(self):
        from backend.services.admin_settings_store import set_active_preset_override
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(model_preset="cpu-only", data_path=tmp)
            set_active_preset_override(settings.data_path, "remote-mpcdf")
            self.assertEqual(settings.get_hardware_preset().name, "remote-mpcdf")

    def test_get_hardware_preset_falls_back_to_model_preset_when_no_override_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(model_preset="cpu-only", data_path=tmp)
            self.assertEqual(settings.get_hardware_preset().name, "cpu-only")

    def test_get_hardware_preset_ignores_unknown_override(self):
        from backend.services.admin_settings_store import set_active_preset_override
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(model_preset="cpu-only", data_path=tmp)
            set_active_preset_override(settings.data_path, "no-such-preset")
            self.assertEqual(settings.get_hardware_preset().name, "cpu-only")
```

Replace it with (adds `settings.ensure_directories()` so the temp `data_path`
actually has seeded preset files, plus one new test for the batch-size
override):

```python
    def test_get_hardware_preset(self):
        """Test getting hardware preset from settings."""
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(model_preset="cpu-only", data_path=tmp)
            settings.ensure_directories()
            preset = settings.get_hardware_preset()

            self.assertEqual(preset.name, "cpu-only")

    def test_get_hardware_preset_uses_active_preset_override_when_set(self):
        from backend.services.admin_settings_store import set_active_preset_override
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(model_preset="cpu-only", data_path=tmp)
            settings.ensure_directories()
            set_active_preset_override(settings.data_path, "remote-mpcdf")
            self.assertEqual(settings.get_hardware_preset().name, "remote-mpcdf")

    def test_get_hardware_preset_falls_back_to_model_preset_when_no_override_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(model_preset="cpu-only", data_path=tmp)
            settings.ensure_directories()
            self.assertEqual(settings.get_hardware_preset().name, "cpu-only")

    def test_get_hardware_preset_ignores_unknown_override(self):
        from backend.services.admin_settings_store import set_active_preset_override
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(model_preset="cpu-only", data_path=tmp)
            settings.ensure_directories()
            set_active_preset_override(settings.data_path, "no-such-preset")
            self.assertEqual(settings.get_hardware_preset().name, "cpu-only")

    def test_get_hardware_preset_applies_embedding_batch_size_env_override(self):
        """EMBEDDING_BATCH_SIZE (docs/cron-indexing.md's low-RAM tuning knob)
        overrides embedding.batch_size for whichever preset is active, not
        just remote-kisski."""
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(model_preset="cpu-only", data_path=tmp)
            settings.ensure_directories()
            with patch.dict(os.environ, {"EMBEDDING_BATCH_SIZE": "7"}):
                preset = settings.get_hardware_preset()
            self.assertEqual(preset.embedding.batch_size, 7)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_config.py::TestSettings -k hardware_preset -v`
Expected: FAIL on `test_get_hardware_preset_applies_embedding_batch_size_env_override` — `AssertionError: 32 != 7` (the override doesn't exist yet). The other 3 should already pass since `ensure_directories()` now seeds the temp dir before `get_hardware_preset()` is called.

- [ ] **Step 3: Update `backend/config/settings.py`**

Change the import on line 17:

```python
from .presets import HardwarePreset, get_preset
```

to:

```python
from .presets import HardwarePreset, get_preset, ensure_default_presets
```

Replace the `get_hardware_preset` method (lines 324-344) with:

```python
    def get_hardware_preset(self) -> HardwarePreset:
        """Get the configured hardware preset.

        An admin-set runtime override (backend.services.admin_settings_store,
        set via POST /api/config) takes precedence over MODEL_PRESET, letting
        an admin hot-swap between presets without a restart — see
        docs/superpowers/specs/2026-10-08-dynamic-remote-preset-config-design.md.
        An override naming an unknown preset (e.g. after a code change removes
        it) is ignored with a warning, falling back to MODEL_PRESET.

        EMBEDDING_BATCH_SIZE, if set in the environment, overrides
        embedding.batch_size on the returned preset regardless of which
        preset is active — a documented memory-tuning knob for the cron
        indexer on low-RAM hosts (see docs/cron-indexing.md).
        """
        from backend.services.admin_settings_store import get_active_preset_override
        override = get_active_preset_override(self.data_path)
        if override:
            try:
                preset = get_preset(override, self.data_path)
            except ValueError:
                logger.warning(
                    "active_preset_override=%r is not a known preset; falling back to MODEL_PRESET=%r",
                    override, self.model_preset,
                )
                preset = get_preset(self.model_preset, self.data_path)
        else:
            preset = get_preset(self.model_preset, self.data_path)

        batch_size_override = os.environ.get("EMBEDDING_BATCH_SIZE")
        if batch_size_override:
            preset.embedding.batch_size = int(batch_size_override)

        return preset
```

Add a call to `ensure_default_presets` inside `ensure_directories` (lines 346-356), appending it as the last line of the method:

```python
    def ensure_directories(self):
        """Create necessary directories if they don't exist."""
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.model_weights_path.mkdir(parents=True, exist_ok=True)
        self.vector_db_path.mkdir(parents=True, exist_ok=True)
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
        if self.registrations_path:
            self.registrations_path.parent.mkdir(parents=True, exist_ok=True)
        if self.autoindex_keys_path:
            self.autoindex_keys_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_default_presets(self.data_path)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_config.py -v`
Expected: PASS (all tests in the file)

- [ ] **Step 5: Commit**

```bash
git add backend/config/settings.py backend/tests/test_config.py
git commit -m "feat(config): seed default presets on startup; generalize EMBEDDING_BATCH_SIZE

Settings.ensure_directories() now seeds data_path/presets/ from the
bundled defaults. get_hardware_preset() passes its own data_path to the
loader (so it respects a Settings instance built with a custom data_path,
not just the global singleton) and applies EMBEDDING_BATCH_SIZE to
whichever preset is active, not only remote-kisski."
```

---

### Task 4: Update `backend/api/config.py` off the removed `PRESETS` dict

**Files:**
- Modify: `backend/api/config.py:12` (import), `:74` (`get_config`), `:108` (`available_presets`), `:136-141` (`update_config` validation)

No new tests — `backend/tests/test_api.py::TestConfigAPI` already covers `GET /api/config` and the invalid-preset case on `POST /api/config`; this task's verification is that those existing tests still pass unchanged.

- [ ] **Step 1: Update the import**

Change line 12:

```python
from backend.config.presets import PRESETS
```

to:

```python
from backend.config.presets import get_preset, list_presets
```

- [ ] **Step 2: Update `get_config`**

Change line 74:

```python
    preset = PRESETS[settings.model_preset]
```

to:

```python
    preset = get_preset(settings.model_preset, settings.data_path)
```

Change line 108:

```python
        available_presets=list(PRESETS.keys()),
```

to:

```python
        available_presets=list_presets(settings.data_path),
```

- [ ] **Step 3: Update `update_config`**

Change lines 136-141:

```python
    if update.preset_name and update.preset_name not in PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid preset: {update.preset_name}. "
                   f"Available presets: {list(PRESETS.keys())}"
        )
```

to:

```python
    if update.preset_name:
        available = list_presets(settings.data_path)
        if update.preset_name not in available:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid preset: {update.preset_name}. "
                       f"Available presets: {available}"
            )
```

- [ ] **Step 4: Run the existing API tests to verify they still pass**

Run: `uv run pytest backend/tests/test_api.py::TestConfigAPI -v`
Expected: PASS (`test_get_config`, `test_get_version`, `test_update_config_invalid_preset`)

- [ ] **Step 5: Commit**

```bash
git add backend/api/config.py
git commit -m "fix(api): switch config endpoints off the removed PRESETS dict"
```

---

### Task 5: Update the standalone scripts off the removed `PRESETS` dict

**Files:**
- Modify: `scripts/check_embedding_compat.py:26` (import), `:57-70` (`pick_preset`)
- Modify: `scripts/eval_embeddings.py:47` (import), `:526-540` (`pick_preset`)

These scripts require network access/model downloads to run end-to-end, so
verification here is a syntax/import smoke test rather than a full run.

- [ ] **Step 1: Update `scripts/check_embedding_compat.py`**

Change line 26:

```python
from backend.config.presets import PRESETS, get_preset
```

to:

```python
from backend.config.presets import get_preset, list_presets
```

Change the `pick_preset` function:

```python
def pick_preset(prompt: str) -> str:
    available = sorted(PRESETS.keys())
    print(f"\n{prompt}")
    for i, name in enumerate(available, 1):
        p = PRESETS[name]
        print(f"  {i:2d}. {name:<32s}  [{p.embedding.model_type}] {p.embedding.model_name}")
    while True:
        raw = input("Enter number or preset name: ").strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(available):
                return available[idx]
        elif raw in PRESETS:
            return raw
        print("  Invalid choice, try again.")
```

to:

```python
def pick_preset(prompt: str) -> str:
    available = list_presets()
    print(f"\n{prompt}")
    for i, name in enumerate(available, 1):
        p = get_preset(name)
        print(f"  {i:2d}. {name:<32s}  [{p.embedding.model_type}] {p.embedding.model_name}")
    while True:
        raw = input("Enter number or preset name: ").strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(available):
                return available[idx]
        elif raw in available:
            return raw
        print("  Invalid choice, try again.")
```

- [ ] **Step 2: Update `scripts/eval_embeddings.py`**

Change line 47:

```python
from backend.config.presets import PRESETS, get_preset
```

to:

```python
from backend.config.presets import get_preset, list_presets
```

Change the `pick_preset` function (lines 526-540):

```python
def pick_preset(prompt: str) -> str:
    available = sorted(PRESETS.keys())
    print(f"\n{prompt}")
    for i, name in enumerate(available, 1):
        p = PRESETS[name]
        print(f"  {i:2d}. {name:<32s}  [{p.embedding.model_type}] {p.embedding.model_name}")
    while True:
        raw = input("Enter number or preset name: ").strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(available):
                return available[idx]
        elif raw in PRESETS:
            return raw
        print("  Invalid choice, try again.")
```

to:

```python
def pick_preset(prompt: str) -> str:
    available = list_presets()
    print(f"\n{prompt}")
    for i, name in enumerate(available, 1):
        p = get_preset(name)
        print(f"  {i:2d}. {name:<32s}  [{p.embedding.model_type}] {p.embedding.model_name}")
    while True:
        raw = input("Enter number or preset name: ").strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(available):
                return available[idx]
        elif raw in available:
            return raw
        print("  Invalid choice, try again.")
```

- [ ] **Step 3: Smoke-test both scripts import cleanly**

Run: `uv run python -c "import ast; ast.parse(open('scripts/check_embedding_compat.py').read()); ast.parse(open('scripts/eval_embeddings.py').read()); print('syntax ok')"`
Run: `uv run python -c "import sys; sys.path.insert(0, '.'); import scripts.check_embedding_compat; import scripts.eval_embeddings; print('imports ok')"`
Expected: `syntax ok` then `imports ok`

- [ ] **Step 4: Commit**

```bash
git add scripts/check_embedding_compat.py scripts/eval_embeddings.py
git commit -m "fix(scripts): switch eval/compat scripts off the removed PRESETS dict"
```

---

### Task 6: Full verification sweep

**Files:** none (verification only)

- [ ] **Step 1: Grep for any remaining reference to the removed `PRESETS` dict**

Run: `grep -rn "PRESETS\b" backend bin scripts --include="*.py" | grep -v __pycache__`
Expected: no output (the only matches should have been the 4 call sites already handled in Tasks 2-5)

- [ ] **Step 2: Run the full backend test suite**

Run: `uv run pytest backend/tests/ -v --ignore=backend/tests/test_real_integration.py -m "not integration and not container"`
Expected: PASS, no failures or errors

- [ ] **Step 3: Confirm the new presets directory structure exists after a real startup**

Run: `uv run python -c "
from backend.config.settings import get_settings
s = get_settings()
from pathlib import Path
files = sorted(p.name for p in (s.data_path / 'presets').glob('*.json'))
print(len(files), files)
"`
Expected: `9 ['apple-silicon-32gb.json', 'apple-silicon-kisski.json', 'cloud-server-kisski.json', 'cpu-only.json', 'high-memory.json', 'remote-kisski.json', 'remote-mpcdf.json', 'remote-openai.json', 'windows-test.json']`

- [ ] **Step 4: No commit needed** (verification-only task; if Step 2 or 3 surfaces an issue, fix it in the relevant task above and re-commit there instead of here).

---

### Task 7: Final review

- [ ] **Step 1: Run `/code-review` (high effort) against the full branch diff against `devel`**, covering all commits from this plan. Address any CONFIRMED findings with a follow-up commit; note and consciously accept or reject any PLAUSIBLE findings.
