"""Unit tests for backend.services.chapter_upload."""

import asyncio
import io
import unittest
from unittest.mock import MagicMock, patch

from pypdf import PdfReader, PdfWriter

from backend.services.chapter_upload import slice_pdf_range
from backend.services.chapter_upload import build_book_section_item_data
from backend.services.chapter_upload import author_year_label, ensure_target_collection
from backend.services.chapter_upload import run as upload_run


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


class TestAuthorYearLabel(unittest.TestCase):
    def test_single_author(self):
        self.assertEqual(author_year_label(["Jane Miller"], "2023"), "Miller (2023)")

    def test_two_or_more_authors_uses_et_al(self):
        self.assertEqual(author_year_label(["Jane Smith", "John Doe", "Amy Lee"], "1999"), "Smith et al. (1999)")

    def test_no_authors_falls_back_to_untitled(self):
        self.assertEqual(author_year_label([], "2020"), "Unknown (2020)")


class TestEnsureTargetCollection(unittest.TestCase):
    def test_creates_top_level_and_subcollection_when_absent(self):
        zot = MagicMock()
        zot.collections.return_value = []
        zot.create_collection.return_value = {"successful": {"0": {"key": "TOPKEY01"}}}
        zot.collections_sub.return_value = []

        top_key, sub_key = ensure_target_collection(zot, "Book Chapters", "Miller (2023)")
        zot.create_collection.assert_called()
        self.assertEqual(top_key, "TOPKEY01")

    def test_reuses_existing_collections(self):
        zot = MagicMock()
        zot.collections.return_value = [{"key": "TOPKEY01", "data": {"name": "Book Chapters"}}]
        zot.collections_sub.return_value = [{"key": "SUBKEY01", "data": {"name": "Miller (2023)"}}]

        top_key, sub_key = ensure_target_collection(zot, "Book Chapters", "Miller (2023)")
        self.assertEqual(top_key, "TOPKEY01")
        self.assertEqual(sub_key, "SUBKEY01")
        zot.create_collection.assert_not_called()


class TestUploadRun(unittest.TestCase):
    def _book_and_analysis(self):
        book_item = {
            "key": "BOOK1",
            "data": {
                "key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management",
                "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Editor"}],
                "publisher": "Big Press", "place": "Berlin", "date": "2019", "ISBN": "978-0", "language": "en",
                "extra": "",
            },
        }
        analysis = {
            "item_key": "BOOK1", "attachment_key": "ATT1",
            "chapters": [
                {"title": "Comparing Citation Styles", "authors": ["John Smith"], "pdf_start_index": 2,
                 "pdf_end_index": 4, "citation_pages": "45-67", "confidence": 0.93, "page_mapping_confidence": "high"},
            ],
        }
        return book_item, analysis

    def test_dry_run_creates_nothing(self):
        book_item, analysis = self._book_and_analysis()
        zot = MagicMock()
        zot.item.return_value = book_item
        result = asyncio.run(upload_run(
            zotero_write_client=zot, zotero_read_client=MagicMock(get_attachment_file=MagicMock()),
            slug="groups/1", analyses=[analysis], commit=False, confidence_threshold=0.8,
            target_collection="Book Chapters", max_items=None,
        ))
        zot.create_items.assert_not_called()
        self.assertEqual(len(result["would_create"]), 1)

    def test_commit_creates_and_links(self):
        book_item, analysis = self._book_and_analysis()
        zot = MagicMock()
        zot.item.return_value = book_item
        zot.item_template.return_value = {"itemType": "bookSection", "title": "", "bookTitle": "", "editor": [],
                                           "publisher": "", "place": "", "date": "", "ISBN": "", "language": "",
                                           "pages": "", "creators": []}
        zot.create_items.return_value = {"successful": {"0": {"key": "CHAP1"}}}
        zot.collections.return_value = []
        zot.create_collection.return_value = {"successful": {"0": {"key": "TOPKEY01"}}}
        zot.collections_sub.return_value = []
        read_client = MagicMock()
        read_client.get_attachment_file = unittest.mock.AsyncMock(return_value=b"%PDF-1.4 fake")

        with patch("backend.services.chapter_upload.slice_pdf_range", return_value=b"sliced bytes"):
            result = asyncio.run(upload_run(
                zotero_write_client=zot, zotero_read_client=read_client,
                slug="groups/1", analyses=[analysis], commit=True, confidence_threshold=0.8,
                target_collection="Book Chapters", max_items=None,
            ))
        zot.create_items.assert_called()
        zot.attachment_simple.assert_called()
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(result["created"][0]["chapter_key"], "CHAP1")

    def test_below_threshold_is_skipped(self):
        book_item, analysis = self._book_and_analysis()
        analysis["chapters"][0]["confidence"] = 0.5
        zot = MagicMock()
        zot.item.return_value = book_item

        result = asyncio.run(upload_run(
            zotero_write_client=zot, zotero_read_client=MagicMock(),
            slug="groups/1", analyses=[analysis], commit=True, confidence_threshold=0.8,
            target_collection="Book Chapters", max_items=None,
        ))
        zot.create_items.assert_not_called()
        self.assertEqual(len(result["skipped_low_confidence"]), 1)


if __name__ == "__main__":
    unittest.main()
