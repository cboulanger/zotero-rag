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


def _backend_id_to_slug(backend_id: str) -> str:
    """Convert a backend library ID to a Zotero.org slug for target comparison.

    u12345 -> users/12345
    678    -> groups/678

    Duplicated from backend.api.public_query.backend_id_to_slug (not imported
    from there) to avoid a circular import: backend/dependencies.py already
    imports from this module, and public_query.py imports from dependencies.py.
    """
    if backend_id.startswith("u"):
        return "users/" + backend_id[1:]
    return "groups/" + backend_id


def is_authorized_for_library(identity: Optional[ZoteroIdentity], library_id: str) -> bool:
    """True if identity may access library_id.

    identity=None means the loopback (Part 4) or legacy-shared-key (Part 5)
    bypass path — always authorized. Otherwise library_id (backend format,
    e.g. "u12345" or "678") is converted to Zotero slug format (e.g.
    "users/12345" or "groups/678") before checking identity.targets, since
    the two use different ID formats — see _backend_id_to_slug.
    """
    if identity is None:
        return True
    return _backend_id_to_slug(library_id) in identity.targets


def assert_can_access(identity: Optional[ZoteroIdentity], library_id: str) -> None:
    """Raise 403 unless library_id is within identity's readable targets.

    identity=None means the caller is on the loopback no-auth path (Part 4)
    or the transitional legacy-shared-key path (Part 5) — both bypass
    per-library enforcement (loopback because there is only one trusted
    user; legacy because un-migrated plugins predate per-library targets).
    """
    if not is_authorized_for_library(identity, library_id):
        raise HTTPException(
            status_code=403,
            detail=f"Your Zotero key does not grant read access to library {library_id!r}.",
        )
