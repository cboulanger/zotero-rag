"""Provider layer: the interface of the classes that serve a preset's remote sides.

Core code talks only to ``Provider`` (see ``base.py``), which is also the generic
OpenAI-compatible provider. Vendor providers live in backend plugins
(``backend/plugins/`` and ``BACKEND_PLUGIN_DIRS``) and are selected per side by id
in preset JSON. See docs/backend-plugins.md and
docs/superpowers/specs/2026-10-10-huggingface-preset-and-provisioner-adapters-design.md.
"""

from backend.providers.base import PROVIDERS, GenericProvider, Provider, register_provider
from backend.providers.registry import (
    apply_provider_defaults,
    discover_providers,
    get_provider,
    get_provider_or_none,
    get_providers,
    provider_config_error,
    supported_sides,
)
from backend.providers.types import (
    OPTIONAL_SIDES,
    SIDES,
    Cost,
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
    Pricing,
    ProvisionTimeout,
    Scope,
    Side,
    Usage,
    preset_sides,
    side_config,
)

__all__ = [
    "PROVIDERS",
    "OPTIONAL_SIDES",
    "SIDES",
    "preset_sides",
    "side_config",
    "Cost",
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
    "Pricing",
    "ProvisionTimeout",
    "Scope",
    "Side",
    "Usage",
    "apply_provider_defaults",
    "discover_providers",
    "get_provider",
    "get_provider_or_none",
    "get_providers",
    "provider_config_error",
    "register_provider",
    "supported_sides",
]
