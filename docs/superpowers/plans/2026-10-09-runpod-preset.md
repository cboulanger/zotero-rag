# RunPod Preset + Endpoint-Provisioning Script Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `runpod` hardware preset and a `scripts/provision_runpod_endpoints.py` script that, given a RunPod API key, idempotently creates (or wakes up) two RunPod serverless endpoints — one for `intfloat/multilingual-e5-large-instruct` embeddings, one for a Qwen2.5-7B-Instruct chat LLM — as a self-hosted alternative to KISSKI/MPCDF.

**Architecture:** The preset follows the existing `remote-mpcdf` pattern exactly (`shared_base_url_env`/`shared_api_key_env` in `model_kwargs`, resolved at request time with zero changes to `backend/services/embeddings.py`/`llm.py`). The script is a standalone, dependency-free (beyond `httpx`/`python-dotenv`, already project dependencies) REST client against `https://rest.runpod.io/v1`, following the `bin/migrate_library.py` test convention (`importlib.util.spec_from_file_location` + a `FakeClient` stand-in for `httpx.Client`).

**Tech Stack:** Python 3.12, `httpx`, `python-dotenv`, `pytest`/`unittest`.

**Spec:** `docs/superpowers/specs/2026-10-09-runpod-preset-design.md`

---

### Task 1: Add the `runpod` bundled default preset

**Files:**
- Create: `backend/config/default_presets/runpod.json`
- Modify: `backend/tests/test_config.py:30-40` (add `"runpod"` to `EXPECTED_BUNDLED_PRESET_NAMES`), add new test after `test_get_preset_remote_mpcdf_uses_shared_dynamic_fields` (line ~115-121)

- [ ] **Step 1: Write the failing tests**

Edit `backend/tests/test_config.py`. First, add `"runpod"` to the set:

```python
EXPECTED_BUNDLED_PRESET_NAMES = {
    "apple-silicon-32gb",
    "high-memory",
    "cpu-only",
    "remote-openai",
    "apple-silicon-kisski",
    "remote-kisski",
    "cloud-server-kisski",
    "windows-test",
    "remote-mpcdf",
    "runpod",
}
```

Then add this test directly after `test_get_preset_remote_mpcdf_uses_shared_dynamic_fields`:

```python
    def test_get_preset_runpod_uses_shared_dynamic_fields(self):
        """runpod has no static base_url — both the embedding and LLM endpoint
        URLs are only known after scripts/provision_runpod_endpoints.py creates
        them, so (like remote-mpcdf) they're resolved at request time from the
        shared admin-set store rather than baked into the preset."""
        preset = get_preset("runpod", self.data_path)

        self.assertEqual(preset.name, "runpod")
        self.assertEqual(preset.embedding.model_type, "remote")
        self.assertEqual(preset.embedding.model_name, "intfloat/multilingual-e5-large-instruct")
        self.assertNotIn("base_url", preset.embedding.model_kwargs)
        self.assertEqual(preset.embedding.model_kwargs["shared_base_url_env"], "RUNPOD_EMBEDDING_BASE_URL")
        self.assertEqual(preset.embedding.model_kwargs["shared_api_key_env"], "RUNPOD_API_KEY")
        self.assertEqual(preset.llm.model_type, "remote")
        self.assertEqual(preset.llm.model_name, "Qwen/Qwen2.5-7B-Instruct")
        self.assertNotIn("base_url", preset.llm.model_kwargs)
        self.assertEqual(preset.llm.model_kwargs["shared_base_url_env"], "RUNPOD_LLM_BASE_URL")
        # Same env var for both — a RunPod account has one stable API key used
        # by every endpoint it owns (unlike MPCDF's two independent Slurm jobs,
        # each with its own distinct generated key).
        self.assertEqual(preset.llm.model_kwargs["shared_api_key_env"], "RUNPOD_API_KEY")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_config.py -v -k runpod`
Expected: FAIL — `ValueError: Unknown preset 'runpod'` (file doesn't exist yet), and the bundled-names test also fails since `runpod.json` isn't on disk.

- [ ] **Step 3: Create the preset file**

Create `backend/config/default_presets/runpod.json`:

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

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_config.py -v -k runpod`
Expected: PASS (both `test_exactly_the_expected_bundled_preset_files_exist` picks up the new file, and `test_get_preset_runpod_uses_shared_dynamic_fields` passes)

Also run the full config test file to make sure nothing else broke:

Run: `uv run pytest backend/tests/test_config.py -v`
Expected: PASS (all tests, including `test_every_bundled_default_validates_against_hardware_preset` which now also validates `runpod.json`)

- [ ] **Step 5: Commit**

```bash
git add backend/config/default_presets/runpod.json backend/tests/test_config.py
git commit -m "$(cat <<'EOF'
feat(config): add runpod hardware preset

Fully-remote preset for self-hosted RunPod serverless endpoints
(multilingual-e5-large-instruct embedding + Qwen2.5-7B-Instruct LLM),
using the shared_base_url_env/shared_api_key_env pattern already built
for remote-mpcdf since the endpoint URLs only exist after
scripts/provision_runpod_endpoints.py creates them.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Script scaffold — constants and argument parsing

**Files:**
- Create: `scripts/provision_runpod_endpoints.py`
- Create: `backend/tests/test_provision_runpod_endpoints.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_provision_runpod_endpoints.py`:

```python
"""Unit tests for scripts/provision_runpod_endpoints.py."""

import importlib.util
import unittest
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "provision_runpod_endpoints.py"
_SPEC = importlib.util.spec_from_file_location("provision_runpod_endpoints_script", _SCRIPT_PATH)
provision = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(provision)


class ParseArgsTest(unittest.TestCase):
    def test_defaults(self):
        args = provision._parse_args([])
        self.assertIsNone(args.api_key)
        self.assertEqual(args.llm_model, "Qwen/Qwen2.5-7B-Instruct")
        self.assertEqual(args.embedding_gpu, "NVIDIA RTX A4000")
        self.assertEqual(args.llm_gpu, "NVIDIA RTX A5000")
        self.assertEqual(args.workers_max, 1)
        self.assertEqual(args.idle_timeout, 60)
        self.assertIsNone(args.data_centers)
        self.assertFalse(args.recreate)
        self.assertFalse(args.teardown)
        self.assertFalse(args.yes)
        self.assertFalse(args.skip_warmup)

    def test_overrides(self):
        args = provision._parse_args([
            "--api-key", "rp_test123",
            "--llm-model", "Qwen/Qwen2.5-14B-Instruct",
            "--embedding-gpu", "NVIDIA RTX 4000 Ada",
            "--llm-gpu", "NVIDIA RTX 4090",
            "--workers-max", "2",
            "--idle-timeout", "120",
            "--data-centers", "EU-RO-1,EU-SE-1",
            "--recreate",
            "--skip-warmup",
        ])
        self.assertEqual(args.api_key, "rp_test123")
        self.assertEqual(args.llm_model, "Qwen/Qwen2.5-14B-Instruct")
        self.assertEqual(args.embedding_gpu, "NVIDIA RTX 4000 Ada")
        self.assertEqual(args.llm_gpu, "NVIDIA RTX 4090")
        self.assertEqual(args.workers_max, 2)
        self.assertEqual(args.idle_timeout, 120)
        self.assertEqual(args.data_centers, "EU-RO-1,EU-SE-1")
        self.assertTrue(args.recreate)
        self.assertTrue(args.skip_warmup)

    def test_teardown_flag(self):
        args = provision._parse_args(["--teardown", "--yes"])
        self.assertTrue(args.teardown)
        self.assertTrue(args.yes)


class ResolveApiKeyTest(unittest.TestCase):
    def test_cli_flag_takes_priority_over_env(self):
        key = provision._resolve_api_key("rp_from_cli", env={"RUNPOD_API_KEY": "rp_from_env"})
        self.assertEqual(key, "rp_from_cli")

    def test_falls_back_to_env(self):
        key = provision._resolve_api_key(None, env={"RUNPOD_API_KEY": "rp_from_env"})
        self.assertEqual(key, "rp_from_env")

    def test_raises_when_neither_set(self):
        with self.assertRaises(provision.ProvisionError):
            provision._resolve_api_key(None, env={})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest backend/tests/test_provision_runpod_endpoints.py -v`
Expected: FAIL — `FileNotFoundError` / `ModuleNotFoundError` (script doesn't exist yet)

- [ ] **Step 3: Create the script with constants and argument parsing**

Create `scripts/provision_runpod_endpoints.py`:

```python
#!/usr/bin/env python3
"""
Provision (or wake) the two RunPod serverless endpoints used by the `runpod`
hardware preset: multilingual-e5-large-instruct embeddings
(runpod/worker-infinity-embedding) and a chat LLM (runpod/worker-vllm).

Re-running this script against already-provisioned endpoints is the normal
way to wake them from scale-to-zero before a work session — it finds each
endpoint by name, leaves a matching one alone, and sends a lightweight
warm-up request to trigger a cold start proactively.

Usage:
    uv run python scripts/provision_runpod_endpoints.py
    uv run python scripts/provision_runpod_endpoints.py --llm-model Qwen/Qwen2.5-14B-Instruct
    uv run python scripts/provision_runpod_endpoints.py --teardown

Requires RUNPOD_API_KEY in .env (or pass --api-key).
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

REST_BASE_URL = "https://rest.runpod.io/v1"

EMBEDDING_TEMPLATE_NAME = "zotero-rag-embedding"
EMBEDDING_ENDPOINT_NAME = "zotero-rag-embedding"
EMBEDDING_IMAGE = "runpod/worker-infinity-embedding:stable-cuda12.1.0"
EMBEDDING_MODEL = "intfloat/multilingual-e5-large-instruct"
EMBEDDING_CONTAINER_DISK_GB = 20

LLM_TEMPLATE_NAME = "zotero-rag-llm"
LLM_ENDPOINT_NAME = "zotero-rag-llm"
LLM_IMAGE = "runpod/worker-vllm:stable-cuda12.1.0"
LLM_CONTAINER_DISK_GB = 40

DEFAULT_LLM_MODEL = "Qwen/Qwen2.5-7B-Instruct"
DEFAULT_EMBEDDING_GPU = "NVIDIA RTX A4000"
DEFAULT_LLM_GPU = "NVIDIA RTX A5000"
DEFAULT_WORKERS_MAX = 1
DEFAULT_IDLE_TIMEOUT = 60

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

WARMUP_MAX_SECONDS = 180
WARMUP_RETRY_INTERVAL_SECONDS = 5


class ProvisionError(RuntimeError):
    """Raised when a RunPod API call fails in a way the script can't recover from."""


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provision or wake the RunPod embedding+LLM endpoints for the `runpod` preset."
    )
    parser.add_argument("--api-key", default=None, help="RunPod API key (default: RUNPOD_API_KEY from .env)")
    parser.add_argument(
        "--llm-model", default=DEFAULT_LLM_MODEL,
        help=f"HF repo id for the LLM endpoint (default: {DEFAULT_LLM_MODEL})",
    )
    parser.add_argument(
        "--embedding-gpu", default=DEFAULT_EMBEDDING_GPU,
        help=f"GPU type ID for the embedding endpoint (default: {DEFAULT_EMBEDDING_GPU}). "
             "Current valid IDs: GET https://rest.runpod.io/v1/gpuTypes",
    )
    parser.add_argument(
        "--llm-gpu", default=DEFAULT_LLM_GPU,
        help=f"GPU type ID for the LLM endpoint (default: {DEFAULT_LLM_GPU})",
    )
    parser.add_argument(
        "--workers-max", type=int, default=DEFAULT_WORKERS_MAX,
        help=f"Max workers per endpoint (default: {DEFAULT_WORKERS_MAX})",
    )
    parser.add_argument(
        "--idle-timeout", type=int, default=DEFAULT_IDLE_TIMEOUT,
        help=f"Seconds idle before scale-to-zero (default: {DEFAULT_IDLE_TIMEOUT})",
    )
    parser.add_argument(
        "--data-centers", default=None,
        help="Comma-separated RunPod datacenter IDs (default: unset, RunPod chooses)",
    )
    parser.add_argument(
        "--recreate", action="store_true",
        help="Delete and recreate an endpoint/template whose config differs from requested",
    )
    parser.add_argument(
        "--teardown", action="store_true",
        help="Delete both endpoints and templates by name",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the --teardown confirmation prompt (required when running non-interactively)",
    )
    parser.add_argument(
        "--skip-warmup", action="store_true",
        help="Create/verify only; don't send the warm-up request",
    )
    return parser.parse_args(argv)


def _resolve_api_key(cli_value: Optional[str], env: Optional[dict] = None) -> str:
    """Resolve the RunPod API key: --api-key flag, then RUNPOD_API_KEY env var."""
    if cli_value:
        return cli_value
    source = env if env is not None else os.environ
    key = source.get("RUNPOD_API_KEY")
    if not key:
        raise ProvisionError(
            "No RunPod API key found. Pass --api-key or set RUNPOD_API_KEY in .env."
        )
    return key


if __name__ == "__main__":
    args = _parse_args()
    try:
        api_key = _resolve_api_key(args.api_key)
    except ProvisionError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest backend/tests/test_provision_runpod_endpoints.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/provision_runpod_endpoints.py backend/tests/test_provision_runpod_endpoints.py
git commit -m "$(cat <<'EOF'
feat(scripts): scaffold provision_runpod_endpoints.py arg parsing

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: RunPod REST client core — request wrapper and find-by-name

**Files:**
- Modify: `scripts/provision_runpod_endpoints.py`
- Modify: `backend/tests/test_provision_runpod_endpoints.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_provision_runpod_endpoints.py` (new imports at top, then new test classes):

```python
from unittest.mock import MagicMock
```

```python
class FakeResponse:
    def __init__(self, status_code, json_body):
        self.status_code = status_code
        self._json_body = json_body
        self.text = str(json_body)

    def json(self):
        return self._json_body


class FakeClient:
    """Minimal stand-in for httpx.Client. `responses` is a list of (status_code,
    json_body) tuples, consumed in order, one per .request() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, headers, json))
        status_code, body = self._responses.pop(0)
        return FakeResponse(status_code, body)


class RequestTest(unittest.TestCase):
    def test_get_returns_parsed_json_on_200(self):
        client = FakeClient([(200, {"id": "abc123", "name": "zotero-rag-embedding"})])
        result = provision._request(client, "rp_key", "GET", "/templates")
        self.assertEqual(result, {"id": "abc123", "name": "zotero-rag-embedding"})
        method, url, headers, body = client.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://rest.runpod.io/v1/templates")
        self.assertEqual(headers["Authorization"], "Bearer rp_key")

    def test_raises_provision_error_on_non_2xx(self):
        client = FakeClient([(401, {"error": "invalid API key"})])
        with self.assertRaises(provision.ProvisionError) as ctx:
            provision._request(client, "rp_bad_key", "GET", "/templates")
        self.assertIn("401", str(ctx.exception))

    def test_post_sends_json_body(self):
        client = FakeClient([(200, {"id": "new123"})])
        result = provision._request(
            client, "rp_key", "POST", "/templates", json_body={"name": "zotero-rag-embedding"}
        )
        self.assertEqual(result, {"id": "new123"})
        _, _, _, body = client.calls[0]
        self.assertEqual(body, {"name": "zotero-rag-embedding"})


class FindByNameTest(unittest.TestCase):
    def test_finds_matching_item(self):
        client = FakeClient([
            (200, [{"id": "t1", "name": "other-template"}, {"id": "t2", "name": "zotero-rag-embedding"}]),
        ])
        result = provision._find_by_name(client, "rp_key", "templates", "zotero-rag-embedding")
        self.assertEqual(result["id"], "t2")

    def test_returns_none_when_not_found(self):
        client = FakeClient([(200, [{"id": "t1", "name": "other-template"}])])
        result = provision._find_by_name(client, "rp_key", "templates", "zotero-rag-embedding")
        self.assertIsNone(result)

    def test_returns_none_on_empty_list(self):
        client = FakeClient([(200, [])])
        result = provision._find_by_name(client, "rp_key", "endpoints", "zotero-rag-llm")
        self.assertIsNone(result)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_provision_runpod_endpoints.py -v -k "RequestTest or FindByNameTest"`
Expected: FAIL — `AttributeError: module ... has no attribute '_request'`

- [ ] **Step 3: Implement `_request` and `_find_by_name`**

Add to `scripts/provision_runpod_endpoints.py`, after the `ProvisionError` class and before `_parse_args`:

```python
import httpx


def _request(
    client: "httpx.Client",
    api_key: str,
    method: str,
    path: str,
    json_body: Optional[dict] = None,
) -> object:
    """Make an authenticated request against the RunPod REST API and return
    the parsed JSON body. Raises ProvisionError on a non-2xx response."""
    response = client.request(
        method,
        f"{REST_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=json_body,
        timeout=30.0,
    )
    if not (200 <= response.status_code < 300):
        raise ProvisionError(
            f"RunPod API {method} {path} failed: {response.status_code} {response.text}"
        )
    return response.json()


def _find_by_name(client: "httpx.Client", api_key: str, resource: str, name: str) -> Optional[dict]:
    """Find a RunPod resource (templates or endpoints) by its `name` field.
    Returns None if no item with that name exists."""
    items = _request(client, api_key, "GET", f"/{resource}")
    for item in items:
        if item.get("name") == name:
            return item
    return None
```

Move `import httpx` to the top of the file with the other imports (alongside `import argparse`, `import os`, etc.) rather than inline — this is shown inline above only to mark where it's newly needed in the diff.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_provision_runpod_endpoints.py -v`
Expected: PASS (all tests so far)

- [ ] **Step 5: Commit**

```bash
git add scripts/provision_runpod_endpoints.py backend/tests/test_provision_runpod_endpoints.py
git commit -m "$(cat <<'EOF'
feat(scripts): add RunPod REST request wrapper and find-by-name helper

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Ensure-template and ensure-endpoint (idempotent create-or-reuse-or-recreate)

**Files:**
- Modify: `scripts/provision_runpod_endpoints.py`
- Modify: `backend/tests/test_provision_runpod_endpoints.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_provision_runpod_endpoints.py`:

```python
class EnsureTemplateTest(unittest.TestCase):
    def test_creates_when_missing(self):
        client = FakeClient([
            (200, []),  # GET /templates -> not found
            (200, {"id": "t_new", "name": "zotero-rag-embedding", "imageName": "img:tag", "env": {"A": "1"}}),  # POST
        ])
        result = provision._ensure_template(
            client, "rp_key", name="zotero-rag-embedding", image="img:tag",
            env={"A": "1"}, container_disk_gb=20, recreate=False,
        )
        self.assertEqual(result["id"], "t_new")
        method, url, _, body = client.calls[1]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://rest.runpod.io/v1/templates")
        self.assertEqual(body["name"], "zotero-rag-embedding")
        self.assertEqual(body["imageName"], "img:tag")
        self.assertEqual(body["env"], {"A": "1"})
        self.assertTrue(body["isServerless"])

    def test_reuses_matching_existing_template(self):
        existing = {"id": "t_existing", "name": "zotero-rag-embedding", "imageName": "img:tag", "env": {"A": "1"}}
        client = FakeClient([(200, [existing])])  # GET /templates -> found
        result = provision._ensure_template(
            client, "rp_key", name="zotero-rag-embedding", image="img:tag",
            env={"A": "1"}, container_disk_gb=20, recreate=False,
        )
        self.assertEqual(result["id"], "t_existing")
        self.assertEqual(len(client.calls), 1)  # only the GET, no create

    def test_warns_but_keeps_existing_on_mismatch_without_recreate(self):
        existing = {"id": "t_existing", "name": "zotero-rag-embedding", "imageName": "img:OLD", "env": {"A": "1"}}
        client = FakeClient([(200, [existing])])
        with self.assertLogs(provision.logger, level="WARNING") as ctx:
            result = provision._ensure_template(
                client, "rp_key", name="zotero-rag-embedding", image="img:NEW",
                env={"A": "1"}, container_disk_gb=20, recreate=False,
            )
        self.assertEqual(result["id"], "t_existing")
        self.assertTrue(any("differs" in msg for msg in ctx.output))

    def test_recreates_on_mismatch_with_recreate_flag(self):
        existing = {"id": "t_old", "name": "zotero-rag-embedding", "imageName": "img:OLD", "env": {"A": "1"}}
        client = FakeClient([
            (200, [existing]),  # GET -> found, mismatched
            (200, {}),  # DELETE /templates/t_old
            (200, {"id": "t_new", "name": "zotero-rag-embedding", "imageName": "img:NEW", "env": {"A": "1"}}),  # POST
        ])
        result = provision._ensure_template(
            client, "rp_key", name="zotero-rag-embedding", image="img:NEW",
            env={"A": "1"}, container_disk_gb=20, recreate=True,
        )
        self.assertEqual(result["id"], "t_new")
        delete_call = client.calls[1]
        self.assertEqual(delete_call[0], "DELETE")
        self.assertEqual(delete_call[1], "https://rest.runpod.io/v1/templates/t_old")


class EnsureEndpointTest(unittest.TestCase):
    def test_creates_when_missing(self):
        client = FakeClient([
            (200, []),  # GET /endpoints -> not found
            (200, {"id": "e_new", "name": "zotero-rag-embedding"}),  # POST
        ])
        result = provision._ensure_endpoint(
            client, "rp_key", name="zotero-rag-embedding", template_id="t1",
            gpu_type_ids=["NVIDIA RTX A4000"], workers_max=1, idle_timeout=60,
            data_center_ids=None, recreate=False,
        )
        self.assertEqual(result["id"], "e_new")
        method, url, _, body = client.calls[1]
        self.assertEqual(method, "POST")
        self.assertEqual(body["templateId"], "t1")
        self.assertEqual(body["gpuTypeIds"], ["NVIDIA RTX A4000"])
        self.assertEqual(body["workersMin"], 0)
        self.assertEqual(body["workersMax"], 1)
        self.assertEqual(body["idleTimeout"], 60)
        self.assertNotIn("dataCenterIds", body)

    def test_includes_data_centers_when_given(self):
        client = FakeClient([(200, []), (200, {"id": "e_new", "name": "zotero-rag-llm"})])
        provision._ensure_endpoint(
            client, "rp_key", name="zotero-rag-llm", template_id="t1",
            gpu_type_ids=["NVIDIA RTX A5000"], workers_max=1, idle_timeout=60,
            data_center_ids=["EU-RO-1", "EU-SE-1"], recreate=False,
        )
        _, _, _, body = client.calls[1]
        self.assertEqual(body["dataCenterIds"], ["EU-RO-1", "EU-SE-1"])

    def test_reuses_existing_endpoint(self):
        existing = {"id": "e_existing", "name": "zotero-rag-embedding", "templateId": "t1"}
        client = FakeClient([(200, [existing])])
        result = provision._ensure_endpoint(
            client, "rp_key", name="zotero-rag-embedding", template_id="t1",
            gpu_type_ids=["NVIDIA RTX A4000"], workers_max=1, idle_timeout=60,
            data_center_ids=None, recreate=False,
        )
        self.assertEqual(result["id"], "e_existing")
        self.assertEqual(len(client.calls), 1)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_provision_runpod_endpoints.py -v -k "EnsureTemplateTest or EnsureEndpointTest"`
Expected: FAIL — `AttributeError: module ... has no attribute '_ensure_template'`

- [ ] **Step 3: Implement `_ensure_template` and `_ensure_endpoint`**

Add to `scripts/provision_runpod_endpoints.py` (add `import logging` to the top imports, and `logger = logging.getLogger(__name__)` near the other module-level constants):

```python
logger = logging.getLogger(__name__)


def _ensure_template(
    client: "httpx.Client",
    api_key: str,
    *,
    name: str,
    image: str,
    env: dict,
    container_disk_gb: int,
    recreate: bool,
) -> dict:
    """Find a template by name, or create it. If one exists with a different
    image/env and `recreate` is set, delete and recreate it; otherwise warn
    and keep using the existing one unchanged."""
    existing = _find_by_name(client, api_key, "templates", name)
    if existing is not None:
        mismatched = existing.get("imageName") != image or existing.get("env") != env
        if not mismatched:
            return existing
        if not recreate:
            logger.warning(
                "Template '%s' exists but its config differs from requested "
                "(image=%s vs %s). Keeping existing template — pass --recreate "
                "to replace it.",
                name, existing.get("imageName"), image,
            )
            return existing
        _request(client, api_key, "DELETE", f"/templates/{existing['id']}")

    return _request(
        client, api_key, "POST", "/templates",
        json_body={
            "name": name,
            "imageName": image,
            "env": env,
            "containerDiskInGb": container_disk_gb,
            "isServerless": True,
        },
    )


def _ensure_endpoint(
    client: "httpx.Client",
    api_key: str,
    *,
    name: str,
    template_id: str,
    gpu_type_ids: list,
    workers_max: int,
    idle_timeout: int,
    data_center_ids: Optional[list],
    recreate: bool,
) -> dict:
    """Find an endpoint by name, or create it referencing template_id. If one
    exists pointing at a different template and `recreate` is set, delete and
    recreate it; otherwise warn and keep using the existing one unchanged."""
    existing = _find_by_name(client, api_key, "endpoints", name)
    if existing is not None:
        mismatched = existing.get("templateId") != template_id
        if not mismatched:
            return existing
        if not recreate:
            logger.warning(
                "Endpoint '%s' exists but points at a different template "
                "(templateId=%s vs %s). Keeping existing endpoint — pass "
                "--recreate to replace it.",
                name, existing.get("templateId"), template_id,
            )
            return existing
        _request(client, api_key, "DELETE", f"/endpoints/{existing['id']}")

    body = {
        "name": name,
        "templateId": template_id,
        "gpuTypeIds": gpu_type_ids,
        "workersMin": 0,
        "workersMax": workers_max,
        "idleTimeout": idle_timeout,
    }
    if data_center_ids:
        body["dataCenterIds"] = data_center_ids
    return _request(client, api_key, "POST", "/endpoints", json_body=body)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_provision_runpod_endpoints.py -v`
Expected: PASS (all tests so far)

- [ ] **Step 5: Commit**

```bash
git add scripts/provision_runpod_endpoints.py backend/tests/test_provision_runpod_endpoints.py
git commit -m "$(cat <<'EOF'
feat(scripts): add idempotent ensure-template/ensure-endpoint logic

Re-running against an already-provisioned endpoint reuses it unchanged;
a config mismatch warns and keeps the existing resource unless --recreate
is passed.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Warm-up request logic

**Files:**
- Modify: `scripts/provision_runpod_endpoints.py`
- Modify: `backend/tests/test_provision_runpod_endpoints.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_provision_runpod_endpoints.py` (add `from unittest.mock import patch` to imports if not already present — it already is, from Task 2's module import, but double check and add if missing):

```python
class WarmUpTest(unittest.TestCase):
    def test_embedding_warmup_posts_to_openai_embeddings_path(self):
        client = FakeClient([(200, {"data": [{"embedding": [0.1, 0.2]}]})])
        provision._warm_up_embedding(client, "rp_key", "https://api.runpod.ai/v2/e1/openai/v1")
        method, url, headers, body = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.runpod.ai/v2/e1/openai/v1/embeddings")
        self.assertEqual(headers["Authorization"], "Bearer rp_key")
        self.assertEqual(body["input"], "ping")
        self.assertEqual(body["model"], provision.EMBEDDING_MODEL)

    def test_llm_warmup_posts_to_chat_completions_path(self):
        client = FakeClient([(200, {"choices": [{"message": {"content": "hi"}}]})])
        provision._warm_up_llm(client, "rp_key", "https://api.runpod.ai/v2/l1/openai/v1", "Qwen/Qwen2.5-7B-Instruct")
        method, url, headers, body = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.runpod.ai/v2/l1/openai/v1/chat/completions")
        self.assertEqual(body["model"], "Qwen/Qwen2.5-7B-Instruct")
        self.assertEqual(body["max_tokens"], 1)

    def test_retries_on_failure_then_succeeds(self):
        client = FakeClient([(503, {"error": "cold starting"}), (200, {"data": [{"embedding": [0.1]}]})])
        with patch.object(provision.time, "sleep"):
            provision._warm_up_embedding(client, "rp_key", "https://api.runpod.ai/v2/e1/openai/v1")
        self.assertEqual(len(client.calls), 2)

    def test_gives_up_after_max_seconds_and_warns(self):
        # Every call fails; the retry loop must stop instead of looping forever.
        responses = [(503, {"error": "cold starting"})] * 50
        client = FakeClient(responses)
        fake_times = iter([0, 10, 50, 100, 200])  # exceeds WARMUP_MAX_SECONDS=180 on the 5th check
        with patch.object(provision.time, "sleep"), \
             patch.object(provision.time, "monotonic", side_effect=lambda: next(fake_times)), \
             self.assertLogs(provision.logger, level="WARNING") as ctx:
            provision._warm_up_embedding(client, "rp_key", "https://api.runpod.ai/v2/e1/openai/v1")
        self.assertTrue(any("did not warm up" in msg for msg in ctx.output))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_provision_runpod_endpoints.py -v -k WarmUpTest`
Expected: FAIL — `AttributeError: module ... has no attribute '_warm_up_embedding'`

- [ ] **Step 3: Implement the warm-up functions**

Add `import time` to the top imports of `scripts/provision_runpod_endpoints.py`, then add after `_ensure_endpoint`:

```python
def _warm_up_embedding(client: "httpx.Client", api_key: str, base_url: str) -> None:
    """Send one lightweight embedding request to trigger a cold start,
    retrying with backoff while the worker spins up. Logs a warning (does not
    raise) if it never succeeds within WARMUP_MAX_SECONDS — the endpoint still
    exists and will warm up on the next real request regardless."""
    _warm_up_with_retry(
        client, api_key, f"{base_url}/embeddings",
        json_body={"model": EMBEDDING_MODEL, "input": "ping"},
    )


def _warm_up_llm(client: "httpx.Client", api_key: str, base_url: str, model: str) -> None:
    """Send one 1-token chat completion to trigger a cold start, same retry
    behavior as _warm_up_embedding."""
    _warm_up_with_retry(
        client, api_key, f"{base_url}/chat/completions",
        json_body={
            "model": model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        },
    )


def _warm_up_with_retry(client: "httpx.Client", api_key: str, url: str, json_body: dict) -> None:
    start = time.monotonic()
    while True:
        response = client.request(
            "POST", url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=json_body,
            timeout=30.0,
        )
        if 200 <= response.status_code < 300:
            return
        if time.monotonic() - start >= WARMUP_MAX_SECONDS:
            logger.warning(
                "Endpoint at %s did not warm up within %ds (last status: %s). "
                "It still exists and will warm up on the next real request.",
                url, WARMUP_MAX_SECONDS, response.status_code,
            )
            return
        time.sleep(WARMUP_RETRY_INTERVAL_SECONDS)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_provision_runpod_endpoints.py -v`
Expected: PASS (all tests so far)

- [ ] **Step 5: Commit**

```bash
git add scripts/provision_runpod_endpoints.py backend/tests/test_provision_runpod_endpoints.py
git commit -m "$(cat <<'EOF'
feat(scripts): add warm-up requests with retry/backoff

Sends one lightweight embedding/chat request per endpoint to trigger a
cold start proactively; gives up with a warning (not a failure) after
WARMUP_MAX_SECONDS since the endpoint still exists and warms on the
next real request regardless.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: `.env` update logic

**Files:**
- Modify: `scripts/provision_runpod_endpoints.py`
- Modify: `backend/tests/test_provision_runpod_endpoints.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_provision_runpod_endpoints.py` (add `import tempfile` and `from pathlib import Path` to imports if not already present — `Path` is already imported in the script module but the test file needs its own import):

```python
import tempfile


class UpdateEnvFileTest(unittest.TestCase):
    def test_creates_file_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            provision._update_env_file(env_path, {"RUNPOD_API_KEY": "rp_key"})
            content = env_path.read_text()
        self.assertIn("RUNPOD_API_KEY=rp_key\n", content)

    def test_appends_new_keys_to_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("EXISTING_VAR=value\n")
            provision._update_env_file(env_path, {"RUNPOD_API_KEY": "rp_key"})
            content = env_path.read_text()
        self.assertIn("EXISTING_VAR=value\n", content)
        self.assertIn("RUNPOD_API_KEY=rp_key\n", content)

    def test_replaces_existing_key_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("RUNPOD_API_KEY=old_key\nOTHER_VAR=keep_me\n")
            provision._update_env_file(env_path, {"RUNPOD_API_KEY": "new_key"})
            lines = env_path.read_text().splitlines()
        self.assertIn("RUNPOD_API_KEY=new_key", lines)
        self.assertIn("OTHER_VAR=keep_me", lines)
        self.assertNotIn("RUNPOD_API_KEY=old_key", lines)
        self.assertEqual(len(lines), 2)  # no duplicate line added

    def test_handles_file_without_trailing_newline(self):
        """Regression guard: a blind `>>` append onto a file with no trailing
        newline would concatenate onto the last line instead of starting a
        new one — see this project's documented worktree .env hazard."""
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("EXISTING_VAR=value")  # no trailing newline
            provision._update_env_file(env_path, {"RUNPOD_API_KEY": "rp_key"})
            lines = env_path.read_text().splitlines()
        self.assertIn("EXISTING_VAR=value", lines)
        self.assertIn("RUNPOD_API_KEY=rp_key", lines)
        self.assertEqual(len(lines), 2)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_provision_runpod_endpoints.py -v -k UpdateEnvFileTest`
Expected: FAIL — `AttributeError: module ... has no attribute '_update_env_file'`

- [ ] **Step 3: Implement `_update_env_file`**

Add to `scripts/provision_runpod_endpoints.py`, after the warm-up functions:

```python
def _update_env_file(env_path: Path, values: dict) -> None:
    """Insert or replace KEY=value lines in env_path, preserving every other
    line exactly. Never a blind `>>` append — reads the whole file (if it
    exists), replaces lines for keys already present, and appends lines for
    keys that aren't, so a source file missing a trailing newline can't get
    silently concatenated onto (see this project's documented worktree .env
    hazard in CLAUDE.md)."""
    lines = []
    if env_path.exists():
        content = env_path.read_text(encoding="utf-8")
        if content:
            lines = content.splitlines()

    remaining = dict(values)
    for i, line in enumerate(lines):
        if "=" not in line or line.strip().startswith("#"):
            continue
        key = line.split("=", 1)[0]
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"

    for key, value in remaining.items():
        lines.append(f"{key}={value}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_provision_runpod_endpoints.py -v`
Expected: PASS (all tests so far)

- [ ] **Step 5: Commit**

```bash
git add scripts/provision_runpod_endpoints.py backend/tests/test_provision_runpod_endpoints.py
git commit -m "$(cat <<'EOF'
feat(scripts): add safe .env update (insert-or-replace, no blind append)

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: `--teardown` flow

**Files:**
- Modify: `scripts/provision_runpod_endpoints.py`
- Modify: `backend/tests/test_provision_runpod_endpoints.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_provision_runpod_endpoints.py`:

```python
class TeardownTest(unittest.TestCase):
    def test_deletes_existing_endpoint_and_template(self):
        client = FakeClient([
            (200, [{"id": "e1", "name": "zotero-rag-embedding"}]),  # GET endpoints
            (200, {}),  # DELETE endpoint
            (200, [{"id": "t1", "name": "zotero-rag-embedding"}]),  # GET templates
            (200, {}),  # DELETE template
        ])
        provision._teardown_resource(client, "rp_key", name="zotero-rag-embedding")
        methods = [c[0] for c in client.calls]
        self.assertEqual(methods, ["GET", "DELETE", "GET", "DELETE"])
        self.assertEqual(client.calls[1][1], "https://rest.runpod.io/v1/endpoints/e1")
        self.assertEqual(client.calls[3][1], "https://rest.runpod.io/v1/templates/t1")

    def test_noop_when_nothing_exists(self):
        client = FakeClient([(200, []), (200, [])])  # GET endpoints, GET templates — both empty
        provision._teardown_resource(client, "rp_key", name="zotero-rag-embedding")
        methods = [c[0] for c in client.calls]
        self.assertEqual(methods, ["GET", "GET"])  # no DELETE calls made


class ConfirmTeardownTest(unittest.TestCase):
    def test_skips_prompt_when_yes_flag_set(self):
        # Should not touch stdin/input() at all when yes=True.
        with patch("builtins.input", side_effect=AssertionError("should not prompt")):
            result = provision._confirm_teardown(yes=True, interactive=True)
        self.assertTrue(result)

    def test_refuses_when_noninteractive_without_yes(self):
        result = provision._confirm_teardown(yes=False, interactive=False)
        self.assertFalse(result)

    def test_prompts_and_honors_yes_answer(self):
        with patch("builtins.input", return_value="y"):
            result = provision._confirm_teardown(yes=False, interactive=True)
        self.assertTrue(result)

    def test_prompts_and_honors_no_answer(self):
        with patch("builtins.input", return_value="n"):
            result = provision._confirm_teardown(yes=False, interactive=True)
        self.assertFalse(result)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_provision_runpod_endpoints.py -v -k "TeardownTest or ConfirmTeardownTest"`
Expected: FAIL — `AttributeError: module ... has no attribute '_teardown_resource'`

- [ ] **Step 3: Implement teardown functions**

Add to `scripts/provision_runpod_endpoints.py`, after `_update_env_file`:

```python
def _teardown_resource(client: "httpx.Client", api_key: str, *, name: str) -> None:
    """Delete the endpoint and template matching `name`, if they exist.
    A name that doesn't exist for either resource is a no-op, not an error —
    a previous partial teardown can be safely re-run."""
    endpoint = _find_by_name(client, api_key, "endpoints", name)
    if endpoint is not None:
        _request(client, api_key, "DELETE", f"/endpoints/{endpoint['id']}")
        logger.info("Deleted endpoint '%s' (%s)", name, endpoint["id"])

    template = _find_by_name(client, api_key, "templates", name)
    if template is not None:
        _request(client, api_key, "DELETE", f"/templates/{template['id']}")
        logger.info("Deleted template '%s' (%s)", name, template["id"])


def _confirm_teardown(*, yes: bool, interactive: bool) -> bool:
    """Returns True if teardown should proceed. --yes always proceeds without
    prompting. Without --yes, a non-interactive session (no tty) refuses
    rather than silently deleting infrastructure; an interactive session asks."""
    if yes:
        return True
    if not interactive:
        logger.error(
            "Refusing to tear down non-interactively without --yes. "
            "Pass --yes to confirm."
        )
        return False
    answer = input("Delete both RunPod endpoints and templates? [y/N] ").strip().lower()
    return answer == "y"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_provision_runpod_endpoints.py -v`
Expected: PASS (all tests so far)

- [ ] **Step 5: Commit**

```bash
git add scripts/provision_runpod_endpoints.py backend/tests/test_provision_runpod_endpoints.py
git commit -m "$(cat <<'EOF'
feat(scripts): add --teardown flow with interactive confirmation

Deletes both endpoints and templates by name; no-ops cleanly on an
already-absent resource so a partial teardown can be re-run. Refuses
to proceed non-interactively without --yes.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Main orchestration

**Files:**
- Modify: `scripts/provision_runpod_endpoints.py`
- Modify: `backend/tests/test_provision_runpod_endpoints.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_provision_runpod_endpoints.py`:

```python
class RunTest(unittest.TestCase):
    def test_teardown_path_calls_teardown_for_both_resources(self):
        client = FakeClient([
            (200, []), (200, []),  # embedding: GET endpoints, GET templates (nothing to delete)
            (200, []), (200, []),  # llm: GET endpoints, GET templates (nothing to delete)
        ])
        args = provision._parse_args(["--teardown", "--yes"])
        exit_code = provision._run(args, api_key="rp_key", client=client)
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(client.calls), 4)

    def test_teardown_path_aborts_without_confirmation(self):
        client = FakeClient([])
        args = provision._parse_args(["--teardown"])
        with patch("builtins.input", side_effect=AssertionError("should not prompt in this test")), \
             patch.object(provision.sys.stdin, "isatty", return_value=False):
            exit_code = provision._run(args, api_key="rp_key", client=client)
        self.assertEqual(exit_code, 1)
        self.assertEqual(len(client.calls), 0)  # nothing was even looked up

    def test_provision_path_creates_both_and_writes_env(self):
        client = FakeClient([
            (200, []),  # embedding: GET templates -> none
            (200, {"id": "t_emb", "name": provision.EMBEDDING_TEMPLATE_NAME,
                   "imageName": provision.EMBEDDING_IMAGE, "env": {"MODEL_NAMES": provision.EMBEDDING_MODEL}}),  # POST template
            (200, []),  # embedding: GET endpoints -> none
            (200, {"id": "e_emb", "name": provision.EMBEDDING_ENDPOINT_NAME}),  # POST endpoint
            (200, {"data": [{"embedding": [0.1]}]}),  # embedding warm-up
            (200, []),  # llm: GET templates -> none
            (200, {"id": "t_llm", "name": provision.LLM_TEMPLATE_NAME,
                   "imageName": provision.LLM_IMAGE, "env": {"MODEL_NAME": "Qwen/Qwen2.5-7B-Instruct"}}),  # POST template
            (200, []),  # llm: GET endpoints -> none
            (200, {"id": "e_llm", "name": provision.LLM_ENDPOINT_NAME}),  # POST endpoint
            (200, {"choices": [{"message": {"content": "hi"}}]}),  # llm warm-up
        ])
        args = provision._parse_args([])
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            with patch.object(provision, "ENV_PATH", env_path):
                exit_code = provision._run(args, api_key="rp_key", client=client)
            env_content = env_path.read_text()
        self.assertEqual(exit_code, 0)
        self.assertIn("RUNPOD_API_KEY=rp_key", env_content)
        self.assertIn("RUNPOD_EMBEDDING_BASE_URL=https://api.runpod.ai/v2/e_emb/openai/v1", env_content)
        self.assertIn("RUNPOD_LLM_BASE_URL=https://api.runpod.ai/v2/e_llm/openai/v1", env_content)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest backend/tests/test_provision_runpod_endpoints.py -v -k RunTest`
Expected: FAIL — `AttributeError: module ... has no attribute '_run'`

- [ ] **Step 3: Implement `_run` and wire up `__main__`**

Add `import sys` to the top imports if not already present (it is, from Task 2). Add to `scripts/provision_runpod_endpoints.py`, after `_confirm_teardown`:

```python
def _endpoint_base_url(endpoint_id: str) -> str:
    return f"https://api.runpod.ai/v2/{endpoint_id}/openai/v1"


def _run(args: argparse.Namespace, *, api_key: str, client: "httpx.Client") -> int:
    if args.teardown:
        if not _confirm_teardown(yes=args.yes, interactive=sys.stdin.isatty()):
            return 1
        _teardown_resource(client, api_key, name=EMBEDDING_ENDPOINT_NAME)
        _teardown_resource(client, api_key, name=LLM_ENDPOINT_NAME)
        print("Teardown complete.")
        return 0

    data_center_ids = args.data_centers.split(",") if args.data_centers else None

    embedding_template = _ensure_template(
        client, api_key,
        name=EMBEDDING_TEMPLATE_NAME, image=EMBEDDING_IMAGE,
        env={"MODEL_NAMES": EMBEDDING_MODEL},
        container_disk_gb=EMBEDDING_CONTAINER_DISK_GB, recreate=args.recreate,
    )
    embedding_endpoint = _ensure_endpoint(
        client, api_key,
        name=EMBEDDING_ENDPOINT_NAME, template_id=embedding_template["id"],
        gpu_type_ids=[args.embedding_gpu], workers_max=args.workers_max,
        idle_timeout=args.idle_timeout, data_center_ids=data_center_ids,
        recreate=args.recreate,
    )
    embedding_base_url = _endpoint_base_url(embedding_endpoint["id"])
    if not args.skip_warmup:
        _warm_up_embedding(client, api_key, embedding_base_url)
    print(f"Embedding endpoint ready: {embedding_base_url}")

    llm_template = _ensure_template(
        client, api_key,
        name=LLM_TEMPLATE_NAME, image=LLM_IMAGE,
        env={"MODEL_NAME": args.llm_model},
        container_disk_gb=LLM_CONTAINER_DISK_GB, recreate=args.recreate,
    )
    llm_endpoint = _ensure_endpoint(
        client, api_key,
        name=LLM_ENDPOINT_NAME, template_id=llm_template["id"],
        gpu_type_ids=[args.llm_gpu], workers_max=args.workers_max,
        idle_timeout=args.idle_timeout, data_center_ids=data_center_ids,
        recreate=args.recreate,
    )
    llm_base_url = _endpoint_base_url(llm_endpoint["id"])
    if not args.skip_warmup:
        _warm_up_llm(client, api_key, llm_base_url, args.llm_model)
    print(f"LLM endpoint ready: {llm_base_url}")

    _update_env_file(ENV_PATH, {
        "RUNPOD_API_KEY": api_key,
        "RUNPOD_EMBEDDING_BASE_URL": embedding_base_url,
        "RUNPOD_LLM_BASE_URL": llm_base_url,
    })
    print(f"\nWrote RUNPOD_API_KEY / RUNPOD_EMBEDDING_BASE_URL / RUNPOD_LLM_BASE_URL to {ENV_PATH}")
    print("\nTo apply these to an already-running backend without a restart:")
    print(f"""
curl -X POST https://<host>/api/config/remote-fields \\
  -H "X-Zotero-API-Key: <admin-key>" \\
  -H "Content-Type: application/json" \\
  -d '{{"values": {{"RUNPOD_EMBEDDING_BASE_URL": "{embedding_base_url}", "RUNPOD_LLM_BASE_URL": "{llm_base_url}", "RUNPOD_API_KEY": "{api_key}"}}}}'
""")
    return 0
```

Replace the existing `if __name__ == "__main__":` block at the bottom of the file with:

```python
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _parse_args()
    try:
        resolved_api_key = _resolve_api_key(args.api_key)
    except ProvisionError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        sys.exit(1)

    with httpx.Client() as http_client:
        try:
            exit_code = _run(args, api_key=resolved_api_key, client=http_client)
        except ProvisionError as exc:
            print(f"[FAIL] {exc}", file=sys.stderr)
            sys.exit(1)
    sys.exit(exit_code)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_provision_runpod_endpoints.py -v`
Expected: PASS (all tests — this is the full suite for the script)

- [ ] **Step 5: Commit**

```bash
git add scripts/provision_runpod_endpoints.py backend/tests/test_provision_runpod_endpoints.py
git commit -m "$(cat <<'EOF'
feat(scripts): wire up main orchestration for provision_runpod_endpoints.py

Ensures both templates+endpoints, warms them up, writes the resulting
base URLs to .env, and prints the POST /api/config/remote-fields
command to push them into an already-running backend without a restart.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Documentation

**Files:**
- Modify: `docs/presets.md`

- [ ] **Step 1: Add a row to the dependency table**

In `docs/presets.md`, find the table under "## Dependency overview" (around line 21-31) and add a row after the `remote-mpcdf` row:

```markdown
| `runpod` | **No** | `RUNPOD_API_KEY` (shared, admin-set — see below) | any |
```

- [ ] **Step 2: Add a new preset section**

Insert a new `### \`runpod\`` section directly after the `remote-mpcdf` section (after the `---` that follows line 217's "pasting the bare job URL works either way." paragraph, before `## Admin: runtime preset switching & shared remote config`):

```markdown
### `runpod` (self-hosted RunPod serverless endpoints)

**Best for:** Self-hosting embedding + LLM inference on pay-per-use, scale-to-zero GPU endpoints you control, as an alternative to depending on KISSKI/MPCDF

**Configuration:**

- Embedding: `intfloat/multilingual-e5-large-instruct` (RunPod remote, `runpod/worker-infinity-embedding`, 1024-dim)
- LLM: `Qwen/Qwen2.5-7B-Instruct` (RunPod remote, `runpod/worker-vllm`, 32k context)
- Memory: ~0.5 GB (fully remote)
- Top-k: 10 chunks / Max chunk: 800 tokens

**What's different about this preset:** unlike KISSKI's fixed shared gateway, these are two serverless endpoints you provision yourself with `scripts/provision_runpod_endpoints.py <api-key>`. The script is idempotent — re-running it finds existing endpoints by name and sends a lightweight warm-up request to wake them from scale-to-zero, rather than creating duplicates. Endpoint URLs are only known after provisioning, so (like `remote-mpcdf`) they're set at runtime through the admin API rather than being a fixed literal in the preset file — see "Admin: runtime preset switching & shared remote config" below.

**Advantages:**

- Full control over cost and data residency — no dependency on an external academic gateway's rate limits or availability.
- Pay only for active GPU-seconds; scales to zero between uses.
- No local GPU or large Python dependencies on the host running zotero-rag itself.

**Trade-offs:** Requires a RunPod account and the provisioning script to be run (and re-run to wake idle endpoints) before use; a cold start after idle time adds latency to the first request. Uses a smaller, self-hosted 7B LLM rather than KISSKI's 70B model — lower answer quality in exchange for independence from KISSKI. Not hot-swappable at runtime with `remote-kisski`/`remote-mpcdf` despite using the same underlying embedding model — this preset's `embedding.model_name` is the full HuggingFace repo id (`intfloat/multilingual-e5-large-instruct`, required by the RunPod worker image) rather than KISSKI's short served-model alias (`multilingual-e5-large-instruct`), so the runtime compatibility check (exact string match) doesn't recognize them as interchangeable even though the vectors are compatible — verify with `scripts/check_embedding_compat.py` before switching.

**Requires:** `RUNPOD_API_KEY`, `RUNPOD_EMBEDDING_BASE_URL`, `RUNPOD_LLM_BASE_URL` — set automatically in `.env` by `scripts/provision_runpod_endpoints.py`, or via `POST /api/config/remote-fields` (admin only, no restart) to apply to an already-running backend.

---
```

- [ ] **Step 3: Add to the Quick Selection Guide**

In the table under "## Quick Selection Guide" (around line 236-245), add a row:

```markdown
| Want full cost/data control, willing to self-host on RunPod | `runpod` |
```

- [ ] **Step 4: Add RunPod env vars to the Usage section**

In the `## Usage` section's bash block (around line 357-380), add after the MPCDF block:

```bash
# RunPod (runpod) — set automatically by scripts/provision_runpod_endpoints.py;
# shown here only for the env-var fallback path / manual editing.
RUNPOD_API_KEY=...
RUNPOD_EMBEDDING_BASE_URL=https://api.runpod.ai/v2/<embedding-endpoint-id>/openai/v1
RUNPOD_LLM_BASE_URL=https://api.runpod.ai/v2/<llm-endpoint-id>/openai/v1
```

- [ ] **Step 5: Verify the doc renders sensibly**

Run: `grep -c "^### \`" docs/presets.md`
Expected: `10` (9 existing presets + the new `runpod` section)

- [ ] **Step 6: Commit**

```bash
git add docs/presets.md
git commit -m "$(cat <<'EOF'
docs(presets): document the runpod preset

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Manual smoke test against the real RunPod API

**Not a subagent task** — this uses the real `RUNPOD_API_KEY` already present in this project's `.env` and incurs real (small) GPU cost, so it should be run directly, with care taken never to print the key's literal value to any log or terminal output per this project's security rules.

**Files:** none (verification only)

- [ ] **Step 1:** Run the full test suite one more time to confirm everything from Tasks 1-9 is green:

```bash
uv run pytest backend/tests/test_config.py backend/tests/test_provision_runpod_endpoints.py -v
```

Expected: all PASS.

- [ ] **Step 2:** Run the script for real (reads `RUNPOD_API_KEY` from `.env` automatically — do not pass it on the command line, which would expose it in `ps`/shell history per this project's security rules):

```bash
uv run python scripts/provision_runpod_endpoints.py
```

Expected: creates both templates and endpoints, prints both base URLs, warms both up (the LLM warm-up may take 1-3 minutes on first run while `runpod/worker-vllm` pulls `Qwen/Qwen2.5-7B-Instruct`'s weights), and confirms `.env` was updated.

- [ ] **Step 3:** Verify both endpoints actually work by making one real request to each — load the base URLs from `.env` (don't retype the key):

```bash
source .env
curl -s -X POST "$RUNPOD_EMBEDDING_BASE_URL/embeddings" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "intfloat/multilingual-e5-large-instruct", "input": "Zotero ist ein Literaturverwaltungsprogramm."}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('embedding dim:', len(d['data'][0]['embedding']))"
```

Expected output: `embedding dim: 1024`

```bash
curl -s -X POST "$RUNPOD_LLM_BASE_URL/chat/completions" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "Qwen/Qwen2.5-7B-Instruct", "messages": [{"role": "user", "content": "In one sentence, what is Zotero?"}], "max_tokens": 50}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['choices'][0]['message']['content'])"
```

Expected: a one-sentence, roughly-correct description of Zotero.

- [ ] **Step 4:** Verify idempotency — re-run the script and confirm it reuses both endpoints rather than creating duplicates:

```bash
uv run python scripts/provision_runpod_endpoints.py
```

Expected: no new templates/endpoints created (check the RunPod dashboard or `GET /v1/endpoints` shows exactly 2 endpoints named `zotero-rag-embedding`/`zotero-rag-llm`), both warm-up requests succeed quickly (workers may already be warm from Step 2/3, or cold-start again if the 60s idle timeout had already elapsed — either way, no error).

- [ ] **Step 5:** Decide whether to leave the endpoints provisioned (workersMin=0 means no cost while idle) or tear them down:

```bash
uv run python scripts/provision_runpod_endpoints.py --teardown
```

(Only run this if you don't want to keep them for ongoing use — report back to the user either way before deciding.)

- [ ] **Step 6:** Report results — which steps passed, the actual embedding dimension and LLM response text observed, whether idempotency held, and whether the endpoints were left running or torn down.

---

## Plan self-review notes

- **Spec coverage:** Task 1 covers the preset (§1 of the spec). Tasks 2-8 cover the script's full behavior: arg parsing, REST client, idempotent ensure-and-wake (§3), warm-up ("wake up" semantics), `.env` handoff (§3's config handoff), and `--teardown` (§3). Task 9 covers no spec section directly but is required by this project's own documentation conventions (every other preset is documented in `docs/presets.md`). Task 10 covers the spec's "Manual smoke-test checklist" testing requirement (§5).
- **Open item from the spec not resolved here:** confirming `runpod/worker-infinity-embedding`'s exact request/response shape against `RemoteEmbeddingService`'s expectations is exactly what Task 10's live smoke test validates — if the real API's response shape differs from what Task 10's `curl` commands assume (e.g. a different JSON key than `data[0].embedding`), that will surface immediately and should be fixed in `backend/services/embeddings.py` if the discrepancy is on the client side, or by adjusting the script's warm-up body if it's a request-shape issue — not a blind guess ahead of time.
- **GPU/datacenter ID drift**: flagged in the spec as an open item; Task 10's live run is also where a stale default `--embedding-gpu`/`--llm-gpu` value would surface (RunPod would reject the create call), at which point `GET https://rest.runpod.io/v1/gpuTypes` (mentioned in the script's own `--help` text) is the fix.
