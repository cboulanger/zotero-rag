"""Stand-in presets: equivalent presets offered when a provider's quota is exhausted.

Free presets are offered before paid ones. This module holds the cost ranking
that the offer logic builds on.
"""

from typing import Literal

from backend.config.presets import HardwarePreset
from backend.providers import PROVIDERS, discover_providers


def preset_cost(preset: HardwarePreset) -> Literal["free", "paid"]:
    """Return a preset's cost class.

    An explicit ``preset.cost`` wins. Otherwise the preset is free when every
    present side (embedding, llm, rerank) is local or served by a free provider.
    Extraction is not part of the cost.
    """
    if preset.cost is not None:
        return preset.cost
    discover_providers()
    sides = [preset.embedding, preset.llm]
    if preset.rerank is not None:
        sides.append(preset.rerank)
    for side in sides:
        if side.model_type == "local":
            continue
        provider_cls = PROVIDERS.get(side.provider.id)
        if provider_cls is None or provider_cls.cost != "free":
            return "paid"
    return "free"
