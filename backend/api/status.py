"""Unauthenticated service routes: ``/status`` (service, plugins, capabilities) and ``/sides``.

Both serve the server's default preset, like ``/health``; they never carry
secrets. ``/`` only points to them.
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Request

from backend.__version__ import __version__
from backend.config.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


def _db_stats(request: Request) -> dict:
    vector_store = getattr(request.app.state, "vector_store", None)
    if vector_store is None:
        return {"error": "vector store not initialised"}
    try:
        return vector_store.get_collection_info()
    except Exception as exc:
        return {"error": str(exc)}


@router.get("/")
def root() -> dict:
    """Service name and version, and where the details are."""
    return {
        "service": "Zotero RAG API",
        "version": __version__,
        "status": "/status",
        "sides": "/sides",
        "health": "/health",
        "docs": "/docs",
    }


@router.get("/status")
def status(request: Request) -> dict:
    """Service state: version, preset, vector store, auto-indexing, and the loaded
    backend plugins with the capabilities they registered."""
    from backend.pluginhost import get_plugin_host

    settings = get_settings()
    preset = settings.get_hardware_preset()
    vector_store = getattr(request.app.state, "vector_store", None)
    db_stats = _db_stats(request)
    rag_cfg = preset.rag
    response: dict[str, Any] = {
        "service": "Zotero RAG API",
        "version": __version__,
        "status": "running",
        "preset": {
            "name": preset.name,
            "description": preset.description,
            "memory_budget_gb": preset.memory_budget_gb,
        },
        "rag": {
            "top_k": rag_cfg.top_k,
            "score_threshold": rag_cfg.score_threshold,
            "max_chunk_size": rag_cfg.max_chunk_size,
        },
        "vector_db": {
            "path": str(vector_store.storage_path) if vector_store is not None else None,
            "chunks": db_stats.get("chunks_count"),
            "indexed_documents": db_stats.get("dedup_count"),
            "libraries_count": db_stats.get("metadata_count"),
        },
        "cron_indexing": _cron_indexing(settings),
    }
    response.update(get_plugin_host().describe())
    return response


def _cron_indexing(settings) -> dict:
    """Whether auto-indexing is enabled - the only auto-index detail on this
    unauthenticated route. Key counts and live progress are on the authenticated
    ``GET /api/autoindex/status``."""
    from backend.services.autoindex_scheduler import read_scheduler_state
    from backend.services.secret_store import get_key_store

    try:
        enabled = get_key_store().enabled
    except Exception as exc:
        logger.warning("Failed to read auto-index key store: %s", exc)
        enabled = False
    return {
        "enabled": enabled,
        "scheduler": {
            "active": bool(settings.autoindex_interval_minutes),
            "interval_minutes": settings.autoindex_interval_minutes,
            "paused": read_scheduler_state(settings.data_path).get("paused", False),
        },
    }


def _served_by(preset, side: str, cfg) -> dict:
    """Which capability serves a side: the provider (remote) or the local engine."""
    from backend.pluginhost import LocalEngineUnavailable, get_plugin_host, select_local_engine
    from backend.providers import get_provider_or_none

    if cfg.model_type == "local":
        try:
            engine = select_local_engine(side, cfg.engine)
        except LocalEngineUnavailable as exc:
            return {"capability": None, "plugin": None, "available": False, "unavailable_reason": str(exc)}
        reason = engine.unavailable_reason()
        return {
            "capability": f"{engine.kind}:{engine.id}", "plugin": engine.plugin_id, "engine": engine.engine,
            "available": reason is None, **({"unavailable_reason": reason} if reason else {}),
        }
    provider = get_provider_or_none(preset, side)
    if provider is None:
        return {"capability": None, "plugin": None, "provider": cfg.provider.id, "available": False,
                "unavailable_reason": f"no loaded backend plugin provides {cfg.provider.id!r} for this side"}
    cap = get_plugin_host().capability("inference.provider", provider.id)
    return {
        "capability": f"inference.provider:{provider.id}", "plugin": cap.plugin_id if cap else None,
        "provider": provider.id, "scope": provider.scope, "available": True,
    }


def _side_info(preset, side: str, cfg, db_stats: dict) -> dict:
    info: dict[str, Any] = {
        "side": side,
        "model_type": cfg.model_type,
        "model_name": cfg.model_name,
        "base_url": cfg.model_kwargs.get("base_url"),
        "served_by": _served_by(preset, side, cfg),
    }
    if side == "embedding":
        info.update(embedding_dim=db_stats.get("embedding_dim"), distance=db_stats.get("distance"))
    elif side == "llm":
        info.update(max_context_length=cfg.max_context_length, temperature=cfg.temperature)
    elif side == "rerank":
        info.update(pool_size=cfg.pool_size, timeout_seconds=cfg.timeout_seconds)
    return info


@router.get("/sides")
def sides(request: Request) -> dict:
    """The default preset's sides (embedding, answering, optional re-ranking): model,
    endpoint and the plugin capability serving each."""
    from backend.providers import preset_sides, side_config

    preset = get_settings().get_hardware_preset()
    db_stats = _db_stats(request)
    result: dict[str, Optional[dict]] = {}
    for side in preset_sides(preset):
        result[side] = _side_info(preset, side, side_config(preset, side), db_stats)
    return {"preset": preset.name, "sides": result}
