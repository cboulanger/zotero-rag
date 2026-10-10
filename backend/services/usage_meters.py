"""Process-wide record of the latest rate-limit headers per side and API key.

Providers turn raw headers into display meters (``Provider.parse_usage``);
this module only remembers what the last responses said, keyed by side and a
fingerprint of the API key, so one user's quota is never shown to another.
Meters are quota only: no billing figures.
"""

import hashlib
import threading
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from backend.providers import ProviderConfigError, get_providers

_PREFIXES = ("x-ratelimit", "ratelimit")


def key_fingerprint(key: Optional[str]) -> Optional[str]:
    """Short, non-reversible identifier of an API key (None for no key)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12] if key else None


def extract_rate_limit_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """The rate-limit subset of a response's headers (any dialect)."""
    return {k: v for k, v in headers.items() if k.lower().startswith(_PREFIXES)}


class UsageRecorder:
    """Latest rate-limit headers per ``(side, key fingerprint)``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: dict[tuple[str, Optional[str]], tuple[dict[str, str], str]] = {}

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

    def reset(self) -> None:
        """Forget everything (the active preset changed: the numbers belong to the old provider)."""
        with self._lock:
            self._latest.clear()


recorder = UsageRecorder()


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
