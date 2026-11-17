"""Anthropic provider: Claude models over the Anthropic Messages API.

LLM side only (Anthropic has no embeddings API), so it is meant to be combined
with another provider's embedding side. It selects the Anthropic wire protocol
through ``llm_api``; core no longer guesses the vendor from the model name.
"""

from typing import Optional

from backend.providers.base import Provider, match_pricing
from backend.providers.types import Pricing


def _p(input_usd: float, output_usd: float) -> Pricing:
    return Pricing(input_per_million_tokens=input_usd, output_per_million_tokens=output_usd)


#: List prices in USD per million tokens (docs.anthropic.com pricing), without prompt
#: caching or batch discounts. A preset's ``model_kwargs.pricing`` overrides these.
PRICES: list[tuple[str, Pricing]] = [
    ("claude-haiku-4-5*", _p(1.00, 5.00)),
    ("claude-3-5-haiku*", _p(0.80, 4.00)),
    ("claude-3-haiku*", _p(0.25, 1.25)),
    ("claude-sonnet-4*", _p(3.00, 15.00)),
    ("claude-3-7-sonnet*", _p(3.00, 15.00)),
    ("claude-opus-4-5*", _p(5.00, 25.00)),
    ("claude-opus-4-1*", _p(15.00, 75.00)),
    ("claude-opus-4-2025*", _p(15.00, 75.00)),
    ("claude-opus-4-0*", _p(15.00, 75.00)),
]


class AnthropicProvider(Provider):
    id = "anthropic"
    cost = "paid"
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

    def default_pricing(self, model: str) -> Optional[Pricing]:
        return match_pricing(PRICES, model)
