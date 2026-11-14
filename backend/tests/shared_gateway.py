"""Test helper: a preset whose endpoint URL and key are set by an admin (scope ``shared``).

No bundled preset uses admin-set shared fields (MPCDF and RunPod forbid sharing an
account), but the core still supports them for any OpenAI-compatible gateway an
institution runs for its users. These tests exercise that path with the generic
provider and the same embedding model as ``remote-kisski`` (so it is compatible).
"""

import json
from pathlib import Path

GATEWAY_NAME = "shared-gateway"

GATEWAY_FIELDS = {
    "embedding": {"shared_base_url_env": "GATEWAY_EMBEDDING_BASE_URL", "shared_api_key_env": "GATEWAY_EMBEDDING_API_KEY"},
    "llm": {"shared_base_url_env": "GATEWAY_LLM_BASE_URL", "shared_api_key_env": "GATEWAY_LLM_API_KEY"},
}


def write_shared_gateway_preset(data_path: Path) -> str:
    """Write ``<data_path>/presets/shared-gateway.json`` and return its name."""
    from backend.config.presets import ensure_default_presets

    ensure_default_presets(data_path)
    preset = {
        "version": 2,
        "description": "Test-only: an admin-run OpenAI-compatible gateway shared by all users.",
        "embedding": {
            "model_type": "remote", "model_name": "multilingual-e5-large-instruct", "batch_size": 64,
            "provider": {"id": "generic", "scope": "shared"}, "model_kwargs": dict(GATEWAY_FIELDS["embedding"]),
        },
        "llm": {
            "model_type": "remote", "model_names": ["gateway-llm"], "max_context_length": 32768,
            "max_answer_tokens": 2048, "temperature": 0.7,
            "provider": {"id": "generic", "scope": "shared"}, "model_kwargs": dict(GATEWAY_FIELDS["llm"]),
        },
        "rag": {"top_k": 10, "score_threshold": 0.35, "max_chunk_size": 800},
        "memory_budget_gb": 0.5,
        "platform": "any",
    }
    (Path(data_path) / "presets" / f"{GATEWAY_NAME}.json").write_text(json.dumps(preset, indent=2))
    return GATEWAY_NAME
