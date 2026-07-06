"""The Part 2 access gate and Part 4 loopback exception.

zotero_identity.py proves "this is a real Zotero user with these readable
libraries." That is authentication, not authorization to use this instance —
any Zotero account on earth can pass it. This module adds the second check:
is the identity a member of a designated Zotero group, or on an explicit
user_id allowlist? It also holds the per-library enforcement helper used by
route handlers, and the startup check that refuses to run an unguarded
remote instance.
"""

import logging
from typing import Optional

from fastapi import HTTPException

from backend.config.settings import Settings
from backend.services.zotero_identity import ZoteroIdentity

logger = logging.getLogger(__name__)


def is_loopback(settings: Settings) -> bool:
    """True for the single-trusted-local-user deployment mode (Part 4)."""
    return settings.api_host in ("localhost", "127.0.0.1")


def is_gate_configured(settings: Settings) -> bool:
    return bool(settings.authorized_group_id) or bool(settings.authorized_user_ids)


def passes_gate(identity: ZoteroIdentity, settings: Settings) -> bool:
    """True if identity is allowed to use this instance at all (Part 2)."""
    if settings.authorized_group_id and f"groups/{settings.authorized_group_id}" in identity.targets:
        return True
    if settings.authorized_user_ids and identity.user_id in settings.authorized_user_ids:
        return True
    return False


def assert_safe_to_start(settings: Settings) -> None:
    """Refuse to start in remote mode without a configured access gate.

    Loopback deployments are exempt: a single trusted local user needs no
    gate. Remote deployments must configure AUTHORIZED_GROUP_ID and/or
    AUTHORIZED_USER_IDS, or every Zotero user on earth could use the instance.
    """
    if is_loopback(settings):
        return
    if not is_gate_configured(settings):
        raise RuntimeError(
            f"Refusing to start in remote mode (api_host={settings.api_host!r}) "
            "without an access gate: set AUTHORIZED_GROUP_ID and/or "
            "AUTHORIZED_USER_IDS. See docs/history/plan-zotero-key-auth.md Part 2."
        )


def assert_can_access(identity: Optional[ZoteroIdentity], library_id: str) -> None:
    """Raise 403 unless library_id is within identity's readable targets.

    identity=None means the caller is on the loopback no-auth path (Part 4)
    or the transitional legacy-shared-key path (Part 5) — both bypass
    per-library enforcement (loopback because there is only one trusted
    user; legacy because un-migrated plugins predate per-library targets).
    """
    if identity is None:
        return
    if library_id not in identity.targets:
        raise HTTPException(
            status_code=403,
            detail=f"Your Zotero key does not grant read access to library {library_id!r}.",
        )
