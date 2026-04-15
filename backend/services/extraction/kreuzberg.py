"""
Kreuzberg-backed document extraction + chunking adapter.

Replaces the pypdf + spaCy pipeline with a Rust-core implementation that
supports 91+ file formats, OCR for scanned documents, and native async.

Chunk metadata from kreuzberg:
  - chunk.content       → text
  - chunk.metadata['chunk_index']  → 0-based index
  - chunk.metadata['first_page']   → 1-based page number (or None)
"""

import logging

import kreuzberg
from kreuzberg import ExtractionConfig, ChunkingConfig

from backend.services.extraction.base import DocumentExtractor, ExtractionChunk

logger = logging.getLogger(__name__)


class KreuzbergExtractor(DocumentExtractor):
    """
    Extraction adapter backed by the Kreuzberg library (Rust core).

    Supports PDF (with optional OCR), DOCX, HTML, EPUB, and 87+ other formats.
    OCR is attempted when no text layer is found on a page (if ocr_enabled=True);
    falls back gracefully if the OCR backend (e.g. Tesseract) is not installed.
    """

    def __init__(
        self,
        max_chunk_size: int = 512,
        chunk_overlap: int = 50,
        ocr_enabled: bool = True,
    ):
        """
        Args:
            max_chunk_size: Maximum characters per chunk.
            chunk_overlap: Overlap characters between consecutive chunks.
            ocr_enabled: Whether to attempt OCR on image-only pages.
        """
        self._ocr_enabled = ocr_enabled
        # disable_ocr=True suppresses OCR; omit it (default False) to enable OCR
        self._config = ExtractionConfig(
            chunking=ChunkingConfig(
                max_chars=max_chunk_size,
                max_overlap=chunk_overlap,
            ),
            disable_ocr=not ocr_enabled,
        )
        logger.info(
            f"Initialized KreuzbergExtractor "
            f"(max_chars={max_chunk_size}, overlap={chunk_overlap}, ocr={ocr_enabled})"
        )

    async def extract_and_chunk(
        self,
        content: bytes,
        mime_type: str,
    ) -> list[ExtractionChunk]:
        try:
            result = await kreuzberg.extract_bytes(content, mime_type, config=self._config)
        except kreuzberg.MissingDependencyError as exc:
            # OCR backend not installed — retry without OCR
            logger.warning(f"OCR dependency missing, retrying without OCR: {exc}")
            fallback_config = ExtractionConfig(
                chunking=ChunkingConfig(
                    max_chars=self._config.chunking.max_chars,
                    max_overlap=self._config.chunking.max_overlap,
                ),
                disable_ocr=True,
            )
            result = await kreuzberg.extract_bytes(content, mime_type, config=fallback_config)
        except kreuzberg.ParsingError as exc:
            logger.error(f"Kreuzberg failed to parse document (mime={mime_type}): {exc}")
            return []

        if not result.chunks:
            logger.debug(f"Kreuzberg returned no chunks for mime={mime_type}")
            return []

        extraction_chunks = []
        for chunk in result.chunks:
            text = chunk.content
            if not text or not text.strip():
                continue
            meta = chunk.metadata or {}
            extraction_chunks.append(
                ExtractionChunk(
                    text=text,
                    page_number=meta.get("first_page"),  # 1-based; None for non-paginated formats
                    chunk_index=meta.get("chunk_index", len(extraction_chunks)),
                )
            )

        logger.debug(
            f"KreuzbergExtractor: {len(extraction_chunks)} chunks from "
            f"{result.get_page_count() or '?'} pages (mime={mime_type})"
        )
        return extraction_chunks
