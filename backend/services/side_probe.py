"""Reachability of a remote side by a real request, and its quota meters.

For a provider that has no status check of its own (``Provider.health`` returns
None), the "Configure preset" dialog sends one minimal request through the same
service a query uses (one embedding, or a one-token completion) and reports
whether it was answered. Results are cached per side and API key, so a dialog
that refreshes every few seconds does not spend the user's quota: a success is
reused for ``OK_TTL`` seconds, a failure for ``FAIL_TTL``.

The response headers of every request are recorded by ``usage_meters``; the
meters of a side are read from there, whether or not a request was made here.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from backend.services.usage_meters import key_fingerprint, meters_from_headers, recorder

logger = logging.getLogger(__name__)

OK_TTL = 300.0
FAIL_TTL = 60.0
PROBE_TIMEOUT = 25.0


@dataclass
class SideProbe:
    """Outcome of a test request (``ok`` None: none was made)."""

    ok: Optional[bool] = None
    detail: str = ""
    rate_limited: bool = False
    checked_at: float = 0.0
    meters: list[dict] = field(default_factory=list)


_cache: dict[tuple[str, Optional[str]], SideProbe] = {}


def reset() -> None:
    """Forget cached results (tests; a preset switch)."""
    _cache.clear()


def _service(side: str, client_keys: dict[str, str]) -> tuple[Any, Optional[str]]:
    from backend.dependencies import make_embedding_service, make_llm_service

    if side == "embedding":
        service = make_embedding_service(client_keys)
        return service, getattr(service, "_fingerprint", None)
    service = make_llm_service(client_keys)
    resolve = getattr(service, "_resolve_api_key", None)
    return service, key_fingerprint(resolve()) if callable(resolve) else None


async def _request(side: str, service: Any) -> None:
    if side == "embedding":
        create = getattr(service, "_create_embeddings_with_backoff", None)
        if create is not None:  # bypasses the embedding cache, so the request really goes out
            ensure = getattr(service, "_ensure_endpoint", None)
            if ensure is not None:
                await ensure()
            await create(["status check"])
        else:
            await service.embed_text(f"status check {time.time()}")
    else:
        await service.generate("Reply with OK.", max_tokens=1)


def _describe(exc: BaseException) -> tuple[str, bool]:
    from backend.services.embeddings import EmbeddingRateLimitExhaustedError
    from backend.services.llm import LLMRateLimitExhaustedError

    if isinstance(exc, (EmbeddingRateLimitExhaustedError, LLMRateLimitExhaustedError)):
        at = getattr(exc, "available_at", None)
        return ("quota exhausted" + (f" until {at.strftime('%H:%M')} UTC" if at else "")), True
    if isinstance(exc, asyncio.TimeoutError):
        return f"no answer within {int(PROBE_TIMEOUT)} s", False
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    text = str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__
    return (f"HTTP {status}: {text}" if status else text), False


async def probe_side(preset: Any, side: str, client_keys: dict[str, str], *, send: bool) -> SideProbe:
    """The side's meters and, with ``send``, the outcome of a (cached) test request. Never raises."""
    try:
        service, fingerprint = _service(side, client_keys)
    except Exception as exc:  # a misconfigured side: report it, do not fail the view
        return SideProbe(ok=False if send else None, detail=_describe(exc)[0], checked_at=time.time())

    result = SideProbe()
    if send:
        cached = _cache.get((side, fingerprint))
        now = time.time()
        if cached and now - cached.checked_at < (OK_TTL if cached.ok else FAIL_TTL):
            result = SideProbe(ok=cached.ok, detail=cached.detail, rate_limited=cached.rate_limited,
                               checked_at=cached.checked_at)
        else:
            try:
                await asyncio.wait_for(_request(side, service), timeout=PROBE_TIMEOUT)
                result = SideProbe(ok=True, checked_at=now)
            except Exception as exc:
                detail, limited = _describe(exc)
                logger.info("Test request of the %s side failed: %s", side, detail)
                result = SideProbe(ok=False, detail=detail, rate_limited=limited, checked_at=now)
            _cache[(side, fingerprint)] = SideProbe(ok=result.ok, detail=result.detail,
                                                    rate_limited=result.rate_limited, checked_at=result.checked_at)
    if fingerprint is not None:  # without a key, latest() would return someone else's numbers
        headers, captured_at = recorder.latest(side, fingerprint)
        result.meters = meters_from_headers(preset, side, headers, as_of=captured_at)
    return result
