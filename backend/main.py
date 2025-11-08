"""
FastAPI application entry point for Zotero RAG backend.
"""

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from backend.config.settings import get_settings
from backend.api import config, libraries, indexing, query

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    settings = get_settings()
    logger.info(f"Starting Zotero RAG backend v{settings.version}")
    logger.info(f"Using preset: {settings.model_preset}")
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
