"""Backend plugin: the Hugging Face inference provider.

Dedicated Hugging Face Inference Endpoints, provisioned on demand.
"""

from backend.pluginhost import BackendPlugin, InferenceProvider

from .provider import HuggingFaceProvider


def register() -> BackendPlugin:
    return BackendPlugin(
        id="huggingface",
        name="Hugging Face",
        version="1.0",
        description="Dedicated Hugging Face Inference Endpoints, provisioned on demand.",
        capabilities=[InferenceProvider(provider=HuggingFaceProvider)],
    )
