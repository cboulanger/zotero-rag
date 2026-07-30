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
        template = {"itemType": "bookSection", "title": "", "bookTitle": "", "publisher": "",
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
        self.assertNotIn("editor", result)  # Zotero has no standalone "editor" item field
        self.assertEqual(result["publisher"], "Big Press")
        self.assertEqual(result["pages"], "45-67")
        # The chapter's own author comes first, then the book's creators
        # re-typed as "editor" (Zotero only supports "editor" as a
        # creatorType within "creators", not a separate item field).
        self.assertEqual(result["creators"][0], {"creatorType": "author", "firstName": "John", "lastName": "Smith"})
        self.assertEqual(result["creators"][1], {"creatorType": "editor", "firstName": "Jane", "lastName": "Editor"})

    def test_leaves_pages_blank_when_citation_pages_missing(self):
        template = {"itemType": "bookSection", "title": "", "bookTitle": "", "publisher": "",
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
                # Distinctive marker line: if the chapter's extra were (buggily)
                # computed from the BOOK's extra, this would leak into the
                # chapter's update payload — see test_commit_creates_and_links.
                "extra": "X-Citekey: bookMarker2019",
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

        def item_template_side_effect(item_type, **kwargs):
            if item_type == "attachment":
                return {"itemType": "attachment", "linkMode": kwargs.get("linkmode", ""), "title": "",
                        "filename": "", "contentType": "", "parentItem": ""}
            return {"itemType": "bookSection", "title": "", "bookTitle": "",
                    "publisher": "", "place": "", "date": "", "ISBN": "", "language": "",
                    "pages": "", "creators": []}

        zot.item_template.side_effect = item_template_side_effect
        # First create_items call creates the bookSection, the second (inside
        # the attachment-upload block) creates the not-yet-uploaded attachment
        # item -- see chapter_upload.py's NOTE on why attachment_simple() (a
        # single-step helper) can't be used here.
        zot.create_items.side_effect = [
            {"successful": {"0": {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}}},
            {"successful": {"0": {"key": "ATT1", "data": {"key": "ATT1"}}}},
        ]
        zot.upload_attachments.return_value = {"success": [{"key": "ATT1"}], "failure": [], "unchanged": []}
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
        self.assertEqual(zot.create_items.call_count, 2)
        zot.upload_attachments.assert_called()
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(result["created"][0]["chapter_key"], "CHAP1")
        # update_item is called exactly twice per committed chapter: first for
        # the chapter (extra + collection membership folded into one PATCH),
        # then for the book.
        self.assertEqual(zot.update_item.call_count, 2)
        chapter_update_extra = zot.update_item.call_args_list[0][0][0]["data"]["extra"]
        book_update_extra = zot.update_item.call_args_list[1][0][0]["data"]["extra"]
        # The chapter's own X-Contained-By is written from its own (empty)
        # extra, not inherited from the book's extra. contained_by=book_id is a
        # literal, so asserting its presence alone can't distinguish a
        # wrong-source read — instead assert the book's distinctive marker line
        # did NOT leak into the chapter's extra.
        self.assertIn("X-Contained-By", chapter_update_extra)
        self.assertNotIn("bookMarker2019", chapter_update_extra)
        # The book's own update writes X-Contains / X-Chapter-Pdf-Range, never
        # the chapter-side X-Contained-By. Guards against aliasing the two
        # items' extra fields (the fixed non-aliasing bug).
        self.assertNotIn("X-Contained-By", book_update_extra)

    def test_commit_downloads_book_pdf_once_for_multiple_chapters(self):
        book_item, analysis = self._book_and_analysis()
        analysis["chapters"].append({
            "title": "Second Chapter", "authors": ["Jane Doe"], "pdf_start_index": 5,
            "pdf_end_index": 7, "citation_pages": "70-90", "confidence": 0.95, "page_mapping_confidence": "high",
        })
        zot = MagicMock()
        zot.item.return_value = book_item

        def item_template_side_effect(item_type, **kwargs):
            if item_type == "attachment":
                return {"itemType": "attachment", "linkMode": kwargs.get("linkmode", ""), "title": "",
                        "filename": "", "contentType": "", "parentItem": ""}
            return {"itemType": "bookSection", "title": "", "bookTitle": "",
                    "publisher": "", "place": "", "date": "", "ISBN": "", "language": "",
                    "pages": "", "creators": []}

        zot.item_template.side_effect = item_template_side_effect
        zot.create_items.side_effect = [
            {"successful": {"0": {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}}},
            {"successful": {"0": {"key": "ATT1", "data": {"key": "ATT1"}}}},
            {"successful": {"0": {"key": "CHAP2", "data": {"key": "CHAP2", "extra": ""}}}},
            {"successful": {"0": {"key": "ATT2", "data": {"key": "ATT2"}}}},
        ]
        zot.upload_attachments.return_value = {"success": [{"key": "ATT1"}], "failure": [], "unchanged": []}
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

        self.assertEqual(len(result["created"]), 2)
        read_client.get_attachment_file.assert_called_once_with("1", "ATT1", library_type="group")

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
