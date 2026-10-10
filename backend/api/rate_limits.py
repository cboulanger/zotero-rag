import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

from backend.config.settings import get_settings
from backend.dependencies import get_client_api_keys, make_embedding_service
from backend.providers import Meter
from backend.services.rate_limit_info import get_cached_rate_limits
from backend.services.usage_meters import meters_from_headers

router = APIRouter()
logger = logging.getLogger(__name__)


class RateLimitResponse(BaseModel):
    available: bool
    meters: list[Meter] = []


@router.get("/rate-limits", response_model=RateLimitResponse)
async def get_rate_limits(http_request: Request):
    """Return the current usage meters (quota only) of the active preset's remote sides.

    Resolution order:
    1. In-process record (populated during same-process indexing/upload/query).
    2. cron_status.json written by the cron indexer (separate process).
    3. Live probe: a minimal single-item embedding call to fetch fresh headers.
    """
    settings = get_settings()
    client_keys = get_client_api_keys(http_request)
    embedding_service = make_embedding_service(client_keys)
    fingerprints = {"embedding": getattr(embedding_service, "_fingerprint", None)}

    cached = get_cached_rate_limits(settings, fingerprints)
    meters = cached["meters"] if cached else []

    if not meters:
        # Last resort: make a live probe call to the embedding API.
        try:
            headers = await embedding_service.probe_rate_limits()
            if headers:
                meters = meters_from_headers(settings.get_hardware_preset(), "embedding", headers)
        except Exception as exc:
            logger.debug("Rate-limit probe call failed: %s", exc)

    return RateLimitResponse(available=bool(meters), meters=meters)
