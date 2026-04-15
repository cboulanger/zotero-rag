"""
Legacy extraction adapter wrapping the original pypdf + spaCy pipeline.

Provides zero-behavior-change compatibility so the existing implementation
continues to work while Kreuzberg is validated.
"""

import logging

from backend.services.extraction.base import DocumentExtractor, ExtractionChunk
from backend.services.pdf_extractor import PDFExtractor
from backend.services.chunking import TextChunker

logger = logging.getLogger(__name__)

# MIME types handled by the legacy pipeline (PDF only)
_SUPPORTED_MIME_TYPES = {"application/pdf"}


class LegacyExtractor(DocumentExtractor):
    """
    Extraction adapter backed by the original pypdf + spaCy implementation.

    Only supports PDF documents.  Other MIME types return an empty list.
    """

    def __init__(self, max_chunk_size: int = 512, chunk_overlap: int = 50):
        """
        Args:
            max_chunk_size: Maximum characters per chunk.
            chunk_overlap: Overlap characters between consecutive chunks.
        """
        self._pdf_extractor = PDFExtractor()
        self._chunker = TextChunker(
            max_chunk_size=max_chunk_size,
            overlap_size=chunk_overlap,
        )
        logger.info("Initialized LegacyExtractor (pypdf + spaCy)")

    async def extract_and_chunk(
        self,
        content: bytes,
        mime_type: str,
    ) -> list[ExtractionChunk]:
        if mime_type not in _SUPPORTED_MIME_TYPES:
            logger.debug(f"LegacyExtractor: unsupported MIME type '{mime_type}', skipping")
            return []

        # Extract page-level text
        pages = self._pdf_extractor.extract_from_bytes(content)
        if not pages:
            return []

        # Chunk across all pages
        page_tuples = [(page.page_number, page.text) for page in pages]
        text_chunks = self._chunker.chunk_pages(page_tuples)

        return [
            ExtractionChunk(
                text=tc.text,
                page_number=tc.page_number,
                chunk_index=tc.chunk_index,
            )
            for tc in text_chunks
        ]
