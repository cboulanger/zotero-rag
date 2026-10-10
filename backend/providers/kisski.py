"""KISSKI / SAIA (GWDG Academic Cloud Chat-AI) provider.

Everything KISSKI-specific lives here: the live, availability-ordered model
list with its demand labels, the key portal link, and the service's own
``x-ratelimit-{limit,remaining}-{hour,day,month}`` header names. Quotas and keys are
per user (``key_scopes = {"user"}``), which is how KISSKI has always worked.

Generic OpenAI-compatible settings (``base_url``, ``api_key_env``,
``extra_body`` ...) stay plain preset data in ``model_kwargs``.
"""

import logging
from typing import Literal, Mapping, Optional

from backend.providers.base import Provider, _utc_now
from backend.providers.types import Meter, ModelInfo, ProviderOptions
from backend.providers.usage import lowercase_headers, make_meter

logger = logging.getLogger(__name__)

# Model IDs containing these substrings are excluded from RAG model lists.
_EXCLUDED_KEYWORDS = frozenset({"coder", "devstral"})

_PERIODS = ("hour", "day", "month")


class KisskiOptions(ProviderOptions):
    #: URL queried for the live model list; default ``{llm base_url}/models``.
    models_url: Optional[str] = None


def _demand_to_availability(demand: int) -> str:
    if demand == 0:
        return "available"
    if demand <= 5:
        return "busy"
    return "very busy"


def _is_rag_suitable(entry: object) -> bool:
    """True if a KISSKI model entry is suitable for text-based RAG."""
    if not isinstance(entry, dict):
        return False
    model_id = str(entry.get("id", "")).lower()
    if not model_id:
        return False
    # Must take text in and produce text out.
    if "text" not in entry.get("input", []):
        return False
    if "text" not in entry.get("output", []):
        return False
    # Exclude code-focused models.
    return not any(kw in model_id for kw in _EXCLUDED_KEYWORDS)


class KisskiProvider(Provider):
    """KISSKI Chat-AI (SAIA): per-user key, live model list, hour/day/month quotas."""

    id = "kisski"
    label = "KISSKI"
    Options = KisskiOptions
    key_scopes = frozenset({"user"})
    default_scope = "user"
    has_live_models = True
    #: Timeout of the model-list request (seconds); the list is best-effort.
    http_timeout = 5.0

    def default_key_env(self) -> Optional[str]:
        return "KISSKI_API_KEY"

    def key_docs_url(self, env_var: str) -> Optional[str]:
        if env_var == "KISSKI_API_KEY":
            return "https://saia.gwdg.de/dashboard"
        return super().key_docs_url(env_var)

    def live_models(self, base_url: str, api_key: str) -> Optional[list[ModelInfo]]:
        """RAG-suitable models with live demand, most available first.

        Queries ``POST {models_url or base_url/models}``. Returns None on any
        failure so callers fall back to the preset's static list.
        """
        if self.side != "llm":
            return None
        url = self.options.models_url or (base_url.rstrip("/") + "/models")
        try:
            resp = self._http().post(
                url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=self.http_timeout,
            )
            if not (200 <= resp.status_code < 300):
                logger.warning("KISSKI model list returned HTTP %s from %s", resp.status_code, url)
                return None
            entries = resp.json().get("data", [])
            models = []
            for entry in entries:
                if not _is_rag_suitable(entry):
                    continue
                demand = int(entry.get("demand", 0))
                models.append(ModelInfo(id=entry["id"], demand=demand, availability=_demand_to_availability(demand)))
        except Exception as exc:  # network error, bad JSON, bad entry: best-effort feature
            logger.warning("Could not fetch live KISSKI models from %s: %s", url, exc)
            return None
        models.sort(key=lambda m: m.demand if m.demand is not None else 0)
        return models

    def parse_usage(
        self,
        headers: Mapping[str, str],
        *,
        as_of: Optional[str] = None,
        source: Literal["run", "cache"] = "run",
    ) -> list[Meter]:
        """Standard dialects plus KISSKI's ``x-ratelimit-{limit,remaining}-{hour,day,month}``."""
        stamp = as_of or _utc_now()
        meters = super().parse_usage(headers, as_of=stamp, source=source)
        h = lowercase_headers(headers or {})
        for period in _PERIODS:
            try:
                limit = int(float(h.get(f"x-ratelimit-limit-{period}", "")))
                remaining = int(float(h.get(f"x-ratelimit-remaining-{period}", "")))
            except ValueError:
                continue
            meter = make_meter(
                side=self.side, unit="requests", period=period, limit=limit, remaining=remaining,
                as_of=stamp, source=source,
            )
            if meter:
                meters.append(meter)
        return meters
