"""Core test helpers around the test-only plugin in ``backend/tests/fake_plugins``.

The plugin host imports that plugin itself (as ``zotero_rag_plugins.testvendor``),
so tests reach its provider classes through the registry, never by importing the
module: a second import would create other classes, and ``patch.object`` on them
would not reach the providers the code under test uses.
"""

import json
from pathlib import Path

from backend.providers import PROVIDERS, ProvisionContext, discover_providers, get_providers
from backend.providers.testing import FakeHTTP, make_preset

#: Presets of the fake plugin, by the vendor shape they stand in for.
FREE = "test-remote"                  # free per-user-key service for both sides
FREE_OTHER_LLM = "test-remote-other-llm"  # same embeddings, answers from a paid service
FREE_RERANK = "test-remote-rerank"    # FREE plus re-ranking in a login session
SESSION = "test-session"              # every side in the user's own login session
CLOUD = "test-cloud"                  # paid, provisioned endpoints on the user's key
OTHER_EMBEDDING = "test-other-embedding"  # a different embedding model (incompatible)
#: Written by :func:`write_managed_cloud_preset`.
CLOUD_MANAGED = "test-cloud-managed"

CLOUD_KEY_ENV = "TESTCLOUD_API_KEY"
CLOUD_EMB_URL_ENV, CLOUD_LLM_URL_ENV = "TESTCLOUD_EMBEDDING_BASE_URL", "TESTCLOUD_LLM_BASE_URL"


def provider_class(provider_id: str):
    """The registered class of a provider (fake or real)."""
    discover_providers()
    return PROVIDERS[provider_id]


def vendor_cls():
    return provider_class("testvendor")


def other_cls():
    return provider_class("testother")


def messages_cls():
    return provider_class("testmessages")


def session_cls():
    return provider_class("testsession")


def cloud_cls():
    return provider_class("testcloud")


def reset_fake_state() -> None:
    """Clear the fake providers' class-level state."""
    cloud_cls().paused.clear()
    cloud_cls().provisioned.clear()
    session_cls().sessions.clear()
    session_cls().running.clear()
    vendor_cls().models = ["test-llm-large", "test-llm-small"]


def write_managed_cloud_preset(data_path: Path) -> str:
    """Write ``<data_path>/presets/test-cloud-managed.json`` (the cloud preset with
    admin-managed URLs and key, no re-ranking side) and return its name."""
    from backend.config.presets import ensure_default_presets

    ensure_default_presets(data_path)
    preset_dir = data_path / "presets"
    data = json.loads((preset_dir / f"{CLOUD}.json").read_text())
    data.pop("rerank", None)  # a re-ranking side is always per user
    for side, url_env in (("embedding", CLOUD_EMB_URL_ENV), ("llm", CLOUD_LLM_URL_ENV)):
        data[side]["model_kwargs"] = {"shared_base_url_env": url_env, "shared_api_key_env": CLOUD_KEY_ENV}
        data[side]["provider"]["scope"] = "managed"
    (preset_dir / f"{CLOUD_MANAGED}.json").write_text(json.dumps(data, indent=2))
    return CLOUD_MANAGED


def cloud_preset(options=None, scope="managed"):
    """An in-memory cloud preset. ``user``: the user's own key, URLs derived from it.
    ``managed``: admin-set URL and key."""
    preset = make_preset()
    for side, url_env in ((preset.embedding, CLOUD_EMB_URL_ENV), (preset.llm, CLOUD_LLM_URL_ENV)):
        side.provider.id = "testcloud"
        side.provider.scope = scope
        side.provider.options = options or {}
        side.model_kwargs = (
            {"api_key_env": CLOUD_KEY_ENV} if scope == "user"
            else {"shared_api_key_env": CLOUD_KEY_ENV, "shared_base_url_env": url_env}
        )
    preset.embedding.model_name = "intfloat/multilingual-e5-large-instruct"
    preset.llm.model_names = ["Qwen/Qwen2.5-7B-Instruct"]
    return preset


def cloud_provider(side="llm", http=None, scope="managed"):
    """A cloud provider instance for ``side``, with a fake HTTP client."""
    p = get_providers(cloud_preset(scope=scope))[side]
    p._http_client = http or FakeHTTP()
    return p


def cloud_ctx(side="llm", **kw):
    return ProvisionContext(side=side, preset=cloud_preset(), credential=kw.pop("credential", "tck_key"),
                            data_path=Path("."), **kw)
