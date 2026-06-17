"""Utilities for the KISSKI Chat-AI API endpoint."""

import httpx
import logging
from typing import List
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Model IDs containing these substrings are excluded from RAG model lists
_EXCLUDED_KEYWORDS = frozenset({"coder", "devstral"})


class KISSKIModelInfo(BaseModel):
    """A KISSKI model with RAG suitability and live availability data."""

    id: str
    name: str
    demand: int
    availability: str  # "available" | "busy" | "very busy"


def _demand_to_availability(demand: int) -> str:
    if demand == 0:
        return "available"
    if demand <= 5:
        return "busy"
    return "very busy"


def _is_rag_suitable(entry: dict) -> bool:
    """Return True if a KISSKI model entry is suitable for text-based RAG."""
    if not isinstance(entry, dict):
        return False
    model_id = entry.get("id", "").lower()
    if not model_id:
        return False
    # Must support text input and produce text output
    if "text" not in entry.get("input", []):
        return False
    if "text" not in entry.get("output", []):
        return False
    # Exclude code-focused models
    if any(kw in model_id for kw in _EXCLUDED_KEYWORDS):
        return False
    return True


def fetch_kisski_rag_models(base_url: str, api_key: str, timeout: float = 5.0) -> List[KISSKIModelInfo]:
    """
    Fetch RAG-suitable models from the KISSKI Chat-AI API with live availability data.

    Queries ``POST {base_url}/models``, filters out models unsuitable for RAG
    (no text input/output, code-focused), annotates each with an availability label
    derived from the ``demand`` field, and returns the list ordered by demand
    ascending (most available first).

    Args:
        base_url: KISSKI API base URL (e.g. "https://chat-ai.academiccloud.de/v1").
        api_key: KISSKI API bearer token.
        timeout: HTTP request timeout in seconds.

    Returns:
        List of :class:`KISSKIModelInfo` ordered by demand ascending.

    Raises:
        httpx.HTTPError: on network or HTTP-level failure.
    """
    url = base_url.rstrip("/") + "/models"
    resp = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    entries = resp.json().get("data", [])

    models = [
        KISSKIModelInfo(
            id=entry["id"],
            name=entry.get("name", entry["id"]),
            demand=int(entry.get("demand", 0)),
            availability=_demand_to_availability(int(entry.get("demand", 0))),
        )
        for entry in entries
        if _is_rag_suitable(entry)
    ]
    models.sort(key=lambda m: m.demand)
    return models
