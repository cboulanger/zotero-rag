"""Backend plugin: the RunPod inference provider.

Serverless endpoints in each user's own RunPod account, provisioned on demand.
"""

from backend.pluginhost import BackendPlugin, InferenceProvider

from .provider import RunPodProvider


def register() -> BackendPlugin:
    return BackendPlugin(
        id="runpod",
        name="RunPod",
        version="1.0",
        description="Serverless endpoints in each user's own RunPod account, provisioned on demand.",
        capabilities=[InferenceProvider(provider=RunPodProvider)],
    )
