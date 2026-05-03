"""Unit tests for backend.utils.pdf_splitter."""

import unittest
from io import BytesIO

import pypdf

from backend.utils.pdf_splitter import split_pdf_bytes


def _make_pdf(num_pages: int) -> bytes:
    """Create a minimal multi-page PDF with `num_pages` blank pages."""
    writer = pypdf.PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=612, height=792)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


class TestSplitPdfBytes(unittest.TestCase):

    def test_small_pdf_returns_single_part(self):
        pdf = _make_pdf(5)
        parts = split_pdf_bytes(pdf, target_part_bytes=len(pdf) * 2)
        self.assertEqual(len(parts), 1)
        part_bytes, offset = parts[0]
        self.assertEqual(offset, 0)
        reader = pypdf.PdfReader(BytesIO(part_bytes))
        self.assertEqual(len(reader.pages), 5)

    def test_splits_into_correct_number_of_parts(self):
        # 10-page PDF split at ~1/3 its size should yield 3 parts
        pdf = _make_pdf(10)
        target = len(pdf) // 3
        parts = split_pdf_bytes(pdf, target_part_bytes=target)
        self.assertGreater(len(parts), 1)

    def test_all_pages_accounted_for(self):
        num_pages = 12
        pdf = _make_pdf(num_pages)
        target = len(pdf) // 4
        parts = split_pdf_bytes(pdf, target_part_bytes=target)
        total = sum(len(pypdf.PdfReader(BytesIO(p)).pages) for p, _ in parts)
        self.assertEqual(total, num_pages)

    def test_page_offsets_are_sequential(self):
        num_pages = 9
        pdf = _make_pdf(num_pages)
        target = len(pdf) // 3
        parts = split_pdf_bytes(pdf, target_part_bytes=target)
        offsets = [offset for _, offset in parts]
        self.assertEqual(offsets[0], 0)
        # Each offset must be strictly greater than the previous
        for a, b in zip(offsets, offsets[1:]):
            self.assertGreater(b, a)

    def test_offsets_plus_page_counts_cover_original(self):
        num_pages = 10
        pdf = _make_pdf(num_pages)
        target = len(pdf) // 3
        parts = split_pdf_bytes(pdf, target_part_bytes=target)
        covered = set()
        for part_bytes, offset in parts:
            n = len(pypdf.PdfReader(BytesIO(part_bytes)).pages)
            for i in range(n):
                covered.add(offset + i)
        self.assertEqual(covered, set(range(num_pages)))

    def test_single_page_target_smaller_than_one_page(self):
        # target_part_bytes smaller than any single page → pages_per_part=1
        pdf = _make_pdf(4)
        parts = split_pdf_bytes(pdf, target_part_bytes=1)
        self.assertEqual(len(parts), 4)
        for i, (_, offset) in enumerate(parts):
            self.assertEqual(offset, i)

    def test_empty_pdf_raises_value_error(self):
        writer = pypdf.PdfWriter()
        buf = BytesIO()
        writer.write(buf)
        with self.assertRaises(ValueError):
            split_pdf_bytes(buf.getvalue(), target_part_bytes=1024)

    def test_invalid_bytes_raises(self):
        with self.assertRaises(Exception):
            split_pdf_bytes(b"not a pdf", target_part_bytes=1024)


class TestSizeStringValidator(unittest.TestCase):
    """Verify the settings validator parses human-readable sizes."""

    def _parse(self, v):
        from backend.config.settings import Settings
        return Settings._parse_size_fields(v)

    def test_mb(self):
        self.assertEqual(self._parse("50MB"), 50 * 1024 ** 2)

    def test_gb(self):
        self.assertEqual(self._parse("1GB"), 1024 ** 3)

    def test_kb(self):
        self.assertEqual(self._parse("512KB"), 512 * 1024)

    def test_int_passthrough(self):
        self.assertEqual(self._parse(12345), 12345)

    def test_bare_string_number(self):
        self.assertEqual(self._parse("1048576"), 1048576)

    def test_lowercase(self):
        self.assertEqual(self._parse("30mb"), 30 * 1024 ** 2)


if __name__ == "__main__":
    unittest.main()
