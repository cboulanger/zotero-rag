"""Parsers that turn rate-limit response headers into display meters.

The generic provider understands the two standard dialects, so any
OpenAI-compatible service gets usage bars without a provider class:

* OpenAI-style: ``x-ratelimit-{limit,remaining,reset}-{requests,tokens}``,
  where the reset value is a duration such as ``1s`` or ``6m0s``.
* IETF ``RateLimit-Limit`` / ``RateLimit-Remaining`` / ``RateLimit-Reset``
  (delta seconds), with the optional ``RateLimit-Policy`` window (``w=``
  seconds) giving the period; also the combined ``RateLimit`` /
  ``RateLimit-Policy`` structured-field form (``r=``, ``t=``, ``q=``, ``w=``).

Every function here is pure and never raises: malformed input yields no
meter. Vendor dialects live in their provider class and call these first.
"""

import re
from datetime import datetime, timedelta
from typing import Literal, Mapping, Optional

from backend.providers.types import Meter, Side

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

# Window length in seconds -> period name, for the periods the UI knows.
_PERIOD_NAMES = {1: "second", 60: "minute", 3600: "hour", 86400: "day"}


def lowercase_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Header mapping with lower-cased names (HTTP header names are case-insensitive)."""
    try:
        return {str(k).lower(): str(v) for k, v in headers.items()}
    except Exception:
        return {}


def parse_duration(value: str) -> Optional[float]:
    """Seconds in a duration string: ``"1s"``, ``"6m0s"``, ``"1h2m3.5s"``, ``"20ms"`` or ``"30"``.

    Returns None when the string is not a duration.
    """
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)  # plain number of seconds
    except ValueError:
        pass
    pos = 0
    total = 0.0
    for m in _DURATION_PART.finditer(text):
        if m.start() != pos:
            return None
        total += float(m.group(1)) * _UNIT_SECONDS[m.group(2)]
        pos = m.end()
    if pos != len(text) or pos == 0:
        return None
    return total


def period_name(seconds: Optional[float]) -> Optional[str]:
    """Name of a window length (``3600`` -> ``"hour"``), or ``"<n>s"`` for an unusual one."""
    if seconds is None or seconds <= 0:
        return None
    whole = int(round(seconds))
    return _PERIOD_NAMES.get(whole, f"{whole}s")


def _int(value: Optional[str]) -> Optional[int]:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _resets_at(as_of: str, seconds: Optional[float]) -> Optional[str]:
    if seconds is None or seconds < 0:
        return None
    try:
        return (datetime.fromisoformat(as_of) + timedelta(seconds=seconds)).isoformat()
    except ValueError:
        return None


def make_meter(
    *,
    side: Side,
    unit: str,
    period: Optional[str],
    limit: Optional[int],
    remaining: Optional[int],
    as_of: str,
    source: Literal["run", "cache"],
    reset_seconds: Optional[float] = None,
) -> Optional[Meter]:
    """Build a meter, or None when the numbers cannot describe a quota.

    A non-positive limit or a negative remaining count is dropped; a remaining
    count above the limit is clamped to the limit.
    """
    if limit is None or remaining is None or limit <= 0 or remaining < 0:
        return None
    return Meter(
        id=f"{unit}/{period}" if period else unit,
        side=side,
        unit=unit,
        period=period,
        limit=limit,
        remaining=min(remaining, limit),
        resets_at=_resets_at(as_of, reset_seconds),
        as_of=as_of,
        source=source,
    )


def parse_openai_headers(
    headers: Mapping[str, str], *, side: Side, as_of: str, source: Literal["run", "cache"] = "run"
) -> list[Meter]:
    """``x-ratelimit-{limit,remaining,reset}-{requests,tokens}`` meters."""
    h = lowercase_headers(headers)
    meters: list[Meter] = []
    for unit in ("requests", "tokens"):
        meter = make_meter(
            side=side,
            unit=unit,
            period=None,
            limit=_int(h.get(f"x-ratelimit-limit-{unit}")),
            remaining=_int(h.get(f"x-ratelimit-remaining-{unit}")),
            reset_seconds=parse_duration(h.get(f"x-ratelimit-reset-{unit}", "")),
            as_of=as_of,
            source=source,
        )
        if meter:
            meters.append(meter)
    return meters


_STRUCT_PARAM = re.compile(r";\s*([a-z]+)=(\d+(?:\.\d+)?)", re.IGNORECASE)


def _leading_number(value: str) -> Optional[int]:
    m = re.match(r'\s*(?:"[^"]*"\s*;\s*)?(\d+)', value or "")
    return int(m.group(1)) if m else None


def parse_ietf_headers(
    headers: Mapping[str, str], *, side: Side, as_of: str, source: Literal["run", "cache"] = "run"
) -> list[Meter]:
    """IETF ``RateLimit-*`` meter (a single "requests" quota with an optional period)."""
    h = lowercase_headers(headers)
    policy = h.get("ratelimit-policy", "")
    params = {k.lower(): float(v) for k, v in _STRUCT_PARAM.findall(policy)}

    limit = _int(h.get("ratelimit-limit"))
    remaining = _int(h.get("ratelimit-remaining"))
    reset = parse_duration(h.get("ratelimit-reset", ""))

    combined = h.get("ratelimit", "")
    if combined and (limit is None or remaining is None):
        c = {k.lower(): float(v) for k, v in _STRUCT_PARAM.findall(combined)}
        if remaining is None and "r" in c:
            remaining = int(c["r"])
        if reset is None and "t" in c:
            reset = c["t"]
    if limit is None:
        limit = int(params["q"]) if "q" in params else _leading_number(policy)

    meter = make_meter(
        side=side,
        unit="requests",
        period=period_name(params.get("w")),
        limit=limit,
        remaining=remaining,
        reset_seconds=reset,
        as_of=as_of,
        source=source,
    )
    return [meter] if meter else []


def parse_standard_headers(
    headers: Mapping[str, str], *, side: Side, as_of: str, source: Literal["run", "cache"] = "run"
) -> list[Meter]:
    """Meters from both standard dialects. OpenAI-style meters come first."""
    if not headers:
        return []
    try:
        return parse_openai_headers(headers, side=side, as_of=as_of, source=source) + parse_ietf_headers(
            headers, side=side, as_of=as_of, source=source
        )
    except Exception:  # pragma: no cover - parsers are defensive; never break a request
        return []
