"""
Library/user registration endpoints.

POST /api/register   — register a user for a library
GET  /api/registrations — inspect registration data for the caller's accessible libraries
"""

import logging
from typing import Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends

from backend.config.settings import get_settings
from backend.dependencies import get_zotero_identity
from backend.services.access_gate import assert_can_access, is_authorized_for_library
from backend.services.registration_service import RegistrationService
from backend.services.zotero_identity import ZoteroIdentity

router = APIRouter()
logger = logging.getLogger(__name__)


class RegisterRequest(BaseModel):
    """Request body for POST /api/register."""

    library_id: str
    library_name: str
    user_id: int
    username: str


class RegisterResponse(BaseModel):
    """Response from POST /api/register."""

    exists: bool


@router.post(
    "/register",
    response_model=RegisterResponse,
    summary="Register a user for a library",
)
async def register_library(
    request: RegisterRequest,
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
) -> RegisterResponse:
    """Associate a zotero.org user with a library on this backend.

    Returns ``exists: true`` if this library was already registered by any
    user before this call. The registered user_id/username are taken from
    the caller's validated Zotero identity when present, never trusted from
    the request body — only the loopback/legacy no-identity path falls back
    to the body-supplied values.
    """
    assert_can_access(identity, request.library_id)
    settings = get_settings()
    service = RegistrationService(settings.registrations_path)
    user_id = identity.user_id if identity else request.user_id
    username = identity.username if identity else request.username
    existed = service.register(
        library_id=request.library_id,
        library_name=request.library_name,
        user_id=user_id,
        username=username,
    )
    return RegisterResponse(exists=existed)


@router.get(
    "/registrations",
    summary="Inspect library/user registration data for the caller's accessible libraries",
)
async def get_registrations(
    identity: Optional[ZoteroIdentity] = Depends(get_zotero_identity),
) -> dict:
    """Return registration data, filtered to the caller's own readable libraries.

    On loopback deployments (identity=None) returns the full registrations
    file unfiltered, matching the single-trusted-local-user model used
    throughout this backend's Zotero-key auth. On a gated remote deployment,
    each caller sees only registrations for libraries their own Zotero key
    grants read access to — the same filtering GET /api/libraries applies.
    """
    settings = get_settings()
    service = RegistrationService(settings.registrations_path)
    all_registrations = service.get_all()
    if identity is None:
        return all_registrations
    return {
        library_id: entry
        for library_id, entry in all_registrations.items()
        if is_authorized_for_library(identity, library_id)
    }
