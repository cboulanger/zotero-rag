"""Short-lived cache of endpoint URLs that a provider derives from an API key.

A provider whose endpoints live in the key owner's account (RunPod, Hugging
Face) answers "where is my endpoint?" with a management-API call. That call is
too slow and rate-limited to make per request, so answers are cached per
``(provider id, side, key fingerprint)``. A found URL is kept for ``TTL``
seconds, a miss only briefly so a fresh provisioning is picked up quickly.
Provisioning and connection failures call :func:`invalidate`.
"""

import threading
import time
from typing import Callable, Optional

from backend.services.usage_meters import key_fingerprint

FOUND_TTL_SECONDS = 300.0
MISSING_TTL_SECONDS = 15.0

_Key = tuple[str, str, Optional[str]]


class EndpointCache:
    """Thread-safe in-memory cache; the clock is injectable for tests."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[_Key, tuple[Optional[str], float]] = {}

    def resolve(self, provider, side: str, api_key: str) -> Optional[str]:
        """The endpoint URL for this key (cached), or None if it has none.

        Never raises: a provider failure counts as "no endpoint" for the short
        miss TTL.
        """
        key: _Key = (provider.id, side, key_fingerprint(api_key))
        now = self._clock()
        with self._lock:
            hit = self._entries.get(key)
            if hit and hit[1] > now:
                return hit[0]
        try:
            url = provider.endpoint_url(api_key)
        except Exception:
            url = None
        ttl = FOUND_TTL_SECONDS if url else MISSING_TTL_SECONDS
        with self._lock:
            self._entries[key] = (url, now + ttl)
        return url

    def invalidate(self, provider_id: str, side: str, api_key: Optional[str]) -> None:
        """Forget one entry (after provisioning, or when the endpoint stopped answering)."""
        with self._lock:
            self._entries.pop((provider_id, side, key_fingerprint(api_key)), None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


endpoint_cache = EndpointCache()
