"""Backend plugin: the Anthropic inference provider.

Claude models over the Anthropic Messages API (answering side only).
"""

from backend.pluginhost import BackendPlugin, InferenceProvider

from .provider import AnthropicProvider


def register() -> BackendPlugin:
    return BackendPlugin(
        id="anthropic",
        name="Anthropic",
        version="1.0",
        description="Claude models over the Anthropic Messages API (answering side only).",
        capabilities=[InferenceProvider(provider=AnthropicProvider)],
    )
