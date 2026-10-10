"""OpenAI provider: the OpenAI API with a per-user (or institution-managed) key.

It adds only the key metadata to the generic OpenAI-compatible behaviour: the
default key name and where to create a key. The OpenAI embedding sentinel
``model_name: "openai"`` and the known embedding dimensions stay model data in
core, keyed by model name rather than by provider.
"""

from typing import Optional

from backend.providers.base import Provider


class OpenAIProvider(Provider):
    id = "openai"
    label = "OpenAI"
    key_scopes = frozenset({"user", "managed"})
    default_scope = "user"

    def default_key_env(self) -> Optional[str]:
        return "OPENAI_API_KEY"

    def key_docs_url(self, env_var: str) -> Optional[str]:
        if env_var == "OPENAI_API_KEY":
            return "https://platform.openai.com/api-keys"
        return super().key_docs_url(env_var)
