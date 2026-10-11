"""Google (Gemini) provider: the Gemini API through its OpenAI-compatible endpoint.

Google serves chat completions and embeddings under
``https://generativelanguage.googleapis.com/v1beta/openai/``, so the generic
OpenAI-compatible services work unchanged; this class only adds the key
metadata (default key name and where to create a key). The endpoint URL itself
is plain preset data in ``model_kwargs``.
"""

from typing import Optional

from backend.providers.base import Provider


class GoogleProvider(Provider):
    id = "google"
    label = "Google Gemini"
    key_scopes = frozenset({"user", "managed"})
    default_scope = "user"

    def default_key_env(self) -> Optional[str]:
        return "GEMINI_API_KEY"

    def key_docs_url(self, env_var: str) -> Optional[str]:
        if env_var == "GEMINI_API_KEY":
            return "https://aistudio.google.com/apikey"
        return super().key_docs_url(env_var)
