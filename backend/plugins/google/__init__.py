"""Backend plugin: the Google Gemini inference provider.

Gemini models through Google's OpenAI-compatible endpoint.
"""

from backend.pluginhost import BackendPlugin, InferenceProvider

from .provider import GoogleProvider


def register() -> BackendPlugin:
    return BackendPlugin(
        id="google",
        name="Google Gemini",
        version="1.0",
        description="Gemini models through Google's OpenAI-compatible endpoint.",
        capabilities=[InferenceProvider(provider=GoogleProvider)],
    )
