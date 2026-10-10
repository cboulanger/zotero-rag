"""Vendor-neutral wording for "the remote endpoint is not ready" errors.

The core raises ``Embedding/LLMEndpointUnavailableError`` with plain text; the
side's provider contributes what it knows (``classify_http_error`` tells a
paused endpoint from a cold one, ``unavailable_hint`` says how to fix it).
Everything here is best effort and never raises: without a usable provider
the message is just the core text.
"""

import logging
from typing import Any, Literal, Optional

from backend.providers import get_providers

logger = logging.getLogger(__name__)

Kind = Literal["cold", "paused"]

_STATE_TEXT = {
    "paused": "The endpoint is paused; resume it from Preferences.",
    "cold": "The endpoint is starting up (cold); retry in a few minutes.",
}


def _provider(side: str) -> Optional[Any]:
    try:
        from backend.config.settings import get_settings

        return get_providers(get_settings().get_hardware_preset())[side]
    except Exception as exc:
        logger.debug("No provider available for %s error hints: %s", side, exc)
        return None


def classify_status_error(side: str, exc: Exception) -> Optional[Kind]:
    """What an HTTP error from the endpoint means (``cold``/``paused``), if its provider knows."""
    status = getattr(exc, "status_code", None)
    if status is None:
        return None
    provider = _provider(side)
    if provider is None:
        return None
    body = getattr(exc, "body", None)
    text = body if isinstance(body, str) else str(body) if body is not None else ""
    try:
        return provider.classify_http_error(int(status), f"{text} {exc}")
    except Exception:
        return None


def unavailable_message(side: str, base: str, kind: Optional[Kind] = None) -> str:
    """``base`` plus the endpoint state and the provider's remedy, when known."""
    parts = [base.rstrip()]
    if kind:
        parts.append(_STATE_TEXT[kind])
    provider = _provider(side)
    hint = provider.unavailable_hint() if provider is not None else None
    if hint:
        parts.append(hint)
    return " ".join(parts)
