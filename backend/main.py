"""
FastAPI application entry point for Zotero RAG backend.
"""

import asyncio
import contextlib
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx
import logging

from backend.__version__ import __version__
from backend.config.settings import get_settings
from backend.dependencies import make_vector_store, resolve_zotero_identity
from backend.services.access_gate import assert_safe_to_start
from backend.services.caller_context import use_caller
from backend.services.effective_preset import get_user_preset, standin_applies, use_preset
from backend.utils.log_rotation import RotatingLogHandler
from backend.api import status, config, libraries, indexing, query, document_upload, registration, rate_limits, public_query, autoindex, auth, migration, admin_settings, indexed_tags
from backend.api.document_upload import load_item_cache, save_item_cache

# Get settings to access log configuration
settings = get_settings()
assert_safe_to_start(settings)

# Configure logging with both console and file output
# Use UTF-8 encoding to handle Unicode characters in document titles/metadata
import sys
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.DEBUG)
console_handler.setFormatter(logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
))
# Ensure UTF-8 encoding for console output on Windows
if hasattr(console_handler.stream, 'reconfigure'):
    try:
        console_handler.stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass  # Ignore if reconfigure fails

handlers = [console_handler]

# Only add file handler if log_file is set and not empty
# Note: Path("") becomes Path(".") so we need to check for that too
log_file_str = str(settings.log_file).strip() if settings.log_file else ""
if log_file_str and log_file_str != ".":
    file_handler = RotatingLogHandler(settings.log_file)  # rotates on startup + daily
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    ))
    handlers.append(file_handler)

logging.basicConfig(
    level=getattr(logging, settings.log_level),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=handlers,
    force=True  # Override any existing configuration
)

# Suppress overly verbose third-party loggers
# Set them to INFO level even if our log level is DEBUG
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("bitsandbytes").setLevel(logging.INFO)
logging.getLogger("markdown_it").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)  # suppress verbose request/response dumps

# Configure Uvicorn's access logger to use the same format as application logs
uvicorn_access_logger = logging.getLogger("uvicorn.access")
uvicorn_access_logger.handlers = []  # Remove default handlers
uvicorn_access_logger.propagate = True  # Use root logger's handlers and format

# Also configure uvicorn.error logger for consistency
uvicorn_error_logger = logging.getLogger("uvicorn.error")
uvicorn_error_logger.handlers = []
uvicorn_error_logger.propagate = True

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    logger.info(f"Starting Zotero RAG backend v{settings.version}")
    logger.info(f"Using preset: {settings.model_preset}")
    if settings.log_file:
        logger.info(f"Logging to file: {settings.log_file}")

    # Encrypt any plaintext shared API keys left over in admin_settings.json.
    try:
        from backend.services.admin_settings_store import migrate_plaintext_secrets
        migrated = migrate_plaintext_secrets()
        if migrated:
            logger.info(f"Encrypted {migrated} plaintext shared API key(s) in admin_settings.json")
    except Exception as e:
        logger.warning(f"Could not migrate plaintext shared API keys: {e}")

    _cache_path = settings.data_path / "system" / "check_indexed_cache.json"
    load_item_cache(_cache_path)

    # Open a single VectorStore for the lifetime of the process.
    # Sharing one Qdrant client across all requests avoids the lock-file
    # contention that causes BlockingIOError when requests overlap.
    # Retry to handle the case where Qdrant starts after the app — common when
    # podman compose doesn't enforce the service_healthy dependency, or when
    # Qdrant takes >20 s to load existing collections (e.g. large datasets).
    # 20 retries × 5 s = 95 s total window, well above Qdrant's worst-case startup.
    _vs_retries = 20
    _vs_delay = 5.0
    for _attempt in range(1, _vs_retries + 1):
        try:
            vector_store = make_vector_store()
            app.state.vector_store = vector_store
            logger.info("VectorStore singleton initialised")
            break
        except Exception as e:
            if _attempt < _vs_retries:
                logger.warning(
                    f"VectorStore init attempt {_attempt}/{_vs_retries} failed: {e} — "
                    f"retrying in {_vs_delay:.0f}s"
                )
                await asyncio.sleep(_vs_delay)
            else:
                logger.error(f"Failed to initialise VectorStore after {_vs_retries} attempts: {e}")
                app.state.vector_store = None

    scheduler_task: Optional[asyncio.Task] = None
    if settings.autoindex_interval_minutes:
        from backend.services.autoindex_scheduler import run_scheduler_loop
        scheduler_task = asyncio.create_task(run_scheduler_loop(settings))
        app.state.autoindex_scheduler_task = scheduler_task
        logger.info(f"Auto-index scheduler started (every {settings.autoindex_interval_minutes} min)")

    health_check_task: Optional[asyncio.Task] = None
    if settings.health_check_interval_minutes and app.state.vector_store is not None:
        from backend.services.health_check import run_health_check_loop
        health_check_task = asyncio.create_task(run_health_check_loop(settings, app.state.vector_store))
        app.state.health_check_task = health_check_task
        logger.info(f"Health check scheduler started (every {settings.health_check_interval_minutes} min)")

    # Backend plugins: load them (each registers its capabilities) and run their startup hooks.
    from backend.pluginhost import get_plugin_host
    plugin_host = get_plugin_host()
    await plugin_host.startup()

    yield

    await plugin_host.shutdown()

    if scheduler_task is not None:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task

    if health_check_task is not None:
        health_check_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await health_check_task

    logger.info("Shutting down Zotero RAG backend")
    save_item_cache(_cache_path)
    if getattr(app.state, "vector_store", None) is not None:
        app.state.vector_store.close()
        logger.info("VectorStore closed")


# Create FastAPI app
app = FastAPI(
    title="Zotero RAG API",
    description="RAG (Retrieval-Augmented Generation) API for Zotero libraries",
    version=__version__,
    lifespan=lifespan
)

# Global handler: log every unhandled exception before FastAPI returns 500.
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception(f"Unhandled exception on {request.method} {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


# Zotero-key identity middleware
# Resolves and gates the caller's identity for every /api/* request — see
# docs/history/plan-zotero-key-auth.md for the design. Exempt health-check /
# version endpoints so the plugin can discover the backend without needing
# credentials first, and /public/* which is intentionally unauthenticated.
_AUTH_EXEMPT_PATHS = {"/", "/health", "/api/version"}


@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    """Resolve and gate the caller's Zotero identity for every /api/* request.

    Loopback deployments (api_host in {localhost, 127.0.0.1}) skip this
    entirely (Part 4) — there is exactly one trusted local user. Everywhere
    else the caller must present a valid, gate-approved Zotero API key
    (X-Zotero-API-Key). The resolved identity (or None for loopback) is
    stashed on request.state.zotero_identity so downstream route dependencies
    (backend.dependencies.get_zotero_identity) don't re-validate.

    OPTIONS requests (CORS preflight) are always allowed so the browser can
    complete the preflight handshake.
    """
    request.state.zotero_identity = None
    if (
        request.method != "OPTIONS"
        and request.url.path.startswith("/api/")
        and request.url.path not in _AUTH_EXEMPT_PATHS
    ):
        try:
            request.state.zotero_identity = await resolve_zotero_identity(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        # Run the whole request on the caller's own preset (a stand-in on the routes that allow
        # one, else their choice, else the default). A loopback caller can have a stand-in too.
        # Only a non-default preset is bound; everyone else follows the live default.
        preset = await asyncio.to_thread(
            get_user_preset, get_settings(), request.state.zotero_identity,
            allow_standin=standin_applies(request.url.path),
        )
        identity = request.state.zotero_identity
        with use_preset(preset), use_caller(identity.user_id if identity is not None else None):
            return await call_next(request)
    return await call_next(request)


# Configure CORS (allow requests from Zotero plugin)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(config.router, prefix="/api", tags=["config"])
app.include_router(libraries.router, prefix="/api", tags=["libraries"])
app.include_router(indexing.router, prefix="/api", tags=["indexing"])
app.include_router(query.router, prefix="/api", tags=["query"])
app.include_router(document_upload.router, prefix="/api", tags=["document-upload"])
app.include_router(registration.router, prefix="/api", tags=["registration"])
app.include_router(rate_limits.router, prefix="/api", tags=["rate-limits"])
app.include_router(public_query.router, tags=["public"])
app.include_router(autoindex.router, prefix="/api", tags=["autoindex"])
app.include_router(admin_settings.router, prefix="/api", tags=["admin-settings"])
app.include_router(auth.router, prefix="/api", tags=["auth"])
app.include_router(indexed_tags.router, prefix="/api", tags=["indexed-tags"])
app.include_router(migration.router, prefix="/api", tags=["migration"])
app.include_router(status.router, tags=["status"])


async def _check_http(client: httpx.AsyncClient, url: str) -> tuple[str, str | None]:
    """Probe a URL and return (status, error_or_None)."""
    try:
        resp = await client.get(url)
        return ("ok" if resp.is_success else f"http_{resp.status_code}", None)
    except httpx.ConnectError as exc:
        return ("unreachable", str(exc))
    except httpx.TimeoutException:
        return ("timeout", None)
    except Exception as exc:
        return ("error", str(exc))


@app.get("/health")
async def health_check():
    """Health check endpoint — includes kreuzberg and Qdrant sidecar reachability."""
    kreuzberg_url = settings.kreuzberg_url
    qdrant_url = settings.qdrant_url

    async with httpx.AsyncClient(timeout=3.0) as client:
        kreuzberg_status, kreuzberg_error = await _check_http(
            client, f"{kreuzberg_url.rstrip('/')}/health"
        )
        if qdrant_url:
            qdrant_status, qdrant_error = await _check_http(client, qdrant_url)
        else:
            qdrant_status, qdrant_error = "local-mode", None

    result: dict = {
        "status": "healthy",
        "kreuzberg": {
            "url": kreuzberg_url,
            "status": kreuzberg_status,
            **({"error": kreuzberg_error} if kreuzberg_error else {}),
        },
        "qdrant": {
            "url": qdrant_url or "local",
            "status": qdrant_status,
            **({"error": qdrant_error} if qdrant_error else {}),
        },
    }
    return result
