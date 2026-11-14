"""Endpoints that run in the caller's own provider session (e.g. an MPCDF job).

A provider with ``uses_session_endpoints`` has no fixed URL or shared key for a
side: each user starts their own endpoint, and its URL and key change whenever
the job is renewed. :class:`SessionEndpoint` asks the provider for the calling
user's current credentials before each use and reports activity (at most once
a minute) so the provider keeps the endpoint alive while it is in use.
"""

import asyncio
import logging
import time
from typing import Any, Optional

from backend.providers.types import Credentials
from backend.services.caller_context import current_user_id

logger = logging.getLogger(__name__)

#: Seconds between two activity reports for one user and side.
TOUCH_INTERVAL = 60.0


class SessionEndpointError(RuntimeError):
    """The provider could not tell whether the caller's endpoint runs."""


def uses_session_endpoints(provider: Any) -> bool:
    """True when ``provider`` serves its side from per-user session endpoints."""
    return bool(provider is not None and getattr(provider, "uses_session_endpoints", False))


class SessionEndpoint:
    """The calling user's endpoint for one side of a session-based provider."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self._touched: dict[Optional[int], float] = {}

    async def resolve(self) -> Optional[Credentials]:
        """The caller's current URL and key, or None when their endpoint is not running."""
        user_id = current_user_id()
        try:
            creds = await asyncio.to_thread(self.provider.session_endpoint, user_id)
        except Exception as exc:
            raise SessionEndpointError(str(exc)) from exc
        if creds is not None:
            await self._touch(user_id)
        return creds

    async def _touch(self, user_id: Optional[int]) -> None:
        now = time.monotonic()
        if now - self._touched.get(user_id, float("-inf")) < TOUCH_INTERVAL:
            return
        self._touched[user_id] = now
        try:
            await asyncio.to_thread(self.provider.session_activity, user_id)
        except Exception as exc:  # activity is best effort
            logger.debug("Could not report endpoint activity: %s", exc)
