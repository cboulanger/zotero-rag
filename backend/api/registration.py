"""
Library/user registration endpoints.

POST /api/register   — register a user for a library
GET  /api/registrations — inspect all registration data (admin)
"""

import logging
from pydantic import BaseModel
from fastapi import APIRouter

from backend.config.settings import get_settings
from backend.services.registration_service import RegistrationService

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
async def register_library(request: RegisterRequest) -> RegisterResponse:
    """Associate a zotero.org user with a library on this backend.

    Must be called before the first indexing operation for a library when
    REQUIRE_REGISTRATION is enabled. Returns ``exists: true`` if this library
    was already registered by any user before this call.
    """
    settings = get_settings()
    service = RegistrationService(settings.registrations_path)
    existed = service.register(
        library_id=request.library_id,
        library_name=request.library_name,
        user_id=request.user_id,
        username=request.username,
    )
    return RegisterResponse(exists=existed)


@router.get(
    "/registrations",
    summary="Inspect all library/user registration data (admin)",
)
async def get_registrations() -> dict:
    """Return the full registrations JSON for admin inspection.

    Protected by the same API key auth as all other endpoints.
    """
    settings = get_settings()
    service = RegistrationService(settings.registrations_path)
    return service.get_all()
