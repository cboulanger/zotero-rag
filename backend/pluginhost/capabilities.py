"""Capabilities a backend plugin can contribute.

A capability is one thing a plugin adds to the backend. Core asks the plugin host
for the capabilities of a ``kind`` and uses them through the interface that kind
defines; it never imports a plugin module. Two kinds exist today:

- ``inference.provider`` (:class:`InferenceProvider`): a ``Provider`` subclass
  (``backend.providers.base``) that serves remote sides of a preset, selected by
  ``provider.id`` in the preset JSON.
- ``inference.local`` (:class:`LocalInference`): an in-process inference engine for
  one side of a preset whose ``model_type`` is ``local`` (for example sentence-
  transformers on torch), selected by the side's ``engine`` key or, when it names
  none, by availability and ``priority`` on this machine.

- ``presets`` (:class:`PresetDirectory`): a directory of bundled preset JSON files
  that need the plugin's providers; they are seeded into ``<data_path>/presets``
  next to core's provider-free presets only while the plugin is loaded.

A new kind is a new :class:`Capability` subclass with its own ``kind`` string;
the host stores and lists capabilities of any kind, so adding one needs no change
to the loader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from backend.providers.base import Provider
    from backend.providers.types import Side


@dataclass
class Capability:
    """Base of every capability. ``id`` is unique within its ``kind``."""

    kind: ClassVar[str] = "capability"

    id: str
    label: str = ""
    description: str = ""
    #: Filled in by the host: the id of the plugin that registered this capability.
    plugin_id: str = field(default="", init=False)

    def unavailable_reason(self) -> Optional[str]:
        """Why this capability cannot be used on this machine right now, or None when it can.

        A capability that is registered but unavailable is still listed by
        ``/status`` (with this reason), so an admin sees what is missing.
        """
        return None

    def describe(self) -> dict[str, Any]:
        """JSON-safe description for the status route. Never holds secrets."""
        reason = self.unavailable_reason()
        return {
            "kind": self.kind,
            "id": self.id,
            "label": self.label or self.id,
            "description": self.description,
            "plugin": self.plugin_id,
            "available": reason is None,
            **({"unavailable_reason": reason} if reason else {}),
        }


@dataclass
class InferenceProvider(Capability):
    """A provider class for the remote sides of a preset (``provider.id`` in the preset)."""

    kind: ClassVar[str] = "inference.provider"

    id: str = ""
    provider: Optional[type["Provider"]] = None

    def __post_init__(self) -> None:
        if self.provider is None:
            raise ValueError("InferenceProvider needs a provider class")
        self.id = self.id or self.provider.id
        self.label = self.label or self.provider.label
        if self.id != self.provider.id:
            raise ValueError(f"InferenceProvider id {self.id!r} differs from its provider's id {self.provider.id!r}")

    @property
    def sides(self) -> tuple[str, ...]:
        from backend.providers.registry import supported_sides

        return tuple(sorted(supported_sides(self.provider)))

    def describe(self) -> dict[str, Any]:
        return {
            **super().describe(),
            "sides": list(self.sides),
            "key_scopes": sorted(self.provider.key_scopes),
            "cost": self.provider.cost,
            "supports_provisioning": self.provider.supports_provisioning,
        }


@dataclass
class LocalInference(Capability):
    """An in-process inference engine for one side of a ``local`` preset section.

    ``create`` builds the side's service; its arguments depend on the side:

    - ``embedding``: ``create(config, cache_dir=None, hf_token=None)`` returns an
      ``backend.services.embeddings.EmbeddingService``.
    - ``llm``: ``create(settings, cache_dir=None, hf_token=None, model_name_override=None)``
      returns an ``backend.services.llm.LLMService``.
    - ``rerank``: ``create(config)`` returns a ``backend.services.rerank.Reranker``.

    ``engine`` names the engine in a preset (``"engine": "torch"``); several plugins
    may offer the same side with different engines (torch, mlx, ...). ``priority``
    orders the available engines when a preset names none (higher first).
    ``requires`` lists importable module names the engine needs; ``platforms``
    restricts it to ``sys.platform`` values (empty means any).
    """

    kind: ClassVar[str] = "inference.local"

    id: str = ""
    side: Optional["Side"] = None
    engine: str = ""
    create: Optional[Callable[..., Any]] = None
    priority: int = 0
    requires: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    #: Optional extra check, returning a reason when the engine cannot run here.
    check: Optional[Callable[[], Optional[str]]] = None

    def __post_init__(self) -> None:
        if self.side is None or not self.engine or self.create is None:
            raise ValueError("LocalInference needs side, engine and create")
        self.id = self.id or f"{self.engine}:{self.side}"

    def unavailable_reason(self) -> Optional[str]:
        import importlib.util
        import sys

        if self.platforms and sys.platform not in self.platforms:
            return f"runs only on {', '.join(self.platforms)} (this is {sys.platform})"
        missing = [m for m in self.requires if importlib.util.find_spec(m) is None]
        if missing:
            return f"missing Python package(s): {', '.join(missing)}"
        if self.check is not None:
            return self.check()
        return None

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "side": self.side, "engine": self.engine, "priority": self.priority}


@dataclass
class PresetDirectory(Capability):
    """Bundled presets (``*.json``) that come with a plugin, seeded like core's own.

    A preset belongs to the plugin whose providers it needs, so removing the plugin
    also stops shipping presets that could not run without it.
    """

    kind: ClassVar[str] = "presets"

    id: str = ""
    path: Any = None

    def __post_init__(self) -> None:
        from pathlib import Path

        if self.path is None:
            raise ValueError("PresetDirectory needs a path")
        self.path = Path(self.path)
        self.id = self.id or self.path.parent.name

    def files(self) -> list:
        return sorted(self.path.glob("*.json")) if self.path.is_dir() else []

    def unavailable_reason(self) -> Optional[str]:
        return None if self.path.is_dir() else f"preset directory {self.path} does not exist"

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "presets": [f.stem for f in self.files()]}
