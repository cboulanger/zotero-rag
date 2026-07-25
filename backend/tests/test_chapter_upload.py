"""Unit tests for backend.services.chapter_upload."""

import io
import unittest

from pypdf import PdfReader, PdfWriter

from backend.services.chapter_upload import slice_pdf_range
from backend.services.chapter_upload import build_book_section_item_data


def _make_test_pdf(num_pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class TestSlicePdfRange(unittest.TestCase):
    def test_slices_correct_page_range(self):
        content = _make_test_pdf(10)
        sliced = slice_pdf_range(content, pdf_start_index=2, pdf_end_index=4)
        reader = PdfReader(io.BytesIO(sliced))
        self.assertEqual(len(reader.pages), 3)  # indices 2, 3, 4 inclusive


class TestBuildBookSectionItemData(unittest.TestCase):
    def test_inherits_book_metadata(self):
        template = {"itemType": "bookSection", "title": "", "bookTitle": "", "editor": [], "publisher": "",
                    "place": "", "date": "", "ISBN": "", "language": "", "pages": "", "creators": []}
        book_data = {
            "title": "Handbook of Reference Management",
            "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Editor"}],
            "publisher": "Big Press", "place": "Berlin", "date": "2019", "ISBN": "978-0-000000-00-0",
            "language": "en",
        }
        chapter = {"title": "Comparing Citation Styles", "authors": ["John Smith"], "citation_pages": "45-67"}

        result = build_book_section_item_data(template, book_data, chapter)
        self.assertEqual(result["title"], "Comparing Citation Styles")
        self.assertEqual(result["bookTitle"], "Handbook of Reference Management")
        self.assertEqual(result["editor"], book_data["creators"])
        self.assertEqual(result["publisher"], "Big Press")
        self.assertEqual(result["pages"], "45-67")
        self.assertEqual(result["creators"][0]["lastName"], "Smith")

    def test_leaves_pages_blank_when_citation_pages_missing(self):
        template = {"itemType": "bookSection", "title": "", "bookTitle": "", "editor": [], "publisher": "",
                    "place": "", "date": "", "ISBN": "", "language": "", "pages": "", "creators": []}
        book_data = {"title": "T", "creators": [], "publisher": "", "place": "", "date": "", "ISBN": "", "language": ""}
        chapter = {"title": "C", "authors": [], "citation_pages": None}

        result = build_book_section_item_data(template, book_data, chapter)
        self.assertEqual(result["pages"], "")


if __name__ == "__main__":
    unittest.main()
