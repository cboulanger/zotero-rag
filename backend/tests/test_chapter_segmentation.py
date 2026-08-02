"""Unit tests for backend.services.chapter_segmentation."""

import unittest
import unittest.mock
from unittest.mock import AsyncMock, MagicMock

import tempfile
from pathlib import Path
from pathlib import Path as _TestPath
from tempfile import TemporaryDirectory

import io as _io
from pypdf import PdfWriter as _PdfWriter

from backend.config.settings import get_settings, reset_settings
from backend.services.review_queue_store import get_entry
from backend.services.chapter_segmentation import (
    TocEntry,
    extract_page_texts_from_pdf_bytes,
    find_toc_candidates,
    llm_extract_toc_entries,
    load_cached_analysis,
    save_analysis_cache,
    _toc_scan_indices,
    analyze_attachment_with_llm_fallback,
)
from backend.services.chapter_segmentation import (
    ChapterStartCandidate,
    ChapterStartMatch,
    extract_printed_page_number,
    llm_disambiguate_chapter_start,
    locate_chapter_start,
    locate_chapter_start_candidates,
    match_confidence,
    _LOCATE_MARGIN_REQUIRED,
)
from backend.services.chapter_segmentation import _chapters_from_located
from backend.services.chapter_segmentation import extract_authors_near
from backend.services.chapter_segmentation import analyze_attachment
from backend.services.chapter_segmentation import run as analyze_run


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


_FILLER = "Unrelated body filler text, nothing chapter-related in this passage at all."

# Same 20-page, indices-5-and-12 fixture as
# test_chapter_segmentation_strategies.py's _TWO_CHAPTER_PAGES (Task 8) --
# kept as a separate copy here since these two test files don't import from
# each other. See that constant's comment for why the chapters must sit
# outside _toc_scan_indices's front/back exclusion zone.
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
        self.assertEqual(entries[0].title, "Introduction to Reference Management")
        self.assertEqual(entries[0].printed_page_number, 1)
        self.assertEqual(entries[0].source_page_index, 0)
        # The listing's own "CONTENTS" heading is never merged into a
        # wrapped-title variant (see _TOC_MAX_CONTINUATION_LINES walk).
        self.assertEqual(entries[0].title_variants, ())
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
        # found empirically in a real evaluation book. Only lines with
        # plausible page numbers count toward _TOC_MIN_LINES_PER_PAGE (an
        # imprint page full of filtered lines must not qualify as the TOC,
        # see test_metadata_page_of_filtered_lines_does_not_shadow_real_toc)
        # -- three valid lines keep this page qualifying, and the imprint
        # line is still dropped from the result.
        pages = [
            "Publisher Imprint Line          2025\n"
            "Introduction to the Subject          1\n"
            "Comparing Citation Styles          45\n"
            "Zotero in Practice          89\n"
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

    def test_metadata_page_of_filtered_lines_does_not_shadow_real_toc(self):
        # An imprint/metadata page whose lines ALL fail the content filters
        # (years, DOI numbers) must not qualify as the "first TOC cluster"
        # and shadow the real table of contents further in -- the per-line
        # filters run BEFORE the per-page density count (found empirically:
        # a French evaluation book's page-1 metadata block hid its real TOC
        # on pages 5-8).
        pages = [
            "Fancy Book Title Page",
            "DOI : 10.4000/books.example          7527\n"
            "Année d'édition :          2017\n"
            "Date de mise en ligne : 21 mai          2019\n",
            "Some other front matter.",
            "CONTENTS\n"
            "Introduction to Reference Management ..... 12\n"
            "Comparing Citation Styles ..... 45\n"
            "Zotero in Practice ..... 89\n",
        ] + self._FILLER_PAGES
        entries = find_toc_candidates(pages)
        self.assertEqual([e.source_page_index for e in entries], [3, 3, 3])
        self.assertIn("Comparing Citation Styles", [e.title for e in entries])

    def test_wrapped_title_builds_variants_from_preceding_lines(self):
        # A long title wrapped over several lines carries its page number on
        # the LAST line only -- the preceding lines are offered as
        # progressively longer variant readings for the locate step to
        # arbitrate. The walk stops at another entry's line.
        pages = [
            "CONTENTS\n"
            "Introduction to Reference Management ..... 1\n"
            "Making Terrorism: Security Practices and the Production of Terror\n"
            "Activities in Canada ..... 45\n"
            "Zotero in Practice ..... 89\n",
        ] + self._FILLER_PAGES
        entries = find_toc_candidates(pages)
        wrapped = next(e for e in entries if e.title == "Activities in Canada")
        self.assertIn(
            "Making Terrorism: Security Practices and the Production of Terror Activities in Canada",
            wrapped.title_variants,
        )
        # The walk stopped at the previous entry's own line -- it is never
        # part of a variant.
        self.assertFalse(any("Reference Management" in v for v in wrapped.title_variants))

    def test_wrapped_title_walk_stops_at_part_headers(self):
        pages = [
            "CONTENTS\n"
            "Introduction to Reference Management ..... 1\n"
            "Part II Gendered Violence and Racial Subjugation\n"
            "Activities in Canada ..... 45\n"
            "Zotero in Practice ..... 89\n",
        ] + self._FILLER_PAGES
        entries = find_toc_candidates(pages)
        wrapped = next(e for e in entries if e.title == "Activities in Canada")
        self.assertFalse(any("Part II" in v for v in wrapped.title_variants))

    def test_roman_numeral_page_numbers_accepted_for_front_matter(self):
        # "Foreword vii" is a real front-matter TOC entry; the entry is
        # flagged printed_roman so localization may search pre-TOC pages.
        pages = [
            "CONTENTS\n"
            "Foreword vii\n"
            "Introduction to Reference Management ..... 1\n"
            "Comparing Citation Styles ..... 45\n"
            "Zotero in Practice ..... 89\n",
        ] + self._FILLER_PAGES
        entries = find_toc_candidates(pages)
        foreword = next(e for e in entries if e.title == "Foreword")
        self.assertEqual(foreword.printed_page_number, 7)
        self.assertTrue(foreword.printed_roman)
        self.assertFalse(next(e for e in entries if e.title == "Zotero in Practice").printed_roman)

    def test_ordinary_words_of_roman_letters_are_not_page_numbers(self):
        # "civil" is c-i-v-i-l -- every letter a roman digit, but not a
        # valid roman numeral. A TOC line ending in such a word must not
        # become an entry with a hallucinated page number.
        pages = [
            "CONTENTS\n"
            "Introduction to Reference Management ..... 1\n"
            "A Treatise on Matters          civil\n"
            "Comparing Citation Styles ..... 45\n"
            "Zotero in Practice ..... 89\n",
        ] + self._FILLER_PAGES
        entries = find_toc_candidates(pages)
        self.assertFalse(any("Treatise" in e.title for e in entries))

    def test_bare_page_number_line_adopts_preceding_title_line(self):
        # Some TOC layouts put the dot leader + page number on a line of its
        # own, with the title (and author) lines above it.
        pages = [
            "CONTENTS\n"
            "Introduction to Reference Management ..... 1\n"
            "Comparing Citation Styles\n"
            "................................. 45\n"
            "Zotero in Practice ..... 89\n",
        ] + self._FILLER_PAGES
        entries = find_toc_candidates(pages)
        adopted = next((e for e in entries if e.title == "Comparing Citation Styles"), None)
        self.assertIsNotNone(adopted)
        self.assertEqual(adopted.printed_page_number, 45)

    def test_author_marker_toc_keeps_only_chapter_level_entries(self):
        # French/OpenEdition-style TOC: each chapter's page number sits on a
        # "par <Author>" line under the wrapped title, while sub-headings
        # also carry page numbers. With 3+ marker entries, only they are
        # chapters -- and the author names are read off the marker line.
        pages = [
            "SOMMAIRE\n"
            "MODE D'EMPLOI\n"
            "par Lucie Daudin .................. 9\n"
            "UNE SOUS-PARTIE QUELCONQUE .................. 11\n"
            "FRANCE, SOCIÉTÉ MULTICULTURELLE\n"
            "par Patrick Simon .................. 29\n"
            "AUTRE SOUS-PARTIE .................. 31\n"
            "LANGUES ET POLITIQUES PUBLIQUES\n"
            "par Alexandra Filhon .................. 38\n",
        ] + self._FILLER_PAGES
        entries = find_toc_candidates(pages)
        self.assertEqual(len(entries), 3)
        self.assertTrue(all(e.title.startswith("par ") for e in entries))
        simon = next(e for e in entries if "Patrick Simon" in e.title)
        self.assertEqual(simon.authors, ("Patrick Simon",))
        self.assertTrue(any("FRANCE, SOCIÉTÉ MULTICULTURELLE" in v for v in simon.title_variants))

    def test_toc_continuation_page_with_few_entries_joins_cluster(self):
        # The listing's last page may hold only its final two entries --
        # below the density threshold on its own, but a genuine continuation
        # of the already-trusted cluster.
        pages = [
            "CONTENTS\n"
            "Introduction to Reference Management ..... 1\n"
            "Comparing Citation Styles ..... 45\n"
            "Zotero in Practice ..... 60\n",
            "Final Thoughts on Reference Management ..... 75\n"
            "Closing Remarks and Outlook ..... 89\n",
        ] + self._FILLER_PAGES
        entries = find_toc_candidates(pages)
        self.assertIn("Closing Remarks and Outlook", [e.title for e in entries])

    def test_single_stray_line_does_not_extend_toc_cluster(self):
        # One lone matching line on the page after the TOC is
        # indistinguishable from an ordinary body page's incidental
        # "text ... number" line -- swallowing it as "TOC" would cost the
        # first chapter (its opening page becomes excluded from location).
        pages = [
            "CONTENTS\n"
            "Introduction to Reference Management ..... 1\n"
            "Comparing Citation Styles ..... 45\n"
            "Zotero in Practice ..... 60\n",
            "Ordinary body text that happens to end with a number          12\n"
            "and then continues with completely ordinary prose afterwards.",
        ] + self._FILLER_PAGES
        entries = find_toc_candidates(pages)
        self.assertEqual({e.source_page_index for e in entries}, {0})


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

    async def test_ignores_non_list_authors_instead_of_corrupting(self):
        # A malformed LLM response giving a plain string instead of a list
        # (e.g. "authors": "Jane Doe") must not be iterated character-by-
        # character -- it should be treated as no author info at all.
        llm = self._fake_llm('[{"title": "Introduction", "authors": "Jane Doe", "printed_page_number": 1}]')
        entries = await llm_extract_toc_entries(["front matter"] * 5, llm)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].authors, ())

    async def test_passes_is_valid_check_for_json_array_shape(self):
        # Lets an AutoSelectLLMService rotate to a different model when one
        # hallucinates a non-JSON response instead of raising an exception.
        llm = self._fake_llm('[{"title": "Introduction", "authors": [], "printed_page_number": 1}]')
        await llm_extract_toc_entries(["front matter"] * 5, llm)
        is_valid = llm.generate.call_args.kwargs["is_valid"]
        self.assertTrue(is_valid('[{"title": "x"}]'))
        self.assertFalse(is_valid("I'll call a tool instead"))


class TestLlmDisambiguateChapterStart(unittest.IsolatedAsyncioTestCase):
    def _fake_llm(self, response: str):
        llm = MagicMock()
        llm.generate = AsyncMock(return_value=response)
        return llm

    async def test_picks_chosen_candidate(self):
        candidates = [
            ChapterStartCandidate(index=0, score=95.0, author_confirmed=False),
            ChapterStartCandidate(index=5, score=93.0, author_confirmed=False),
        ]
        pages = ["Comparing Citation Styles by Jane Doe..."] * 6
        llm = self._fake_llm('{"chosen_candidate": 2}')
        match = await llm_disambiguate_chapter_start(pages, "Comparing Citation Styles", (), candidates, llm)
        self.assertIsNotNone(match)
        self.assertEqual(match.index, 5)
        self.assertEqual(match.score, 93.0)
        self.assertEqual(match.margin, _LOCATE_MARGIN_REQUIRED)

    async def test_returns_none_when_llm_picks_none(self):
        candidates = [ChapterStartCandidate(index=0, score=95.0, author_confirmed=False)]
        pages = ["some text"] * 2
        llm = self._fake_llm('{"chosen_candidate": null}')
        match = await llm_disambiguate_chapter_start(pages, "Title", (), candidates, llm)
        self.assertIsNone(match)

    async def test_returns_none_on_out_of_range_choice(self):
        candidates = [ChapterStartCandidate(index=0, score=95.0, author_confirmed=False)]
        pages = ["some text"] * 2
        llm = self._fake_llm('{"chosen_candidate": 5}')
        match = await llm_disambiguate_chapter_start(pages, "Title", (), candidates, llm)
        self.assertIsNone(match)

    async def test_returns_none_on_zero_choice(self):
        # 0 is a distinct boundary from "too high" -- a plausible LLM
        # off-by-one response if it 0-indexes instead of 1-indexing.
        candidates = [ChapterStartCandidate(index=0, score=95.0, author_confirmed=False)]
        pages = ["some text"] * 2
        llm = self._fake_llm('{"chosen_candidate": 0}')
        match = await llm_disambiguate_chapter_start(pages, "Title", (), candidates, llm)
        self.assertIsNone(match)

    async def test_returns_none_on_malformed_response(self):
        candidates = [ChapterStartCandidate(index=0, score=95.0, author_confirmed=False)]
        pages = ["some text"] * 2
        llm = self._fake_llm("not json")
        match = await llm_disambiguate_chapter_start(pages, "Title", (), candidates, llm)
        self.assertIsNone(match)

    async def test_passes_is_valid_check_for_json_object_shape(self):
        candidates = [ChapterStartCandidate(index=0, score=95.0, author_confirmed=False)]
        pages = ["some text"] * 2
        llm = self._fake_llm('{"chosen_candidate": 1}')
        await llm_disambiguate_chapter_start(pages, "Title", (), candidates, llm)
        is_valid = llm.generate.call_args.kwargs["is_valid"]
        self.assertTrue(is_valid('{"chosen_candidate": 1}'))
        self.assertFalse(is_valid("I'll call a tool instead"))


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

    def test_running_headers_are_stripped_before_matching(self):
        # A book that stamps every page with a long running header (page
        # number + publisher + book title) would otherwise fill the whole
        # head window locate_chapter_start scores against, hiding the actual
        # chapter title (found empirically on a real evaluation book whose
        # ~150-character header made every chapter unlocatable). The header
        # varies only in its page number, so digit-insensitive detection
        # recognizes it on every page.
        def page(n: int, body: str) -> str:
            return (
                f"{n} | Presses de l'exemple, 2017. <http://www.example.fr/presses/>\n"
                "Accueillir des publics divers. Une perspective de bibliothèque\n"
                + body
            )

        pages = [page(i + 1, "Ordinary body text for this page, nothing special here at all.") for i in range(10)]
        pages[6] = page(7, "Comparing Citation Styles\npar Jane Doe\n\nThis chapter examines citation styles.")
        match = locate_chapter_start(pages, "Comparing Citation Styles", exclude_indices=set())
        self.assertIsNotNone(match)
        self.assertEqual(match.index, 6)

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

    def test_part_dividers_and_back_matter_bound_but_are_not_chapters(self):
        # "Part I ..." divider pages and standard back-matter sections
        # (Index, Contributors) are located -- so they bound their
        # neighbors' page ranges -- but never emitted as chapters.
        # Filler is varied per page (by words, not digits -- running-header
        # detection is digit-insensitive): byte-identical opening lines
        # across many pages would (correctly) be detected as a running
        # header and stripped, which is not what this test is about.
        def filler(n: int) -> str:
            topic = ["archives", "borrowing", "cataloguing", "digitisation", "editions",
                     "facsimiles", "gazettes", "holdings", "incunabula", "journals"][n]
            return (f"This page carries plenty of ordinary body text about {topic}, continuing "
                    f"the chapter well past the blank-page threshold and discussing {topic} "
                    "in enough detail that nothing gets trimmed by accident. ") * 2

        pages = [
            "CONTENTS\n"
            "Introduction to Reference Management ..... 1\n"
            "Part I Foundations of the Field ..... 3\n"
            "Comparing Citation Styles ..... 5\n"
            "Index ..... 9\n",
            "Introduction to Reference Management\nBy Jane Author\n\n" + filler(1),  # 1
            filler(2) + "\n2",  # 2
            "Part I Foundations of the Field",  # 3 -- divider page
            filler(4) + "\n4",  # 4 (padding)
            "Comparing Citation Styles\n\nBy John Smith\n\n" + filler(5),  # 5
            filler(6) + "\n6",  # 6
            filler(7) + "\n7",  # 7
            filler(8) + "\n8",  # 8
            "Index\n\naardvark, 12\nzotero, 45\n" + filler(9),  # 9
        ]
        result = analyze_attachment(pages)
        titles = [c["title"] for c in result["chapters"]]
        self.assertNotIn("Part I Foundations of the Field", titles)
        self.assertNotIn("Index", titles)
        ranges = {c["title"]: (c["pdf_start_index"], c["pdf_end_index"]) for c in result["chapters"]}
        # The divider bounds the Introduction's end; the Index bounds the
        # second chapter's end.
        self.assertEqual(ranges["Introduction to Reference Management"], (1, 2))
        self.assertEqual(ranges["Comparing Citation Styles"], (5, 8))

    def test_toc_order_resolves_shared_title_suffix_ambiguity(self):
        # Introduction and Conclusion sharing one distinctive suffix (the
        # book's own title) are individually ambiguous -- each matches both
        # pages -- but TOC order pins the Introduction to the earlier page
        # and the Conclusion to the later one.
        filler = ("Plenty of ordinary body text continues the chapter here, "
                  "well past the blank-page threshold so nothing is trimmed. ") * 3
        pages = [
            "CONTENTS\n"
            "Introduction: Transformations of Reference Management ..... 1\n"
            "A Middle Chapter About Citation Tools ..... 5\n"
            "Conclusion: Transformations of Reference Management ..... 9\n",
            "Introduction: Transformations of Reference Management\n\nBy Jane Author\n\n" + filler,  # 1
            filler + "\n2",
            filler + "\n3",
            filler + "\n4",
            "A Middle Chapter About Citation Tools\n\nBy John Smith\n\n" + filler,  # 5
            filler + "\n6",
            filler + "\n7",
            filler + "\n8",
            "Conclusion: Transformations of Reference Management\n\nBy Jane Author\n\n" + filler,  # 9
            filler + "\n10",
        ]
        result = analyze_attachment(pages)
        ranges = {c["title"]: c["pdf_start_index"] for c in result["chapters"]}
        self.assertEqual(ranges.get("Introduction: Transformations of Reference Management"), 1)
        self.assertEqual(ranges.get("Conclusion: Transformations of Reference Management"), 9)


class TestAnalyzeAttachmentWithLlmFallback(unittest.IsolatedAsyncioTestCase):
    def _fake_llm(self, toc_response: str | None = None, disambiguation_response: str | None = None):
        llm = MagicMock()
        responses = [r for r in (toc_response, disambiguation_response) if r is not None]
        llm.generate = AsyncMock(side_effect=responses)
        return llm

    async def test_llm_toc_extraction_fires_when_heuristic_finds_nothing(self):
        # 20 pages total so the front/back scan zones (15%/5% of the page
        # count, same fractions find_toc_candidates and llm_extract_toc_entries
        # both use) don't accidentally overlap the two real chapters' opening
        # pages, which are placed well inside the body.
        filler = "Unrelated body filler text, nothing chapter-related in this passage at all."
        pages = [
            "Front matter with an irregular listing the regex can't parse: "
            "Introduction (Jane Author) ... Comparing Citation Styles (John Smith)",
            "Filler front-matter page, nothing chapter-like here at all.",
            "Filler front-matter page, nothing chapter-like here at all.",
            *([filler] * 7),  # indices 3-9
            "Introduction\n\nJane Author\n\nThis book explores reference management in depth.",  # index 10
            *([filler] * 4),  # indices 11-14
            "Comparing Citation Styles\n\nJohn Smith\n\nThis chapter examines APA and MLA styles.",  # index 15
            *([filler] * 3),  # indices 16-18
            "Back matter index page, nothing chapter-related here.",  # index 19
        ]
        self.assertEqual(len(pages), 20)
        response = (
            '[{"title": "Introduction", "authors": ["Jane Author"], "printed_page_number": 1}, '
            '{"title": "Comparing Citation Styles", "authors": ["John Smith"], "printed_page_number": 15}]'
        )
        llm = self._fake_llm(toc_response=response)
        result = await analyze_attachment_with_llm_fallback(pages, llm)
        self.assertTrue(result["diagnostics"]["llm_toc_extraction_used"])
        self.assertEqual(len(result["chapters"]), 2)
        self.assertTrue(all(c["source"] == "llm" for c in result["chapters"]))
        self.assertEqual(result["chapters"][0]["pdf_start_index"], 10)
        self.assertEqual(result["chapters"][1]["pdf_start_index"], 15)

    async def test_does_not_call_llm_when_heuristic_already_succeeds(self):
        pages = [
            "CONTENTS\n"
            "Introduction ..... 1\n"
            "Comparing Citation Styles ..... 3\n"
            "Appendix ..... 5\n",
            "Introduction\nJane Author\n\nThis book explores reference management.\n\n1",
            "...continued text follows here, with enough body content on this "
            "page that it clearly reads as a real continuation of the "
            "chapter rather than a blank divider page between sections.\n\n2",
            "Comparing Citation Styles\n\nJohn Smith\n\nThis chapter examines APA and MLA.\n\n3",
            "...continued chapter text, with enough body content on this "
            "final page that it clearly reads as a real continuation of "
            "the chapter rather than a blank divider page.\n\n4",
        ]
        llm = MagicMock()
        llm.generate = AsyncMock(side_effect=AssertionError("LLM should not be called"))
        result = await analyze_attachment_with_llm_fallback(pages, llm)
        self.assertFalse(result["diagnostics"]["llm_toc_extraction_used"])
        self.assertEqual(result["diagnostics"]["llm_disambiguation_used"], 0)
        llm.generate.assert_not_called()

    async def test_llm_disambiguation_resolves_ambiguous_chapter(self):
        # An entry ambiguous between two locations is normally resolved
        # heuristically by TOC-order constraints (see _locate_toc_entries's
        # second pass), so the LLM path only fires when ordering CANNOT
        # help: here the TOC lists "Comparing Citation Styles" between two
        # entries whose located pages (3 and 4) leave no room, while its
        # own two candidate pages (6 and 12) both sit outside that interval
        # -- a disordered/incorrect TOC only the LLM can arbitrate.
        filler = "Unrelated body filler text, nothing chapter-related here."
        pages = [
            "CONTENTS\n"
            "Alpha Overview ..... 1\n"
            "Comparing Citation Styles ..... 5\n"
            "Omega Summary ..... 9\n",
            filler,
            filler,
            "Alpha Overview\n\nBy Jane Author\n\nThis opening chapter surveys the field.",  # index 3
            "Omega Summary\n\nBy Jane Author\n\nThis closing chapter wraps everything up.",  # index 4
            filler,
            "Comparing Citation Styles\n\nBy Jane Doe\n\nThis chapter examines APA style only.",  # index 6
            *([filler] * 5),  # indices 7-11
            "Comparing Citation Style\n\nBy John Smith\n\nAnother chapter about MLA style.",  # index 12
            *([filler] * 7),  # indices 13-19
        ]
        self.assertEqual(len(pages), 20)
        llm = self._fake_llm(disambiguation_response='{"chosen_candidate": 1}')
        result = await analyze_attachment_with_llm_fallback(pages, llm)
        self.assertFalse(result["diagnostics"]["llm_toc_extraction_used"])
        self.assertEqual(result["diagnostics"]["llm_disambiguation_used"], 1)
        sources = {c["title"]: c["source"] for c in result["chapters"]}
        self.assertEqual(sources.get("Comparing Citation Styles"), "llm")


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


class TestAnalysisCache(unittest.TestCase):
    def test_round_trip(self):
        with TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            entry = {"item_key": "B1", "attachment_key": "A1", "chapters": []}
            save_analysis_cache(cache_dir, "B1", "A1", 5, "heuristic", entry)
            result = load_cached_analysis(cache_dir, "B1", "A1", 5, "heuristic")
            self.assertEqual(result, entry)

    def test_returns_none_when_not_cached(self):
        with TemporaryDirectory() as tmp:
            self.assertIsNone(load_cached_analysis(Path(tmp), "B1", "A1", 5, "heuristic"))

    def test_different_version_is_a_cache_miss(self):
        with TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            save_analysis_cache(cache_dir, "B1", "A1", 5, "heuristic", {"chapters": []})
            self.assertIsNone(load_cached_analysis(cache_dir, "B1", "A1", 6, "heuristic"))

    def test_different_mode_is_a_cache_miss(self):
        with TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            save_analysis_cache(cache_dir, "B1", "A1", 5, "heuristic", {"chapters": []})
            self.assertIsNone(load_cached_analysis(cache_dir, "B1", "A1", 5, "llm_fallback"))


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
        import asyncio

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
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0002", "version": 1, "data": {"key": "BOOK0002", "itemType": "book", "extra": ""}},
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

    def test_needs_ocr_attachment_is_upserted_into_review_queue(self):
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK9", "version": 1, "data": {"key": "BOOK9", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT9", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 no text layer"

        with unittest.mock.patch(
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
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
        import asyncio

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
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["Just filler prose, no TOC pattern here at all. " * 3],
        ), unittest.mock.patch(
            "backend.services.chapter_segmentation.analyze_attachment_with_llm_fallback",
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
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0004", "version": 1, "data": {"key": "BOOK0004", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0003", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["", ""],  # no text layer -- scanned PDF
        ), unittest.mock.patch(
            "backend.services.chapter_segmentation.load_cached_ocr",
            return_value={"detected_language": "eng", "pages": ["Just filler prose, no TOC pattern here at all. " * 3]},
        ) as mock_load_cache, unittest.mock.patch(
            # This test exercises the separate needs_ocr/load_cached_ocr path,
            # not the analysis cache -- ocr_cache_dir=ANY above is a truthy
            # placeholder, not a real Path, so both analysis-cache touchpoints
            # (the pre-download check and the post-analysis save) must be
            # neutralized rather than dereference it.
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
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0005", "version": 1, "data": {"key": "BOOK0005", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0004", "itemType": "attachment", "contentType": "application/pdf"}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
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
        import asyncio

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
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0007", "version": 1, "data": {"key": "BOOK0007", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0006", "itemType": "attachment", "contentType": "application/pdf", "version": 3}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["Just filler prose, no TOC pattern here at all. " * 3],
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
        import asyncio

        zotero_client = AsyncMock()
        zotero_client.get_library_items_since.return_value = [
            {"key": "BOOK0008", "version": 1, "data": {"key": "BOOK0008", "itemType": "book", "extra": ""}},
        ]
        zotero_client.get_item_children.return_value = [
            {"data": {"key": "ATT0007", "itemType": "attachment", "contentType": "application/pdf", "version": 1}},
        ]
        zotero_client.get_attachment_file.return_value = b"%PDF-1.4 fake bytes"

        with unittest.mock.patch(
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
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
        import asyncio

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
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
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
        import asyncio

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
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
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
        import asyncio

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
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["Just filler prose, nothing chapter-related here at all. " * 5] * 3,
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
        import asyncio

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
            "backend.services.chapter_segmentation.extract_page_texts_from_pdf_bytes",
            return_value=["Just filler prose, nothing chapter-related here at all. " * 5] * 3,
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
