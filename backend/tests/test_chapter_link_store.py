"""Unit tests for backend.services.chapter_link_store."""

import unittest

from backend.services.chapter_link_store import (
    ChapterLinks,
    format_chapter_id,
    parse_library_slug,
    parse_links,
    write_links,
)


class TestParseLibrarySlug(unittest.TestCase):
    def test_user_slug(self):
        self.assertEqual(parse_library_slug("users/12345"), ("user", "12345", "u12345"))

    def test_group_slug(self):
        self.assertEqual(parse_library_slug("groups/6297749"), ("group", "6297749", "6297749"))


class TestFormatChapterId(unittest.TestCase):
    def test_format(self):
        self.assertEqual(format_chapter_id("groups/6297749", "WXYZ5678"), "groups/6297749:WXYZ5678")


class TestParseLinks(unittest.TestCase):
    def test_empty_extra(self):
        links = parse_links("")
        self.assertIsNone(links.contained_by)
        self.assertEqual(links.contains, [])
        self.assertEqual(links.pdf_ranges, {})

    def test_parses_all_three_keys(self):
        extra = (
            "Some other line: value\n"
            "X-Contained-By: groups/6297749:ABCD1234\n"
            "X-Contains: groups/6297749:WXYZ5678,groups/6297749:MNOP9012\n"
            "X-Chapter-Pdf-Range: groups/6297749:WXYZ5678:52-74,groups/6297749:MNOP9012:75-98\n"
        )
        links = parse_links(extra)
        self.assertEqual(links.contained_by, "groups/6297749:ABCD1234")
        self.assertEqual(
            links.contains, ["groups/6297749:WXYZ5678", "groups/6297749:MNOP9012"]
        )
        self.assertEqual(
            links.pdf_ranges,
            {
                "groups/6297749:WXYZ5678": (52, 74),
                "groups/6297749:MNOP9012": (75, 98),
            },
        )

    def test_ignores_unrelated_lines(self):
        links = parse_links("Citation Key: smith2023\nOther: stuff")
        self.assertIsNone(links.contained_by)
        self.assertEqual(links.contains, [])


class TestWriteLinks(unittest.TestCase):
    def test_appends_to_empty_extra(self):
        result = write_links("", contained_by="groups/1:AAAA1111")
        self.assertEqual(result, "X-Contained-By: groups/1:AAAA1111")

    def test_preserves_unrelated_content(self):
        result = write_links("Citation Key: smith2023", contains=["groups/1:BBBB2222"])
        self.assertIn("Citation Key: smith2023", result)
        self.assertIn("X-Contains: groups/1:BBBB2222", result)

    def test_replaces_existing_line_not_appends_duplicate(self):
        extra = "X-Contains: groups/1:OLD0000"
        result = write_links(extra, contains=["groups/1:NEW1111"])
        self.assertEqual(result.count("X-Contains:"), 1)
        self.assertIn("groups/1:NEW1111", result)
        self.assertNotIn("OLD0000", result)

    def test_idempotent(self):
        once = write_links("", contained_by="groups/1:AAAA1111", contains=["groups/1:BBBB2222"])
        twice = write_links(once, contained_by="groups/1:AAAA1111", contains=["groups/1:BBBB2222"])
        self.assertEqual(once, twice)

    def test_round_trip_with_pdf_ranges(self):
        extra = write_links(
            "",
            contains=["groups/1:BBBB2222"],
            pdf_ranges={"groups/1:BBBB2222": (52, 74)},
        )
        links = parse_links(extra)
        self.assertEqual(links.pdf_ranges, {"groups/1:BBBB2222": (52, 74)})

    def test_untouched_keys_left_as_is(self):
        extra = "X-Contained-By: groups/1:AAAA1111"
        result = write_links(extra, contains=["groups/1:BBBB2222"])
        self.assertIn("X-Contained-By: groups/1:AAAA1111", result)
        self.assertIn("X-Contains: groups/1:BBBB2222", result)


if __name__ == "__main__":
    unittest.main()
