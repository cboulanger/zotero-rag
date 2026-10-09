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
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

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

logger = logging.getLogger(__name__)


class ProvisionError(RuntimeError):
    """Raised for any unrecoverable failure while provisioning or tearing down
    the RunPod endpoints (a missing credential, a failed API call, etc.)."""


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
            # Deleting an in-use template is destructive and costs real money
            # to respin; default to a loud warning, not silent destruction.
            diffs = []
            if existing.get("imageName") != image:
                diffs.append(f"image={existing.get('imageName')!r} vs {image!r}")
            if existing.get("env") != env:
                diffs.append(f"env={existing.get('env')!r} vs {env!r}")
            logger.warning(
                "Template '%s' exists but its config differs from requested (%s). "
                "Keeping existing template — pass --recreate to replace it.",
                name, "; ".join(diffs),
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
            # Deleting an in-use endpoint is destructive and costs real money
            # to respin; default to a loud warning, not silent destruction.
            logger.warning(
                "Endpoint '%s' exists but its config differs from requested "
                "(template_id=%r vs %r). Keeping existing endpoint — pass "
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


def _warm_up_embedding(client: "httpx.Client", api_key: str, base_url: str) -> None:
    """Send one lightweight embedding request to trigger a cold start,
    retrying at a fixed interval while the worker spins up. Logs a warning
    (does not raise) if it never succeeds within WARMUP_MAX_SECONDS — the
    endpoint still exists and will warm up on the next real request
    regardless."""
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
    """Retry the warm-up request at a fixed interval until it succeeds or
    WARMUP_MAX_SECONDS elapses. Never raises: a persistently failing warm-up
    is logged as a warning, since the endpoint still exists and will warm up
    on the next real request regardless. A connection-level failure (e.g. a
    cold-starting worker not yet accepting connections) is treated the same
    as a non-2xx response. A non-retryable 4xx (anything but 429, which can
    mean rate-limited-but-fine) short-circuits immediately instead of
    burning the full time budget on an error retrying can't fix."""
    start = time.monotonic()
    while True:
        try:
            response = client.request(
                "POST", url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=json_body,
                timeout=30.0,
            )
        except httpx.TransportError as exc:
            status_desc = f"connection error ({exc})"
            non_retryable = False
        else:
            if 200 <= response.status_code < 300:
                return
            status_desc = f"{response.status_code} {response.text[:200]}"
            non_retryable = 400 <= response.status_code < 500 and response.status_code != 429

        if non_retryable:
            logger.warning(
                "Endpoint at %s returned a non-retryable error: %s. "
                "It still exists and will warm up on the next real request.",
                url, status_desc,
            )
            return

        if time.monotonic() - start >= WARMUP_MAX_SECONDS:
            logger.warning(
                "Endpoint at %s did not warm up within %ds (last status: %s). "
                "It still exists and will warm up on the next real request.",
                url, WARMUP_MAX_SECONDS, status_desc,
            )
            return
        time.sleep(WARMUP_RETRY_INTERVAL_SECONDS)


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

    new_content = "\n".join(lines) + "\n"
    fd, tmp_path = tempfile.mkstemp(dir=env_path.parent, suffix=".tmp", prefix=".env_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp_path, env_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


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


def _confirm_teardown(*, resource_names: list, yes: bool, interactive: bool) -> bool:
    """Returns True if teardown should proceed. --yes always proceeds without
    prompting. Without --yes, a non-interactive session (no tty) refuses
    rather than silently deleting infrastructure; an interactive session is
    told exactly which named resources will be deleted and asked to confirm."""
    if yes:
        return True
    if not interactive:
        logger.error(
            "Refusing to tear down non-interactively without --yes. "
            "Pass --yes to confirm."
        )
        return False
    names = ", ".join(resource_names)
    answer = input(f"Delete these RunPod endpoints and templates: {names}? [y/N] ").strip().lower()
    return answer == "y"


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provision or wake the RunPod embedding+LLM endpoints for the `runpod` preset."
    )
    parser.add_argument(
        "--api-key", default=None,
        help="RunPod API key (default: RUNPOD_API_KEY from .env)",
    )
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
    load_dotenv(ENV_PATH)
    args = _parse_args()
    try:
        api_key = _resolve_api_key(args.api_key)
    except ProvisionError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        sys.exit(1)
