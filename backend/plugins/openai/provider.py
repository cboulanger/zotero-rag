"""OpenAI provider: the OpenAI API with a per-user (or institution-managed) key.

It adds only the key metadata to the generic OpenAI-compatible behaviour: the
default key name and where to create a key. The OpenAI embedding sentinel
``model_name: "openai"`` and the known embedding dimensions stay model data in
core, keyed by model name rather than by provider.
"""

from typing import Optional

from backend.providers.base import Provider, match_pricing
from backend.providers.types import Pricing


def _p(input_usd: float, output_usd: float = 0.0) -> Pricing:
    return Pricing(input_per_million_tokens=input_usd, output_per_million_tokens=output_usd)


#: Standard-tier list prices in USD per million tokens (platform.openai.com/docs/pricing).
#: More specific patterns first. A preset's ``model_kwargs.pricing`` overrides these.
PRICES: list[tuple[str, Pricing]] = [
    ("text-embedding-3-small", _p(0.02)),
    ("text-embedding-3-large", _p(0.13)),
    ("text-embedding-ada-002", _p(0.10)),
    ("gpt-4o-mini*", _p(0.15, 0.60)),
    ("gpt-4o*", _p(2.50, 10.00)),
    ("gpt-4.1-nano*", _p(0.10, 0.40)),
    ("gpt-4.1-mini*", _p(0.40, 1.60)),
    ("gpt-4.1*", _p(2.00, 8.00)),
    ("gpt-5-nano*", _p(0.05, 0.40)),
    ("gpt-5-mini*", _p(0.25, 2.00)),
    ("gpt-5", _p(1.25, 10.00)),
    ("gpt-5-2025*", _p(1.25, 10.00)),
]


class OpenAIProvider(Provider):
    id = "openai"
    cost = "paid"
    label = "OpenAI"
    key_scopes = frozenset({"user", "managed"})
    default_scope = "user"

    def default_key_env(self) -> Optional[str]:
        return "OPENAI_API_KEY"

    def key_docs_url(self, env_var: str) -> Optional[str]:
        if env_var == "OPENAI_API_KEY":
            return "https://platform.openai.com/api-keys"
        return super().key_docs_url(env_var)

    def default_pricing(self, model: str) -> Optional[Pricing]:
        # The embedding sentinel ``model_name: "openai"`` stands for text-embedding-3-small.
        return match_pricing(PRICES, "text-embedding-3-small" if model == "openai" else model)
