"""
FastAPI application entry point for Zotero RAG backend.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging
import asyncio

from backend.config.settings import get_settings
from backend.api import config, libraries, indexing, query

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

# Configure Uvicorn's access logger to use the same format as application logs
uvicorn_access_logger = logging.getLogger("uvicorn.access")
uvicorn_access_logger.handlers = []  # Remove default handlers
uvicorn_access_logger.propagate = True  # Use root logger's handlers and format

# Also configure uvicorn.error logger for consistency
uvicorn_error_logger = logging.getLogger("uvicorn.error")
uvicorn_error_logger.handlers = []
uvicorn_error_logger.propagate = True

logger = logging.getLogger(__name__)


async def check_zotero_connectivity():
    """
    Check connectivity to Zotero local API at startup.
    Logs a clear warning if connection fails.
    """
    import aiohttp

    zotero_url = settings.zotero_api_url
    logger.info(f"Checking Zotero API connectivity at {zotero_url}")

    try:
        async with aiohttp.ClientSession() as session:
            # Try to connect to Zotero's ping endpoint
            async with session.get(f"{zotero_url}/connector/ping", timeout=aiohttp.ClientTimeout(total=3)) as response:
                if response.status == 200:
                    logger.info(f"✓ Successfully connected to Zotero API at {zotero_url}")
                    return True
                else:
                    logger.warning(f"✗ Zotero API responded with status {response.status}")
    except aiohttp.ClientConnectorError as e:
        logger.warning("=" * 80)
        logger.warning("⚠ WARNING: Cannot connect to Zotero API!")
        logger.warning(f"⚠ Configured URL: {zotero_url}")
        logger.warning("⚠ ")
        logger.warning("⚠ Please ensure:")
        logger.warning("⚠ 1. Zotero is running")
        logger.warning("⚠ 2. HTTP server is enabled in Zotero preferences")
        logger.warning("⚠    (Zotero → Settings → Advanced → General → Enable HTTP server)")
        logger.warning("⚠ 3. The port in ZOTERO_API_URL (.env) matches Zotero's HTTP server port")
        logger.warning("⚠ ")
        logger.warning(f"⚠ Connection error: {e}")
        logger.warning("=" * 80)
        return False
    except asyncio.TimeoutError:
        logger.warning("=" * 80)
        logger.warning("⚠ WARNING: Zotero API connection timeout!")
        logger.warning(f"⚠ Configured URL: {zotero_url}")
        logger.warning("⚠ Zotero may be running but not responding.")
        logger.warning("=" * 80)
        return False
    except Exception as e:
        logger.warning(f"⚠ Unexpected error checking Zotero connectivity: {e}")
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    logger.info(f"Starting Zotero RAG backend v{settings.version}")
    logger.info(f"Using preset: {settings.model_preset}")
    if settings.log_file:
        logger.info(f"Logging to file: {settings.log_file}")

    # Check Zotero API connectivity
    await check_zotero_connectivity()

    yield
    logger.info("Shutting down Zotero RAG backend")


# Create FastAPI app
app = FastAPI(
    title="Zotero RAG API",
    description="RAG (Retrieval-Augmented Generation) API for Zotero libraries",
    version="0.1.0",
    lifespan=lifespan
)

# Configure CORS (allow requests from Zotero plugin)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Local-only, so accept all origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(config.router, prefix="/api", tags=["config"])
app.include_router(libraries.router, prefix="/api", tags=["libraries"])
app.include_router(indexing.router, prefix="/api", tags=["indexing"])
app.include_router(query.router, prefix="/api", tags=["query"])


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": "Zotero RAG API",
        "version": "0.1.0",
        "status": "running"
    }


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}
