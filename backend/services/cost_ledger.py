"""What the calls to paid providers consumed while handling one request or run.

The embedding, answering and re-ranking services report what each remote call
consumed (``record_usage``); the side's provider turns it into a ``Cost`` record
(``Provider.estimate_cost``: provider, model, tokens, seconds and the cost at
list prices) and it is added to the ledger of the surrounding request
(``collect``). Free providers' calls are not recorded. The plugin prices the
records with the user's billing settings, since only the user knows whether a
service bills them per token, per second or by subscription. Nothing is recorded outside a ``collect``
block, and nothing here ever raises into an inference call: costs are a
display-only signal.

The ledger lives in a context variable, so concurrent requests never see each
other's costs. Tasks and threads started inside a ``collect`` block share its
ledger (``asyncio`` and ``asyncio.to_thread`` copy the context, and the copy
refers to the same ledger object).
"""

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Optional

from backend.providers import Cost, Usage

logger = logging.getLogger(__name__)


class CostLedger:
    """Running totals of what paid providers' calls consumed, per side, provider and model."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._totals: dict[tuple[str, str, str, str], Cost] = {}

    def add(self, cost: Cost) -> None:
        """Add one call to the totals of its side, provider, model and currency."""
        with self._lock:
            key = (cost.side, cost.provider, cost.model, cost.currency)
            total = self._totals.get(key)
            if total is None:
                self._totals[key] = cost.model_copy()
                return
            if cost.amount is not None:
                total.amount = (total.amount or 0.0) + cost.amount
            total.requests += cost.requests
            total.input_tokens += cost.input_tokens
            total.output_tokens += cost.output_tokens
            total.seconds += cost.seconds
            if cost.note and cost.note not in total.note:
                total.note = f"{total.note} {cost.note}".strip()

    def costs(self) -> list[Cost]:
        """One entry per side, provider, model and currency, in the order first used."""
        with self._lock:
            return [c.model_copy() for c in self._totals.values()]

    def as_dicts(self) -> list[dict]:
        """JSON-ready ``costs`` (empty when no paid provider was called)."""
        return [c.model_dump() for c in self.costs()]


_current: ContextVar[Optional[CostLedger]] = ContextVar("cost_ledger", default=None)


@contextmanager
def collect(ledger: Optional[CostLedger] = None) -> Iterator[CostLedger]:
    """Collect the costs of every call made inside the block into ``ledger`` (or a new one).

    A block nested in another one also adds its costs to the outer ledger when it
    ends, so a run that calls a request handler (the auto-indexer draining uploads)
    still sees every cost.
    """
    ledger = ledger if ledger is not None else CostLedger()
    outer = _current.get()
    token = _current.set(ledger)
    try:
        yield ledger
    finally:
        _current.reset(token)
        if outer is not None and outer is not ledger:
            for cost in ledger.costs():
                outer.add(cost)


def current_ledger() -> Optional[CostLedger]:
    """The ledger of the surrounding ``collect`` block, if any."""
    return _current.get()


def record_usage(provider: Any, usage: Usage) -> Optional[Cost]:
    """Price ``usage`` with the side's ``provider`` and add it to the current ledger.

    Without a provider, the active preset's provider of ``usage.side`` is used.
    Returns the cost, or None when nothing is collecting, the provider is free or
    the model has no known price. Never raises.
    """
    ledger = _current.get()
    if ledger is None:
        return None
    try:
        if provider is None:
            from backend.config.settings import get_settings
            from backend.providers import get_providers

            provider = get_providers(get_settings().get_hardware_preset()).get(usage.side)
        cost = provider.estimate_cost(usage) if provider is not None else None
    except Exception:  # costs are display-only; never break an inference call
        logger.debug("Could not estimate the cost of a %s call", usage.side, exc_info=True)
        return None
    if cost is not None:
        ledger.add(cost)
    return cost


def paid_providers(preset: Any) -> list[dict]:
    """The paid providers of ``preset``'s remote sides: ``{id, label, sides}`` each (never raises)."""
    try:
        from backend.providers import get_providers

        providers = get_providers(preset)
    except Exception:
        return []
    found: dict[str, dict] = {}
    for side, provider in providers.items():
        try:
            if provider.cost == "free" or provider.side_config.model_type != "remote":
                continue
        except Exception:
            continue
        entry = found.setdefault(provider.id, {"id": provider.id, "label": provider.label, "sides": []})
        entry["sides"].append(side)
    return list(found.values())


def token_counts(usage_obj: Any) -> tuple[int, int]:
    """``(input_tokens, output_tokens)`` from an OpenAI or Anthropic ``usage`` object (0 when absent)."""
    if usage_obj is None:
        return 0, 0

    def _get(name: str) -> int:
        value = usage_obj.get(name) if isinstance(usage_obj, dict) else getattr(usage_obj, name, None)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    input_tokens = _get("prompt_tokens") or _get("input_tokens")
    output_tokens = _get("completion_tokens") or _get("output_tokens")
    return input_tokens, output_tokens
