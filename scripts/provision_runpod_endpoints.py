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
