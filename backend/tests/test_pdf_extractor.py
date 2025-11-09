"""
Tests for PDF text extraction.
"""

import pytest
from pathlib import Path
from io import BytesIO
from pypdf import PdfWriter

from backend.services.pdf_extractor import PDFExtractor, PageText


@pytest.fixture
def pdf_extractor():
    """Create PDF extractor instance."""
    return PDFExtractor()


@pytest.fixture
def fixtures_dir():
    """Get path to test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_pdf_path(fixtures_dir):
    """Path to sample academic paper PDF."""
    pdf_path = fixtures_dir / "10.5771__2699-1284-2024-3-149.pdf"
    if not pdf_path.exists():
        pytest.skip(f"Sample PDF not found at {pdf_path}")
    return pdf_path


@pytest.fixture
def multi_page_pdf_bytes():
    """Create a multi-page PDF as bytes."""
    writer = PdfWriter()

    # Add 3 blank pages
    for _ in range(3):
        writer.add_blank_page(width=612, height=792)

    # Write to bytes
    buffer = BytesIO()
    writer.write(buffer)
    buffer.seek(0)
    return buffer.getvalue()


class TestPageText:
    """Tests for PageText class."""

    def test_initialization(self):
        """Test PageText initialization."""
        page = PageText(1, "  Sample text  ")

        assert page.page_number == 1
        assert page.text == "Sample text"  # Strips whitespace

    def test_repr_short_text(self):
        """Test repr with short text."""
        page = PageText(1, "Short text")
        repr_str = repr(page)

        assert "page=1" in repr_str
        assert "Short text" in repr_str

    def test_repr_long_text(self):
        """Test repr with long text."""
        long_text = "A" * 100
        page = PageText(1, long_text)
        repr_str = repr(page)

        assert "page=1" in repr_str
        assert "..." in repr_str  # Truncated
        assert len(repr_str) < len(long_text)


class TestPDFExtractor:
    """Tests for PDFExtractor class."""

    def test_initialization(self, pdf_extractor):
        """Test PDFExtractor initialization."""
        assert pdf_extractor is not None

    def test_extract_from_file_not_found(self, pdf_extractor):
        """Test extraction with non-existent file."""
        with pytest.raises(FileNotFoundError):
            pdf_extractor.extract_from_file(Path("nonexistent.pdf"))

    def test_extract_from_file_invalid_pdf(self, pdf_extractor, tmp_path):
        """Test extraction with invalid PDF file."""
        invalid_file = tmp_path / "invalid.pdf"
        invalid_file.write_text("Not a PDF file")

        with pytest.raises(ValueError, match="Invalid PDF file"):
            pdf_extractor.extract_from_file(invalid_file)

    def test_extract_from_bytes_invalid(self, pdf_extractor):
        """Test extraction from invalid bytes."""
        with pytest.raises(ValueError, match="Invalid PDF bytes"):
            pdf_extractor.extract_from_bytes(b"Not a PDF")

    def test_extract_from_bytes_valid(self, pdf_extractor, multi_page_pdf_bytes):
        """Test extraction from valid PDF bytes."""
        pages = pdf_extractor.extract_from_bytes(multi_page_pdf_bytes)

        # Blank pages have no text, so result should be empty or minimal
        assert isinstance(pages, list)

    def test_get_page_count_not_found(self, pdf_extractor):
        """Test page count with non-existent file."""
        with pytest.raises(FileNotFoundError):
            pdf_extractor.get_page_count(Path("nonexistent.pdf"))

    def test_get_page_count_invalid(self, pdf_extractor, tmp_path):
        """Test page count with invalid PDF."""
        invalid_file = tmp_path / "invalid.pdf"
        invalid_file.write_text("Not a PDF file")

        with pytest.raises(ValueError, match="Invalid PDF file"):
            pdf_extractor.get_page_count(invalid_file)

    def test_get_page_count_valid(self, pdf_extractor, tmp_path):
        """Test page count with valid PDF."""
        # Create a PDF with 3 pages
        pdf_path = tmp_path / "test.pdf"
        writer = PdfWriter()

        for _ in range(3):
            writer.add_blank_page(width=612, height=792)

        with open(pdf_path, "wb") as f:
            writer.write(f)

        count = pdf_extractor.get_page_count(pdf_path)
        assert count == 3

    def test_extract_page_range_invalid_start(self, pdf_extractor, sample_pdf_path):
        """Test page range extraction with invalid start page."""
        with pytest.raises(ValueError, match="start_page must be >= 1"):
            pdf_extractor.extract_page_range(sample_pdf_path, start_page=0)

    def test_extract_page_range_invalid_end(self, pdf_extractor, sample_pdf_path):
        """Test page range extraction with invalid end page."""
        with pytest.raises(ValueError, match="end_page must be >= start_page"):
            pdf_extractor.extract_page_range(sample_pdf_path, start_page=5, end_page=3)

    def test_extract_page_range_not_found(self, pdf_extractor):
        """Test page range extraction with non-existent file."""
        with pytest.raises(FileNotFoundError):
            pdf_extractor.extract_page_range(Path("nonexistent.pdf"), start_page=1)


class TestPDFExtractionWithRealPDF:
    """Tests with the real sample PDF fixture."""

    def test_extract_from_real_pdf(self, pdf_extractor, sample_pdf_path):
        """Test extraction from a real academic paper PDF."""
        pages = pdf_extractor.extract_from_file(sample_pdf_path)

        # Should extract text from pages
        assert len(pages) > 0, "Should extract at least one page"
        assert all(isinstance(p, PageText) for p in pages)
        assert all(p.page_number > 0 for p in pages)
        assert all(len(p.text) > 0 for p in pages), "All pages should have text"

        # Check page numbers are sequential starting from 1
        page_numbers = [p.page_number for p in pages]
        assert page_numbers == sorted(page_numbers)
        assert page_numbers[0] == 1, "First page should be numbered 1"

    def test_page_count(self, pdf_extractor, sample_pdf_path):
        """Test page count for the sample PDF."""
        count = pdf_extractor.get_page_count(sample_pdf_path)
        assert count > 0, "PDF should have at least one page"

    def test_extracted_text_content(self, pdf_extractor, sample_pdf_path):
        """Test that extracted text has meaningful content."""
        pages = pdf_extractor.extract_from_file(sample_pdf_path)

        # Check first page has substantial content
        first_page = pages[0]
        assert len(first_page.text) > 100, "First page should have substantial text"

        # Text should contain typical academic paper elements (words, not just noise)
        # This is a simple heuristic - real academic PDFs should have many words
        word_count = len(first_page.text.split())
        assert word_count > 50, "First page should have many words"

    def test_extract_from_bytes(self, pdf_extractor, sample_pdf_path):
        """Test extraction from PDF loaded as bytes."""
        # Read the PDF as bytes
        with open(sample_pdf_path, "rb") as f:
            pdf_bytes = f.read()

        # Extract from bytes
        pages = pdf_extractor.extract_from_bytes(pdf_bytes)

        # Should get same result as extracting from file
        assert len(pages) > 0
        assert all(isinstance(p, PageText) for p in pages)
        assert all(p.page_number > 0 for p in pages)

    def test_extract_page_range(self, pdf_extractor, sample_pdf_path):
        """Test extracting a specific page range."""
        # Get total page count first
        total_pages = pdf_extractor.get_page_count(sample_pdf_path)

        if total_pages >= 2:
            # Extract first two pages
            pages = pdf_extractor.extract_page_range(sample_pdf_path, start_page=1, end_page=2)

            # Should get at most 2 pages
            assert len(pages) <= 2
            assert all(1 <= p.page_number <= 2 for p in pages)

    def test_extract_page_range_from_middle(self, pdf_extractor, sample_pdf_path):
        """Test extracting pages from the middle of the document."""
        # Get total page count
        total_pages = pdf_extractor.get_page_count(sample_pdf_path)

        if total_pages >= 3:
            # Extract from page 2 onwards
            pages = pdf_extractor.extract_page_range(sample_pdf_path, start_page=2)

            # All pages should be >= 2
            assert all(p.page_number >= 2 for p in pages)

            # Should have fewer pages than total
            assert len(pages) < total_pages

    def test_extract_single_page(self, pdf_extractor, sample_pdf_path):
        """Test extracting a single page."""
        pages = pdf_extractor.extract_page_range(sample_pdf_path, start_page=1, end_page=1)

        # Should get exactly one page (or zero if page 1 is empty)
        assert len(pages) <= 1

        if len(pages) == 1:
            assert pages[0].page_number == 1
