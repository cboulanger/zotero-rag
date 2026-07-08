"""Zotero group admin/owner check, used to gate admin-only auto-index controls.

Relies on Zotero's own computed `meta.isAdmin` field on GET /groups/<id>,
which the API populates only when the request is authenticated with a key
belonging to the caller being checked — see is_group_admin's docstring.
"""

import logging
import time

import aiohttp

logger = logging.getLogger(__name__)

ZOTERO_API_BASE = "https://api.zotero.org"
CACHE_TTL_SECONDS = 300


async def is_group_admin(user_id: int, group_id: int, api_key: str, base_url: str = ZOTERO_API_BASE) -> bool:
    """True if user_id is the owner or an admin of the given Zotero group.

    Relies on Zotero's own computed `meta.isAdmin` field on GET /groups/<id>,
    which is populated only when the request is authenticated with a key
    belonging to that user (confirmed live: an unauthenticated call to the
    same endpoint omits `meta.isAdmin` entirely). Fails closed (False) on any
    non-200 response, including 403/404 for a group the caller can't see.
    """
    async with aiohttp.ClientSession(headers={
        "Zotero-API-Version": "3",
        "Zotero-API-Key": api_key,
    }) as session:
        try:
            async with session.get(f"{base_url}/groups/{group_id}") as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
        except aiohttp.ClientError:
            return False
    return bool(data.get("meta", {}).get("isAdmin", False))


class AdminRoleCache:
    """In-memory TTL cache of is_group_admin results, keyed by (user_id, group_id).

    No stale-serving-on-error behavior (unlike ZoteroIdentityCache) — an
    admin check should fail closed on a Zotero API hiccup rather than serve
    a possibly-stale "yes".
    """

    def __init__(self, ttl_seconds: int = CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[tuple[int, int], tuple[float, bool]] = {}

    async def is_admin(self, user_id: int, group_id: int, api_key: str) -> bool:
        key = (user_id, group_id)
        now = time.monotonic()
        cached = self._entries.get(key)
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]
        result = await is_group_admin(user_id, group_id, api_key)
        self._entries[key] = (now, result)
        return result

    def clear(self) -> None:
        """Test helper: drop all cached entries."""
        self._entries.clear()


_cache = AdminRoleCache()


def get_admin_role_cache() -> AdminRoleCache:
    """Return the process-wide admin-role cache used by admin-gated routes."""
    return _cache


def reset_admin_role_cache() -> None:
    """Test helper: clear the process-wide cache between test cases."""
    _cache.clear()
