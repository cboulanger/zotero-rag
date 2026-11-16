"""Backend plugin: the KISSKI / SAIA inference provider.

GWDG Academic Cloud Chat-AI: live model list, per-user keys and quotas.
"""

from backend.pluginhost import BackendPlugin, InferenceProvider

from .provider import KisskiProvider


def register() -> BackendPlugin:
    return BackendPlugin(
        id="kisski",
        name="KISSKI / SAIA",
        version="1.0",
        description="GWDG Academic Cloud Chat-AI: live model list, per-user keys and quotas.",
        capabilities=[InferenceProvider(provider=KisskiProvider)],
    )
