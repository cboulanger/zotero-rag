"""Process-wide record of the latest rate-limit headers per side and API key.

Providers turn raw headers into display meters (``Provider.parse_usage``);
this module only remembers what the last responses said, keyed by side and a
fingerprint of the API key, so one user's quota is never shown to another.
Meters are quota only: no billing figures.

It also remembers, per the same ``(side, fingerprint)`` key, the cooldown a
429 response has already told us about (``mark_rate_limited`` /
``rate_limited_until``). A remote service's own backoff loop (see
``RemoteEmbeddingService._create_embeddings_with_backoff`` and
``RemoteLLMService._generate_with_rate_limit_backoff``) checks this *before*
issuing a request, so once one call has been rate-limited, every other call
on the same provider+key — a different question, a different agent, a
concurrent request — waits out or respects the same cooldown instead of
rediscovering the limit by hammering the endpoint again itself.
"""

import hashlib
import threading
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from backend.providers import ProviderConfigError, get_providers

_PREFIXES = ("x-ratelimit", "ratelimit")

# Header names (lowercase, in priority order) that a 429 response may use to
# say how long to wait before retrying. Shared by every remote provider's
# backoff loop so the dialect is recognised consistently everywhere.
_RETRY_AFTER_HEADERS = ("retry-after", "ratelimit-reset", "x-ratelimit-reset-minute")


def key_fingerprint(key: Optional[str]) -> Optional[str]:
    """Short, non-reversible identifier of an API key (None for no key)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12] if key else None


def extract_rate_limit_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """The rate-limit subset of a response's headers (any dialect)."""
    return {k: v for k, v in headers.items() if k.lower().startswith(_PREFIXES)}


def extract_retry_after(headers: Optional[Mapping[str, str]]) -> Optional[float]:
    """Seconds to wait before retrying a 429, from whichever known header is present.

    Checks ``retry-after``, ``ratelimit-reset``, then ``x-ratelimit-reset-minute``
    (case-insensitive); returns None when none is present or parseable.
    """
    if not headers:
        return None
    h = {str(k).lower(): v for k, v in headers.items()}
    for header in _RETRY_AFTER_HEADERS:
        val = h.get(header)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return None


class UsageRecorder:
    """Latest rate-limit headers, and any known cooldown, per ``(side, key fingerprint)``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[tuple[str, Optional[str]], tuple[dict[str, str], str]] = {}
        self._rate_limited_until: dict[tuple[str, Optional[str]], datetime] = {}

    def record(self, side: str, headers: Mapping[str, str], fingerprint: Optional[str] = None) -> None:
        """Remember rate-limit headers seen on a response (ignored when there are none)."""
        extracted = extract_rate_limit_headers(headers)
        if not extracted:
            return
        with self._lock:
            self._latest[(side, fingerprint)] = (extracted, datetime.now(timezone.utc).isoformat())

    def latest(self, side: str, fingerprint: Optional[str] = None) -> tuple[Optional[dict[str, str]], Optional[str]]:
        """``(headers, captured_at)`` for this key; with no fingerprint, the newest for the side."""
        with self._lock:
            if fingerprint is not None:
                found = self._latest.get((side, fingerprint))
            else:
                candidates = [v for (s, _), v in self._latest.items() if s == side]
                found = max(candidates, key=lambda v: v[1], default=None)
        return (found[0], found[1]) if found else (None, None)

    def mark_rate_limited(self, side: str, available_at: datetime, fingerprint: Optional[str] = None) -> None:
        """Remember that this (side, key) is rate-limited until ``available_at`` (UTC)."""
        with self._lock:
            self._rate_limited_until[(side, fingerprint)] = available_at

    def rate_limited_until(self, side: str, fingerprint: Optional[str] = None) -> Optional[datetime]:
        """Last-known cooldown end for this (side, key), or None if not rate-limited."""
        with self._lock:
            return self._rate_limited_until.get((side, fingerprint))

    def clear_rate_limit(self, side: str, fingerprint: Optional[str] = None) -> None:
        """Forget a (side, key)'s cooldown (it has elapsed, or a request just succeeded)."""
        with self._lock:
            self._rate_limited_until.pop((side, fingerprint), None)

    def reset(self) -> None:
        """Forget everything (the active preset changed: the numbers belong to the old provider)."""
        with self._lock:
            self._latest.clear()
            self._rate_limited_until.clear()


recorder = UsageRecorder()


class RateLimitStillActive(Exception):
    """A remembered cooldown (see ``mark_rate_limited``) has not elapsed yet.

    Raised only when the remaining wait exceeds the caller's own short-wait
    budget; the caller catches this and raises its own typed exception
    (``EmbeddingRateLimitExhaustedError`` / ``LLMRateLimitExhaustedError``)
    with the same ``available_at``, so callers keep their own exception type
    while sharing this one cooldown-checking implementation.
    """

    def __init__(self, available_at: datetime):
        super().__init__(f"Rate limit cooldown active until {available_at.isoformat()} UTC")
        self.available_at = available_at


async def wait_out_known_rate_limit(
    side: str, fingerprint: Optional[str], max_wait: float
) -> None:
    """Honor a cooldown an earlier 429 already recorded for this (side, key).

    A short remaining wait (<= ``max_wait``) is slept through here so the
    caller's own retry loop proceeds as if nothing had been remembered. A
    longer one raises ``RateLimitStillActive`` immediately, without the
    caller ever issuing the request, so a cooldown discovered by one call
    (any question, any request) is respected by every other call on the same
    provider+key instead of each one re-discovering it by hammering the
    endpoint again itself.
    """
    import asyncio

    until = recorder.rate_limited_until(side, fingerprint)
    if until is None:
        return
    remaining = (until - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        recorder.clear_rate_limit(side, fingerprint)
        return
    if remaining > max_wait:
        raise RateLimitStillActive(until)
    await asyncio.sleep(remaining)
    recorder.clear_rate_limit(side, fingerprint)


def meters_from_headers(
    preset: Any, side: str, headers: Optional[Mapping[str, str]], *, as_of: Optional[str] = None, source: str = "run"
) -> list[dict]:
    """Display meters (JSON-ready dicts) for headers seen on ``side`` of ``preset``.

    Never raises: an unusable preset or unrecognised headers yield ``[]``.
    """
    if not headers:
        return []
    try:
        provider = get_providers(preset)[side]
        return [m.model_dump() for m in provider.parse_usage(headers, as_of=as_of, source=source)]
    except (ProviderConfigError, KeyError, ValueError):
        return []
