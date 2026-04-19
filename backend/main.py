"""
FastAPI application entry point for Zotero RAG backend.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import httpx
import logging

from backend.__version__ import __version__
from backend.config.settings import get_settings
from backend.db.vector_store import VectorStore
from backend.dependencies import make_vector_store
from backend.api import config, libraries, indexing, query, document_upload

# Get settings to access log configuration
settings = get_settings()

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
    file_handler = logging.FileHandler(settings.log_file, encoding='utf-8')
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
logging.getLogger("httpcore").setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.INFO)
logging.getLogger("urllib3").setLevel(logging.INFO)
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

    # Open a single VectorStore for the lifetime of the process.
    # Sharing one Qdrant client across all requests avoids the lock-file
    # contention that causes BlockingIOError when requests overlap.
    try:
        vector_store = make_vector_store()
        app.state.vector_store = vector_store
        logger.info("VectorStore singleton initialised")
    except Exception as e:
        logger.error(f"Failed to initialise VectorStore: {e}")
        app.state.vector_store = None

    yield

    logger.info("Shutting down Zotero RAG backend")
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

# API key authentication middleware
# Exempt health-check / version endpoints so the plugin can discover the backend
# without needing credentials first.
_AUTH_EXEMPT_PATHS = {"/", "/health", "/api/version"}


@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    """Validate X-API-Key when api_key is configured.

    Accepts the key via:
    - X-API-Key request header  (all endpoints)
    - ?api_key= query parameter (SSE endpoints where EventSource cannot set headers)

    OPTIONS requests (CORS preflight) and health/version endpoints are always
    allowed so the browser can complete the preflight handshake and the plugin
    can discover the backend without credentials.
    """
    if (
        settings.api_key
        and request.method != "OPTIONS"
        and request.url.path not in _AUTH_EXEMPT_PATHS
    ):
        header_key = request.headers.get("X-API-Key")
        query_key = request.query_params.get("api_key")
        if header_key != settings.api_key and query_key != settings.api_key:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
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


@app.get("/")
async def root(request: Request):
    """Root endpoint — service info, preset config, and vector store statistics."""
    preset = settings.get_hardware_preset()
    vector_store: VectorStore = request.app.state.vector_store

    embedding_cfg = preset.embedding
    llm_cfg = preset.llm
    rag_cfg = preset.rag

    try:
        db_stats = vector_store.get_collection_info()
    except Exception as exc:
        db_stats = {"error": str(exc)}

    return {
        "service": "Zotero RAG API",
        "version": __version__,
        "status": "running",
        "preset": {
            "name": preset.name,
            "description": preset.description,
            "memory_budget_gb": preset.memory_budget_gb,
        },
        "embedding": {
            "model_type": embedding_cfg.model_type,
            "model_name": embedding_cfg.model_name,
            "base_url": embedding_cfg.model_kwargs.get("base_url"),
            "embedding_dim": db_stats.get("embedding_dim"),
            "distance": db_stats.get("distance"),
        },
        "llm": {
            "model_type": llm_cfg.model_type,
            "model_name": llm_cfg.model_name,
            "base_url": llm_cfg.model_kwargs.get("base_url"),
            "max_context_length": llm_cfg.max_context_length,
            "temperature": llm_cfg.temperature,
        },
        "rag": {
            "top_k": rag_cfg.top_k,
            "score_threshold": rag_cfg.score_threshold,
            "max_chunk_size": rag_cfg.max_chunk_size,
        },
        "vector_db": {
            "path": str(vector_store.storage_path),
            "chunks": db_stats.get("chunks_count"),
            "indexed_documents": db_stats.get("dedup_count"),
            "libraries": db_stats.get("metadata_count"),
        },
    }


@app.get("/health")
async def health_check():
    """Health check endpoint — includes kreuzberg sidecar reachability."""
    kreuzberg_url = settings.kreuzberg_url
    kreuzberg_status = "unknown"
    kreuzberg_error = None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{kreuzberg_url.rstrip('/')}/health")
            kreuzberg_status = "ok" if resp.is_success else f"http_{resp.status_code}"
    except httpx.ConnectError as exc:
        kreuzberg_status = "unreachable"
        kreuzberg_error = str(exc)
    except httpx.TimeoutException:
        kreuzberg_status = "timeout"
    except Exception as exc:
        kreuzberg_status = "error"
        kreuzberg_error = str(exc)

    return {
        "status": "healthy",
        "kreuzberg": {
            "url": kreuzberg_url,
            "status": kreuzberg_status,
            **({"error": kreuzberg_error} if kreuzberg_error else {}),
        },
    }
