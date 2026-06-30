"""Validate Zotero API keys and resolve them to indexable library slugs.

A key is accepted only if it is read-only (no write flag anywhere in `access`)
and grants read access to at least one library. The validator resolves the key
to concrete target slugs: `users/<userID>` plus accessible `groups/<id>`.
"""

import logging
from dataclasses import dataclass, field

import aiohttp

logger = logging.getLogger(__name__)

ZOTERO_API_BASE = "https://api.zotero.org"


@dataclass
class KeyValidation:
    """Result of validating a Zotero API key."""

    user_id: int | None
    username: str | None
    targets: list[str] = field(default_factory=list)
    read_only: bool = False
    reason: str | None = None
    transient: bool = False


def _has_write(access: dict) -> bool:
    """True if any write flag is set anywhere in the access object."""
    if access.get("user", {}).get("write"):
        return True
    for grp in access.get("groups", {}).values():
        if isinstance(grp, dict) and grp.get("write"):
            return True
    return False


async def validate_key(api_key: str, base_url: str = ZOTERO_API_BASE) -> KeyValidation:
    """Validate `api_key` and resolve indexable target slugs.

    Returns a KeyValidation. On any failure (revoked, write scope, network),
    `read_only` is False and `reason` explains why; `targets` is empty.
    """
    async with aiohttp.ClientSession(headers={"Zotero-API-Version": "3"}) as session:
        try:
            async with session.get(f"{base_url}/keys/{api_key}") as resp:
                if resp.status == 404:
                    return KeyValidation(None, None, read_only=False, reason="Key not found (revoked or expired).")
                if resp.status != 200:
                    return KeyValidation(None, None, read_only=False, reason=f"Zotero key lookup failed (HTTP {resp.status}).", transient=True)
                data = await resp.json()
        except aiohttp.ClientError as exc:
            return KeyValidation(None, None, read_only=False, reason=f"Could not reach Zotero API: {exc}", transient=True)

        user_id = data.get("userID")
        username = data.get("username")
        access = data.get("access", {})

        if _has_write(access):
            return KeyValidation(
                user_id, username, read_only=False,
                reason="This key has write access. Create a read-only key at "
                       "https://www.zotero.org/settings/keys.",
            )

        targets: list[str] = []
        if user_id and access.get("user", {}).get("library"):
            targets.append(f"users/{user_id}")

        groups = access.get("groups", {})
        if user_id and groups.get("all", {}).get("library"):
            # Key can read all groups the user belongs to — enumerate them.
            try:
                async with session.get(f"{base_url}/users/{user_id}/groups") as gresp:
                    if gresp.status == 200:
                        for grp in await gresp.json():
                            gid = grp.get("id")
                            if gid is not None:
                                targets.append(f"groups/{gid}")
            except aiohttp.ClientError as exc:
                logger.warning("Failed to enumerate groups for user %s: %s", user_id, exc)
        for gid, grp in groups.items():
            if gid != "all" and isinstance(grp, dict) and grp.get("library"):
                slug = f"groups/{gid}"
                if slug not in targets:
                    targets.append(slug)

        if not targets:
            return KeyValidation(
                user_id, username, read_only=False,
                reason="Key grants no readable library.",
            )

        return KeyValidation(user_id, username, targets=targets, read_only=True)
