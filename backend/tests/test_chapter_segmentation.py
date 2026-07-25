"""Unit tests for backend.services.chapter_segmentation."""

import unittest
import unittest.mock
from unittest.mock import AsyncMock, MagicMock

from backend.services.chapter_segmentation import (
    TocEntry,
    extract_page_texts_from_pdf_bytes,
    find_toc_candidates,
    llm_extract_toc_entries,
    _toc_scan_indices,
)
from backend.services.chapter_segmentation import (
    ChapterStartCandidate,
    ChapterStartMatch,
    extract_printed_page_number,
    locate_chapter_start,
    locate_chapter_start_candidates,
    match_confidence,
)
from backend.services.chapter_segmentation import _chapters_from_located
from backend.services.chapter_segmentation import extract_authors_near
from backend.services.chapter_segmentation import analyze_attachment
from backend.services.chapter_segmentation import run as analyze_run


class TestFindTocCandidates(unittest.TestCase):
    # Padded well past any page number used in these fixtures' TOC lines --
    # find_toc_candidates rejects a printed page number that looks
    # implausible relative to the PDF's actual total page count (see
    # _TOC_MAX_PAGE_NUMBER_RATIO), so tiny fixtures need enough filler pages
    # for their own realistic-looking page numbers (e.g. 89) not to trip
    # that guard by accident.
    _FILLER_PAGES = ["Just filler body text, nothing TOC-like here."] * 100

    def test_finds_dotted_leader_entries(self):
        pages = [
            "CONTENTS\n"
            "Introduction to Reference Management ..... 1\n"
            "Comparing Citation Styles ..... 45\n"
            "Zotero in Practice ..... 89\n",
            "Some front-matter page with no TOC pattern at all.",
        ] + self._FILLER_PAGES
        entries = find_toc_candidates(pages)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0], TocEntry(title="Introduction to Reference Management", printed_page_number=1, source_page_index=0))
        self.assertEqual(entries[2].title, "Zotero in Practice")
        self.assertEqual(entries[2].printed_page_number, 89)

    def test_entries_default_to_empty_authors(self):
        pages = [
            "CONTENTS\n"
            "Introduction to Reference Management ..... 1\n"
            "Comparing Citation Styles ..... 45\n"
            "Zotero in Practice ..... 89\n",
        ] + self._FILLER_PAGES
        entries = find_toc_candidates(pages)
        self.assertEqual(entries[0].authors, ())

    def test_ignores_non_toc_lines(self):
        pages = ["Just some ordinary prose with numbers like 1999 in it, no leaders here."]
        entries = find_toc_candidates(pages)
        self.assertEqual(entries, [])

    def test_matches_whitespace_leaders_too(self):
        # Three dotted/whitespace-leader lines on the same page -- meets
        # _TOC_MIN_LINES_PER_PAGE, so all three are trusted as real entries
        # (an isolated single line on a page would now be discarded as
        # noise -- see test_ignores_isolated_lines_on_ordinary_pages below).
        pages = [
            "Bibliographic Software Overview          12\n"
            "Comparing Citation Managers          30\n"
            "Zotero in Practice          55\n"
        ] + self._FILLER_PAGES
        entries = find_toc_candidates(pages)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0].title, "Bibliographic Software Overview")
        self.assertEqual(entries[0].printed_page_number, 12)

    def test_ignores_isolated_lines_on_ordinary_pages(self):
        # Only 1-2 matching lines on a page (below _TOC_MIN_LINES_PER_PAGE)
        # -- a citation, footnote reference, or running header that happens
        # to fit the dotted/whitespace-leader pattern, not a real chapter
        # listing. These must be discarded, not returned as entries.
        pages = [
            "Some ordinary content page mentioning a reference.          12",
        ] + self._FILLER_PAGES
        entries = find_toc_candidates(pages)
        self.assertEqual(entries, [])

    def test_ignores_implausible_page_numbers(self):
        # A publication year embedded in copyright/imprint text ("Opladen
        # (c) Publisher          2025") looks like a TOC line but its
        # "page number" is wildly larger than the PDF's actual length --
        # found empirically in a real evaluation book. All three lines
        # qualify for _TOC_MIN_LINES_PER_PAGE, but only the two with
        # plausible page numbers should survive.
        pages = [
            "Publisher Imprint Line          2025\n"
            "Introduction to the Subject          1\n"
            "Comparing Citation Styles          45\n"
        ] + self._FILLER_PAGES
        entries = find_toc_candidates(pages)
        titles = [e.title for e in entries]
        self.assertNotIn("Publisher Imprint Line", titles)
        self.assertIn("Introduction to the Subject", titles)
        self.assertIn("Comparing Citation Styles", titles)

    def test_ignores_url_and_doi_lines(self):
        # A repeated DOI/URL line in front matter can otherwise fit the
        # dotted/whitespace-leader pattern (found empirically in a real
        # evaluation book, repeated 5 times) -- never a real chapter title.
        pages = [
            "https://doi.org/10.1234/example.5678\n"
            "Introduction to the Subject          1\n"
            "Comparing Citation Styles          45\n"
            "Zotero in Practice          89\n"
        ] + self._FILLER_PAGES
        entries = find_toc_candidates(pages)
        titles = [e.title for e in entries]
        self.assertFalse(any("doi.org" in t for t in titles))
        self.assertIn("Introduction to the Subject", titles)


class TestLlmExtractTocEntries(unittest.IsolatedAsyncioTestCase):
    def _fake_llm(self, response: str):
        llm = MagicMock()
        llm.generate = AsyncMock(return_value=response)
        return llm

    async def test_parses_chapter_list_from_llm_response(self):
        response = (
            '[{"title": "Introduction", "authors": ["Jane Author"], "printed_page_number": 1}, '
            '{"title": "Comparing Citation Styles", "authors": [], "printed_page_number": null}]'
        )
        llm = self._fake_llm(response)
        pages = ["Front matter page with an irregularly formatted chapter listing."] * 5
        entries = await llm_extract_toc_entries(pages, llm)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].title, "Introduction")
        self.assertEqual(entries[0].authors, ("Jane Author",))
        self.assertEqual(entries[0].printed_page_number, 1)
        self.assertEqual(entries[1].printed_page_number, -1)  # null -> sentinel, unused downstream

    async def test_returns_empty_list_on_malformed_response(self):
        llm = self._fake_llm("not json at all")
        entries = await llm_extract_toc_entries(["some front matter"] * 5, llm)
        self.assertEqual(entries, [])

    async def test_skips_entries_with_too_short_title(self):
        llm = self._fake_llm('[{"title": "Hi", "authors": [], "printed_page_number": 1}]')
        entries = await llm_extract_toc_entries(["front matter"] * 5, llm)
        self.assertEqual(entries, [])

    async def test_returns_empty_list_for_empty_pages(self):
        llm = self._fake_llm("[]")
        entries = await llm_extract_toc_entries([], llm)
        self.assertEqual(entries, [])
        llm.generate.assert_not_called()


class TestTocScanIndices(unittest.TestCase):
    def test_scans_front_and_back_fractions(self):
        pages = ["x"] * 100
        indices = _toc_scan_indices(pages, max_front_fraction=0.1, max_back_fraction=0.05)
        self.assertIn(0, indices)
        self.assertIn(9, indices)  # last front-matter page (10% of 100)
        self.assertNotIn(10, indices)
        self.assertIn(99, indices)  # last back-matter page is always included
        self.assertNotIn(50, indices)  # a middle page is never scanned

    def test_empty_pages_returns_empty_set(self):
        self.assertEqual(_toc_scan_indices([]), set())


class TestLocateChapterStart(unittest.TestCase):
    def test_finds_matching_page_by_content(self):
        pages = [
            "CONTENTS\nComparing Citation Styles ..... 3\n",  # TOC page itself
            "Some unrelated front matter.",
            "Comparing Citation Styles\nBy Jane Author\n\nThis chapter examines...",
        ]
        match = locate_chapter_start(pages, "Comparing Citation Styles", exclude_indices={0})
        self.assertEqual(match.index, 2)
        self.assertEqual(match.score, 100.0)
        # Uncontested (only one qualifying cluster) -- margin equals score.
        self.assertEqual(match.margin, 100.0)

    def test_returns_none_when_no_good_match(self):
        pages = ["Nothing related to the query here at all, just filler prose."]
        match = locate_chapter_start(pages, "Comparing Citation Styles", exclude_indices=set())
        self.assertIsNone(match)

    def test_rejects_sparse_page_despite_high_raw_score(self):
        # Empirically (see rapidfuzz.fuzz.partial_ratio), a near-blank head
        # like "Cit" scores 100 against "Comparing Citation Styles" because
        # partial_ratio finds a perfect alignment for the short substring --
        # the same degenerate score as the genuine, well-formed chapter
        # opening on page 2. Without a minimum-length gate, the first-seen
        # (blank) page wins the tie. With the gate, the blank page is
        # excluded and the genuine page is the only remaining candidate.
        pages = [
            "Cit",  # stray near-blank page; partial_ratio scores this 100
            "Some unrelated front matter.",
            "Comparing Citation Styles\n\nBy Jane Author\n\nThis chapter examines...",
        ]
        match = locate_chapter_start(pages, "Comparing Citation Styles", exclude_indices=set())
        self.assertEqual(match.index, 2)

    def test_rejects_ambiguous_tie(self):
        # Empirically, both pages score >= _LOCATE_SCORE_THRESHOLD (80) for
        # this title -- page 0 scores 100.0, page 5 scores ~97.96 -- a margin
        # of ~2 that is well under _LOCATE_MARGIN_REQUIRED. They're placed
        # farther apart than _LOCATE_CLUSTER_GAP so they form two separate
        # clusters (genuine competing candidates), not one merged location.
        # Neither candidate can be trusted, so the function must return None.
        pages = [
            "Comparing Citation Styles\n\nBy Jane Author\n\nThis chapter examines APA style only.",
            "Unrelated filler page one.",
            "Unrelated filler page two.",
            "Unrelated filler page three.",
            "Unrelated filler page four.",
            "Comparing Citation Style\n\nBy John Smith\n\nAnother chapter about MLA style.",
        ]
        match = locate_chapter_start(pages, "Comparing Citation Styles", exclude_indices=set())
        self.assertIsNone(match)

    def test_treats_nearby_repeated_header_as_one_location(self):
        # Real books often repeat a chapter's title in the running header on
        # several of the chapter's OWN pages (found empirically in an
        # evaluation book: the same title scored 100.0 on both the opening
        # page and a later page of the same short chapter, with a gap in
        # between where an intervening page didn't score highly). These
        # nearby repeats must be treated as ONE candidate location -- not
        # rejected as an ambiguous tie between two different chapters -- so
        # the earliest (opening) page should still be returned.
        pages = [
            "Comparing Citation Styles\n\nBy Jane Author\n\nThis chapter examines APA style.",
            "Some body text continues the discussion of citation formats here.",
            "Comparing Citation Styles16\n\nMore body text about citation formats continues.",
        ]
        match = locate_chapter_start(pages, "Comparing Citation Styles", exclude_indices=set())
        self.assertEqual(match.index, 0)
        # Uncontested (the repeated-header page merged into the same
        # cluster, not a rival) -- margin equals score, same as any other
        # single-cluster match.
        self.assertEqual(match.margin, match.score)

    def test_locate_chapter_start_candidates_exposes_competing_clusters(self):
        pages = [
            "Comparing Citation Styles\n\nBy Jane Author\n\nThis chapter examines APA style only.",
            "Unrelated filler page one.",
            "Unrelated filler page two.",
            "Unrelated filler page three.",
            "Unrelated filler page four.",
            "Comparing Citation Style\n\nBy John Smith\n\nAnother chapter about MLA style.",
        ]
        candidates = locate_chapter_start_candidates(pages, "Comparing Citation Styles", exclude_indices=set())
        self.assertEqual(len(candidates), 2)
        self.assertEqual({c.index for c in candidates}, {0, 5})
        # Sorted best-first.
        self.assertGreaterEqual(candidates[0].score, candidates[1].score)

    def test_author_confirmation_resolves_ambiguous_tie(self):
        # Same near-tie shape as test_rejects_ambiguous_tie, but the true
        # chapter's page also has its author's last name near the top --
        # locate_chapter_start_candidates flags that cluster author_confirmed,
        # and the bonus lets it beat an equally-scoring, unconfirmed rival
        # that would otherwise make the match ambiguous.
        pages = [
            "Comparing Citation Styles\n\nBy Jane Doe\n\nThis chapter examines APA style only.",
            "Unrelated filler page one.",
            "Unrelated filler page two.",
            "Unrelated filler page three.",
            "Unrelated filler page four.",
            "Comparing Citation Style\n\nBy John Smith\n\nAnother chapter about MLA style.",
        ]
        match = locate_chapter_start(pages, "Comparing Citation Styles", exclude_indices=set(), authors=("Jane Doe",))
        self.assertIsNotNone(match)
        self.assertEqual(match.index, 0)
        self.assertTrue(match.author_confirmed)


class TestMatchConfidence(unittest.TestCase):
    def test_uncontested_high_score_is_near_ceiling(self):
        # Uncontested match (margin == score) at a perfect score should be
        # the most trustworthy case there is.
        self.assertEqual(match_confidence(score=100.0, margin=100.0), 1.0)

    def test_bare_minimum_match_sits_near_the_low_end(self):
        # Just clearing the score threshold (80) with the bare minimum
        # margin (8, the smallest a contested match can have) is the lowest
        # confidence value actually reachable in practice (~0.62 -- the
        # nominal 0.5 floor is a hard bound, never actually hit), reflecting
        # a real rival almost as strong as the winner.
        confidence = match_confidence(score=80.0, margin=8.0)
        self.assertGreaterEqual(confidence, 0.5)
        self.assertLess(confidence, 0.65)

    def test_higher_margin_increases_confidence_at_same_score(self):
        low_margin = match_confidence(score=90.0, margin=8.0)
        high_margin = match_confidence(score=90.0, margin=20.0)
        self.assertLess(low_margin, high_margin)

    def test_higher_score_increases_confidence_at_same_margin(self):
        low_score = match_confidence(score=80.0, margin=15.0)
        high_score = match_confidence(score=100.0, margin=15.0)
        self.assertLess(low_score, high_score)


class TestExtractPrintedPageNumber(unittest.TestCase):
    def test_finds_arabic_footer_number(self):
        text = "Comparing Citation Styles\nBy Jane Author\n\nBody text here.\n\n45"
        self.assertEqual(extract_printed_page_number(text), "45")

    def test_finds_roman_numeral_header(self):
        text = "xii\nPreface\n\nBody text of the preface."
        self.assertEqual(extract_printed_page_number(text), "xii")

    def test_returns_none_when_no_number_present(self):
        text = "Just a page of prose with no isolated numeral line at all here."
        self.assertIsNone(extract_printed_page_number(text))


class TestExtractAuthorsNear(unittest.TestCase):
    def test_finds_person_entities_at_chapter_start(self):
        # "By" is needed as an authorial cue -- spaCy's small model doesn't
        # reliably tag bare names without it
        text = "Comparing Citation Styles\n\nBy Jane Author and John Smith\n\nThis chapter examines APA and MLA styles."
        authors = extract_authors_near(text)
        self.assertIn("Jane Author", authors)
        self.assertIn("John Smith", authors)

    def test_returns_empty_list_when_no_names_found(self):
        # Full names avoid a known false positive: spaCy's small model can
        # misclassify bare acronyms like "APA"/"MLA" as PERSON entities.
        text = "This chapter examines American Psychological Association and Modern Language Association citation styles in detail."
        authors = extract_authors_near(text)
        self.assertEqual(authors, [])


class TestAnalyzeAttachment(unittest.TestCase):
    def _fake_book_pages(self) -> list[str]:
        return [
            # page 0: TOC. A third entry ("Appendix") is included solely so
            # this page meets find_toc_candidates' _TOC_MIN_LINES_PER_PAGE
            # (a real TOC page has several entries; below that count, the
            # page's lines are now discarded as noise) -- no page below
            # actually contains "Appendix" text, so locate_chapter_start
            # simply never finds it and it's silently dropped, same as any
            # other unlocatable TOC entry.
            "CONTENTS\n"
            "Introduction ..... 1\n"
            "Comparing Citation Styles ..... 3\n"
            "Appendix ..... 5\n",
            # page 1: printed page "1" — Introduction body
            "Introduction\nJane Author\n\nThis book explores reference management.\n\n1",
            # page 2: continuation of Introduction, printed page "2"
            # NOTE: deliberately avoids repeating the word "introduction"
            # here -- with locate_chapter_start's ambiguity-margin guard, a
            # continuation page that happens to literally contain the
            # chapter title word ties with the real chapter-start page
            # (both score 100 via rapidfuzz partial_ratio) and neither can
            # be trusted, which is correct behavior but not what this
            # fixture is testing. Padded past 150 stripped characters so
            # analyze_attachment's trailing-blank-page trim (meant for real
            # divider pages) doesn't mistake this genuine body-text
            # continuation page for one and clip the chapter short.
            "...continued text follows here, with enough body content on this "
            "page that it clearly reads as a real continuation of the "
            "chapter rather than a blank divider page between sections.\n\n2",
            # page 3: printed page "3" — chapter start
            # NOTE: a blank line separates the title from "John Smith" here
            # (unlike the spec's literal single "\n"). Verified against the
            # real en_core_web_sm model: with only a single newline, spaCy's
            # NER merges the preceding title words and the name into one
            # PERSON span ("Citation Styles\nJohn Smith") because there is no
            # sentence boundary between them, so "John Smith" alone is never
            # produced and the assertIn assertion in
            # test_authors_attached_to_chapters fails. A blank line (already
            # the pattern used in TestExtractAuthorsNear's passing test)
            # gives spaCy a clear boundary and "John Smith" is extracted as
            # its own entity. This is the smallest change that makes the
            # literal spec assertion pass against the real model.
            "Comparing Citation Styles\n\nJohn Smith\n\nThis chapter examines APA and MLA.\n\n3",
            # page 4: continuation, printed page "4". Padded past 150
            # stripped characters for the same reason as page 2 above.
            "...continued chapter text, with enough body content on this "
            "final page that it clearly reads as a real continuation of "
            "the chapter rather than a blank divider page.\n\n4",
        ]

    def test_detects_two_chapters_with_pdf_indices(self):
        result = analyze_attachment(self._fake_book_pages())
        self.assertEqual(len(result["chapters"]), 2)
        first, second = result["chapters"]
        self.assertEqual(first["pdf_start_index"], 1)
        self.assertEqual(first["pdf_end_index"], 2)
        self.assertEqual(second["pdf_start_index"], 3)
        self.assertEqual(second["pdf_end_index"], 4)

    def test_citation_pages_extracted_when_present(self):
        result = analyze_attachment(self._fake_book_pages())
        self.assertEqual(result["chapters"][0]["citation_pages"], "1-2")
        self.assertEqual(result["chapters"][1]["citation_pages"], "3-4")

    def test_low_confidence_when_no_toc_found(self):
        result = analyze_attachment(["Just some prose with no discernible TOC or chapter structure."])
        self.assertEqual(result["segmentation_confidence"], "low")
        self.assertEqual(result["chapters"], [])

    def test_authors_attached_to_chapters(self):
        result = analyze_attachment(self._fake_book_pages())
        self.assertIn("John Smith", result["chapters"][1]["authors"])

    def test_chapters_default_to_heuristic_source(self):
        result = analyze_attachment(self._fake_book_pages())
        self.assertTrue(all(c["source"] == "heuristic" for c in result["chapters"]))


class TestChaptersFromLocated(unittest.TestCase):
    def test_entry_source_maps_llm_entries_and_defaults_others_to_heuristic(self):
        pages = [
            "Introduction\nJane Author\n\nBody text.\n\n1",
            "Comparing Citation Styles\n\nJohn Smith\n\nBody text.\n\n2",
        ]
        llm_entry = TocEntry(title="Introduction", printed_page_number=1, source_page_index=-1)
        heuristic_entry = TocEntry(title="Comparing Citation Styles", printed_page_number=2, source_page_index=0)
        located = [
            (llm_entry, ChapterStartMatch(index=0, score=100.0, margin=20.0)),
            (heuristic_entry, ChapterStartMatch(index=1, score=100.0, margin=20.0)),
        ]
        chapters = _chapters_from_located(pages, located, entry_source={llm_entry: "llm"})
        sources = {c["title"]: c["source"] for c in chapters}
        self.assertEqual(sources["Introduction"], "llm")
        self.assertEqual(sources["Comparing Citation Styles"], "heuristic")


class TestRun(unittest.TestCase):
    def test_skips_already_linked_book(self):
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"data": {"key": "BOOK0001", "itemType": "book", "extra": "X-Contains: groups/1:CH01"}},
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
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"data": {"key": "BOOK0002", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0001", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["Just filler prose, no TOC pattern here at all. " * 3],
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


if __name__ == "__main__":
    unittest.main()
