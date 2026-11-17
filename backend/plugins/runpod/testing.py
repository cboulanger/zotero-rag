"""Test support for the RunPod plugin's own tests.

RunPod itself only allows the ``user`` scope (each user's own key; its terms do
not allow sharing an account). ``ManagedRunPodTestProvider`` is a test-only
subclass that allows ``managed``, so the provider's managed-shape code (admin-set
URL and key, their patterns) stays covered. Importing this module registers it.
"""

from pathlib import Path

from backend.plugins.runpod.provider import RunPodProvider
from backend.providers import ProvisionContext, get_providers, register_provider
from backend.providers.testing import FakeHTTP, make_preset

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
