"""Unit tests for backend.services.chapter_retrofit."""

import tempfile
import unittest
from pathlib import Path as _TestPath
from unittest.mock import MagicMock

from backend.config.settings import get_settings, reset_settings
from backend.services.chapter_retrofit import find_best_book_match, find_matches, commit_links, locate_chapter_pdf_range
from backend.services.chapter_retrofit import run as retrofit_run
from backend.services.review_queue_store import get_entry


class TestFindBestBookMatch(unittest.TestCase):
    def setUp(self):
        self.books = [
            {"key": "BOOK1", "title": "Handbook of Reference Management", "year": 2019},
            {"key": "BOOK2", "title": "Introduction to Bibliographic Software", "year": 2021},
        ]

    def test_matches_exact_title_and_year(self):
        result = find_best_book_match("Handbook of Reference Management", 2019, self.books)
        self.assertIsNotNone(result)
        self.assertEqual(result.book_key, "BOOK1")
        self.assertGreaterEqual(result.score, 0.9)

    def test_matches_within_year_tolerance(self):
        result = find_best_book_match("Handbook of Reference Management", 2020, self.books)
        self.assertIsNotNone(result)
        self.assertEqual(result.book_key, "BOOK1")

    def test_rejects_year_outside_tolerance(self):
        result = find_best_book_match("Handbook of Reference Management", 2023, self.books)
        self.assertIsNone(result)

    def test_no_candidates_returns_none(self):
        result = find_best_book_match("Something Entirely Different", 2019, self.books)
        self.assertIsNone(result)

    def test_ambiguous_close_scores_returns_none(self):
        books = [
            {"key": "A", "title": "Studies in Modern History", "year": 2020},
            {"key": "B", "title": "Studies in Modern History Vol 2", "year": 2020},
        ]
        result = find_best_book_match("Studies in Modern History", 2020, books)
        self.assertIsNone(result)


class TestLocateChapterPdfRange(unittest.TestCase):
    def test_finds_contiguous_span(self):
        book_pages = [
            "Front matter, nothing relevant here.",
            "Comparing Citation Styles\nThis chapter examines APA and MLA styles in depth.",
            "...continued examination of citation styles and their history.",
            "Unrelated next chapter begins here with different content entirely.",
        ]
        chapter_text = "Comparing Citation Styles\nThis chapter examines APA and MLA styles in depth. ...continued examination of citation styles and their history."
        result = locate_chapter_pdf_range(chapter_text, book_pages)
        self.assertEqual(result, (1, 2))

    def test_returns_none_when_no_confident_span(self):
        book_pages = ["Completely unrelated content about gardening techniques."]
        chapter_text = "This is about astrophysics and black holes entirely."
        result = locate_chapter_pdf_range(chapter_text, book_pages)
        self.assertIsNone(result)


class TestFindMatches(unittest.TestCase):
    def test_matches_without_touching_zotero_client(self):
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
        ]
        result = find_matches(all_items, item_keys=None, max_items=None)
        self.assertEqual(len(result["would_link"]), 1)
        self.assertEqual(result["would_link"][0]["chapter_key"], "CHAP1")
        self.assertEqual(result["would_link"][0]["book_key"], "BOOK1")
        self.assertEqual(result["ambiguous"], [])
        self.assertEqual(result["no_match"], [])

    def test_already_linked_chapter_is_excluded(self):
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": "X-Contained-By: groups/1:BOOK1"}},
        ]
        result = find_matches(all_items, item_keys=None, max_items=None)
        self.assertEqual(result["would_link"], [])

    def test_item_keys_restricts_which_chapters_are_considered(self):
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP2", "data": {"key": "CHAP2", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
        ]
        result = find_matches(all_items, item_keys=["CHAP2"], max_items=None)
        self.assertEqual({e["chapter_key"] for e in result["would_link"]}, {"CHAP2"})

    def test_no_book_title_is_no_match(self):
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "", "date": "2019", "extra": ""}},
        ]
        result = find_matches(all_items, item_keys=None, max_items=None)
        self.assertEqual(result["no_match"], ["CHAP1"])


class TestCommitLinks(unittest.TestCase):
    def test_writes_links_without_full_fetch(self):
        zot = MagicMock()
        book_item = {"key": "BOOK1", "data": {"key": "BOOK1", "extra": ""}}
        chapter_item = {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}
        zot.item.side_effect = lambda key: {"BOOK1": book_item, "CHAP1": chapter_item}[key]

        result = commit_links(zot, "groups/1", [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}])

        zot.everything.assert_not_called()
        self.assertEqual(result["linked"], [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}])
        self.assertEqual(zot.update_item.call_count, 2)
        first_call_arg = zot.update_item.call_args_list[0].args[0]
        self.assertEqual(first_call_arg["data"]["key"], "BOOK1")

    def test_write_failure_is_isolated_per_entry(self):
        zot = MagicMock()
        book_item = {"key": "BOOK1", "data": {"key": "BOOK1", "extra": ""}}
        chapter_item = {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}
        zot.item.side_effect = lambda key: {"BOOK1": book_item, "CHAP1": chapter_item}[key]
        zot.update_item.side_effect = [None, Exception("boom")]

        result = commit_links(zot, "groups/1", [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}])

        self.assertEqual(result["linked"], [])
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["chapter_key"], "CHAP1")
        self.assertIn("boom", result["failed"][0]["error"])

    def test_multiple_entries_each_processed_independently(self):
        zot = MagicMock()
        items = {
            "BOOK1": {"key": "BOOK1", "data": {"key": "BOOK1", "extra": ""}},
            "CHAP1": {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}},
            "BOOK2": {"key": "BOOK2", "data": {"key": "BOOK2", "extra": ""}},
            "CHAP2": {"key": "CHAP2", "data": {"key": "CHAP2", "extra": ""}},
        }
        zot.item.side_effect = lambda key: items[key]

        result = commit_links(zot, "groups/1", [
            {"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0},
            {"chapter_key": "CHAP2", "book_key": "BOOK2", "score": 0.95},
        ])

        self.assertEqual(len(result["linked"]), 2)
        self.assertEqual({e["chapter_key"] for e in result["linked"]}, {"CHAP1", "CHAP2"})

    def test_malformed_entry_is_isolated_as_failure(self):
        zot = MagicMock()
        items = {
            "BOOK1": {"key": "BOOK1", "data": {"key": "BOOK1", "extra": ""}},
            "CHAP1": {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}},
        }
        zot.item.side_effect = lambda key: items[key]

        result = commit_links(zot, "groups/1", [
            {"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0},
            {"chapter_key": "CHAP2"},  # missing "book_key" -- must not raise
        ])

        self.assertEqual(len(result["linked"]), 1)
        self.assertEqual(result["linked"][0]["chapter_key"], "CHAP1")
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["chapter_key"], "CHAP2")
        self.assertEqual(result["failed"][0]["book_key"], "?")
        self.assertIn("book_key", result["failed"][0]["error"])

    def test_sets_native_relations_on_book_only_relies_on_server_auto_mirror(self):
        # Only the BOOK side is written explicitly. Zotero's API auto-mirrors
        # a relation onto the item it points at, so the chapter side is
        # expected to pick up the reverse link server-side (via the re-fetch
        # right before the chapter's own PATCH), not via a second manual
        # write here -- a MagicMock has no such auto-mirroring, so this test
        # only asserts the one write this code path actually performs.
        zot = MagicMock()
        book_item = {"key": "BOOK1", "data": {"key": "BOOK1", "extra": ""}}
        chapter_item = {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}
        zot.item.side_effect = lambda key: {"BOOK1": book_item, "CHAP1": chapter_item}[key]

        commit_links(zot, "groups/1", [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}])

        book_update = zot.update_item.call_args_list[0].args[0]
        chapter_update = zot.update_item.call_args_list[1].args[0]
        self.assertIn("http://zotero.org/groups/1/items/CHAP1", book_update["data"]["relations"]["dc:relation"])
        self.assertNotIn("relations", chapter_update["data"])

    def test_target_collection_none_by_default_no_collection_calls(self):
        zot = MagicMock()
        book_item = {"key": "BOOK1", "data": {"key": "BOOK1", "extra": ""}}
        chapter_item = {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}
        zot.item.side_effect = lambda key: {"BOOK1": book_item, "CHAP1": chapter_item}[key]

        commit_links(zot, "groups/1", [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}])

        zot.collections.assert_not_called()
        zot.create_collection.assert_not_called()
        self.assertNotIn("collections", chapter_item["data"])

    def test_target_collection_given_files_chapter_into_subcollection(self):
        zot = MagicMock()
        book_item = {
            "key": "BOOK1",
            "data": {"key": "BOOK1", "extra": "", "creators": [{"lastName": "Miller"}], "date": "2020"},
        }
        chapter_item = {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}
        zot.item.side_effect = lambda key: {"BOOK1": book_item, "CHAP1": chapter_item}[key]
        zot.collections.return_value = []
        zot.create_collection.return_value = {"successful": {"0": {"key": "TOPKEY01"}}}
        zot.collections_sub.return_value = []

        commit_links(
            zot, "groups/1", [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}],
            target_collection="My Collection",
        )

        # ensure_target_collection calls create_collection twice: once for
        # the top-level "My Collection", once for the "Miller (2020)" sub.
        self.assertEqual(zot.create_collection.call_count, 2)
        self.assertIn("collections", chapter_item["data"])
        self.assertNotIn("collections", book_item["data"])
        # Extra + relations + collections all folded into the SAME single
        # update_item(chapter_item) call -- confirmed by the call count
        # staying at 2 total (one for the book, one for the chapter).
        self.assertEqual(zot.update_item.call_count, 2)

    def test_collection_resolution_failure_isolated_as_per_entry_failure(self):
        zot = MagicMock()
        book_item = {"key": "BOOK1", "data": {"key": "BOOK1", "extra": "", "creators": [], "date": "2020"}}
        chapter_item = {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}
        zot.item.side_effect = lambda key: {"BOOK1": book_item, "CHAP1": chapter_item}[key]
        zot.collections.side_effect = Exception("network error")

        result = commit_links(
            zot, "groups/1", [{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}],
            target_collection="My Collection",
        )

        self.assertEqual(result["linked"], [])
        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("network error", result["failed"][0]["error"])


class TestRetrofitRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset_settings()
        get_settings().review_queue_path = _TestPath(self.tmp.name) / "review_queue.json"

    def tearDown(self):
        self.tmp.cleanup()
        reset_settings()

    def test_links_confident_match_and_skips_ambiguous(self):
        zot = MagicMock()
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
        ]
        zot.everything.return_value = all_items
        zot.item.side_effect = lambda key: next(i for i in all_items if i["key"] == key)

        result = retrofit_run(
            zotero_write_client=zot,
            slug="groups/1",
            item_keys=None,
            max_items=None,
            commit=True,
        )
        self.assertEqual(len(result["linked"]), 1)
        self.assertEqual(result["linked"][0]["chapter_key"], "CHAP1")
        self.assertEqual(result["linked"][0]["book_key"], "BOOK1")
        zot.update_item.assert_called()

    def test_book_write_succeeds_but_chapter_write_fails_reports_failed(self):
        zot = MagicMock()
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
        ]
        zot.everything.return_value = all_items
        zot.item.side_effect = lambda key: next(i for i in all_items if i["key"] == key)
        # First update_item call (the book, written first) succeeds; the
        # second call (the chapter) raises, simulating a version conflict.
        zot.update_item.side_effect = [None, Exception("boom")]

        result = retrofit_run(
            zotero_write_client=zot,
            slug="groups/1",
            item_keys=None,
            max_items=None,
            commit=True,
        )

        self.assertEqual(result["linked"], [])
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["chapter_key"], "CHAP1")
        self.assertEqual(result["failed"][0]["book_key"], "BOOK1")
        self.assertIn("boom", result["failed"][0]["error"])

        # Confirm the book write was attempted before the chapter write
        # that raised: update_item's first call must have been for BOOK1.
        first_call_arg = zot.update_item.call_args_list[0].args[0]
        self.assertEqual(first_call_arg["data"]["key"], "BOOK1")
        self.assertEqual(zot.update_item.call_count, 2)

    def test_defaults_to_dry_run_and_writes_nothing(self):
        zot = MagicMock()
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
        ]
        zot.everything.return_value = all_items
        zot.item.side_effect = lambda key: next(i for i in all_items if i["key"] == key)

        result = retrofit_run(
            zotero_write_client=zot,
            slug="groups/1",
            item_keys=None,
            max_items=None,
        )

        zot.update_item.assert_not_called()
        self.assertEqual(result["linked"], [])
        self.assertEqual(len(result["would_link"]), 1)
        self.assertEqual(result["would_link"][0]["chapter_key"], "CHAP1")
        self.assertEqual(result["would_link"][0]["book_key"], "BOOK1")

    def test_commit_with_would_link_skips_full_fetch(self):
        zot = MagicMock()
        book_item = {"key": "BOOK1", "data": {"key": "BOOK1", "extra": ""}}
        chapter_item = {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}
        zot.item.side_effect = lambda key: {"BOOK1": book_item, "CHAP1": chapter_item}[key]

        result = retrofit_run(
            zotero_write_client=zot,
            slug="groups/1",
            item_keys=None,
            max_items=None,
            commit=True,
            would_link=[{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}],
        )

        zot.everything.assert_not_called()
        self.assertEqual(len(result["linked"]), 1)
        self.assertEqual(result["linked"][0]["chapter_key"], "CHAP1")
        self.assertEqual(result["ambiguous"], [])
        self.assertEqual(result["no_match"], [])
        self.assertEqual(result["would_link"], [])

    def test_commit_without_would_link_still_does_full_fetch(self):
        zot = MagicMock()
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
        ]
        zot.everything.return_value = all_items
        zot.item.side_effect = lambda key: next(i for i in all_items if i["key"] == key)

        result = retrofit_run(zotero_write_client=zot, slug="groups/1", item_keys=None, max_items=None, commit=True)

        zot.everything.assert_called_once()
        self.assertEqual(len(result["linked"]), 1)

    def test_dry_run_ignores_would_link_and_still_matches_fresh(self):
        zot = MagicMock()
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
        ]
        zot.everything.return_value = all_items
        zot.item.side_effect = lambda key: next(i for i in all_items if i["key"] == key)

        result = retrofit_run(
            zotero_write_client=zot, slug="groups/1", item_keys=None, max_items=None,
            commit=False, would_link=[{"chapter_key": "IGNORED", "book_key": "IGNORED", "score": 1.0}],
        )

        zot.update_item.assert_not_called()
        self.assertEqual(result["would_link"][0]["chapter_key"], "CHAP1")

    def test_commit_with_empty_would_link_list_short_circuits_to_noop(self):
        zot = MagicMock()

        result = retrofit_run(
            zotero_write_client=zot,
            slug="groups/1",
            item_keys=None,
            max_items=None,
            commit=True,
            would_link=[],
        )

        zot.everything.assert_not_called()
        zot.item.assert_not_called()
        self.assertEqual(result, {"linked": [], "failed": [], "would_link": [], "ambiguous": [], "no_match": []})

    def test_target_collection_threaded_through_to_commit_links(self):
        zot = MagicMock()
        book_item = {"key": "BOOK1", "data": {"key": "BOOK1", "extra": ""}}
        chapter_item = {"key": "CHAP1", "data": {"key": "CHAP1", "extra": ""}}
        zot.item.side_effect = lambda key: {"BOOK1": book_item, "CHAP1": chapter_item}[key]
        zot.collections.return_value = []
        zot.create_collection.return_value = {"successful": {"0": {"key": "TOPKEY01"}}}
        zot.collections_sub.return_value = []

        retrofit_run(
            zotero_write_client=zot, slug="groups/1", item_keys=None, max_items=None,
            commit=True, would_link=[{"chapter_key": "CHAP1", "book_key": "BOOK1", "score": 1.0}],
            target_collection="My Collection",
        )

        self.assertIn("collections", chapter_item["data"])

    def test_dry_run_upserts_would_link_as_commit_bucket(self):
        zot = MagicMock()
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Handbook of Reference Management", "date": "2019", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Handbook of Reference Management", "date": "2019", "extra": ""}},
        ]
        zot.everything.return_value = all_items

        retrofit_run(zotero_write_client=zot, slug="groups/1", item_keys=None, max_items=None, commit=False)

        entry = get_entry(get_settings().review_queue_path, "groups/1", "match:CHAP1")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["bucket"], "commit")
        self.assertEqual(entry["payload"]["book_key"], "BOOK1")

    def test_dry_run_upserts_ambiguous_as_review_bucket(self):
        zot = MagicMock()
        # NOTE: uses the same close-title pattern as
        # TestFindBestBookMatch.test_ambiguous_close_scores_returns_none
        # ("Studies in Modern History" vs "...Vol 2" -> token_sort_ratio
        # margin ~10.7, just under _MARGIN_REQUIRED=11.0). A pair like
        # "Same Title" / "Same Title Vol 2" produces a much larger margin
        # (~23) and would resolve as a confident match, not ambiguous.
        all_items = [
            {"key": "BOOK1", "data": {"key": "BOOK1", "itemType": "book", "title": "Studies in Modern History", "date": "2020", "extra": ""}},
            {"key": "BOOK2", "data": {"key": "BOOK2", "itemType": "book", "title": "Studies in Modern History Vol 2", "date": "2020", "extra": ""}},
            {"key": "CHAP1", "data": {"key": "CHAP1", "itemType": "bookSection", "bookTitle": "Studies in Modern History", "date": "2020", "extra": ""}},
        ]
        zot.everything.return_value = all_items

        retrofit_run(zotero_write_client=zot, slug="groups/1", item_keys=None, max_items=None, commit=False)

        entry = get_entry(get_settings().review_queue_path, "groups/1", "match:CHAP1")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["bucket"], "review")
        self.assertEqual(len(entry["payload"]["candidates"]), 2)


if __name__ == "__main__":
    unittest.main()
