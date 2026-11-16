"""What a backend plugin hands the host when it registers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from backend.pluginhost.capabilities import Capability

#: An async hook without arguments (see ``BackendPlugin.on_startup``).
Hook = Callable[[], Awaitable[None]]


@dataclass
class BackendPlugin:
    """One backend plugin: its identity, its capabilities and optional lifecycle hooks.

    A plugin is a Python package in a plugin directory whose ``__init__.py``
    defines ``register() -> BackendPlugin``. The host calls ``register()`` once at
    startup; the plugin may decide there which capabilities to offer (for example
    only those that can run on this platform).

    ``on_startup`` runs when the API server starts (after the vector store is
    open) and ``on_shutdown`` when it stops; a failing hook is logged and does not
    stop the server. Command-line tools load plugins without running the hooks.
    """

    id: str
    name: str
    version: str = "0"
    description: str = ""
    capabilities: list[Capability] = field(default_factory=list)
    on_startup: Optional[Hook] = None
    on_shutdown: Optional[Hook] = None
