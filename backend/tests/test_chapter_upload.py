"""Unit tests for backend.services.chapter_upload."""

import io
import unittest

from pypdf import PdfReader, PdfWriter

from backend.services.chapter_upload import slice_pdf_range


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


if __name__ == "__main__":
    unittest.main()
