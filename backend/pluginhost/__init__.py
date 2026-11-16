"""Backend plugin host: the interface between core and backend plugins.

Plugin authors import from this package only (plus ``backend.providers`` for the
``Provider`` base class and its types). See docs/backend-plugins.md.
"""

from typing import Optional

from backend.pluginhost.capabilities import Capability, InferenceProvider, LocalInference, PresetDirectory
from backend.pluginhost.host import (
    CORE_PLUGIN_ID,
    SHIPPED_PLUGIN_DIR,
    PluginHost,
    PluginRecord,
    configured_plugin_dirs,
    get_plugin_host,
)
from backend.pluginhost.plugin import BackendPlugin


class LocalEngineUnavailable(RuntimeError):
    """No local inference engine can serve a side (none installed, or the named one is missing)."""


def local_engines(side: str) -> list[LocalInference]:
    """Local inference engines registered for ``side``, best first (available, then priority)."""
    engines = [c for c in get_plugin_host().capabilities(LocalInference.kind) if c.side == side]
    return sorted(engines, key=lambda c: (c.unavailable_reason() is not None, -c.priority))


def select_local_engine(side: str, engine: Optional[str] = None) -> LocalInference:
    """The engine that runs ``side`` of a local preset section.

    ``engine`` (the side's ``engine`` key) names one; otherwise the best available
    engine wins. When none is available the highest-priority one is returned
    anyway, so its own error (for example a missing package) reaches the user.

    Raises:
        LocalEngineUnavailable: no plugin offers local inference for ``side``, or
            none offers the named engine.
    """
    engines = local_engines(side)
    if engine:
        for cap in engines:
            if cap.engine == engine:
                return cap
        known = ", ".join(sorted({c.engine for c in engines})) or "none"
        raise LocalEngineUnavailable(
            f"no backend plugin provides the local engine {engine!r} for the {side} side (installed: {known})"
        )
    if not engines:
        raise LocalEngineUnavailable(
            f"no backend plugin provides local inference for the {side} side; "
            "install one (for example the shipped local_torch plugin) or use a remote preset"
        )
    return engines[0]


__all__ = [
    "BackendPlugin",
    "CORE_PLUGIN_ID",
    "Capability",
    "InferenceProvider",
    "LocalEngineUnavailable",
    "LocalInference",
    "PluginHost",
    "PluginRecord",
    "PresetDirectory",
    "SHIPPED_PLUGIN_DIR",
    "configured_plugin_dirs",
    "get_plugin_host",
    "local_engines",
    "select_local_engine",
]
