"""Test helper: an admin-operated ("managed") copy of the bundled runpod preset.

RunPod itself only allows the ``user`` scope (each user's own key; its terms do
not allow sharing an account). The core behaviour of the ``managed`` scope (admin-only
operation, one global job slot, admin-set URL and key) still exists for providers
such as ``google`` and ``huggingface``; these tests exercise it through a test-only
RunPod subclass that allows ``managed``, so the RunPod fakes can be reused.
"""

import json
from pathlib import Path

from backend.providers.runpod import RunPodProvider

MANAGED_NAME = "runpod-managed"
MANAGED_PROVIDER_ID = "demo-test-only-managed-runpod"


class ManagedRunPodTestProvider(RunPodProvider):
    """RunPod with the ``managed`` scope, for tests of the core managed-scope paths only."""

    id = MANAGED_PROVIDER_ID
    key_scopes = frozenset({"user", "managed"})


def write_managed_runpod_preset(data_path: Path) -> str:
    """Write ``<data_path>/presets/runpod-managed.json`` and return its name."""
    from backend.config.presets import ensure_default_presets

    ensure_default_presets(data_path)
    preset_dir = data_path / "presets"
    data = json.loads((preset_dir / "runpod.json").read_text())
    data.pop("rerank", None)  # a re-ranking side is always per user; these tests cover the managed sides
    for side, url_env in (("embedding", "RUNPOD_EMBEDDING_BASE_URL"), ("llm", "RUNPOD_LLM_BASE_URL")):
        data[side]["model_kwargs"] = {"shared_base_url_env": url_env, "shared_api_key_env": "RUNPOD_API_KEY"}
        data[side]["provider"]["scope"] = "managed"
        data[side]["provider"]["id"] = MANAGED_PROVIDER_ID
    (preset_dir / f"{MANAGED_NAME}.json").write_text(json.dumps(data, indent=2))
    return MANAGED_NAME
