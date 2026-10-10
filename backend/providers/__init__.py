"""Provider layer: one class per vendor, selected per side by id in preset JSON.

Core code talks only to ``Provider`` (see ``base.py``); everything vendor-specific
lives in a module of this package. See
docs/superpowers/specs/2026-10-10-huggingface-preset-and-provisioner-adapters-design.md.
"""

from backend.providers.base import PROVIDERS, GenericProvider, Provider
from backend.providers.registry import (
    apply_provider_defaults,
    discover_providers,
    get_provider,
    get_provider_or_none,
    get_providers,
    provider_config_error,
)
from backend.providers.types import (
    SIDES,
    Credentials,
    Health,
    Meter,
    ModelInfo,
    NoOptions,
    ProviderConfigError,
    ProviderDescriptor,
    ProviderOptions,
    ProvisionContext,
    ProvisionError,
    ProvisionTimeout,
    Scope,
    Side,
)

__all__ = [
    "PROVIDERS",
    "SIDES",
    "Credentials",
    "GenericProvider",
    "Health",
    "Meter",
    "ModelInfo",
    "NoOptions",
    "Provider",
    "ProviderConfigError",
    "ProviderDescriptor",
    "ProviderOptions",
    "ProvisionContext",
    "ProvisionError",
    "ProvisionTimeout",
    "Scope",
    "Side",
    "apply_provider_defaults",
    "discover_providers",
    "get_provider",
    "get_provider_or_none",
    "get_providers",
    "provider_config_error",
]
