import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

from backend.config.settings import get_settings
from backend.dependencies import get_client_api_keys, make_embedding_service
from backend.services.rate_limit_info import get_cached_rate_limits

router = APIRouter()
logger = logging.getLogger(__name__)


class RateLimitResponse(BaseModel):
    available: bool
    limits: dict[str, str] | None


@router.get("/rate-limits", response_model=RateLimitResponse)
async def get_rate_limits(http_request: Request):
    """Return current rate-limit headers for the configured embedding API.

    Resolution order:
    1. In-process cache (populated during same-process indexing/upload).
    2. cron_status.json written by the cron indexer (separate process).
    3. Live probe: a minimal single-item embedding call to fetch fresh headers.
    """
    client_keys = get_client_api_keys(http_request)
    embedding_service = make_embedding_service(client_keys)
    info = await embedding_service.get_rate_limit_info()

    if info is None:
        # Fall back to headers persisted by the cron indexer (separate process).
        cached = get_cached_rate_limits(get_settings())
        info = cached["limits"] if cached else None

    if info is None:
        # Last resort: make a live probe call to the embedding API.
        try:
            info = await embedding_service.probe_rate_limits()
        except Exception as exc:
            logger.debug("Rate-limit probe call failed: %s", exc)

    return RateLimitResponse(available=info is not None, limits=info)
