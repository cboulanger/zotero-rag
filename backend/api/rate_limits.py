import json
import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

from backend.config.settings import get_settings
from backend.dependencies import get_client_api_keys, make_embedding_service

router = APIRouter()
logger = logging.getLogger(__name__)


class RateLimitResponse(BaseModel):
    available: bool
    limits: dict[str, str] | None


@router.get("/rate-limits", response_model=RateLimitResponse)
async def get_rate_limits(http_request: Request):
    """Return cached rate-limit headers from the last embedding API call.

    Checks the in-process cache first (populated during same-process indexing),
    then falls back to cron_status.json (populated by the cron indexer process).
    """
    client_keys = get_client_api_keys(http_request)
    embedding_service = make_embedding_service(client_keys)
    info = await embedding_service.get_rate_limit_info()

    if info is None:
        # Fall back to headers persisted by the cron indexer (separate process).
        try:
            settings = get_settings()
            cron_status_path = settings.data_path / "system" / "cron_status.json"
            if cron_status_path.exists():
                cron_data = json.loads(cron_status_path.read_text(encoding="utf-8"))
                info = cron_data.get("last_rate_limit_headers") or None
        except Exception as exc:
            logger.debug("Could not read rate-limit headers from cron status: %s", exc)

    return RateLimitResponse(available=info is not None, limits=info)
