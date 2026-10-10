"""Provider discovery, lookup and the load-time validation of a preset's providers.

Importing this package imports every sibling module, and each ``Provider``
subclass registers itself by its ``id`` (see ``Provider.__init_subclass__``),
so adding a provider is adding a module - there is no central list to edit.
"""

import importlib
import logging
import pkgutil
from typing import Optional

from backend.providers.base import PROVIDERS, Provider
from backend.providers.types import SIDES, ProviderConfigError, Side

logger = logging.getLogger(__name__)

#: Modules of this package that are infrastructure, not providers.
_NON_PROVIDER_MODULES = frozenset({"base", "registry", "types", "usage"})

_discovered = False


def discover_providers() -> dict[str, type[Provider]]:
    """Import every provider module once and return the registry."""
    global _discovered
    if not _discovered:
        package = importlib.import_module("backend.providers")
        for info in pkgutil.iter_modules(package.__path__):
            if info.name.startswith("_") or info.name in _NON_PROVIDER_MODULES:
                continue
            importlib.import_module(f"backend.providers.{info.name}")
        _discovered = True
    return PROVIDERS


def _side_config(preset, side: Side):
    return preset.embedding if side == "embedding" else preset.llm


def _key_env_names(side_config) -> set[str]:
    kwargs = side_config.model_kwargs or {}
    return {kwargs[k] for k in ("api_key_env", "shared_api_key_env") if kwargs.get(k)}


def get_providers(preset) -> dict[Side, Provider]:
    """One validated provider instance per side of ``preset``.

    Raises:
        ProviderConfigError: the preset breaks a load-time rule (unknown
            provider id, invalid options, a side or scope the provider does not
            allow, a provider on a local side, one key shared by two providers).
            The message names the rule and the side. Callers treat such a preset
            as unavailable; it never has to fail backend startup.
    """
    registry = discover_providers()
    providers: dict[Side, Provider] = {}
    for side in SIDES:
        cfg = _side_config(preset, side)
        provider_cfg = cfg.provider
        cls = registry.get(provider_cfg.id)
        if cls is None:
            raise ProviderConfigError(
                f"{side}: unknown provider id {provider_cfg.id!r} (available: {', '.join(sorted(registry))})"
            )
        if cfg.model_type == "local" and provider_cfg.id != "generic":
            raise ProviderConfigError(
                f"{side}: provider {provider_cfg.id!r} cannot serve a local model "
                "(a local side needs no provider; remove the provider block)"
            )
        if side not in cls.supported_sides:
            raise ProviderConfigError(
                f"{side}: provider {provider_cfg.id!r} does not support the {side} side "
                f"(supports: {', '.join(sorted(cls.supported_sides))})"
            )
        scope = provider_cfg.scope or cls.default_scope
        if scope not in cls.key_scopes:
            raise ProviderConfigError(
                f"{side}: provider {provider_cfg.id!r} does not allow scope {scope!r} "
                f"(allowed: {', '.join(sorted(cls.key_scopes))})"
            )
        try:
            options = cls.Options.model_validate(provider_cfg.options)
        except Exception as exc:
            raise ProviderConfigError(f"{side}: invalid options for provider {provider_cfg.id!r}: {exc}") from exc
        providers[side] = cls(side, preset, options, scope)

    # One header carries one vendor's key: sides sharing a key env var must share a provider.
    shared = _key_env_names(preset.embedding) & _key_env_names(preset.llm)
    for env in sorted(shared):
        if preset.embedding.provider.id != preset.llm.provider.id:
            raise ProviderConfigError(
                f"key {env!r} is used by both sides but they use different providers "
                f"({preset.embedding.provider.id!r} and {preset.llm.provider.id!r})"
            )
    return providers


def get_provider(preset, side: Side) -> Provider:
    """The validated provider for one side of ``preset``."""
    return get_providers(preset)[side]


def apply_provider_defaults(preset) -> None:
    """Run each side's ``apply_defaults()`` on ``preset`` (mutates its ``model_kwargs``).

    A preset whose providers fail validation is left untouched; the validation
    error is reported where the preset is listed or activated.
    """
    try:
        providers = get_providers(preset)
    except ProviderConfigError as exc:
        logger.debug("Skipping provider defaults for %s: %s", getattr(preset, "name", "?"), exc)
        return
    for provider in providers.values():
        provider.apply_defaults()


def provider_config_error(preset) -> Optional[str]:
    """The validation message for ``preset``'s providers, or None when valid."""
    try:
        get_providers(preset)
    except ProviderConfigError as exc:
        return str(exc)
    return None
