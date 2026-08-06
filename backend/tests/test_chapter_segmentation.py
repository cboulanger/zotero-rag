"""Unit tests for backend.services.chapter_segmentation (the Zotero-specific
orchestrator -- the actual chapter-boundary detection engine now lives in
the standalone chapter_segmentation package; its own tests live there)."""

import asyncio
import io as _io
import tempfile
import unittest
import unittest.mock
from pathlib import Path as _TestPath
from unittest.mock import AsyncMock, MagicMock

from pypdf import PdfWriter as _PdfWriter

from backend.config.settings import get_settings, reset_settings
from backend.services.chapter_segmentation import run as analyze_run
from backend.services.review_queue_store import get_entry


def _blank_pdf(num_pages: int) -> bytes:
    writer = _PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    buf = _io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _pdf_with_outline(num_pages: int, entries: list[tuple[str, int]]) -> bytes:
    writer = _PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    for title, page_number in entries:
        writer.add_outline_item(title, page_number)
    buf = _io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# Repeated (not a single short line) so this filler page clears
# pages_need_ocr's per-page "substantial" (>500 chars) and "not degenerate"
# (>=3 newlines) thresholds -- a single short line reads as OCR-shaped input
# and short-circuits run() into the needs_ocr branch before any chapter
# segmentation strategy runs.
_FILLER = "Unrelated body filler text, nothing chapter-related in this passage at all.\n" * 8

_TWO_CHAPTER_PAGES = [
    _FILLER,  # 0
    _FILLER,  # 1
    _FILLER,  # 2
    _FILLER,  # 3
    _FILLER,  # 4
    "Introduction\nJane Author\n\nBody text opening the chapter.\n\n1",  # 5
    "...continued introduction text with real body content here.\n\n2",  # 6
    "...more continued introduction text with real body content.\n\n3",  # 7
    "...final continued introduction text with real body content.\n\n4",  # 8
    _FILLER,  # 9
    _FILLER,  # 10
    _FILLER,  # 11
    "Comparing Citation Styles\n\nJohn Smith\n\nBody text opening this chapter.\n\n5",  # 12
    "...continued citation styles text with real body content here.\n\n6",  # 13
    "...more continued citation styles text with real body content.\n\n7",  # 14
    "...final continued citation styles text with real body content.\n\n8",  # 15
    _FILLER,  # 16
    _FILLER,  # 17
    _FILLER,  # 18
    _FILLER,  # 19
]

_SUBSTANTIAL_FILLER_PAGE = "Just filler prose, no TOC pattern here at all.\n" * 12


class TestRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        get_settings().review_queue_path = _TestPath(self.tmp.name) / "review_queue.json"
        get_settings().zotero_cache_path = _TestPath(self.tmp.name) / "zotero_cache"

    def tearDown(self):
        self.tmp.cleanup()
        reset_settings()

    def test_skips_already_linked_book(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0001", "version": 1, "data": {"key": "BOOK0001", "itemType": "book", "extra": "X-Contains: groups/1:CH01"}},
        ]
        progress_calls = []
        result = asyncio.run(analyze_run(
            zotero_client=zotero_client,
            library_id="1",
            library_type="group",
            slug="groups/1",
            item_keys=None,
            max_items=None,
            relink=False,
            progress_callback=lambda p, m: progress_calls.append((p, m)),
        ))
        self.assertEqual(result["attachments"], [])
        zotero_client.get_item_children.assert_not_called()

    def test_processes_unlinked_book(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0002", "version": 1, "data": {"key": "BOOK0002", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0001", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=[_SUBSTANTIAL_FILLER_PAGE],
        ):
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
            ))
        self.assertEqual(len(result["attachments"]), 1)
        self.assertEqual(result["attachments"][0]["item_key"], "BOOK0002")
        self.assertTrue(result["attachments"][0]["has_text_layer"])
        zotero_client.get_attachment_file.assert_called_once_with("1", "ATT0001", library_type="group")

    def test_needs_ocr_attachment_is_upserted_into_review_queue(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK9", "version": 1, "data": {"key": "BOOK9", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT9", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 no text layer"

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=[""],
        ):
            asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
            ))

        entry = get_entry(get_settings().review_queue_path, "groups/1", "ocr:ATT9")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["type"], "ocr")
        self.assertEqual(entry["bucket"], "review")
        self.assertEqual(entry["payload"], {"book_key": "BOOK9", "attachment_key": "ATT9"})

    def test_uses_llm_fallback_when_llm_service_provided(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0003", "version": 1, "data": {"key": "BOOK0003", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0002", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"
        fake_llm = MagicMock()

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=[_SUBSTANTIAL_FILLER_PAGE],
        ), unittest.mock.patch(
            "chapter_segmentation.segmentation.analyze_attachment_with_llm_fallback",
            new_callable=AsyncMock,
            return_value={"total_pdf_pages": 1, "segmentation_confidence": "low", "chapters": [], "diagnostics": {}},
        ) as mock_fallback:
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
                llm_service=fake_llm,
            ))
        mock_fallback.assert_called_once()
        self.assertEqual(result["attachments"][0]["item_key"], "BOOK0003")

    def test_reads_ocr_cache_when_no_text_layer_and_cache_hit(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0004", "version": 1, "data": {"key": "BOOK0004", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0003", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["", ""],  # no text layer -- scanned PDF
        ), unittest.mock.patch(
            "backend.services.chapter_segmentation.load_cached_ocr",
            return_value={"detected_language": "eng", "pages": [_SUBSTANTIAL_FILLER_PAGE]},
        ) as mock_load_cache, unittest.mock.patch(
            "backend.services.chapter_segmentation.load_cached_analysis",
            return_value=None,
        ), unittest.mock.patch(
            "backend.services.chapter_segmentation.save_analysis_cache",
        ):
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
                ocr_cache_dir=unittest.mock.ANY,
            ))
        mock_load_cache.assert_called_once()
        self.assertTrue(result["attachments"][0]["has_text_layer"])
        self.assertFalse(result["attachments"][0]["needs_ocr"])

    def test_still_reports_needs_ocr_when_no_cache_dir_given(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0005", "version": 1, "data": {"key": "BOOK0005", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0004", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["", ""],
        ), unittest.mock.patch(
            "backend.services.chapter_segmentation.load_cached_ocr",
        ) as mock_load_cache:
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
            ))
        mock_load_cache.assert_not_called()
        self.assertFalse(result["attachments"][0]["has_text_layer"])
        self.assertTrue(result["attachments"][0]["needs_ocr"])

    def test_analysis_cache_hit_skips_download(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0006", "version": 1, "data": {"key": "BOOK0006", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0005", "itemType": "attachment", "contentType": "application/pdf", "version": 7}},
        ]
        cached_entry = {
            "item_key": "BOOK0006", "attachment_key": "ATT0005", "has_text_layer": True,
            "needs_ocr": False, "total_pdf_pages": 3, "segmentation_confidence": "high",
            "chapters": [], "diagnostics": {},
        }

        with unittest.mock.patch(
            "backend.services.chapter_segmentation.load_cached_analysis",
            return_value=cached_entry,
        ) as mock_load:
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
                ocr_cache_dir=unittest.mock.ANY,
            ))
        mock_load.assert_called_once_with(unittest.mock.ANY, "BOOK0006", "ATT0005", 7, "strategies")
        zotero_client.get_attachment_file.assert_not_called()
        self.assertEqual(result["attachments"], [cached_entry])

    def test_analysis_cache_miss_saves_result(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0007", "version": 1, "data": {"key": "BOOK0007", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0006", "itemType": "attachment", "contentType": "application/pdf", "version": 3}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=[_SUBSTANTIAL_FILLER_PAGE],
        ), unittest.mock.patch(
            "backend.services.chapter_segmentation.load_cached_analysis", return_value=None,
        ), unittest.mock.patch(
            "backend.services.chapter_segmentation.save_analysis_cache",
        ) as mock_save:
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
                ocr_cache_dir=unittest.mock.ANY,
            ))
        mock_save.assert_called_once()
        saved_args = mock_save.call_args.args
        self.assertEqual(saved_args[1:4], ("BOOK0007", "ATT0006", 3))
        self.assertEqual(saved_args[4], "strategies")
        self.assertEqual(result["attachments"][0]["item_key"], "BOOK0007")

    def test_no_cache_dir_never_touches_analysis_cache(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0008", "version": 1, "data": {"key": "BOOK0008", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0007", "itemType": "attachment", "contentType": "application/pdf", "version": 1}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["Just filler prose, no TOC pattern here at all. " * 3],
        ), unittest.mock.patch(
            "backend.services.chapter_segmentation.load_cached_analysis",
        ) as mock_load, unittest.mock.patch(
            "backend.services.chapter_segmentation.save_analysis_cache",
        ) as mock_save:
            asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
            ))
        mock_load.assert_not_called()
        mock_save.assert_not_called()

    def test_uses_outline_strategy_when_present(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK3", "version": 1, "data": {"key": "BOOK3", "itemType": "book", "extra": "", "title": "Some Book"}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT3", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        pdf_bytes = _pdf_with_outline(20, [("Introduction", 5), ("Comparing Citation Styles", 12)])
        zotero_client.get_attachment_file.return_value = pdf_bytes

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=_TWO_CHAPTER_PAGES,
        ):
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
                enable_crossref=False,
            ))
        chapters = result["attachments"][0]["chapters"]
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0]["source"], "outline")

    def test_builds_zotero_catalog_index_from_unlinked_book_sections(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK4", "version": 1, "data": {"key": "BOOK4", "itemType": "book", "extra": "", "title": "Some Book"}},
            {"key": "CH1", "version": 1, "data": {
                "key": "CH1", "itemType": "bookSection", "extra": "",
                "title": "Introduction", "bookTitle": "Some Book", "pages": "1-20",
                "creators": [{"creatorType": "author", "firstName": "Jane", "lastName": "Author"}],
            }},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT4", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        pdf_bytes = _blank_pdf(8)  # no outline -- forces content-search localization
        zotero_client.get_attachment_file.return_value = pdf_bytes

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=_TWO_CHAPTER_PAGES,
        ):
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
                enable_crossref=False,
            ))
        chapters = result["attachments"][0]["chapters"]
        titles = {c["title"]: c for c in chapters}
        self.assertIn("Introduction", titles)
        self.assertEqual(titles["Introduction"]["source"], "zotero_catalog")

    def test_already_linked_book_section_is_not_offered_as_candidate(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK5", "version": 1, "data": {"key": "BOOK5", "itemType": "book", "extra": "", "title": "Some Book"}},
            {"key": "CH2", "version": 1, "data": {
                "key": "CH2", "itemType": "bookSection",
                "extra": "X-Contained-By: groups/1:OTHERBOOK",
                "title": "Introduction", "bookTitle": "Some Book", "pages": "1-20",
                "creators": [],
            }},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT5", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        pdf_bytes = _blank_pdf(3)
        zotero_client.get_attachment_file.return_value = pdf_bytes

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["Just filler prose, nothing chapter-related here at all.\n" * 12] * 3,
        ):
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
                enable_crossref=False,
            ))
        self.assertEqual(result["attachments"][0]["diagnostics"]["strategies_used"], [])

    def test_enable_crossref_false_skips_crossref_entirely(self):
        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK6", "version": 1, "data": {"key": "BOOK6", "itemType": "book", "extra": "", "title": "Some Book", "ISBN": "9783031466373"}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT6", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        pdf_bytes = _blank_pdf(3)
        zotero_client.get_attachment_file.return_value = pdf_bytes

        with unittest.mock.patch(
            "chapter_segmentation.segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["Just filler prose, nothing chapter-related here at all.\n" * 12] * 3,
        ):
            result = asyncio.run(analyze_run(
                zotero_client=zotero_client,
                library_id="1",
                library_type="group",
                slug="groups/1",
                item_keys=None,
                max_items=None,
                relink=False,
                progress_callback=lambda p, m: None,
                enable_crossref=False,
            ))
        self.assertIsNone(result["attachments"][0]["diagnostics"]["crossref_isbn_used"])


if __name__ == "__main__":
    unittest.main()
