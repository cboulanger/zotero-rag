"""
Document extraction + chunking adapter package.

Provides a `DocumentExtractor` abstraction over different backends so the
extraction strategy can be changed via configuration without modifying
the document processing pipeline.

Available backends:
  - "kreuzberg"  KreuzbergExtractor  (default) — Rust-core, 91+ formats, OCR
  - "legacy"     LegacyExtractor     — original pypdf + spaCy pipeline
"""

from backend.services.extraction.base import DocumentExtractor, ExtractionChunk
from backend.services.extraction.legacy import LegacyExtractor
from backend.services.extraction.kreuzberg import KreuzbergExtractor

__all__ = [
    "DocumentExtractor",
    "ExtractionChunk",
    "LegacyExtractor",
    "KreuzbergExtractor",
    "create_document_extractor",
]


def create_document_extractor(
    backend: str = "kreuzberg",
    max_chunk_size: int = 512,
    chunk_overlap: int = 50,
    ocr_enabled: bool = True,
    kreuzberg_url: str = "http://localhost:8100",
) -> DocumentExtractor:
    """
    Factory: create a DocumentExtractor for the named backend.

    Args:
        backend: One of "kreuzberg" or "legacy".
        max_chunk_size: Maximum characters per chunk.
        chunk_overlap: Overlap between consecutive chunks.
        ocr_enabled: Whether to enable OCR (Kreuzberg only).
        kreuzberg_url: Base URL of the kreuzberg sidecar (kreuzberg backend only).

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
            )
        case "legacy":
            return LegacyExtractor(
                max_chunk_size=max_chunk_size,
                chunk_overlap=chunk_overlap,
            )
        case _:
            available = ("kreuzberg", "legacy")
            raise ValueError(
                f"Unknown extraction backend '{backend}'. Available: {available}"
            )
