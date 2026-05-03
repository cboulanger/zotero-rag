"""
Unit tests for the PDF splitting feature in DocumentProcessor.

Tests _extract_pdf_in_parts in isolation (extractor mocked, split_pdf_bytes
mocked or real) and the routing in _process_attachment_bytes (large PDFs go
through the split path, small ones do not).
"""

import unittest
from io import BytesIO
from unittest.mock import AsyncMock, Mock, patch, call

import pypdf

from backend.services.document_processor import DocumentProcessor
from backend.services.extraction.base import DocumentExtractor, ExtractionChunk
from backend.services.extraction.kreuzberg import KreuzbergTimeoutError, KreuzbergParsingError
from backend.models.document import DocumentMetadata


def _make_doc_metadata(**kwargs) -> DocumentMetadata:
    defaults = dict(library_id="lib1", item_key="ITEM0001", attachment_key="ATT00001")
    defaults.update(kwargs)
    return DocumentMetadata(**defaults)


def _make_chunks(*texts_and_pages) -> list[ExtractionChunk]:
    return [
        ExtractionChunk(text=t, page_number=p, chunk_index=i)
        for i, (t, p) in enumerate(texts_and_pages)
    ]


def _make_pdf(num_pages: int) -> bytes:
    writer = pypdf.PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=612, height=792)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_processor():
    mock_extractor = AsyncMock(spec=DocumentExtractor)
    mock_embedding = AsyncMock()
    mock_vs = Mock()
    mock_vs.check_duplicate.return_value = None
    mock_vs.find_cross_library_duplicate.return_value = None
    mock_vs.add_chunks_batch.return_value = []
    mock_vs.add_deduplication_record.return_value = None
    proc = DocumentProcessor(
        zotero_client=AsyncMock(),
        embedding_service=mock_embedding,
        vector_store=mock_vs,
        document_extractor=mock_extractor,
    )
    return proc, mock_extractor, mock_embedding, mock_vs


# ---------------------------------------------------------------------------
# Tests for _extract_pdf_in_parts
# ---------------------------------------------------------------------------

class TestExtractPdfInParts(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.proc, self.extractor, _, _ = _make_processor()

    async def test_page_offset_applied_to_part2(self):
        # part 1: pages 0-2 (offset=0), part 2: pages 3-5 (offset=3)
        part1_chunks = _make_chunks(("text A", 1), ("text B", 2))
        part2_chunks = _make_chunks(("text C", 1), ("text D", 3))
        self.extractor.extract_and_chunk.side_effect = [part1_chunks, part2_chunks]

        with patch("backend.utils.pdf_splitter.split_pdf_bytes",
                   return_value=[(b"part1", 0), (b"part2", 3)]):
            chunks = await self.proc._extract_pdf_in_parts(b"pdf", "ATT1", 30 * 1024 ** 2)

        pages = [c.page_number for c in chunks]
        # part1: 1+0=1, 2+0=2  |  part2: 1+3=4, 3+3=6
        self.assertEqual(pages, [1, 2, 4, 6])

    async def test_none_page_number_not_offset(self):
        chunks = _make_chunks(("text", None))
        self.extractor.extract_and_chunk.return_value = chunks

        with patch("backend.utils.pdf_splitter.split_pdf_bytes",
                   return_value=[(b"part1", 10)]):
            result = await self.proc._extract_pdf_in_parts(b"pdf", "ATT1", 30 * 1024 ** 2)

        self.assertIsNone(result[0].page_number)

    async def test_chunk_index_resequenced_across_parts(self):
        self.extractor.extract_and_chunk.side_effect = [
            _make_chunks(("a", 1), ("b", 2), ("c", 3)),
            _make_chunks(("d", 1), ("e", 2)),
        ]

        with patch("backend.utils.pdf_splitter.split_pdf_bytes",
                   return_value=[(b"p1", 0), (b"p2", 5)]):
            result = await self.proc._extract_pdf_in_parts(b"pdf", "ATT1", 30 * 1024 ** 2)

        self.assertEqual([c.chunk_index for c in result], [0, 1, 2, 3, 4])

    async def test_timeout_on_one_part_skipped_others_collected(self):
        self.extractor.extract_and_chunk.side_effect = [
            _make_chunks(("ok", 1)),
            KreuzbergTimeoutError("timed out"),
            _make_chunks(("also ok", 1)),
        ]

        with patch("backend.utils.pdf_splitter.split_pdf_bytes",
                   return_value=[(b"p1", 0), (b"p2", 5), (b"p3", 10)]):
            result = await self.proc._extract_pdf_in_parts(b"pdf", "ATT1", 30 * 1024 ** 2)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].text, "ok")
        self.assertEqual(result[1].text, "also ok")

    async def test_parse_error_on_part_skipped(self):
        self.extractor.extract_and_chunk.side_effect = [
            KreuzbergParsingError("bad pdf"),
            _make_chunks(("good", 1)),
        ]

        with patch("backend.utils.pdf_splitter.split_pdf_bytes",
                   return_value=[(b"p1", 0), (b"p2", 5)]):
            result = await self.proc._extract_pdf_in_parts(b"pdf", "ATT1", 30 * 1024 ** 2)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].text, "good")

    async def test_all_parts_timeout_returns_empty(self):
        self.extractor.extract_and_chunk.side_effect = KreuzbergTimeoutError("out")

        with patch("backend.utils.pdf_splitter.split_pdf_bytes",
                   return_value=[(b"p1", 0), (b"p2", 5)]):
            result = await self.proc._extract_pdf_in_parts(b"pdf", "ATT1", 30 * 1024 ** 2)

        self.assertEqual(result, [])

    async def test_split_failure_falls_back_to_whole_file(self):
        self.extractor.extract_and_chunk.return_value = _make_chunks(("fallback", 1))
        whole_pdf = b"whole_pdf_bytes"

        with patch("backend.utils.pdf_splitter.split_pdf_bytes",
                   side_effect=ValueError("corrupt")):
            result = await self.proc._extract_pdf_in_parts(whole_pdf, "ATT1", 30 * 1024 ** 2)

        # extractor must have been called with the original bytes
        self.extractor.extract_and_chunk.assert_called_once_with(whole_pdf, "application/pdf")
        self.assertEqual(len(result), 1)

    async def test_page_offset_zero_on_first_part(self):
        self.extractor.extract_and_chunk.return_value = _make_chunks(("a", 5))

        with patch("backend.utils.pdf_splitter.split_pdf_bytes",
                   return_value=[(b"p1", 0)]):
            result = await self.proc._extract_pdf_in_parts(b"pdf", "ATT1", 30 * 1024 ** 2)

        self.assertEqual(result[0].page_number, 5)  # 5 + 0 = 5, unchanged


# ---------------------------------------------------------------------------
# Tests for routing in _process_attachment_bytes
# ---------------------------------------------------------------------------

class TestProcessAttachmentBytesRouting(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.proc, self.extractor, self.mock_embedding, self.mock_vs = _make_processor()
        self.doc_meta = _make_doc_metadata()

    def _mock_settings(self, threshold_bytes: int = 50 * 1024 ** 2):
        mock_settings = Mock()
        mock_settings.pdf_split_threshold = threshold_bytes
        mock_settings.pdf_split_target_part_size = 30 * 1024 ** 2
        return mock_settings

    async def test_large_pdf_routed_through_split_path(self):
        pdf = _make_pdf(5)

        # Threshold below PDF size, target_part_size=1 forces pages_per_part=1
        # so the splitter produces 5 parts and the extractor is called 5 times.
        mock_settings = Mock()
        mock_settings.pdf_split_threshold = len(pdf) // 2
        mock_settings.pdf_split_target_part_size = 1

        self.extractor.extract_and_chunk.return_value = _make_chunks(("text", 1))
        self.mock_embedding.embed_batch.return_value = [[0.1] * 3] * 10

        with patch("backend.services.document_processor.get_settings",
                   return_value=mock_settings):
            result = await self.proc._process_attachment_bytes(
                file_bytes=pdf,
                mime_type="application/pdf",
                doc_metadata=self.doc_meta,
                item_version=1,
                attachment_version=1,
                item_modified="2026-01-01T00:00:00Z",
            )

        # extractor called once per page → split path was used
        self.assertEqual(self.extractor.extract_and_chunk.call_count, 5)
        self.assertIn(result.status, ("indexed_fresh", "skipped_empty"))

    async def test_small_pdf_uses_direct_path(self):
        pdf = _make_pdf(3)
        # threshold much larger than the PDF
        mock_settings = self._mock_settings(threshold_bytes=len(pdf) * 10)
        self.extractor.extract_and_chunk.return_value = _make_chunks(("text", 1))
        self.mock_embedding.embed_batch.return_value = [[0.1] * 3]

        with patch("backend.services.document_processor.get_settings",
                   return_value=mock_settings):
            result = await self.proc._process_attachment_bytes(
                file_bytes=pdf,
                mime_type="application/pdf",
                doc_metadata=self.doc_meta,
                item_version=1,
                attachment_version=1,
                item_modified="2026-01-01T00:00:00Z",
            )

        self.extractor.extract_and_chunk.assert_called_once()
        self.assertEqual(result.status, "indexed_fresh")

    async def test_non_pdf_not_split_regardless_of_size(self):
        big_html = b"<html>" + b"x" * (100 * 1024 ** 2) + b"</html>"
        # threshold=1 would normally trigger split for a PDF of this size
        mock_settings = self._mock_settings(threshold_bytes=1)
        self.extractor.extract_and_chunk.return_value = _make_chunks(("text", None))
        self.mock_embedding.embed_batch.return_value = [[0.1] * 3]

        with patch("backend.services.document_processor.get_settings",
                   return_value=mock_settings):
            await self.proc._process_attachment_bytes(
                file_bytes=big_html,
                mime_type="text/html",
                doc_metadata=self.doc_meta,
                item_version=1,
                attachment_version=1,
                item_modified="2026-01-01T00:00:00Z",
            )

        # Only one call — split path never entered for non-PDF
        self.extractor.extract_and_chunk.assert_called_once()

    async def test_page_numbers_in_stored_chunks_include_offset(self):
        """End-to-end: page numbers stored in vector store must reflect the offset."""
        pdf = _make_pdf(6)
        threshold = len(pdf) // 2  # force split into ~2 parts

        # part1 (offset=0): kreuzberg says pages 1,2,3
        # part2 (offset=3): kreuzberg says pages 1,2,3  → should become 4,5,6
        call_count = 0

        async def fake_extract(content, mime):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_chunks(("a", 1), ("b", 2), ("c", 3))
            return _make_chunks(("d", 1), ("e", 2), ("f", 3))

        self.extractor.extract_and_chunk.side_effect = fake_extract
        self.mock_embedding.embed_batch.return_value = [[0.1] * 3] * 6
        mock_settings = self._mock_settings(threshold_bytes=threshold)

        with patch("backend.services.document_processor.get_settings",
                   return_value=mock_settings):
            await self.proc._process_attachment_bytes(
                file_bytes=pdf,
                mime_type="application/pdf",
                doc_metadata=self.doc_meta,
                item_version=1,
                attachment_version=1,
                item_modified="2026-01-01T00:00:00Z",
            )

        stored = self.mock_vs.add_chunks_batch.call_args[0][0]
        stored_pages = [c.metadata.page_number for c in stored]
        # Part 1 pages unchanged (offset 0); part 2 pages shifted by part 1 page count
        self.assertEqual(stored_pages[:3], [1, 2, 3])
        self.assertTrue(all(p > 3 for p in stored_pages[3:]),
                        f"Expected part-2 pages > 3, got {stored_pages[3:]}")


if __name__ == "__main__":
    unittest.main()
