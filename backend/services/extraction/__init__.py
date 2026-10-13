"""
Document extraction + chunking adapter package.

Provides a `DocumentExtractor` abstraction over different backends so the
extraction strategy can be changed via configuration without modifying
the document processing pipeline.

Available backends:
  - "kreuzberg"  KreuzbergExtractor  (default) — Rust-core, 91+ formats, OCR
  - "docling"    DoclingExtractor    — a remote Docling service (docling-serve or KISSKI/SAIA)
  - "legacy"     LegacyExtractor     — original pypdf + spaCy pipeline

Which one indexes a document: the preset's ``extraction`` section when it has
one, else ``Settings.extractor_backend`` (see ``create_configured_extractor``).
"""

from typing import Any, Optional

from backend.config.presets import ExtractionConfig
from backend.services.extraction.base import DocumentExtractor, ExtractionChunk
from backend.services.extraction.docling import DoclingExtractor
from backend.services.extraction.legacy import LegacyExtractor
from backend.services.extraction.kreuzberg import KreuzbergExtractor

__all__ = [
    "DocumentExtractor",
    "ExtractionChunk",
    "DoclingExtractor",
    "LegacyExtractor",
    "KreuzbergExtractor",
    "create_configured_extractor",
    "create_document_extractor",
    "effective_extractor_backend",
]

BACKENDS = ("kreuzberg", "docling", "legacy")


def create_document_extractor(
    backend: str = "kreuzberg",
    max_chunk_size: int = 512,
    chunk_overlap: int = 50,
    ocr_enabled: bool = True,
    kreuzberg_url: str = "http://localhost:8100",
    kreuzberg_timeout_cap: int = 1800,
    kreuzberg_max_content_bytes: Optional[int] = None,
    kreuzberg_connect_retry_seconds: int = 600,
    docling_options: Optional[dict[str, Any]] = None,
) -> DocumentExtractor:
    """
    Factory: create a DocumentExtractor for the named backend.

    Args:
        backend: One of "kreuzberg", "docling" or "legacy".
        max_chunk_size: Maximum characters per chunk.
        chunk_overlap: Overlap between consecutive chunks.
        ocr_enabled: Whether to enable OCR (Kreuzberg, docling-serve).
        kreuzberg_url: Base URL of the kreuzberg sidecar (kreuzberg backend only).
        kreuzberg_timeout_cap: Upper bound (seconds) for the per-request timeout
            computed from document size (kreuzberg and docling).
        kreuzberg_max_content_bytes: Hard cap on bytes sent in one request;
            None means no cap (kreuzberg and docling).
        kreuzberg_connect_retry_seconds: How long to retry a connection failure
            before giving up (kreuzberg and docling) — see
            KreuzbergExtractor.__init__'s connect_retry_budget_seconds.
        docling_options: Keyword arguments for DoclingExtractor (``url``,
            ``api_flavor``, ``api_key``, ``min_request_interval``, ...); they
            take precedence over the shared arguments above.

    Returns:
        Configured DocumentExtractor instance.

    Raises:
        ValueError: If the backend name is not recognised.
    """
    match backend:
        case "kreuzberg":
            return KreuzbergExtractor(
                kreuzberg_url=kreuzberg_url,
                max_chunk_size=max_chunk_size,
                chunk_overlap=chunk_overlap,
                ocr_enabled=ocr_enabled,
                timeout_cap=kreuzberg_timeout_cap,
                max_content_bytes=kreuzberg_max_content_bytes,
                connect_retry_budget_seconds=kreuzberg_connect_retry_seconds,
            )
        case "docling":
            kwargs: dict[str, Any] = {
                "url": "http://localhost:5001",
                "max_chunk_size": max_chunk_size,
                "chunk_overlap": chunk_overlap,
                "ocr_enabled": ocr_enabled,
                "timeout_cap": kreuzberg_timeout_cap,
                "max_content_bytes": kreuzberg_max_content_bytes,
                "connect_retry_budget_seconds": kreuzberg_connect_retry_seconds,
            }
            kwargs.update(docling_options or {})
            return DoclingExtractor(**kwargs)
        case "legacy":
            return LegacyExtractor(
                max_chunk_size=max_chunk_size,
                chunk_overlap=chunk_overlap,
            )
        case _:
            raise ValueError(
                f"Unknown extraction backend '{backend}'. Available: {BACKENDS}"
            )


def _preset_extraction(preset: Any) -> Optional[ExtractionConfig]:
    extraction = getattr(preset, "extraction", None)
    return extraction if isinstance(extraction, ExtractionConfig) else None


def effective_extractor_backend(settings: Any, preset: Any = None) -> str:
    """The extractor that indexes documents under ``preset`` (default: the active one)."""
    if preset is None:
        preset = settings.get_hardware_preset()
    extraction = _preset_extraction(preset)
    return extraction.backend if extraction is not None else settings.extractor_backend


# Preset ``extraction.options`` keys for the docling backend, mapped to
# DoclingExtractor arguments. ``api_key_env`` is resolved separately.
_DOCLING_OPTION_KEYS = {
    "url": "url",
    "api_flavor": "api_flavor",
    "async_mode": "async_mode",
    "min_request_interval_seconds": "min_request_interval",
    "timeout_seconds": "timeout_cap",
    "path": "path",
    "extra_options": "extra_options",
    "max_pages_per_request": "max_pages_per_request",
}


def _resolve_api_key(name: str, preset: Any, embedding_service: Any, data_path: Any) -> Optional[str]:
    """The value of a provider key a preset names for its extractor.

    When the embedding side declares the same key name (e.g. KISSKI_API_KEY
    for both KISSKI embeddings and KISSKI's Docling endpoint), the key the
    embedding service runs with is used: the caller's request header during
    an upload, the stored auto-index key in the cron indexer. Otherwise the
    admin-set shared value (POST /api/config/remote-fields).
    """
    kwargs = getattr(getattr(preset, "embedding", None), "model_kwargs", {}) or {}
    if name in (kwargs.get("api_key_env"), kwargs.get("shared_api_key_env")):
        key = getattr(embedding_service, "api_key", None)
        if key:
            return key
    from backend.services.admin_settings_store import resolve_shared_value

    return resolve_shared_value(name, data_path)


def create_configured_extractor(
    settings: Any,
    preset: Any = None,
    embedding_service: Any = None,
    max_chunk_size: int = 512,
    chunk_overlap: int = 50,
) -> DocumentExtractor:
    """Build the extractor ``preset`` (default: the active one) indexes with.

    The preset's ``extraction`` section, when present, picks the backend and
    its options; otherwise ``settings.extractor_backend`` with the server's
    ``KREUZBERG_*``/``DOCLING_*`` settings. Docling options a preset may set:
    ``url``, ``api_flavor`` (``serve``|``saia``), ``api_key_env`` (the name of
    the provider key to send, see ``_resolve_api_key``), ``async_mode``,
    ``min_request_interval_seconds``, ``timeout_seconds``, ``max_pages_per_request``
    (send PDFs in page ranges), ``path`` and ``extra_options`` (extra form fields /
    query parameters).
    """
    if preset is None:
        preset = settings.get_hardware_preset()
    extraction = _preset_extraction(preset)
    backend = extraction.backend if extraction is not None else settings.extractor_backend
    options = dict(extraction.options) if extraction is not None else {}

    docling_options: dict[str, Any] = {}
    if backend == "docling":
        docling_options = {
            "url": settings.docling_url,
            "api_flavor": settings.docling_api_flavor,
            "async_mode": settings.docling_async,
            "min_request_interval": settings.docling_min_request_interval,
        }
        for key, arg in _DOCLING_OPTION_KEYS.items():
            if key in options:
                docling_options[arg] = options[key]
        key_name = options.get("api_key_env")
        if key_name:
            docling_options["api_key_name"] = key_name
            docling_options["api_key"] = _resolve_api_key(key_name, preset, embedding_service, settings.data_path)

    return create_document_extractor(
        backend=backend,
        max_chunk_size=max_chunk_size,
        chunk_overlap=chunk_overlap,
        ocr_enabled=options.get("ocr_enabled", settings.ocr_enabled),
        kreuzberg_url=settings.kreuzberg_url,
        kreuzberg_timeout_cap=settings.kreuzberg_timeout_seconds,
        kreuzberg_max_content_bytes=settings.kreuzberg_max_content_bytes,
        # 0 in testing mode: a test exercising the real upload path without
        # mocking httpx (no extraction service runs under pytest) must fail on
        # the first connection attempt, not retry for minutes and hang past the
        # test's own timeout.
        kreuzberg_connect_retry_seconds=0 if settings.testing else 600,
        docling_options=docling_options,
    )
