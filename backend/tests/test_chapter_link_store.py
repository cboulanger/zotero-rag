"""Unit tests for backend.services.chapter_link_store."""

import unittest
from unittest.mock import MagicMock

from backend.services.chapter_link_store import (
    ChapterLinks,
    add_related_item,
    author_year_label,
    ensure_target_collection,
    format_chapter_id,
    parse_library_slug,
    parse_links,
    write_links,
    zotero_item_uri,
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


class TestZoteroItemUri(unittest.TestCase):
    def test_group_slug(self):
        self.assertEqual(
            zotero_item_uri("groups/6297749", "ABCD1234"),
            "http://zotero.org/groups/6297749/items/ABCD1234",
        )

    def test_user_slug(self):
        self.assertEqual(
            zotero_item_uri("users/12345", "WXYZ5678"),
            "http://zotero.org/users/12345/items/WXYZ5678",
        )


class TestAddRelatedItem(unittest.TestCase):
    def test_adds_to_empty_relations(self):
        result = add_related_item({}, "http://zotero.org/groups/1/items/AAAA1111")
        self.assertEqual(result, {"dc:relation": ["http://zotero.org/groups/1/items/AAAA1111"]})

    def test_idempotent_no_duplicate(self):
        once = add_related_item({}, "http://zotero.org/groups/1/items/AAAA1111")
        twice = add_related_item(once, "http://zotero.org/groups/1/items/AAAA1111")
        self.assertEqual(twice["dc:relation"], ["http://zotero.org/groups/1/items/AAAA1111"])

    def test_normalizes_existing_bare_string_to_list(self):
        result = add_related_item(
            {"dc:relation": "http://zotero.org/groups/1/items/OLD0000"},
            "http://zotero.org/groups/1/items/NEW1111",
        )
        self.assertEqual(
            result["dc:relation"],
            ["http://zotero.org/groups/1/items/OLD0000", "http://zotero.org/groups/1/items/NEW1111"],
        )

    def test_preserves_unrelated_relation_types(self):
        result = add_related_item(
            {"owl:sameAs": ["http://zotero.org/groups/1/items/DUPE0000"]},
            "http://zotero.org/groups/1/items/NEW1111",
        )
        self.assertEqual(result["owl:sameAs"], ["http://zotero.org/groups/1/items/DUPE0000"])
        self.assertEqual(result["dc:relation"], ["http://zotero.org/groups/1/items/NEW1111"])

    def test_does_not_mutate_input(self):
        original = {"dc:relation": ["http://zotero.org/groups/1/items/OLD0000"]}
        add_related_item(original, "http://zotero.org/groups/1/items/NEW1111")
        self.assertEqual(original["dc:relation"], ["http://zotero.org/groups/1/items/OLD0000"])


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


if __name__ == "__main__":
    unittest.main()
