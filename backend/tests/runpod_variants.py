"""Test helper: the admin-operated ("managed") variant of the bundled runpod preset.

The bundled ``runpod`` preset uses the ``user`` credential scope (each user's own
key, URLs derived from it). Institutions that fund a shared RunPod account copy it
to a new file with scope ``managed`` and the admin-set shared fields; this builds
that copy for tests of the admin-operated behaviour.
"""

import json
from pathlib import Path

MANAGED_NAME = "runpod-managed"


def write_managed_runpod_preset(data_path: Path) -> str:
    """Write ``<data_path>/presets/runpod-managed.json`` and return its name."""
    from backend.config.presets import ensure_default_presets

    ensure_default_presets(data_path)
    preset_dir = data_path / "presets"
    data = json.loads((preset_dir / "runpod.json").read_text())
    for side, url_env in (("embedding", "RUNPOD_EMBEDDING_BASE_URL"), ("llm", "RUNPOD_LLM_BASE_URL")):
        data[side]["model_kwargs"] = {"shared_base_url_env": url_env, "shared_api_key_env": "RUNPOD_API_KEY"}
        data[side]["provider"]["scope"] = "managed"
    (preset_dir / f"{MANAGED_NAME}.json").write_text(json.dumps(data, indent=2))
    return MANAGED_NAME
