"""Identity-check endpoint used by the plugin's Preferences pane and setup
wizard to validate a Zotero API key before/without saving it, and to show
the caller which libraries their key can access.

Deliberately thin: all the validation, caching, and gate logic already runs
in the auth middleware (backend.dependencies.resolve_zotero_identity) for
every /api/* request. This handler only reads back the result — a 401/403/503
never reaches it, the middleware already returned that response.
"""

from typing import Optional

from fastapi import APIRouter, Depends

from backend.dependencies import get_zotero_identity
from backend.services.zotero_identity import ZoteroIdentity

router = APIRouter()


@router.get("/auth/whoami", summary="Validate the caller's Zotero API key and return their identity")
def whoami(identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity)) -> dict:
    if identity is None:
        return {"authorized": True, "loopback": True}
    return {
        "authorized": True,
        "loopback": False,
        "user_id": identity.user_id,
        "username": identity.username,
        "targets": identity.targets,
    }
