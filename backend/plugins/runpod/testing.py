"""Test support for the RunPod plugin, also used by core tests that exercise
provisioning and the ``managed`` scope through RunPod's fakes.

RunPod itself only allows the ``user`` scope (each user's own key; its terms do
not allow sharing an account). ``ManagedRunPodTestProvider`` is a test-only
subclass that allows ``managed``, so core's managed-scope paths (admin-only
operation, one global job slot, admin-set URL and key) can reuse these fakes.
Importing this module registers it.
"""

import json
from pathlib import Path

from backend.plugins.runpod.provider import RunPodProvider
from backend.providers import ProvisionContext, get_providers, register_provider
from backend.providers.testing import FakeHTTP, make_preset

MANAGED_NAME = "runpod-managed"
MANAGED_PROVIDER_ID = "demo-test-only-managed-runpod"


class ManagedRunPodTestProvider(RunPodProvider):
    """RunPod with the ``managed`` scope, for tests of the core managed-scope paths only."""

    id = MANAGED_PROVIDER_ID
    key_scopes = frozenset({"user", "managed"})


register_provider(ManagedRunPodTestProvider)


EMB_URL_ENV, LLM_URL_ENV = "RUNPOD_EMBEDDING_BASE_URL", "RUNPOD_LLM_BASE_URL"


def runpod_preset(options=None, scope="managed"):
    """A runpod preset. ``user``: the user's own key, URLs derived from it. ``managed``:
    the shared-field shape on the test-only managed subclass (RunPod itself is user-only)."""
    preset = make_preset()
    for side, url_env in ((preset.embedding, EMB_URL_ENV), (preset.llm, LLM_URL_ENV)):
        side.provider.id = "runpod" if scope == "user" else MANAGED_PROVIDER_ID
        side.provider.scope = scope
        side.provider.options = options or {}
        side.model_kwargs = (
            {"api_key_env": "RUNPOD_API_KEY"} if scope == "user"
            else {"shared_api_key_env": "RUNPOD_API_KEY", "shared_base_url_env": url_env}
        )
    preset.embedding.model_name = "intfloat/multilingual-e5-large-instruct"
    preset.llm.model_names = ["Qwen/Qwen2.5-7B-Instruct"]
    return preset


def provider(side="llm", options=None, http=None, scope="managed"):
    p = get_providers(runpod_preset(options, scope))[side]
    p._http_client = http or FakeHTTP()
    return p


def ctx(side="llm", **kw):
    return ProvisionContext(side=side, preset=runpod_preset(), credential=kw.pop("credential", "rpa_key"),
                            data_path=Path("."), **kw)


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
