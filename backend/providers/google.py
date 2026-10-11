"""Google (Gemini) provider: the Gemini API through its OpenAI-compatible endpoint.

Google serves chat completions and embeddings under
``https://generativelanguage.googleapis.com/v1beta/openai/``, so the generic
OpenAI-compatible services work unchanged; this class only adds the key
metadata (default key name and where to create a key). The endpoint URL itself
is plain preset data in ``model_kwargs``.
"""

import logging
import re
from typing import Optional

from backend.providers.base import Provider
from backend.providers.types import ModelInfo

logger = logging.getLogger(__name__)

#: The RAG model list only offers Flash models (fast and cheap enough for retrieval answers)...
_INCLUDE = "flash"
#: ...and not their non-text-answer variants (speech, images, realtime audio).
_EXCLUDED_KEYWORDS = ("tts", "image", "live", "audio", "embedding")


class GoogleProvider(Provider):
    id = "google"
    label = "Google Gemini"
    key_scopes = frozenset({"user", "managed"})
    default_scope = "user"
    has_live_models = True
    #: Timeout of the model-list request (seconds); the list is best-effort.
    http_timeout = 5.0

    def default_key_env(self) -> Optional[str]:
        return "GEMINI_API_KEY"

    def key_docs_url(self, env_var: str) -> Optional[str]:
        if env_var == "GEMINI_API_KEY":
            return "https://aistudio.google.com/apikey"
        return super().key_docs_url(env_var)

    def live_models(self, base_url: str, api_key: str) -> Optional[list[ModelInfo]]:
        """Gemini Flash models from ``GET {base_url}/models``, newest first.

        Returns None on any failure so callers fall back to the preset's static list.
        """
        if self.side != "llm":
            return None
        url = base_url.rstrip("/") + "/models"
        try:
            resp = self._http().get(
                url, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                timeout=self.http_timeout,
            )
            if not (200 <= resp.status_code < 300):
                logger.warning("Gemini model list returned HTTP %s from %s", resp.status_code, url)
                return None
            ids = {_model_id(e) for e in resp.json().get("data", []) if isinstance(e, dict)}
        except Exception as exc:  # network error, bad JSON: best-effort feature
            logger.warning("Could not fetch live Gemini models from %s: %s", url, exc)
            return None
        wanted = [i for i in ids if _is_rag_suitable(i)]
        wanted.sort(key=_version_key, reverse=True)
        return [ModelInfo(id=i) for i in wanted]


def _model_id(entry: dict) -> str:
    """Entry id without Google's ``models/`` prefix (the chat endpoint takes the bare name)."""
    return str(entry.get("id", "")).removeprefix("models/")


def _is_rag_suitable(model_id: str) -> bool:
    return _INCLUDE in model_id and not any(kw in model_id for kw in _EXCLUDED_KEYWORDS)


def _version_key(model_id: str) -> tuple:
    """Sort key: higher version first, then stable over preview/lite variants (``gemini-3.5-flash`` > ``gemini-3.5-flash-lite``)."""
    nums = tuple(int(n) for n in re.findall(r"\d+", model_id.split("flash")[0]))
    return (nums, -len(model_id), model_id)
