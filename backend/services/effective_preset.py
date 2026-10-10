"""Which preset a request runs on: the user's own choice, else the server default.

The server has one *default* preset (stored by an admin, else ``MODEL_PRESET``,
else ``remote-kisski``). A signed-in user may choose another one, stored by
:mod:`backend.services.user_settings`. A choice is honoured only while it is
compatible with the default (see :func:`is_compatible`); a removed, incompatible
or invalid one silently falls back to the default. A request without an identity
(loopback, public query) always runs on the default.

The auth middleware calls :func:`get_user_preset` once per request and, when the
user runs on their own preset, binds it with :func:`use_preset`; ``Settings.get_hardware_preset()`` then returns
it for the rest of the request (including worker threads and tasks it starts),
so deep services need no extra plumbing. Outside a request (cron, CLI) it
returns the default.
"""

import logging
from contextlib import contextmanager
from typing import Iterator, Optional

from backend.config import settings as settings_module
from backend.config.presets import HardwarePreset, current_platform, get_preset
from backend.providers import ProviderConfigError, get_providers
from backend.services.user_settings import get_preferred_preset

logger = logging.getLogger(__name__)


def embedding_model_identity(model_name: str) -> str:
    """Normalize an embedding model name for cross-preset compatibility comparison:
    the basename after any "org/" prefix.

    Different providers serve the same underlying model under different literal
    API model-name strings (KISSKI/MPCDF "multilingual-e5-large-instruct", RunPod's
    vLLM worker the full HuggingFace id "intfloat/multilingual-e5-large-instruct").
    Comparing basenames treats them as one model without changing either preset's
    on-the-wire name.
    """
    return model_name.rsplit("/", 1)[-1]


def is_compatible(default: HardwarePreset, candidate: HardwarePreset) -> bool:
    """True if a user on ``candidate`` can share the server's vector store with ``default``.

    Both presets must be fully remote (no local model to load or unload per user)
    and use the same embedding model, i.e. the same vector space. If the default is
    not fully remote, only the default itself qualifies.
    """
    if candidate.name == default.name:
        return True
    if default.embedding.model_type != "remote" or default.llm.model_type != "remote":
        return False
    return (
        candidate.embedding.model_type == "remote"
        and candidate.llm.model_type == "remote"
        and embedding_model_identity(candidate.embedding.model_name)
        == embedding_model_identity(default.embedding.model_name)
    )


def get_user_preset(settings, identity) -> Optional[HardwarePreset]:
    """The user's own valid, compatible preset, or None when they run on the default."""
    if identity is None:
        return None
    default = settings.get_default_preset()
    chosen = get_preferred_preset(settings.data_path, identity.user_id)
    if not chosen or chosen == default.name:
        return None
    try:
        candidate = get_preset(chosen, settings.data_path)
        get_providers(candidate)
    except (ValueError, ProviderConfigError) as exc:
        logger.warning("User %s's preset %r is unavailable (%s); using the default", identity.user_id, chosen, exc)
        return None
    if candidate.platform not in ("any", current_platform()):
        return None
    if not is_compatible(default, candidate):
        logger.warning("User %s's preset %r is not compatible with the default %r; using the default",
                       identity.user_id, chosen, default.name)
        return None
    return candidate


def get_effective_preset(settings, identity) -> HardwarePreset:
    """The preset for this identity (see the module docstring)."""
    return get_user_preset(settings, identity) or settings.get_default_preset()


def preset_for_target(settings, target: dict) -> HardwarePreset:
    """The preset an auto-index target runs on (its owner's, recorded by the resolver)."""
    name = target.get("preset_name")
    if name:
        try:
            return get_preset(name, settings.data_path)
        except ValueError:
            logger.warning("Preset %r of an auto-index target is gone; using the default", name)
    return settings.get_hardware_preset()  # outside a request this is the server default


@contextmanager
def use_preset(preset: Optional[HardwarePreset]) -> Iterator[None]:
    """Bind ``preset`` as this request's preset (``None`` leaves the default)."""
    token = settings_module._request_preset.set(preset)
    try:
        yield
    finally:
        settings_module._request_preset.reset(token)
