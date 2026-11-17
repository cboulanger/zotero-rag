"""Discovers, loads and keeps the backend plugins and the capabilities they register.

Plugins live in plugin directories: ``backend/plugins/`` (shipped with the
project) first, then every directory named in ``BACKEND_PLUGIN_DIRS`` (separated
by ``os.pathsep``: ``:`` on Linux/macOS, ``;`` on Windows). Each subdirectory with
an ``__init__.py`` is one plugin; its ``register()`` returns a
:class:`~backend.pluginhost.plugin.BackendPlugin`. ``DISABLED_BACKEND_PLUGINS``
(comma-separated plugin ids) skips plugins without deleting them.

A plugin that fails to import or register, or that reuses an id that is already
taken, is skipped and reported (log and ``/status``); it never stops the backend.
The core's own generic OpenAI-compatible provider is registered as the built-in
plugin ``core``.

Loading happens once per process, on first use (``get_plugin_host().load()``,
also called by ``backend.providers.discover_providers``).
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import re
import sys
import threading
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from backend.pluginhost.capabilities import Capability, InferenceProvider
from backend.pluginhost.plugin import BackendPlugin

logger = logging.getLogger(__name__)

#: Plugins shipped with the project.
SHIPPED_PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"
#: Package that external plugins are imported under (``zotero_rag_plugins.<dir>``).
EXTERNAL_PACKAGE = "zotero_rag_plugins"
#: Id of the pseudo-plugin holding the capabilities core implements itself.
CORE_PLUGIN_ID = "core"

_PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


@dataclass
class PluginRecord:
    """What the host knows about one plugin, loaded or not."""

    id: str
    source: str
    status: str  # "loaded", "failed" or "disabled"
    plugin: Optional[BackendPlugin] = None
    error: Optional[str] = None
    builtin: bool = False
    capabilities: list[Capability] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "source": self.source,
            "builtin": self.builtin,
        }
        if self.plugin is not None:
            info.update(name=self.plugin.name, version=self.plugin.version, description=self.plugin.description)
        if self.error:
            info["error"] = self.error
        info["capabilities"] = [f"{c.kind}:{c.id}" for c in self.capabilities]
        return info


def _split_list(value: str, sep: str) -> list[str]:
    return [part.strip() for part in (value or "").split(sep) if part.strip()]


def configured_plugin_dirs() -> list[Path]:
    """The shipped plugin directory, then those named in ``BACKEND_PLUGIN_DIRS``."""
    dirs = [SHIPPED_PLUGIN_DIR]
    for raw in _split_list(_setting("backend_plugin_dirs", "BACKEND_PLUGIN_DIRS"), os.pathsep):
        path = Path(raw).expanduser().resolve()
        if path not in dirs:
            dirs.append(path)
    return dirs


def disabled_plugin_ids() -> set[str]:
    return set(_split_list(_setting("disabled_backend_plugins", "DISABLED_BACKEND_PLUGINS"), ","))


def _setting(attr: str, env: str) -> str:
    """A plugin setting from the application settings (``.env`` included), else the environment.

    Reads a fresh ``Settings()`` rather than ``get_settings()``: creating the global
    settings seeds the bundled presets, which needs the plugins being loaded here.
    """
    try:
        from backend.config.settings import Settings

        value = getattr(Settings(), attr, None)
        if value is not None:
            return str(value)
    except Exception:  # pragma: no cover - settings unavailable (e.g. a broken .env)
        pass
    return os.environ.get(env, "")


class PluginHost:
    """The registry of loaded plugins and their capabilities."""

    def __init__(self, dirs: Optional[Iterable[Path]] = None, disabled: Optional[Iterable[str]] = None) -> None:
        self._dirs = list(dirs) if dirs is not None else None
        self._disabled = set(disabled) if disabled is not None else None
        self._records: dict[str, PluginRecord] = {}
        self._failed: list[PluginRecord] = []
        self._capabilities: dict[tuple[str, str], Capability] = {}
        self._loaded = False
        self._lock = threading.RLock()
        self._started: list[BackendPlugin] = []

    # -- loading ----------------------------------------------------------------

    def load(self) -> "PluginHost":
        """Load every plugin once (idempotent, thread-safe)."""
        with self._lock:
            if self._loaded:
                return self
            self._loaded = True
            self._register_core()
            dirs = self._dirs if self._dirs is not None else configured_plugin_dirs()
            disabled = self._disabled if self._disabled is not None else disabled_plugin_ids()
            for directory in dirs:
                self._load_directory(Path(directory), disabled)
            loaded = [r.id for r in self._records.values() if r.status == "loaded" and not r.builtin]
            logger.info("Backend plugins loaded: %s", ", ".join(loaded) or "none")
            return self

    def _register_core(self) -> None:
        from backend.providers.base import Provider

        plugin = BackendPlugin(
            id=CORE_PLUGIN_ID,
            name="Core",
            description="Built-in capabilities: the generic provider for any OpenAI-compatible endpoint.",
            capabilities=[InferenceProvider(provider=Provider, description="Any OpenAI-compatible HTTP endpoint (local or remote).")],
        )
        self.add(plugin, source="builtin", builtin=True)

    def _load_directory(self, directory: Path, disabled: set[str]) -> None:
        if not directory.is_dir():
            logger.warning("Backend plugin directory %s does not exist; skipped", directory)
            return
        shipped = directory.resolve() == SHIPPED_PLUGIN_DIR
        for entry in sorted(directory.iterdir()):
            if not entry.is_dir() or entry.name.startswith(("_", ".")) or not (entry / "__init__.py").is_file():
                continue
            if entry.name in disabled:
                self._failed.append(PluginRecord(id=entry.name, source=str(entry), status="disabled"))
                continue
            try:
                module = self._import(entry, shipped)
                register = getattr(module, "register", None)
                if not callable(register):
                    raise TypeError("the package defines no register() function")
                plugin = register()
                if not isinstance(plugin, BackendPlugin):
                    raise TypeError(f"register() returned {type(plugin).__name__}, not BackendPlugin")
                if plugin.id in disabled:
                    self._failed.append(PluginRecord(id=plugin.id, source=str(entry), status="disabled", plugin=plugin))
                    continue
                self.add(plugin, source=str(entry))
            except Exception as exc:
                logger.error("Backend plugin %s could not be loaded: %s", entry, exc, exc_info=True)
                self._failed.append(PluginRecord(id=entry.name, source=str(entry), status="failed", error=f"{type(exc).__name__}: {exc}"))

    @staticmethod
    def _import(entry: Path, shipped: bool) -> types.ModuleType:
        if shipped:
            return importlib.import_module(f"backend.plugins.{entry.name}")
        if EXTERNAL_PACKAGE not in sys.modules:
            parent = types.ModuleType(EXTERNAL_PACKAGE)
            parent.__path__ = []  # a namespace for the external plugins
            sys.modules[EXTERNAL_PACKAGE] = parent
        name = f"{EXTERNAL_PACKAGE}.{re.sub(r'[^0-9A-Za-z_]', '_', entry.name)}"
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.spec_from_file_location(name, entry / "__init__.py", submodule_search_locations=[str(entry)])
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot import {entry}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
        return module

    def add(self, plugin: BackendPlugin, source: str = "", builtin: bool = False) -> PluginRecord:
        """Register ``plugin`` and its capabilities, all or nothing.

        Raises:
            ValueError: the plugin id is invalid or taken, or a capability's
                ``(kind, id)`` is already registered by another plugin.
        """
        with self._lock:
            if not _PLUGIN_ID_RE.match(plugin.id):
                raise ValueError(f"invalid plugin id {plugin.id!r} (lowercase letters, digits, '_', '-', '.')")
            if plugin.id in self._records:
                raise ValueError(f"plugin id {plugin.id!r} is already registered by {self._records[plugin.id].source}")
            seen: set[tuple[str, str]] = set()
            for cap in plugin.capabilities:
                if not isinstance(cap, Capability):
                    raise TypeError(f"{cap!r} is not a Capability")
                key = (cap.kind, cap.id)
                if key in self._capabilities or key in seen:
                    owner = self._capabilities[key].plugin_id if key in self._capabilities else plugin.id
                    raise ValueError(f"capability {cap.kind}:{cap.id} is already registered by plugin {owner!r}")
                seen.add(key)
            self._check_providers(plugin)
            record = PluginRecord(id=plugin.id, source=source, status="loaded", plugin=plugin, builtin=builtin)
            for cap in plugin.capabilities:
                cap.plugin_id = plugin.id
                self._capabilities[(cap.kind, cap.id)] = cap
                record.capabilities.append(cap)
                if isinstance(cap, InferenceProvider):
                    from backend.providers.base import register_provider

                    register_provider(cap.provider)
            self._records[plugin.id] = record
            return record

    @staticmethod
    def _check_providers(plugin: BackendPlugin) -> None:
        from backend.providers.base import PROVIDERS

        for cap in plugin.capabilities:
            if isinstance(cap, InferenceProvider):
                existing = PROVIDERS.get(cap.provider.id)
                if existing is not None and existing is not cap.provider:
                    raise ValueError(f"provider id {cap.provider.id!r} is already registered")

    # -- lookup -------------------------------------------------------------------

    def capabilities(self, kind: Optional[str] = None) -> list[Capability]:
        """Registered capabilities, optionally of one ``kind``, in registration order."""
        self.load()
        return [c for (k, _), c in self._capabilities.items() if kind is None or k == kind]

    def capability(self, kind: str, cap_id: str) -> Optional[Capability]:
        self.load()
        return self._capabilities.get((kind, cap_id))

    def plugins(self) -> list[PluginRecord]:
        """Every plugin the host saw: loaded ones first, then disabled and failed ones."""
        self.load()
        return [*self._records.values(), *self._failed]

    def describe(self) -> dict[str, Any]:
        """The status route's ``plugins`` and ``capabilities`` sections."""
        return {
            "plugin_dirs": [str(d) for d in (self._dirs if self._dirs is not None else configured_plugin_dirs())],
            "plugins": [r.describe() for r in self.plugins()],
            "capabilities": [c.describe() for c in self.capabilities()],
        }

    # -- lifecycle ------------------------------------------------------------------

    async def startup(self) -> None:
        """Run every loaded plugin's ``on_startup`` hook (failures are logged)."""
        self.load()
        for record in list(self._records.values()):
            plugin = record.plugin
            if plugin is None or plugin.on_startup is None:
                continue
            try:
                await plugin.on_startup()
                self._started.append(plugin)
            except Exception as exc:
                logger.warning("Startup of backend plugin %s failed: %s", plugin.id, exc)

    async def shutdown(self) -> None:
        """Run the ``on_shutdown`` hooks of the plugins that started, in reverse order."""
        for plugin in reversed(self._started):
            if plugin.on_shutdown is None:
                continue
            try:
                await plugin.on_shutdown()
            except Exception as exc:
                logger.warning("Shutdown of backend plugin %s failed: %s", plugin.id, exc)
        self._started.clear()


_host: Optional[PluginHost] = None
_host_lock = threading.Lock()


def get_plugin_host() -> PluginHost:
    """The process-wide plugin host (plugins load on first use)."""
    global _host
    with _host_lock:
        if _host is None:
            _host = PluginHost()
    return _host.load()
