# Chapter-Segmentation New Repository — Implementation Plan (Part 1 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the chapter-segmentation engine and evaluation harness out of zotero-rag into a new standalone repository (`github.com/cboulanger/chapter-segmentation`), installable and testable entirely on its own, with pluggable OCR (Kreuzberg sidecar or local Tesseract binary) and LLM backends.

**Architecture:** History-preserving `git filter-repo` extraction of exactly the files listed in the design spec into a `src/chapter_segmentation/` + `evaluation/` layout, followed by mechanical import-path fixes (via `perl -pi -e` sweeps) and a small number of hand-written new/rewritten files (`OcrBackend`/`LLMClient` protocols, the Tesseract backend, the standalone CLI, and the two run()-orchestration functions removed from the moved engine files). A GitHub Actions workflow publishes a prose-free results snapshot to GitHub Pages.

**Tech Stack:** Python 3.12, `uv`/hatchling, `pytest`, `git-filter-repo`, `gh` CLI, GitHub Actions, GitHub Pages.

**Companion plan:** Part 2 (`docs/superpowers/plans/2026-08-06-chapter-segmentation-zotero-rag-integration.md`) covers the zotero-rag side (deleting the moved files, adding the dependency, writing the two thin orchestrators) and depends on this plan's Task 15 (the `v0.1.0` tag) being complete.

**Design spec:** `docs/superpowers/specs/2026-08-06-chapter-segmentation-extraction-design.md` — read it before starting; this plan implements it section by section and cites sections inline.

---

## Before you start: reference tables

**Full "moves" file list** (spec §3), with exact old → new paths. `git filter-repo` will do these renames while preserving history; nothing here needs manual `git mv`.

| Old path (zotero-rag) | New path (chapter-segmentation) | Kind |
|---|---|---|
| `backend/services/chapter_common.py` | `src/chapter_segmentation/common.py` | file |
| `backend/services/chapter_evidence/` | `src/chapter_segmentation/evidence/` | dir |
| `backend/services/chapter_segmentation.py` | `src/chapter_segmentation/segmentation.py` | file |
| `backend/services/chapter_ocr.py` | `src/chapter_segmentation/ocr.py` | file |
| `backend/evaluation/book-segmentation/` | `evaluation/` | dir |
| `backend/evaluation/harness.py` | `evaluation/harness.py` | file |
| `scripts/evaluation_redaction/` | `evaluation/redaction/` | dir |
| `scripts/ocr_evaluation_pdfs.py` | `evaluation/scripts/ocr_evaluation_pdfs.py` | file |
| `scripts/generate_public_evaluation_cache.py` | `evaluation/scripts/generate_public_evaluation_cache.py` | file |
| `scripts/fetch_evaluation_pdfs.py` | `evaluation/scripts/fetch_evaluation_pdfs.py` | file |
| `scripts/ground_truth_helper.py` | `evaluation/scripts/ground_truth_helper.py` | file |
| `scripts/evaluate_chapter_segmentation_strategies.py` | `evaluation/scripts/evaluate_chapter_segmentation_strategies.py` | file |
| `scripts/evaluate_chapter_segmentation_llm_fallback.py` | `evaluation/scripts/evaluate_chapter_segmentation_llm_fallback.py` | file |
| `backend/tests/test_chapter_common.py` | `tests/test_common.py` | file |
| `backend/tests/test_chapter_evidence_types.py` | `tests/evidence/test_types.py` | file |
| `backend/tests/test_chapter_evidence_fusion.py` | `tests/evidence/test_fusion.py` | file |
| `backend/tests/test_chapter_evidence_outline.py` | `tests/evidence/test_outline_strategy.py` | file |
| `backend/tests/test_chapter_evidence_crossref.py` | `tests/evidence/test_crossref_strategy.py` | file |
| `backend/tests/test_chapter_evidence_zotero_catalog.py` | `tests/evidence/test_zotero_catalog_strategy.py` | file |
| `backend/tests/test_chapter_segmentation.py` | `tests/test_segmentation.py` | file |
| `backend/tests/test_chapter_segmentation_accuracy.py` | `tests/test_segmentation_accuracy.py` | file |
| `backend/tests/test_chapter_segmentation_strategies.py` | `tests/test_segmentation_strategies.py` | file |
| `backend/tests/test_chapter_ocr.py` | `tests/test_ocr.py` | file |
| `backend/tests/test_evaluation_harness.py` | `tests/test_harness.py` | file |
| `backend/tests/test_evaluation_redaction.py` | `tests/test_redaction.py` | file |
| `backend/tests/test_public_evaluation_cache_parity.py` | `tests/test_public_evaluation_cache_parity.py` | file |

**Deliberate behavior changes made during this extraction** (so later tasks don't look like mistakes):

1. `ocr_pdf_pages()`'s per-page slicing moves from the caller into whichever `OcrBackend` is passed in (spec §4). The `on_page` per-page progress callback is dropped entirely — no backend is required to expose per-page granularity. `evaluation/scripts/ocr_evaluation_pdfs.py` loses its "N/25 pages" progress ticker; it keeps a per-book "OCR-ing..." print.
2. `analyze_attachment_with_strategies()`/`analyze_attachment_with_llm_fallback()`/`llm_extract_toc_entries()`/`llm_disambiguate_chapter_start()`'s `llm_service: LLMService` parameter is renamed `llm_client: LLMClient` (spec §5) — a new minimal Protocol, not zotero-rag's `LLMService` ABC.
3. `evaluation/scripts/evaluate_chapter_segmentation_llm_fallback.py` no longer uses zotero-rag's multi-provider `make_llm_service()` (model rotation, KISSKI presets) — that machinery is Zotero-integration-specific and doesn't belong in the standalone package. It gets its own minimal OpenAI-compatible `LLMClient`, configurable via `--model`/`--base-url` and an `OPENAI_API_KEY` env var. This narrows what the script can do (no auto-select-model retry) but keeps the package free of zotero-rag dependencies.

---

### Task 1: Verify tooling and create the extraction clone

**Files:** none yet — this task only produces a disposable working clone outside the repo.

- [ ] **Step 1: Confirm `git-filter-repo` and `gh` are available and authenticated**

```bash
git filter-repo --version || pip install --user git-filter-repo
gh auth status
```

Expected: a version string from the first command (install it if missing — it's a single pure-Python script, `pip install --user git-filter-repo` is sufficient), and `gh auth status` reporting "Logged in to github.com account cboulanger".

- [ ] **Step 2: Clone zotero-rag fresh into a disposable directory**

```bash
cd /tmp
rm -rf chapter-segmentation-extract
git clone /Users/cboulanger/Code/zotero-rag chapter-segmentation-extract
cd chapter-segmentation-extract
git log --oneline -1
```

Expected: the clone succeeds and prints the current HEAD commit of zotero-rag's `feature/chapter-segmentation-linking` branch (or whatever branch was checked out in the source). `git filter-repo` will refuse to run on a non-fresh clone (one with uncommitted changes or extra remotes) as a safety check — this fresh clone satisfies that check by construction.

---

### Task 2: Run the history-preserving filter-repo extraction

**Files:** operates on `/tmp/chapter-segmentation-extract` (the clone from Task 1).

- [ ] **Step 1: Run the filter**

```bash
cd /tmp/chapter-segmentation-extract
git filter-repo \
  --path backend/services/chapter_common.py --path-rename backend/services/chapter_common.py:src/chapter_segmentation/common.py \
  --path backend/services/chapter_evidence/ --path-rename backend/services/chapter_evidence/:src/chapter_segmentation/evidence/ \
  --path backend/services/chapter_segmentation.py --path-rename backend/services/chapter_segmentation.py:src/chapter_segmentation/segmentation.py \
  --path backend/services/chapter_ocr.py --path-rename backend/services/chapter_ocr.py:src/chapter_segmentation/ocr.py \
  --path backend/evaluation/book-segmentation/ --path-rename backend/evaluation/book-segmentation/:evaluation/ \
  --path backend/evaluation/harness.py --path-rename backend/evaluation/harness.py:evaluation/harness.py \
  --path scripts/evaluation_redaction/ --path-rename scripts/evaluation_redaction/:evaluation/redaction/ \
  --path scripts/ocr_evaluation_pdfs.py --path-rename scripts/ocr_evaluation_pdfs.py:evaluation/scripts/ocr_evaluation_pdfs.py \
  --path scripts/generate_public_evaluation_cache.py --path-rename scripts/generate_public_evaluation_cache.py:evaluation/scripts/generate_public_evaluation_cache.py \
  --path scripts/fetch_evaluation_pdfs.py --path-rename scripts/fetch_evaluation_pdfs.py:evaluation/scripts/fetch_evaluation_pdfs.py \
  --path scripts/ground_truth_helper.py --path-rename scripts/ground_truth_helper.py:evaluation/scripts/ground_truth_helper.py \
  --path scripts/evaluate_chapter_segmentation_strategies.py --path-rename scripts/evaluate_chapter_segmentation_strategies.py:evaluation/scripts/evaluate_chapter_segmentation_strategies.py \
  --path scripts/evaluate_chapter_segmentation_llm_fallback.py --path-rename scripts/evaluate_chapter_segmentation_llm_fallback.py:evaluation/scripts/evaluate_chapter_segmentation_llm_fallback.py \
  --path backend/tests/test_chapter_common.py --path-rename backend/tests/test_chapter_common.py:tests/test_common.py \
  --path backend/tests/test_chapter_evidence_types.py --path-rename backend/tests/test_chapter_evidence_types.py:tests/evidence/test_types.py \
  --path backend/tests/test_chapter_evidence_fusion.py --path-rename backend/tests/test_chapter_evidence_fusion.py:tests/evidence/test_fusion.py \
  --path backend/tests/test_chapter_evidence_outline.py --path-rename backend/tests/test_chapter_evidence_outline.py:tests/evidence/test_outline_strategy.py \
  --path backend/tests/test_chapter_evidence_crossref.py --path-rename backend/tests/test_chapter_evidence_crossref.py:tests/evidence/test_crossref_strategy.py \
  --path backend/tests/test_chapter_evidence_zotero_catalog.py --path-rename backend/tests/test_chapter_evidence_zotero_catalog.py:tests/evidence/test_zotero_catalog_strategy.py \
  --path backend/tests/test_chapter_segmentation.py --path-rename backend/tests/test_chapter_segmentation.py:tests/test_segmentation.py \
  --path backend/tests/test_chapter_segmentation_accuracy.py --path-rename backend/tests/test_chapter_segmentation_accuracy.py:tests/test_segmentation_accuracy.py \
  --path backend/tests/test_chapter_segmentation_strategies.py --path-rename backend/tests/test_chapter_segmentation_strategies.py:tests/test_segmentation_strategies.py \
  --path backend/tests/test_chapter_ocr.py --path-rename backend/tests/test_chapter_ocr.py:tests/test_ocr.py \
  --path backend/tests/test_evaluation_harness.py --path-rename backend/tests/test_evaluation_harness.py:tests/test_harness.py \
  --path backend/tests/test_evaluation_redaction.py --path-rename backend/tests/test_evaluation_redaction.py:tests/test_redaction.py \
  --path backend/tests/test_public_evaluation_cache_parity.py --path-rename backend/tests/test_public_evaluation_cache_parity.py:tests/test_public_evaluation_cache_parity.py
```

Expected: filter-repo reports it rewrote history and finishes without error. It also strips the `origin` remote (its own safety behavior) — that's fine, Task 3 adds a new one.

- [ ] **Step 2: Verify the resulting tree**

```bash
git log --oneline | wc -l
find src evaluation tests -type f | sort
```

Expected: the commit count is large (multi-week history preserved, not squashed to one commit), and the file listing matches the table above exactly — `src/chapter_segmentation/{common.py,ocr.py,segmentation.py,evidence/{types,fusion,outline_strategy,crossref_strategy,zotero_catalog_strategy}.py}`, `evaluation/{manifest.json,*.expected.json,public-cache/,README.md,RESULTS.md,CLAUDE.md,.gitignore,harness.py,redaction/{__init__.py,redact.py,region_classification.py,wordlists.py},scripts/*.py}`, `tests/{test_common.py,test_segmentation.py,test_segmentation_accuracy.py,test_segmentation_strategies.py,test_ocr.py,test_harness.py,test_redaction.py,test_public_evaluation_cache_parity.py,evidence/*.py}`. No `backend/` or root-level `scripts/` directory should exist at all.

---

### Task 3: Create the GitHub repository and push

**Files:** none — this pushes the filtered clone as-is.

- [ ] **Step 1: Create the repo**

```bash
gh repo create cboulanger/chapter-segmentation --public \
  --description "Standalone PDF chapter-boundary detection engine with pluggable OCR/LLM backends" \
  --source /tmp/chapter-segmentation-extract --remote origin --push
```

Expected: `gh` creates the repo, adds `origin`, and pushes the filtered history as `main`. If your default branch after filter-repo isn't named `main`, rename it first: `git branch -m main` before running the `gh repo create` command.

- [ ] **Step 2: Verify on GitHub**

```bash
gh repo view cboulanger/chapter-segmentation --web
```

Confirm the file tree matches Task 2's verification output.

From here on, all steps operate inside `/tmp/chapter-segmentation-extract` (the pushed clone) unless stated otherwise. Commit after each task as instructed — do not batch multiple tasks into one commit, since later tasks assume earlier ones are already committed (useful if you need to `git bisect` a later test failure).

---

### Task 4: Add the package scaffold (`pyproject.toml`, `.gitignore`, `README.md`)

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore` (root-level; `evaluation/.gitignore` already exists from the move)
- Create: `README.md`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "chapter-segmentation"
version = "0.1.0"
description = "Standalone PDF chapter-boundary detection engine with pluggable OCR/LLM backends"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "spacy>=3.8.0",
    "pypdf>=5.1.0",
    "rapidfuzz>=3.10.0",
    "langdetect>=1.0.9",
]

[project.optional-dependencies]
kreuzberg = ["httpx>=0.27.0"]
tesseract = ["pytesseract>=0.3.10", "pymupdf>=1.24.0", "pillow>=10.0.0"]
llm-eval = ["openai>=1.0.0"]

[project.scripts]
chapter-segmentation = "chapter_segmentation.cli:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = "test_*.py"
python_classes = "Test*"
python_functions = "test_*"
asyncio_mode = "auto"
markers = [
    "integration: marks tests that need a real tesseract binary or the real evaluation PDFs",
]
addopts = "-rs -m 'not integration'"

[tool.hatch.build.targets.wheel]
packages = ["src/chapter_segmentation"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[dependency-groups]
dev = [
    "pytest>=8.3.0",
    "pytest-asyncio>=0.24.0",
    "faker>=33.0.0",
]
```

- [ ] **Step 2: Write `.gitignore`**

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
public/
```

(`public/` is the report generator's output directory, added in Task 14 — gitignored since it's a CI build artifact, not source.)

- [ ] **Step 3: Write `README.md`**

```markdown
# chapter-segmentation

Standalone PDF chapter-boundary detection: given a book's page text (or raw
PDF bytes), finds where each chapter starts and ends using a table-of-contents
heuristic, an optional PDF-outline/Crossref/catalog metadata fusion pipeline,
and an optional LLM fallback for irregular layouts.

Extracted from [zotero-rag](https://github.com/cboulanger/zotero-rag), where
it originated as part of a Zotero chapter-linking feature — see that
project's `docs/superpowers/specs/2026-08-06-chapter-segmentation-extraction-design.md`
for the extraction rationale.

## Install

```bash
pip install "chapter-segmentation[tesseract]"   # local OCR, no container
# or
pip install "chapter-segmentation[kreuzberg]"   # OCR via a Kreuzberg sidecar
```

## Standalone CLI

```bash
chapter-segmentation analyze mybook.pdf
```

Requires the `tesseract` binary on `PATH` if the PDF needs OCR (see
"OCR backends" below) — `apt-get install tesseract-ocr tesseract-ocr-deu
tesseract-ocr-fra tesseract-ocr-spa` (Debian/Ubuntu) or `brew install
tesseract tesseract-lang` (macOS).

## OCR backends

Two `OcrBackend` implementations ship in `chapter_segmentation.ocr_backends`:

- `TesseractOcrBackend` (`[tesseract]` extra) — local binary, no container, no network. The CLI's default.
- `KreuzbergOcrBackend` (`[kreuzberg]` extra) — calls a running Kreuzberg sidecar's HTTP API.

Implement `chapter_segmentation.ocr.OcrBackend` yourself for anything else.

## Evaluation

See `evaluation/README.md` for the ground-truth corpus, how to run the
accuracy suite, and how to add a new evaluation book. Current numbers:
https://cboulanger.github.io/chapter-segmentation/ (auto-published, no
hand-written analysis) and `evaluation/RESULTS.md` (hand-maintained,
includes mechanism/root-cause notes).

## Development

```bash
uv sync
uv run pytest
```
```

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml .gitignore README.md
git commit -m "chore: add package scaffold (pyproject.toml, gitignore, README)"
git push
```

---

### Task 5: Fix imports in the evidence strategies and add `__init__.py` files

**Files:**
- Modify: `src/chapter_segmentation/evidence/fusion.py`
- Modify: `src/chapter_segmentation/evidence/outline_strategy.py`
- Modify: `src/chapter_segmentation/evidence/zotero_catalog_strategy.py`
- Create: `src/chapter_segmentation/__init__.py`
- Create: `src/chapter_segmentation/evidence/__init__.py`
- Create: `evaluation/scripts/__init__.py`

`crossref_strategy.py` and `types.py` need no import changes (they only import from `evidence.types`/`evidence.crossref_strategy`, which don't move relative to each other, or have no `backend.*` imports at all).

- [ ] **Step 1: Apply the mechanical import-path fixes across the whole tree**

Run each of these once, from the repo root. Each is a no-op on files it doesn't match, so it's safe to run all of them across the whole tree rather than hand-picking files per command:

```bash
find src evaluation tests -name '*.py' -print0 | xargs -0 perl -pi -e \
  's/from backend\.services\.chapter_common import/from chapter_segmentation.common import/'
find src evaluation tests -name '*.py' -print0 | xargs -0 perl -pi -e \
  's/from backend\.services\.chapter_evidence\.types import/from chapter_segmentation.evidence.types import/'
find src evaluation tests -name '*.py' -print0 | xargs -0 perl -pi -e \
  's/from backend\.services\.chapter_evidence\.fusion import/from chapter_segmentation.evidence.fusion import/'
find src evaluation tests -name '*.py' -print0 | xargs -0 perl -pi -e \
  's/from backend\.services\.chapter_evidence\.outline_strategy import/from chapter_segmentation.evidence.outline_strategy import/'
find src evaluation tests -name '*.py' -print0 | xargs -0 perl -pi -e \
  's/from backend\.services\.chapter_evidence\.crossref_strategy import/from chapter_segmentation.evidence.crossref_strategy import/'
find src evaluation tests -name '*.py' -print0 | xargs -0 perl -pi -e \
  's/from backend\.services\.chapter_evidence\.zotero_catalog_strategy import/from chapter_segmentation.evidence.zotero_catalog_strategy import/'
find src evaluation tests -name '*.py' -print0 | xargs -0 perl -pi -e \
  's/from backend\.services\.chapter_segmentation import/from chapter_segmentation.segmentation import/'
find src evaluation tests -name '*.py' -print0 | xargs -0 perl -pi -e \
  's/from backend\.services\.chapter_ocr import/from chapter_segmentation.ocr import/'
find src evaluation tests -name '*.py' -print0 | xargs -0 perl -pi -e \
  's/from backend\.evaluation\.harness import/from evaluation.harness import/'
find src evaluation tests -name '*.py' -print0 | xargs -0 perl -pi -e \
  's/from scripts\.evaluation_redaction\./from evaluation.redaction./'
```

- [ ] **Step 2: Verify no `backend.*` or `scripts.evaluation_redaction` references remain**

```bash
grep -rn "backend\.\|scripts\.evaluation_redaction" src evaluation tests
```

Expected: no output. (Task 7's `evaluate_chapter_segmentation_llm_fallback.py` rewrite and Task 8's `ocr_evaluation_pdfs.py` rewrite still have `backend.dependencies`/`backend.config.settings`/`backend.services.extraction.kreuzberg` references at this point — those are handled by hand in their own tasks, not by this sweep, since they need real logic changes, not a mechanical rename. If this grep shows hits in those two files only, that's expected right now; every other file must be clean.)

- [ ] **Step 3: Add package `__init__.py` files**

```bash
touch src/chapter_segmentation/__init__.py
touch src/chapter_segmentation/evidence/__init__.py
touch evaluation/__init__.py
touch evaluation/scripts/__init__.py
```

(`evaluation/redaction/__init__.py` already exists from the move — verify with `ls evaluation/redaction/__init__.py`.)

- [ ] **Step 4: Install and run the evidence + common tests**

```bash
uv sync
uv run pytest tests/test_common.py tests/evidence/ -v
```

Expected: all pass. These files needed only import fixes, no logic changes.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "fix: rewrite backend.* imports to chapter_segmentation.* across the tree"
git push
```

---

### Task 6: Add the `LLMClient` protocol

**Files:**
- Create: `src/chapter_segmentation/llm.py`

- [ ] **Step 1: Write the protocol**

```python
"""Minimal LLM-client interface the segmentation engine's optional LLM
fallback depends on -- see design spec
docs/superpowers/specs/2026-08-06-chapter-segmentation-extraction-design.md
section 5 (in the zotero-rag repo this was extracted from).

Any object exposing this single async method works -- structural typing
means callers never need to subclass this Protocol explicitly.
"""

from typing import Callable, Optional, Protocol


class LLMClient(Protocol):
    async def generate(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
        is_valid: Optional[Callable[[str], bool]] = None,
    ) -> str:
        """Return the model's raw text completion for `prompt`.

        `is_valid`, when given, lets an implementation retry against a
        different model/provider if the first response doesn't satisfy it
        (e.g. isn't parseable as the JSON the caller asked for) -- entirely
        optional to honor; a minimal implementation may ignore it.
        """
        ...
```

- [ ] **Step 2: Commit**

```bash
git add src/chapter_segmentation/llm.py
git commit -m "feat: add LLMClient protocol"
git push
```

---

### Task 7: Split `segmentation.py` — remove `run()`, add `_llm_json.py`, rename the LLM parameter

**Files:**
- Modify: `src/chapter_segmentation/segmentation.py`
- Create: `src/chapter_segmentation/_llm_json.py`

`backend/utils/llm_json.py` is also used by zotero-rag's unrelated `query_router.py`, so it stays there — this is a small (~30 line), dependency-free duplication into the new package, not a shared import, to avoid a new cross-repo coupling for something this trivial.

- [ ] **Step 1: Write `_llm_json.py`** (identical content to zotero-rag's `backend/utils/llm_json.py`)

```python
"""Shared JSON-extraction helpers for parsing structured LLM output.

LLMs are asked to "return ONLY JSON" but the real world routinely adds
markdown code fences or a sentence of leading prose anyway -- both helpers
strip that off before parsing.
"""

import json


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(line for line in lines if not line.startswith("```")).strip()
    return text


def parse_json_object(text: str) -> dict:
    """Extract and parse the first JSON object ({...}) found in *text*."""
    text = _strip_code_fence(text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in LLM response: {text!r}")
    return json.loads(text[start: end + 1])


def parse_json_array(text: str) -> list:
    """Extract and parse the first JSON array ([...]) found in *text*."""
    text = _strip_code_fence(text)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON array found in LLM response: {text!r}")
    return json.loads(text[start: end + 1])
```

- [ ] **Step 2: Delete `run()` from `segmentation.py`**

Open `src/chapter_segmentation/segmentation.py` and delete everything from the line `async def run(` (the last function in the file) through the end of the file. Nothing follows `run()` in the original file, so this is a clean truncation — after deleting, the file must end with `analyze_attachment_with_strategies`'s closing `return {...}` block (the function right before `run()`).

Verify the deletion left no dangling reference:

```bash
grep -n "^async def run(\|^def run(" src/chapter_segmentation/segmentation.py
```

Expected: no output.

- [ ] **Step 3: Fix the import block**

Replace the file's import block (everything from the first `import` line down to `logger = logging.getLogger(__name__)`) with:

```python
import hashlib
import io
import json
import logging
import re
from collections import Counter
from functools import lru_cache
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

import spacy
from pypdf import PdfReader
from rapidfuzz import fuzz

from chapter_segmentation._llm_json import parse_json_array, parse_json_object
from chapter_segmentation.common import (
    _BACK_MATTER_TITLES,
    _PART_DIVIDER_RE,
    _is_back_matter,
    _is_non_chapter_structural_title,
    _is_part_divider,
    _normalized_title,
    year_from_date,
)
from chapter_segmentation.evidence.crossref_strategy import CrossrefMetadataStrategy, normalize_isbn
from chapter_segmentation.evidence.fusion import merge_candidates, merge_metadata_sources
from chapter_segmentation.evidence.outline_strategy import extract_outline_candidates
from chapter_segmentation.evidence.types import BookContext, ChapterCandidate, MetadataStrategy
from chapter_segmentation.evidence.zotero_catalog_strategy import ZoteroCatalogMetadataStrategy
from chapter_segmentation.llm import LLMClient
from chapter_segmentation.ocr import load_cached_ocr

logger = logging.getLogger(__name__)
```

This drops `httpx` (only used by the now-removed `run()`), `backend.config.settings.get_settings`, `backend.services.review_queue_store`, `backend.services.chapter_link_store.parse_links`, `backend.zotero.library_cache.ZoteroLibraryCache` (all only used by `run()`), and `Callable` from the `typing` import (only used by `run()`'s `progress_callback` parameter).

- [ ] **Step 4: Rename `llm_service`/`LLMService` to `llm_client`/`LLMClient` throughout the file**

`\b` word-boundary matching means this is safe against the one unrelated occurrence of "AutoSelectLLMService" in a comment (no word boundary exists between the preceding `t` and `L`, so it won't match):

```bash
perl -pi -e 's/\bllm_service\b/llm_client/g; s/\bLLMService\b/LLMClient/g' src/chapter_segmentation/segmentation.py
grep -n "AutoSelectLLMService" src/chapter_segmentation/segmentation.py
```

Expected: the grep still shows the untouched "AutoSelectLLMService" comment reference (confirming the word-boundary regex didn't corrupt it), while every real parameter/type usage is now `llm_client`/`LLMClient`.

- [ ] **Step 5: Verify the file imports cleanly**

```bash
uv run python -c "import chapter_segmentation.segmentation"
```

Expected: no error. (This will fail until Task 8 also removes `run()` from `ocr.py`'s dependency `load_cached_ocr` stays intact — `load_cached_ocr` itself isn't touched by that task, so this import should already succeed now.)

- [ ] **Step 6: Commit**

```bash
git add src/chapter_segmentation/segmentation.py src/chapter_segmentation/_llm_json.py
git commit -m "refactor: remove run() from segmentation.py, rename llm_service to llm_client"
git push
```

---

### Task 8: Split `ocr.py` — remove `run()`, add `OcrBackend`, refactor `ocr_pdf_pages`

**Files:**
- Modify: `src/chapter_segmentation/ocr.py`

- [ ] **Step 1: Delete `run()` and its imports**

Open `src/chapter_segmentation/ocr.py`. Delete everything from `async def run(` (the last function) through end of file. Then remove the now-unused `Callable` from the `typing` import line (only `run()`'s `progress_callback` and `ocr_pdf_pages`'s soon-to-be-removed `on_page` used it).

- [ ] **Step 2: Add the `OcrBackend` protocol and refactor `ocr_pdf_pages`**

Replace the whole `ocr_pdf_pages` function (and the module's `typing` import line) with:

```python
from typing import Optional, Protocol
```

```python
class OcrBackend(Protocol):
    async def ocr_pdf_pages(self, content: bytes, *, language: Optional[str] = None) -> list[str]:
        """Return one text string per physical page, 0-indexed. Implementations
        own how they talk to their OCR engine -- per-page requests, a single
        whole-document request split by page, etc."""
        ...


async def ocr_pdf_pages(
    content: bytes,
    *,
    backend: OcrBackend,
    cache_dir: Path,
    language: str,
) -> list[str]:
    """OCR `content` via `backend`, returning one text string per physical
    page (index 0 = first page). Results are cached in `cache_dir` keyed by
    the PDF's own content hash -- a later call with the same bytes returns
    the cached pages without touching `backend` at all.
    """
    content_hash = hashlib.sha256(content).hexdigest()
    cached = load_cached_ocr(cache_dir, content_hash)
    if cached is not None:
        return cached["pages"]

    page_texts = await backend.ocr_pdf_pages(content, language=language)
    save_ocr_cache(cache_dir, content_hash, detected_language=language, pages=page_texts)
    return page_texts
```

This removes `slice_single_page_pdf`'s only caller — but `slice_single_page_pdf` and `PdfReader`/`io` are still useful building blocks for backend implementations (Task 9's `TesseractOcrBackend` uses `pymupdf` instead, but keep `slice_single_page_pdf` in this module since it's a small, independently-testable, still-exported utility — don't delete it).

- [ ] **Step 3: Verify the final file's shape**

```bash
grep -n "^def \|^async def \|^class " src/chapter_segmentation/ocr.py
```

Expected: `detect_language`, `_cache_path`, `load_cached_ocr`, `save_ocr_cache`, `slice_single_page_pdf`, `OcrBackend` (class), `ocr_pdf_pages` — and nothing named `run`.

- [ ] **Step 4: Commit**

```bash
git add src/chapter_segmentation/ocr.py
git commit -m "refactor: remove run() from ocr.py, add OcrBackend protocol"
git push
```

---

### Task 9: Rewrite `tests/test_ocr.py` for the new `OcrBackend`-based signature

**Files:**
- Modify: `tests/test_ocr.py`

Deletes `TestOcrRun` entirely (it tested the removed `run()` — its zotero-rag-side replacement gets new tests in Part 2 of this plan) and rewrites `TestOcrPdfPages` to mock an `OcrBackend` instead of pypdf internals.

- [ ] **Step 1: Delete the `TestOcrRun` class**

Remove the whole `class TestOcrRun(unittest.TestCase): ...` block (everything between `class TestOcrRun` and the following `def _two_page_pdf_bytes():` helper function). Also remove the now-unused `from chapter_segmentation.ocr import run as ocr_run` import line and the `hashlib`/`AsyncMock` imports if nothing else in the file uses them (check with `grep -n "hashlib\.\|AsyncMock" tests/test_ocr.py` after deleting — `TestOcrPdfPages` below doesn't need either).

- [ ] **Step 2: Rewrite `TestOcrPdfPages`**

Replace the whole `class TestOcrPdfPages(unittest.IsolatedAsyncioTestCase): ...` block with:

```python
class TestOcrPdfPages(unittest.IsolatedAsyncioTestCase):
    async def test_delegates_to_backend_and_caches_by_content_hash(self):
        pdf_bytes = _two_page_pdf_bytes()
        backend = AsyncMock()
        backend.ocr_pdf_pages.return_value = ["page one text", "page two text"]
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            pages = await ocr_pdf_pages(pdf_bytes, backend=backend, cache_dir=cache_dir, language="deu")
            self.assertEqual(pages, ["page one text", "page two text"])
            backend.ocr_pdf_pages.assert_awaited_once_with(pdf_bytes, language="deu")
            self.assertEqual(len(list(cache_dir.glob("*.json"))), 1)

            # Second call with identical bytes: served from cache, backend untouched.
            pages_again = await ocr_pdf_pages(pdf_bytes, backend=backend, cache_dir=cache_dir, language="deu")
            self.assertEqual(pages_again, ["page one text", "page two text"])
            backend.ocr_pdf_pages.assert_awaited_once()  # still just the one call
```

(This drops the old `test_reports_per_page_progress` test — the `on_page` callback no longer exists, per this plan's "deliberate behavior changes" note.)

- [ ] **Step 3: Confirm `MagicMock` is still needed** — it isn't used anywhere else in this file after the deletions above, so remove it from the `unittest.mock` import too if unused:

```bash
grep -n "MagicMock" tests/test_ocr.py
```

If no hits remain outside the import line, remove `MagicMock` from `from unittest.mock import AsyncMock, MagicMock`.

- [ ] **Step 4: Run the file**

```bash
uv run pytest tests/test_ocr.py -v
```

Expected: all pass (`TestDetectLanguage`, `TestOcrCache`, `TestOcrPdfPages`).

- [ ] **Step 5: Commit**

```bash
git add tests/test_ocr.py
git commit -m "test: rewrite test_ocr.py for the OcrBackend-based ocr_pdf_pages, drop TestOcrRun"
git push
```

---

### Task 10: Trim `tests/test_segmentation.py` — remove `TestRun`, verify the rest

**Files:**
- Modify: `tests/test_segmentation.py`

`TestRun` (currently ~448 lines) tests the removed `run()` — its replacement gets new tests in Part 2. Every other test class in this file calls the pure functions positionally (verified while writing this plan — no test anywhere passes `llm_service=` as a keyword to a function in this file), so the Task 7 rename requires **no other edits** to this file.

- [ ] **Step 1: Delete the `TestRun` class**

Remove the whole `class TestRun(unittest.TestCase): ...` block (from `class TestRun` up to, but not including, the following `class TestPagesNeedOcr(unittest.TestCase):`).

- [ ] **Step 2: Remove now-unused imports**

`TestRun` was the only user of `from backend.config.settings import get_settings, reset_settings` and `from backend.services.review_queue_store import get_entry` (already rewritten to `chapter_segmentation.*` paths by Task 5's sweep only if they matched one of those ten patterns — they don't, since `settings`/`review_queue_store` never moved, so these two lines still literally say `backend.config.settings`/`backend.services.review_queue_store`). Delete both lines entirely — there is no equivalent in the new package.

```bash
grep -n "backend\." tests/test_segmentation.py
```

Expected after deleting those two lines: no output.

- [ ] **Step 3: Check for other `TestRun`-only helpers**

Some helper functions/constants near the top of the file (e.g. `_SUBSTANTIAL_FILLER_PAGE`, `_pdf_with_outline`, `_blank_pdf`) may be used only by the deleted `TestRun`. Check each:

```bash
grep -n "_SUBSTANTIAL_FILLER_PAGE\|_pdf_with_outline\|_blank_pdf\|_TWO_CHAPTER_PAGES" tests/test_segmentation.py
```

For any name that now appears only in its own definition line (zero remaining call sites), delete that definition — dead code left behind by the `TestRun` deletion. Keep any that are still referenced by a surviving test class.

- [ ] **Step 4: Run the file**

```bash
uv run pytest tests/test_segmentation.py -v
```

Expected: all pass, and the earlier `perl` rename (Task 7, Step 4) already made the `llm` positional-argument tests work unchanged.

- [ ] **Step 5: Commit**

```bash
git add tests/test_segmentation.py
git commit -m "test: remove TestRun (tested the removed run()) and dead helpers from test_segmentation.py"
git push
```

---

### Task 11: Add the two OCR backend implementations

**Files:**
- Create: `src/chapter_segmentation/ocr_backends/__init__.py`
- Create: `src/chapter_segmentation/ocr_backends/kreuzberg.py`
- Create: `src/chapter_segmentation/ocr_backends/tesseract.py`
- Test: `tests/ocr_backends/test_kreuzberg.py`
- Test: `tests/ocr_backends/test_tesseract.py`

- [ ] **Step 1: `ocr_backends/__init__.py`**

```bash
mkdir -p src/chapter_segmentation/ocr_backends tests/ocr_backends
touch src/chapter_segmentation/ocr_backends/__init__.py tests/ocr_backends/__init__.py
```

- [ ] **Step 2: Write `ocr_backends/kreuzberg.py`**

A self-contained adapter (independent of zotero-rag's own Kreuzberg extractor) calling a Kreuzberg sidecar's `/extract` endpoint once per page, mirroring what `chapter_ocr.py`'s old `ocr_pdf_pages` used to do inline:

```python
"""Kreuzberg-sidecar-backed OcrBackend. Requires the `kreuzberg` optional
extra (httpx). See chapter_segmentation.ocr.OcrBackend.
"""

import io
import json
from typing import Optional

import httpx
from pypdf import PdfReader, PdfWriter

from chapter_segmentation.ocr import slice_single_page_pdf


class KreuzbergOcrBackend:
    """Calls a running Kreuzberg sidecar's HTTP API, one request per page --
    guarantees a clean 1:1 page-index<->text mapping, matching pypdf's own
    indexing used throughout the segmentation engine.
    """

    def __init__(self, kreuzberg_url: str = "http://localhost:8100", timeout: float = 120.0):
        self._kreuzberg_url = kreuzberg_url.rstrip("/")
        self._timeout = timeout

    async def ocr_pdf_pages(self, content: bytes, *, language: Optional[str] = None) -> list[str]:
        reader = PdfReader(io.BytesIO(content))
        total_pages = len(reader.pages)
        config = {"force_ocr": True}
        if language:
            config["ocr"] = {"language": language}

        page_texts: list[str] = []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for page_index in range(total_pages):
                single_page_bytes = slice_single_page_pdf(content, page_index)
                response = await client.post(
                    f"{self._kreuzberg_url}/extract",
                    files={"files": ("page.pdf", single_page_bytes, "application/pdf")},
                    data={"config": json.dumps(config)},
                )
                response.raise_for_status()
                results = response.json()
                chunks = (results[0].get("chunks") or []) if results else []
                page_texts.append(" ".join(c.get("content") or "" for c in chunks))
        return page_texts
```

- [ ] **Step 3: Write `tests/ocr_backends/test_kreuzberg.py`**

```python
import io
import unittest
from unittest.mock import AsyncMock, patch

from pypdf import PdfWriter

from chapter_segmentation.ocr_backends.kreuzberg import KreuzbergOcrBackend


def _two_page_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestKreuzbergOcrBackend(unittest.IsolatedAsyncioTestCase):
    async def test_sends_one_request_per_page_and_joins_chunks(self):
        pdf_bytes = _two_page_pdf_bytes()
        backend = KreuzbergOcrBackend(kreuzberg_url="http://fake:8100")

        responses = [
            AsyncMock(status_code=200, json=lambda: [{"chunks": [{"content": "page one"}]}]),
            AsyncMock(status_code=200, json=lambda: [{"chunks": [{"content": "page two"}]}]),
        ]
        for r in responses:
            r.raise_for_status = lambda: None

        with patch("httpx.AsyncClient.post", AsyncMock(side_effect=responses)) as mock_post:
            pages = await backend.ocr_pdf_pages(pdf_bytes, language="eng")

        self.assertEqual(pages, ["page one", "page two"])
        self.assertEqual(mock_post.await_count, 2)
        first_call_kwargs = mock_post.await_args_list[0].kwargs
        self.assertEqual(first_call_kwargs["data"], {"config": '{"force_ocr": true, "ocr": {"language": "eng"}}'})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Run and fix**

```bash
uv sync --extra kreuzberg
uv run pytest tests/ocr_backends/test_kreuzberg.py -v
```

If the JSON key-order assertion is brittle (dict ordering isn't guaranteed across Python versions in older code, though 3.7+ preserves insertion order so this should be stable given the fixed literal construction order in Step 2), replace it with `json.loads(first_call_kwargs["data"]["config"])` and compare the parsed dict instead of the raw string. Expected: passes either way.

- [ ] **Step 5: Write `ocr_backends/tesseract.py`**

```python
"""Local-binary OcrBackend: renders PDF pages with pymupdf and recognizes
text with the system `tesseract` binary via pytesseract. No daemon, no
network, no container -- see chapter_segmentation.ocr.OcrBackend and
design spec section 4 (in the zotero-rag repo this was extracted from) for
why this exists alongside KreuzbergOcrBackend.

Requires the `tesseract` extra (pytesseract, pymupdf, pillow) AND the
system `tesseract` binary + language data on PATH:
    apt-get install tesseract-ocr tesseract-ocr-deu tesseract-ocr-fra tesseract-ocr-spa   # Debian/Ubuntu
    brew install tesseract tesseract-lang                                                  # macOS
"""

import asyncio
import shutil
from typing import Optional


_INSTALL_HINT = (
    "tesseract binary not found on PATH. Install it: "
    "apt-get install tesseract-ocr tesseract-ocr-deu tesseract-ocr-fra tesseract-ocr-spa (Debian/Ubuntu), "
    "brew install tesseract tesseract-lang (macOS), "
    "or see https://tesseract-ocr.github.io/tessdoc/Installation.html"
)


class TesseractOcrBackend:
    """Renders each page at `dpi` and OCRs it with pytesseract/tesseract."""

    def __init__(self, dpi: int = 300):
        if shutil.which("tesseract") is None:
            raise RuntimeError(_INSTALL_HINT)
        self._dpi = dpi

    async def ocr_pdf_pages(self, content: bytes, *, language: Optional[str] = None) -> list[str]:
        return await asyncio.to_thread(self._ocr_sync, content, language or "eng")

    def _ocr_sync(self, content: bytes, language: str) -> list[str]:
        import fitz  # pymupdf
        import pytesseract
        from PIL import Image

        doc = fitz.open(stream=content, filetype="pdf")
        try:
            pages: list[str] = []
            for page in doc:
                pix = page.get_pixmap(dpi=self._dpi)
                image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                pages.append(pytesseract.image_to_string(image, lang=language))
            return pages
        finally:
            doc.close()
```

- [ ] **Step 6: Write `tests/ocr_backends/test_tesseract.py`**

Two unit tests (constructor error path, no real tesseract binary needed) plus one `integration`-marked test that requires the real binary:

```python
import shutil
import unittest
from unittest.mock import patch

import pytest

from chapter_segmentation.ocr_backends.tesseract import TesseractOcrBackend


class TestTesseractOcrBackendConstructor(unittest.TestCase):
    def test_raises_actionable_error_when_binary_missing(self):
        with patch("shutil.which", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                TesseractOcrBackend()
        self.assertIn("apt-get install tesseract-ocr", str(ctx.exception))

    @unittest.skipUnless(shutil.which("tesseract"), "tesseract binary not installed")
    def test_constructs_when_binary_present(self):
        TesseractOcrBackend()  # must not raise


@pytest.mark.integration
class TestTesseractOcrBackendRealOcr(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(shutil.which("tesseract"), "tesseract binary not installed")
    async def test_ocrs_a_real_rendered_page(self):
        import fitz  # pymupdf

        doc = fitz.open()
        page = doc.new_page(width=400, height=200)
        page.insert_text((50, 100), "HELLO WORLD", fontsize=24)
        pdf_bytes = doc.tobytes()
        doc.close()

        backend = TesseractOcrBackend()
        pages = await backend.ocr_pdf_pages(pdf_bytes, language="eng")

        self.assertEqual(len(pages), 1)
        self.assertIn("HELLO", pages[0].upper())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 7: Run**

```bash
uv sync --extra tesseract
uv run pytest tests/ocr_backends/test_tesseract.py -v            # unit tests only (default addopts skips "integration")
uv run pytest tests/ocr_backends/test_tesseract.py -v -m integration   # only if tesseract is installed locally
```

Expected: unit tests pass everywhere; the integration test passes wherever `tesseract` is actually installed (install it locally to verify once — `brew install tesseract` on macOS — then re-run).

- [ ] **Step 8: Commit**

```bash
git add src/chapter_segmentation/ocr_backends tests/ocr_backends
git commit -m "feat: add KreuzbergOcrBackend and TesseractOcrBackend"
git push
```

---

### Task 12: Write the standalone CLI

**Files:**
- Create: `src/chapter_segmentation/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write `cli.py`**

```python
"""Standalone CLI: `chapter-segmentation analyze <pdf>` -- no Zotero, no
RAG app, just a PDF in and a chapter list out. Defaults to TesseractOcrBackend
(no container required) when the PDF needs OCR; pass --ocr-backend kreuzberg
to use a running Kreuzberg sidecar instead.
"""

import argparse
import asyncio
import json
import sys

from chapter_segmentation.ocr import ocr_pdf_pages
from chapter_segmentation.segmentation import (
    analyze_attachment,
    extract_page_texts_for_analysis,
    pages_need_ocr,
)


def _build_ocr_backend(name: str, kreuzberg_url: str):
    if name == "tesseract":
        from chapter_segmentation.ocr_backends.tesseract import TesseractOcrBackend
        return TesseractOcrBackend()
    if name == "kreuzberg":
        from chapter_segmentation.ocr_backends.kreuzberg import KreuzbergOcrBackend
        return KreuzbergOcrBackend(kreuzberg_url=kreuzberg_url)
    raise ValueError(f"Unknown --ocr-backend {name!r}")


async def _analyze(pdf_path: str, ocr_backend_name: str, kreuzberg_url: str, ocr_cache_dir: str) -> dict:
    file_bytes = open(pdf_path, "rb").read()
    pages, _layout_used = extract_page_texts_for_analysis(file_bytes)

    if pages_need_ocr(pages):
        from pathlib import Path
        backend = _build_ocr_backend(ocr_backend_name, kreuzberg_url)
        pages = await ocr_pdf_pages(file_bytes, backend=backend, cache_dir=Path(ocr_cache_dir), language="eng")

    return analyze_attachment(pages)


def main() -> None:
    parser = argparse.ArgumentParser(prog="chapter-segmentation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze", help="Detect chapter boundaries in a PDF")
    analyze_parser.add_argument("pdf", help="Path to the PDF file")
    analyze_parser.add_argument(
        "--ocr-backend", choices=["tesseract", "kreuzberg"], default="tesseract",
        help="OCR backend to use if the PDF needs OCR (default: tesseract, no container required)",
    )
    analyze_parser.add_argument(
        "--kreuzberg-url", default="http://localhost:8100",
        help="Kreuzberg sidecar URL (only used with --ocr-backend kreuzberg)",
    )
    analyze_parser.add_argument("--ocr-cache-dir", default=".chapter-segmentation-ocr-cache")

    args = parser.parse_args()
    if args.command == "analyze":
        result = asyncio.run(_analyze(args.pdf, args.ocr_backend, args.kreuzberg_url, args.ocr_cache_dir))
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write `tests/test_cli.py`**

```python
import json
import subprocess
import sys
import unittest

from pypdf import PdfWriter


class TestCli(unittest.TestCase):
    def test_analyze_a_blank_pdf_runs_end_to_end(self):
        writer = PdfWriter()
        writer.add_blank_page(width=400, height=600)
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            writer.write(f)
            pdf_path = f.name

        result = subprocess.run(
            [sys.executable, "-m", "chapter_segmentation.cli", "analyze", pdf_path],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(result.stdout)
        self.assertIn("chapters", parsed)
        self.assertIn("total_pdf_pages", parsed)


if __name__ == "__main__":
    unittest.main()
```

Note: `python -m chapter_segmentation.cli` requires a `__main__`-runnable module; `if __name__ == "__main__": main()` at the bottom of `cli.py` (already present in Step 1) makes this work exactly like invoking the installed `chapter-segmentation` console script.

- [ ] **Step 3: Run**

```bash
uv sync
uv run pytest tests/test_cli.py -v
uv run chapter-segmentation analyze evaluation/9783031466373.pdf 2>/dev/null | head -5 || echo "(skip if the real PDF isn't present locally -- it's gitignored)"
```

Expected: `test_cli.py` passes (a blank PDF has no text layer and no TOC, so it needs OCR — the tesseract backend will attempt it; if `tesseract` isn't installed locally, this test will raise `RuntimeError` from `TesseractOcrBackend`'s constructor and fail. Install `tesseract` locally first — `brew install tesseract` on macOS — or mark this specific test `@pytest.mark.integration` like Task 11's tesseract tests if you'd rather not require the binary for the default test run. Prefer marking it `integration`, consistent with Task 11's convention, since CI's default job (Task 14) shouldn't need the binary either — decide and apply this before committing.)

- [ ] **Step 4: Commit**

```bash
git add src/chapter_segmentation/cli.py tests/test_cli.py
git commit -m "feat: add standalone CLI (chapter-segmentation analyze <pdf>)"
git push
```

---

### Task 13: Fix the remaining evaluation scripts

**Files:**
- Modify: `evaluation/scripts/generate_public_evaluation_cache.py` (mechanical only — already covered by Task 5's sweep; this task just adjusts its `sys.path` depth)
- Modify: `evaluation/scripts/evaluate_chapter_segmentation_strategies.py` (mechanical only, plus `sys.path` depth)
- Modify: `evaluation/scripts/ocr_evaluation_pdfs.py` (rewrite — was still using `backend.config.settings`/`backend.services.extraction.kreuzberg`)
- Modify: `evaluation/scripts/evaluate_chapter_segmentation_llm_fallback.py` (rewrite — was still using `backend.dependencies.make_llm_service`)
- Verify only, no changes needed: `evaluation/scripts/fetch_evaluation_pdfs.py`, `evaluation/scripts/ground_truth_helper.py`

- [ ] **Step 1: Fix the `sys.path` depth in the four scripts that have it**

These scripts moved one directory deeper (`scripts/foo.py` → `evaluation/scripts/foo.py`), so their existing `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` (which reached repo root from one level deep) now needs one more `.parent` to still reach repo root:

```bash
perl -pi -e 's/Path\(__file__\)\.resolve\(\)\.parent\.parent\)\)/Path(__file__).resolve().parent.parent.parent))/' \
  evaluation/scripts/ocr_evaluation_pdfs.py \
  evaluation/scripts/generate_public_evaluation_cache.py \
  evaluation/scripts/evaluate_chapter_segmentation_strategies.py \
  evaluation/scripts/evaluate_chapter_segmentation_llm_fallback.py
```

- [ ] **Step 2: Rewrite `ocr_evaluation_pdfs.py`**

Replace the `from backend.config.settings import get_settings` and `from backend.services.extraction.kreuzberg import KreuzbergExtractor` lines, and the body, with:

```python
#!/usr/bin/env python3
"""OCR the evaluation books whose text layer is absent or degenerate, into
the gitignored evaluation OCR cache (evaluation/.ocr-cache/, content-hash
keyed), so the accuracy harness and evaluation scripts can analyze them the
way a real caller would.

Uses KreuzbergOcrBackend by default (pass --ocr-backend tesseract for the
local-binary path instead). Books already cached are skipped instantly, so
re-runs are cheap; the first run over several full scanned books takes a
long time.

    uv run python evaluation/scripts/ocr_evaluation_pdfs.py
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from chapter_segmentation.ocr import detect_language, ocr_pdf_pages
from chapter_segmentation.segmentation import extract_page_texts_for_analysis, pages_need_ocr
from evaluation.harness import OCR_CACHE_DIR, available_books


async def _main(ocr_backend_name: str, kreuzberg_url: str) -> int:
    if ocr_backend_name == "tesseract":
        from chapter_segmentation.ocr_backends.tesseract import TesseractOcrBackend
        backend = TesseractOcrBackend()
    else:
        from chapter_segmentation.ocr_backends.kreuzberg import KreuzbergOcrBackend
        backend = KreuzbergOcrBackend(kreuzberg_url=kreuzberg_url)

    for pdf_path, _expected_path, book in available_books():
        file_bytes = pdf_path.read_bytes()
        pages, _layout_used = extract_page_texts_for_analysis(file_bytes)
        if not pages_need_ocr(pages):
            print(f"{pdf_path.name}: text layer usable, no OCR needed")
            continue
        language = detect_language(book.get("language"), book.get("title", ""))
        print(f"{pdf_path.name}: OCR-ing {len(pages)} pages (language={language}) ...", flush=True)
        try:
            page_texts = await ocr_pdf_pages(file_bytes, backend=backend, cache_dir=OCR_CACHE_DIR, language=language)
        except Exception as exc:
            print(f"{pdf_path.name}: FAILED ({exc}) -- skipping, will retry on next run", flush=True)
            continue
        print(f"{pdf_path.name}: done, {sum(len(p) for p in page_texts)} chars cached", flush=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ocr-backend", choices=["kreuzberg", "tesseract"], default="kreuzberg")
    parser.add_argument("--kreuzberg-url", default="http://localhost:8100")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.ocr_backend, args.kreuzberg_url)))
```

(Note: the module docstring's mention of a fixed `on_page`-driven "N/25 pages" ticker is intentionally gone — see this plan's "deliberate behavior changes" note.)

- [ ] **Step 3: Rewrite `evaluate_chapter_segmentation_llm_fallback.py`**

Replace `from backend.dependencies import make_llm_service` and the `llm_service = make_llm_service(...)` construction with a minimal, self-contained OpenAI-compatible client:

```python
#!/usr/bin/env python3
"""Runs the chapter-segmentation evaluation set through
analyze_attachment_with_llm_fallback instead of the pure-heuristic
analyze_attachment, and prints the same precision/recall table format
test_segmentation_accuracy.py already uses, plus per-book fallback-usage
counts.

Requires the `llm-eval` extra (openai) and a real, working LLM endpoint --
costs a paid API call per book. Not a pytest test, run manually:

    OPENAI_API_KEY=... uv run python evaluation/scripts/evaluate_chapter_segmentation_llm_fallback.py

Pass --base-url to point at any OpenAI-compatible endpoint (e.g. KISSKI)
instead of api.openai.com, and --model to pick the model:

    OPENAI_API_KEY=... uv run python evaluation/scripts/evaluate_chapter_segmentation_llm_fallback.py \\
      --base-url https://chat-ai.academiccloud.de/v1 --model meta-llama-3.1-8b-instruct
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from chapter_segmentation.segmentation import analyze_attachment_with_llm_fallback
from evaluation.harness import analysis_pages_for, available_books


class _OpenAICompatibleLLMClient:
    """Minimal LLMClient (see chapter_segmentation.llm.LLMClient) backed by
    any OpenAI-compatible chat completions endpoint. Deliberately does not
    replicate zotero-rag's own multi-provider preset/model-rotation
    machinery -- that lives with the Zotero integration, not this
    standalone evaluation script.
    """

    def __init__(self, model: str, base_url: Optional[str] = None):
        from openai import AsyncOpenAI
        self._client = AsyncOpenAI(base_url=base_url, api_key=os.environ["OPENAI_API_KEY"])
        self._model = model

    async def generate(
        self, prompt: str, *, max_tokens: int, temperature: float,
        is_valid: Optional[Callable[[str], bool]] = None,
    ) -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return response.choices[0].message.content or ""


async def _main(model: str, base_url: Optional[str]) -> int:
    pairs = available_books()
    if not pairs:
        print("No evaluation PDFs present -- run: uv run python evaluation/scripts/fetch_evaluation_pdfs.py")
        return 1

    llm_client = _OpenAICompatibleLLMClient(model=model, base_url=base_url)

    for pdf_path, expected_path, _book in pairs:
        expected = json.loads(expected_path.read_text(encoding="utf-8"))["chapters"]
        pages = analysis_pages_for(pdf_path.read_bytes())
        if pages is None:
            print(f"{pdf_path.name}: SKIPPED (needs OCR — populate the cache with: "
                  f"uv run python evaluation/scripts/ocr_evaluation_pdfs.py)")
            continue
        result = await analyze_attachment_with_llm_fallback(pages, llm_client)

        expected_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in expected}
        found_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in result["chapters"]}
        true_positives = expected_ranges & found_ranges

        precision = len(true_positives) / len(found_ranges) if found_ranges else 0.0
        recall = len(true_positives) / len(expected_ranges) if expected_ranges else 0.0
        diag = result["diagnostics"]
        print(
            f"{pdf_path.name}: precision={precision:.2f} recall={recall:.2f} "
            f"({len(true_positives)}/{len(found_ranges)} found, {len(true_positives)}/{len(expected_ranges)} expected) "
            f"llm_toc_extraction_used={diag.get('llm_toc_extraction_used')} "
            f"llm_disambiguation_used={diag.get('llm_disambiguation_used')}"
        )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--base-url", default=None)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(model=args.model, base_url=args.base_url)))
```

- [ ] **Step 4: Verify `fetch_evaluation_pdfs.py` and `ground_truth_helper.py` need no code changes**

```bash
grep -n "backend\." evaluation/scripts/fetch_evaluation_pdfs.py evaluation/scripts/ground_truth_helper.py
```

Expected: no output (confirmed while researching this plan — neither file ever imported anything from `backend.*`). They only moved directory, which `git filter-repo` already handled.

- [ ] **Step 5: Sanity-check every evaluation script at least imports cleanly**

```bash
uv sync --extra kreuzberg --extra tesseract --extra llm-eval
for f in evaluation/scripts/*.py; do
  echo "=== $f ==="
  uv run python -c "import ast; ast.parse(open('$f').read())" && echo OK
done
grep -rn "backend\." evaluation/
```

Expected: every file reports `OK` (valid Python syntax) and the final grep shows no output at all.

- [ ] **Step 6: Commit**

```bash
git add evaluation/scripts
git commit -m "fix: rewrite ocr_evaluation_pdfs.py and evaluate_chapter_segmentation_llm_fallback.py to drop zotero-rag dependencies"
git push
```

---

### Task 14: Fix the redaction pipeline imports and run the full non-integration suite

**Files:**
- Verify only: `evaluation/redaction/redact.py`, `evaluation/redaction/region_classification.py` (already fixed by Task 5's sweep)
- Modify: `evaluation/README.md`, `evaluation/CLAUDE.md`, `evaluation/RESULTS.md` (stale path references)

- [ ] **Step 1: Confirm the redaction pipeline's imports were already fixed**

```bash
grep -n "^from \|^import " evaluation/redaction/redact.py evaluation/redaction/region_classification.py
```

Expected: `from chapter_segmentation.segmentation import (...)` and `from evaluation.redaction.region_classification import ...` / `from evaluation.redaction.wordlists import ...` — Task 5's sweep already handles both patterns (`backend.services.chapter_segmentation` → `chapter_segmentation.segmentation`, and `scripts.evaluation_redaction.` → `evaluation.redaction.`). If either still shows a `backend.` or `scripts.` reference, re-run the relevant `perl` command from Task 5, Step 1.

- [ ] **Step 2: Update stale path references in the moved docs**

`evaluation/README.md`, `evaluation/CLAUDE.md`, and `evaluation/RESULTS.md` were written when this content lived at `backend/evaluation/book-segmentation/` and referenced scripts at `scripts/*.py`. Search and fix:

```bash
grep -rn "backend/evaluation/book-segmentation\|backend/services/chapter\|scripts/ocr_evaluation_pdfs\|scripts/generate_public_evaluation_cache\|scripts/fetch_evaluation_pdfs\|scripts/ground_truth_helper\|scripts/evaluate_chapter_segmentation\|scripts/evaluation_redaction\|backend/tests/test_chapter\|backend/tests/test_evaluation\|backend/tests/test_public_evaluation" \
  evaluation/README.md evaluation/CLAUDE.md evaluation/RESULTS.md
```

For each hit, update the path to its new location per the table at the top of this plan (e.g. `backend/evaluation/book-segmentation/` → the file is now already inside `evaluation/`, so drop that prefix entirely; `scripts/ocr_evaluation_pdfs.py` → `evaluation/scripts/ocr_evaluation_pdfs.py`; `backend/tests/test_chapter_segmentation_accuracy.py` → `tests/test_segmentation_accuracy.py`; `backend/services/chapter_segmentation.py`/`chapter_common.py` → `src/chapter_segmentation/segmentation.py`/`common.py`). Also add one line near the top of `evaluation/RESULTS.md` pointing at the Task 15 Pages URL as the always-current numbers source, per spec §11:

```markdown
> **Always-current numbers:** https://cboulanger.github.io/chapter-segmentation/ (auto-published from `evaluation/generate_report.py`, no hand-written analysis). This file adds mechanism/root-cause commentary the published page deliberately omits, and is only updated by hand.
```

- [ ] **Step 3: Run the whole non-integration suite**

```bash
uv sync --extra kreuzberg --extra tesseract --extra llm-eval
uv run pytest -v
```

Expected: everything passes except tests requiring real evaluation PDFs (gitignored, not present in this clone — they'll report `SKIPPED`, not `FAILED`) and anything marked `integration`. Investigate and fix any genuine `FAILED` result before proceeding — do not move on with red tests.

- [ ] **Step 4: Run the public-cache parity check specifically** (this is what Task 15's CI job runs)

```bash
uv run pytest tests/test_public_evaluation_cache_parity.py -v -s -m integration
```

Expected: passes, printing one precision/recall line per book in the committed `public-cache/` corpus (no PDFs or `.ocr-cache/` needed).

- [ ] **Step 5: Commit**

```bash
git add evaluation/README.md evaluation/CLAUDE.md evaluation/RESULTS.md
git commit -m "docs: fix stale backend/scripts path references after the repo split"
git push
```

---

### Task 15: Add the CI-published results page

**Files:**
- Create: `evaluation/generate_report.py`
- Modify: `tests/test_public_evaluation_cache_parity.py` (share the precision/recall computation with the generator)
- Create: `.github/workflows/publish-results.yml`

- [ ] **Step 1: Write `evaluation/generate_report.py`**

```python
#!/usr/bin/env python3
"""Generates a prose-free static results page from the committed
public-cache corpus -- see design spec section 11 (in the zotero-rag repo
this was extracted from). No LLM call anywhere in this path; a plain
f-string template, no templating-engine dependency.

    uv run python evaluation/generate_report.py --out public/
"""

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chapter_segmentation.segmentation import analyze_attachment
from evaluation.harness import available_public_books, public_pages_for


def compute_precision_recall(expected: list[dict], found: list[dict]) -> tuple[float, float, int, int, int]:
    """Returns (precision, recall, true_positives, found_count, expected_count)."""
    expected_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in expected}
    found_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in found}
    true_positives = expected_ranges & found_ranges
    precision = len(true_positives) / len(found_ranges) if found_ranges else 0.0
    recall = len(true_positives) / len(expected_ranges) if expected_ranges else 0.0
    return precision, recall, len(true_positives), len(found_ranges), len(expected_ranges)


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def _row(manifest_key: str, precision: float, recall: float, tp: int, found: int, expected: int) -> str:
    return (
        f"<tr><td>{manifest_key}</td><td>{precision:.2f}</td><td>{recall:.2f}</td>"
        f"<td>{tp}/{found} found, {tp}/{expected} expected</td></tr>"
    )


def generate(out_dir: Path) -> None:
    import json

    rows: list[str] = []
    total_tp = total_found = total_expected = 0

    for manifest_key, expected_path, _book in available_public_books():
        expected = json.loads(expected_path.read_text(encoding="utf-8"))["chapters"]
        pages = public_pages_for(manifest_key)
        result = analyze_attachment(pages)
        precision, recall, tp, found, exp = compute_precision_recall(expected, result["chapters"])
        rows.append(_row(manifest_key, precision, recall, tp, found, exp))
        total_tp += tp
        total_found += found
        total_expected += exp

    micro_precision = total_tp / total_found if total_found else 0.0
    micro_recall = total_tp / total_expected if total_expected else 0.0

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>chapter-segmentation results</title>
<style>table {{ border-collapse: collapse; }} td, th {{ border: 1px solid #ccc; padding: 4px 8px; }}</style>
</head><body>
<h1>chapter-segmentation: public-cache corpus results</h1>
<table>
<tr><th>Book</th><th>Precision</th><th>Recall</th><th>Found / Expected</th></tr>
{"".join(rows)}
<tr><th>Aggregate (micro)</th><th>{micro_precision:.2f}</th><th>{micro_recall:.2f}</th><th>{total_tp}/{total_found} found, {total_tp}/{total_expected} expected</th></tr>
</table>
<p>Generated {datetime.now(timezone.utc).isoformat()} from commit {_git_sha()}.</p>
</body></html>
"""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="public")
    args = parser.parse_args()
    generate(Path(args.out))
```

- [ ] **Step 2: Refactor `tests/test_public_evaluation_cache_parity.py` to import the shared metric**

Replace the inline precision/recall computation with a call to the new shared function:

```python
from evaluation.generate_report import compute_precision_recall
```

(add this import), then replace the block:

```python
                expected_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in expected}
                found_ranges = {(c["pdf_start_index"], c["pdf_end_index"]) for c in result["chapters"]}
                true_positives = expected_ranges & found_ranges

                precision = len(true_positives) / len(found_ranges) if found_ranges else 0.0
                recall = len(true_positives) / len(expected_ranges) if expected_ranges else 0.0
                print(f"{manifest_key}: precision={precision:.2f} recall={recall:.2f} "
                      f"({len(true_positives)}/{len(found_ranges)} found, "
                      f"{len(true_positives)}/{len(expected_ranges)} expected)")
```

with:

```python
                precision, recall, tp, found, exp = compute_precision_recall(expected, result["chapters"])
                print(f"{manifest_key}: precision={precision:.2f} recall={recall:.2f} "
                      f"({tp}/{found} found, {tp}/{exp} expected)")
```

- [ ] **Step 3: Run both**

```bash
uv run pytest tests/test_public_evaluation_cache_parity.py -v -s -m integration
uv run python evaluation/generate_report.py --out /tmp/report-check
cat /tmp/report-check/index.html | head -20
open /tmp/report-check/index.html   # macOS; use xdg-open on Linux
```

Expected: the test still passes with identical printed numbers as before the refactor, and the generated HTML opens showing a table with the same per-book numbers plus an aggregate row and a generation timestamp/commit footer.

- [ ] **Step 4: Write `.github/workflows/publish-results.yml`**

```yaml
name: Publish results

on:
  push:
    branches: [main]
  workflow_dispatch: {}

permissions:
  pages: write
  id-token: write

jobs:
  build-and-publish:
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync
      - run: uv run python evaluation/generate_report.py --out public/
      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with:
          path: public/
      - id: deployment
        uses: actions/deploy-pages@v4
```

- [ ] **Step 5: Commit**

```bash
git add evaluation/generate_report.py tests/test_public_evaluation_cache_parity.py .github/workflows/publish-results.yml
git commit -m "feat: publish a prose-free results page to GitHub Pages on every push to main"
git push
```

---

### Task 16: One-time Pages setup, first workflow run, and the `v0.1.0` tag

**Files:** none — repository configuration and a release tag.

- [ ] **Step 1: Switch Pages to Actions-based deployment**

```bash
gh api repos/cboulanger/chapter-segmentation/pages -X POST -f build_type=workflow
```

Expected: a 201 response (or 409 if it's already configured — either is fine). If this returns 404/422 because Pages has never been touched on this repo, this call is exactly what initializes it — no need to also enable Pages from the web UI first.

- [ ] **Step 2: Confirm the workflow runs and publishes**

```bash
gh workflow run publish-results.yml
gh run watch
```

Expected: the run completes successfully (green). Then:

```bash
curl -sI https://cboulanger.github.io/chapter-segmentation/ | head -1
```

Expected: `HTTP/2 200` (allow a minute or two after the run finishes for Pages to propagate if this returns 404 immediately).

- [ ] **Step 3: Tag the release Part 2 depends on**

```bash
git tag v0.1.0
git push origin v0.1.0
```

- [ ] **Step 4: Verify the tag resolves as an installable dependency**

```bash
cd /tmp
uv venv verify-chapter-segmentation-venv
source verify-chapter-segmentation-venv/bin/activate
uv pip install "chapter-segmentation[kreuzberg] @ git+https://github.com/cboulanger/chapter-segmentation.git@v0.1.0"
python -c "import chapter_segmentation.segmentation, chapter_segmentation.ocr_backends.kreuzberg; print('OK')"
deactivate
rm -rf verify-chapter-segmentation-venv
```

Expected: `OK`. This confirms the exact dependency string Part 2 will add to zotero-rag's `pyproject.toml` actually resolves and installs cleanly.

**Part 1 is complete.** Proceed to `docs/superpowers/plans/2026-08-06-chapter-segmentation-zotero-rag-integration.md`.
