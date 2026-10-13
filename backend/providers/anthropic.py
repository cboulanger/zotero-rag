"""Anthropic provider: Claude models over the Anthropic Messages API.

LLM side only (Anthropic has no embeddings API), so it is meant to be combined
with another provider's embedding side. It selects the Anthropic wire protocol
through ``llm_api``; core no longer guesses the vendor from the model name.
"""

from typing import Optional

from backend.providers.base import Provider


class AnthropicProvider(Provider):
    id = "anthropic"
    label = "Anthropic"
    supported_sides = frozenset({"llm"})
    key_scopes = frozenset({"user", "managed"})
    default_scope = "user"
    llm_api = "anthropic"

    def default_key_env(self) -> Optional[str]:
        return "ANTHROPIC_API_KEY"

    def key_docs_url(self, env_var: str) -> Optional[str]:
        if env_var == "ANTHROPIC_API_KEY":
            return "https://console.anthropic.com/settings/keys"
        return super().key_docs_url(env_var)
