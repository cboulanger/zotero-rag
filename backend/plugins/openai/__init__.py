"""Backend plugin: the OpenAI inference provider.

The OpenAI API with a per-user or institution-managed key.
"""

from backend.pluginhost import BackendPlugin, InferenceProvider

from .provider import OpenAIProvider


def register() -> BackendPlugin:
    return BackendPlugin(
        id="openai",
        name="OpenAI",
        version="1.0",
        description="The OpenAI API with a per-user or institution-managed key.",
        capabilities=[InferenceProvider(provider=OpenAIProvider)],
    )
