"""Test-only backend plugin: fake providers and presets for core tests.

The providers (see ``provider.py``) stand in for the shapes of real vendors: a free
per-user-key service, a paid one, a login-session service and a provisioned cloud.
The presets in ``presets/`` combine them the way the vendor plugins' presets do.
"""

from pathlib import Path

from backend.pluginhost import BackendPlugin, InferenceProvider, PresetDirectory

from .provider import TestCloudProvider, TestMessagesProvider, TestOtherProvider, TestSessionProvider, TestVendorProvider


def register() -> BackendPlugin:
    return BackendPlugin(
        id="testvendor",
        name="Test vendor (core tests only)",
        version="1.0",
        description="Fake providers and presets so core tests need no vendor plugin.",
        capabilities=[
            InferenceProvider(provider=TestVendorProvider),
            InferenceProvider(provider=TestOtherProvider),
            InferenceProvider(provider=TestMessagesProvider),
            InferenceProvider(provider=TestSessionProvider),
            InferenceProvider(provider=TestCloudProvider),
            PresetDirectory(id="testvendor", path=Path(__file__).parent / "presets"),
        ],
    )
