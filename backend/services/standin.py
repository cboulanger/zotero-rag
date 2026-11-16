"""Stand-in presets: equivalent presets offered when a provider's quota is exhausted.

Free presets are offered before paid ones. This module holds the cost ranking
that the offer logic builds on.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from backend.config.presets import HardwarePreset, current_platform, get_preset, list_presets
from backend.providers import PROVIDERS, ProviderConfigError, discover_providers, get_providers
from backend.services.effective_preset import is_compatible

logger = logging.getLogger(__name__)

#: A quota is only worth a stand-in when it stays exhausted at least this long.
MIN_STANDIN_SECONDS = 900

#: Session states of a per-user login that mean "logged in".
_ACTIVE_SESSION_STATES = ("ok", "active")


@dataclass
class ExhaustedSide:
    """One side of the pipeline whose provider quota is used up."""

    side: str
    provider: str
    available_at: datetime


@dataclass
class StandIn:
    """A preset offered in place of the current one, with what it needs first."""

    preset: str
    cost: str
    label: str
    needs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready form for the API response."""
        return {"preset": self.preset, "cost": self.cost, "label": self.label, "needs": list(self.needs)}


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


def _sides(preset: HardwarePreset) -> list[tuple[str, Any]]:
    """The (name, side config) pairs of a preset's present sides."""
    sides = [("embedding", preset.embedding), ("llm", preset.llm)]
    if preset.rerank is not None:
        sides.append(("rerank", preset.rerank))
    return sides


def _label(preset: HardwarePreset) -> str:
    """The first sentence of the preset's description (its name when it has none)."""
    text = (preset.description or "").strip()
    if not text:
        return preset.name
    first = text.split(". ")[0].strip()
    return first.rstrip(".") if first else preset.name


def _needs(preset: HardwarePreset, providers: dict, client_keys: dict[str, str],
           user_id: Optional[int]) -> list[str]:
    """``login:<provider>`` needs first, then ``key:<NAME>`` needs."""
    logins: list[str] = []
    keys: list[str] = []
    for side, config in _sides(preset):
        if config.model_type != "remote":
            continue
        provider = providers.get(side)
        if provider is not None and provider.id not in logins:
            descriptor = provider.describe()
            if descriptor.provisioning and descriptor.provisioning.login:
                try:
                    status = provider.session_status(user_id)
                except Exception as exc:  # unknown session: ask for a login
                    logger.info("Session state of %s unavailable: %s", provider.id, type(exc).__name__)
                    status = None
                if not status or status.get("state") not in _ACTIVE_SESSION_STATES:
                    logins.append(provider.id)
        kwargs = config.model_kwargs or {}
        if kwargs.get("shared_api_key_env"):
            continue  # admin-shared: never needs a key from the caller
        name = kwargs.get("api_key_env")
        if name and not client_keys.get(name) and f"key:{name}" not in keys:
            keys.append(f"key:{name}")
    return [f"login:{p}" for p in logins] + keys


def find_standins(settings, current: HardwarePreset, exhausted: list[ExhaustedSide],
                  client_keys: dict[str, str], user_id: Optional[int]) -> list[StandIn]:
    """Presets that can replace ``current`` while the ``exhausted`` sides' quotas are used up.

    A candidate must differ from ``current``, allow stand-ins, run on this platform,
    be compatible with the server default (same vector store), and serve each
    exhausted side by another provider. Sorted free first, then by descending
    ``standin_priority``, then by name. Unusable presets are skipped.
    """
    data_path = settings.data_path
    default = settings.get_default_preset()
    found: list[tuple[HardwarePreset, StandIn]] = []
    for name in list_presets(data_path):
        if name == current.name:
            continue
        try:
            candidate = get_preset(name, data_path)
            providers = get_providers(candidate)
        except (ValueError, ProviderConfigError) as exc:
            logger.debug("Preset %r skipped as stand-in: %s", name, exc)
            continue
        if not candidate.standin or candidate.platform not in ("any", current_platform()):
            continue
        if not is_compatible(default, candidate):
            continue
        if any(providers.get(e.side) is not None and providers[e.side].id == e.provider for e in exhausted):
            continue
        found.append((candidate, StandIn(
            preset=candidate.name, cost=preset_cost(candidate), label=_label(candidate),
            needs=_needs(candidate, providers, client_keys, user_id))))
    found.sort(key=lambda pair: (pair[1].cost != "free", -pair[0].standin_priority, pair[0].name))
    return [standin for _, standin in found]


def current_exhausted_sides(settings, preset: HardwarePreset, client_keys: dict[str, str],
                            now: Optional[datetime] = None) -> list[ExhaustedSide]:
    """Remote sides of ``preset`` whose recorded rate limit lasts at least ``MIN_STANDIN_SECONDS`` more.

    The key per side mirrors the services' own resolution (the caller's key for
    ``api_key_env``, else the admin-shared value), so the fingerprint matches the
    one the rate limit was recorded under. Keys are never logged or returned.
    """
    from backend.services.admin_settings_store import resolve_shared_value
    from backend.services.usage_meters import key_fingerprint, recorder

    now = now or datetime.now(timezone.utc)
    result: list[ExhaustedSide] = []
    for side, config in (("embedding", preset.embedding), ("llm", preset.llm)):
        if config.model_type != "remote":
            continue
        kwargs = config.model_kwargs or {}
        key = client_keys.get(kwargs["api_key_env"]) if kwargs.get("api_key_env") else None
        if not key and kwargs.get("shared_api_key_env"):
            key = resolve_shared_value(kwargs["shared_api_key_env"], settings.data_path)
        until = recorder.rate_limited_until(side, key_fingerprint(key))
        if until is not None and (until - now).total_seconds() >= MIN_STANDIN_SECONDS:
            result.append(ExhaustedSide(side, config.provider.id, until))
    return result


def quota_error_detail(settings, preset: HardwarePreset, side: str, available_at: datetime,
                       message: str, client_keys: dict[str, str], user_id: Optional[int],
                       now: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """Structured 503 detail offering stand-ins, or None when the reset is under 15 minutes away.

    ``message`` is the fallback wording used when the provider's label is unknown.
    May block (provider session checks): call it off the event loop.
    """
    now = now or datetime.now(timezone.utc)
    if (available_at - now).total_seconds() < MIN_STANDIN_SECONDS:
        return None
    config = preset.embedding if side == "embedding" else preset.llm
    provider_id = config.provider.id
    discover_providers()
    provider_cls = PROVIDERS.get(provider_id)
    label = getattr(provider_cls, "label", None) or provider_id
    until = available_at.astimezone(timezone.utc)
    standins = find_standins(settings, preset, [ExhaustedSide(side, provider_id, available_at)],
                             client_keys, user_id)
    return {
        "code": "quota_exhausted",
        "message": f"{label}'s {side} quota is used up until {until:%H:%M} UTC." if provider_cls else message,
        "side": side,
        "provider": provider_id,
        "available_at": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "standins": [s.to_dict() for s in standins],
    }
