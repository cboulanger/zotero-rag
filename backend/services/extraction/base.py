"""
Abstract base for document extraction + chunking adapters.

Provides a unified interface for different extraction backends (Legacy, Kreuzberg, Docling).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ExtractionChunk:
    """A chunk of extracted text with location metadata."""

    text: str
    page_number: int | None
    chunk_index: int

    @property
    def text_preview(self) -> str:
        """First 5 words as a citation anchor."""
        return " ".join(self.text.split()[:5])


class DocumentExtractor(ABC):
    """
    Abstract base for document extraction + chunking backends.

    Implementations must handle both extraction and chunking in a single call,
    returning a flat list of ExtractionChunk objects with page tracking.
    """

    @abstractmethod
    async def extract_and_chunk(
        self,
        content: bytes,
        mime_type: str,
    ) -> list[ExtractionChunk]:
        """
        Extract text from document bytes and split into chunks.

        Args:
            content: Raw document bytes.
            mime_type: MIME type hint (e.g. "application/pdf", "text/html").

        Returns:
            Ordered list of ExtractionChunk objects.  Empty list if no text
            could be extracted (e.g. image-only PDF without OCR configured).
        """
