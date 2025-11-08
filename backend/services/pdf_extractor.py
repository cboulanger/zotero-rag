"""
PDF text extraction with page number tracking.

Extracts text from PDF files while preserving page numbers for citation.
"""

import logging
from pathlib import Path
from typing import Optional
from io import BytesIO

from pypdf import PdfReader


logger = logging.getLogger(__name__)


class PageText:
    """Text from a single PDF page."""

    def __init__(self, page_number: int, text: str):
        """
        Initialize page text.

        Args:
            page_number: 1-indexed page number
            text: Extracted text content
        """
        self.page_number = page_number
        self.text = text.strip()

    def __repr__(self):
        preview = self.text[:50] + "..." if len(self.text) > 50 else self.text
        return f"PageText(page={self.page_number}, text='{preview}')"


class PDFExtractor:
    """
    Extracts text from PDF files with page tracking.

    Uses pypdf for text extraction, maintaining page numbers for citation.
    """

    def __init__(self):
        """Initialize PDF extractor."""
        logger.info("Initialized PDFExtractor")

    def extract_from_file(self, pdf_path: Path) -> list[PageText]:
        """
        Extract text from a PDF file.

        Args:
            pdf_path: Path to PDF file

        Returns:
            List of PageText objects, one per page

        Raises:
            FileNotFoundError: If PDF file doesn't exist
            ValueError: If file is not a valid PDF
        """
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")

        try:
            with open(pdf_path, "rb") as f:
                return self._extract_from_stream(f)
        except Exception as e:
            logger.error(f"Failed to extract text from {pdf_path}: {e}")
            raise ValueError(f"Invalid PDF file: {pdf_path}") from e

    def extract_from_bytes(self, pdf_bytes: bytes) -> list[PageText]:
        """
        Extract text from PDF bytes.

        Args:
            pdf_bytes: PDF file content as bytes

        Returns:
            List of PageText objects, one per page

        Raises:
            ValueError: If bytes are not a valid PDF
        """
        try:
            stream = BytesIO(pdf_bytes)
            return self._extract_from_stream(stream)
        except Exception as e:
            logger.error(f"Failed to extract text from bytes: {e}")
            raise ValueError("Invalid PDF bytes") from e

    def _extract_from_stream(self, stream) -> list[PageText]:
        """
        Extract text from a file-like object.

        Args:
            stream: File-like object containing PDF data

        Returns:
            List of PageText objects
        """
        pages = []

        try:
            reader = PdfReader(stream)
            num_pages = len(reader.pages)

            logger.info(f"Extracting text from PDF with {num_pages} pages")

            for page_num, page in enumerate(reader.pages, start=1):
                try:
                    text = page.extract_text()

                    # Only add pages with content
                    if text and text.strip():
                        pages.append(PageText(page_num, text))
                    else:
                        logger.debug(f"Page {page_num} has no extractable text")

                except Exception as e:
                    logger.warning(f"Failed to extract text from page {page_num}: {e}")
                    continue

            logger.info(f"Extracted text from {len(pages)}/{num_pages} pages")

        except Exception as e:
            logger.error(f"Failed to read PDF: {e}")
            raise

        return pages

    def extract_page_range(
        self,
        pdf_path: Path,
        start_page: int,
        end_page: Optional[int] = None,
    ) -> list[PageText]:
        """
        Extract text from a specific page range.

        Args:
            pdf_path: Path to PDF file
            start_page: Starting page number (1-indexed)
            end_page: Ending page number (inclusive), or None for all remaining pages

        Returns:
            List of PageText objects for the specified range

        Raises:
            FileNotFoundError: If PDF file doesn't exist
            ValueError: If page range is invalid
        """
        if start_page < 1:
            raise ValueError("start_page must be >= 1")

        if end_page is not None and end_page < start_page:
            raise ValueError("end_page must be >= start_page")

        # Extract all pages
        all_pages = self.extract_from_file(pdf_path)

        # Filter to requested range
        if end_page is None:
            filtered = [p for p in all_pages if p.page_number >= start_page]
        else:
            filtered = [
                p for p in all_pages
                if start_page <= p.page_number <= end_page
            ]

        logger.info(f"Extracted {len(filtered)} pages from range {start_page}-{end_page or 'end'}")
        return filtered

    def get_page_count(self, pdf_path: Path) -> int:
        """
        Get the number of pages in a PDF.

        Args:
            pdf_path: Path to PDF file

        Returns:
            Number of pages

        Raises:
            FileNotFoundError: If PDF file doesn't exist
            ValueError: If file is not a valid PDF
        """
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")

        try:
            with open(pdf_path, "rb") as f:
                reader = PdfReader(f)
                return len(reader.pages)
        except Exception as e:
            logger.error(f"Failed to get page count from {pdf_path}: {e}")
            raise ValueError(f"Invalid PDF file: {pdf_path}") from e
