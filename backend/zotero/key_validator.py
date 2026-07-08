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
    # Human-readable group name / real Zotero owner user id, keyed by
    # "groups/<id>" slug — captured from whichever group lookup resolved that
    # slug below (`data.name` / `data.owner`) so that group libraries which
    # are only ever auto-indexed (never separately registered via the manual
    # RAG-query flow) can still be labeled in the status view.
    target_names: dict[str, str] = field(default_factory=dict)
    target_owners: dict[str, int] = field(default_factory=dict)
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
        target_names: dict[str, str] = {}
        target_owners: dict[str, int] = {}
        if user_id and access.get("user", {}).get("library"):
            targets.append(f"users/{user_id}")

        # Group name/owner lookups need the key itself as auth — group
        # libraries are typically private, so an unauthenticated request
        # would see nothing. All group-detail calls below pass it explicitly.
        auth_headers = {"Zotero-API-Key": api_key}

        groups = access.get("groups", {})
        if user_id and groups.get("all", {}).get("library"):
            # Key can read all groups the user belongs to — enumerate them.
            # The response already carries each group's name/owner under
            # `data`; capture them instead of discarding everything but `id`.
            try:
                async with session.get(f"{base_url}/users/{user_id}/groups", headers=auth_headers) as gresp:
                    if gresp.status == 200:
                        for grp in await gresp.json():
                            gid = grp.get("id")
                            if gid is None:
                                continue
                            slug = f"groups/{gid}"
                            targets.append(slug)
                            grp_data = grp.get("data", {})
                            name = grp_data.get("name")
                            if name:
                                target_names[slug] = name
                            owner = grp_data.get("owner")
                            if owner is not None:
                                target_owners[slug] = owner
            except aiohttp.ClientError as exc:
                logger.warning("Failed to enumerate groups for user %s: %s", user_id, exc)
        for gid, grp in groups.items():
            if gid != "all" and isinstance(grp, dict) and grp.get("library"):
                slug = f"groups/{gid}"
                if slug not in targets:
                    targets.append(slug)
                if slug not in target_names:
                    # Individually-granted groups aren't covered by the "all
                    # groups" enumeration above (a common scenario: a key
                    # scoped to one or a few specific libraries rather than
                    # every group the user belongs to) — fetch each one's
                    # name/owner directly instead of leaving it unlabeled.
                    try:
                        async with session.get(f"{base_url}/groups/{gid}", headers=auth_headers) as gresp:
                            if gresp.status == 200:
                                grp_data = (await gresp.json()).get("data", {})
                                name = grp_data.get("name")
                                if name:
                                    target_names[slug] = name
                                owner = grp_data.get("owner")
                                if owner is not None:
                                    target_owners[slug] = owner
                    except aiohttp.ClientError as exc:
                        logger.warning("Failed to fetch group %s details: %s", gid, exc)

        if not targets:
            return KeyValidation(
                user_id, username, read_only=False,
                reason="Key grants no readable library.",
            )

        return KeyValidation(
            user_id, username, targets=targets,
            target_names=target_names, target_owners=target_owners,
            read_only=True,
        )
