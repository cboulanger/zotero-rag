"""Zotero-key identity resolution and caching (Part 1.3 of the design).

A Zotero API key resolves to a stable identity — (user_id, username, targets)
— via backend.zotero.key_validator.validate_key(), which calls
api.zotero.org. That call is too slow and too fragile to make on every
request, so ZoteroIdentityCache caches the result per key fingerprint with a
TTL. A transient zotero.org failure (5xx, network error) serves a still-cached
entry rather than failing the request; a hard failure (revoked key, write
scope, no readable library) always overwrites the cache so it is never masked
by stale "valid" data.
"""

import logging
import time
from dataclasses import dataclass

from backend.zotero.key_validator import KeyValidation, validate_key
from backend.services.autoindex_key_store import fingerprint

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 600


@dataclass
class ZoteroIdentity:
    """A validated, gate-approved Zotero identity resolved from an API key."""

    user_id: int
    username: str
    targets: list[str]


class ZoteroIdentityCache:
    """In-memory TTL cache of KeyValidation results, keyed by key fingerprint."""

    def __init__(self, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, tuple[float, KeyValidation]] = {}

    async def resolve(self, api_key: str) -> KeyValidation:
        """Return a cached or freshly validated KeyValidation for api_key."""
        fp = fingerprint(api_key)
        now = time.monotonic()
        cached = self._entries.get(fp)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]

        result = await validate_key(api_key)
        if result.transient and cached is not None:
            logger.warning("zotero.org validation failed transiently; serving stale cache for %s", fp)
            return cached[1]
        if not result.transient:
            self._entries[fp] = (now, result)
        return result

    def clear(self) -> None:
        """Test helper: drop all cached entries."""
        self._entries.clear()


_cache = ZoteroIdentityCache()


def get_identity_cache() -> ZoteroIdentityCache:
    """Return the process-wide identity cache used by the auth middleware."""
    return _cache


def reset_identity_cache() -> None:
    """Test helper: clear the process-wide cache between test cases."""
    _cache.clear()
