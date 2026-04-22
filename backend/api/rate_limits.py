from fastapi import APIRouter, Request
from pydantic import BaseModel

from backend.dependencies import get_client_api_keys, make_embedding_service

router = APIRouter()


class RateLimitResponse(BaseModel):
    available: bool
    limits: dict[str, str] | None


@router.get("/rate-limits", response_model=RateLimitResponse)
async def get_rate_limits(http_request: Request):
    """Return cached rate-limit headers from the last embedding API call."""
    client_keys = get_client_api_keys(http_request)
    embedding_service = make_embedding_service(client_keys)
    info = await embedding_service.get_rate_limit_info()
    return RateLimitResponse(available=info is not None, limits=info)
