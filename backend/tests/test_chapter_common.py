"""Unit tests for backend.services.chapter_common."""

import unittest

from backend.services.chapter_common import (
    _is_back_matter,
    _is_part_divider,
    _normalized_title,
    year_from_date,
)


class TestIsPartDivider(unittest.TestCase):
    def test_recognizes_part_and_teil(self):
        self.assertTrue(_is_part_divider("Part I"))
        self.assertTrue(_is_part_divider("Teil 2: Grundlagen"))
        self.assertTrue(_is_part_divider("PARTIE I. Introduction"))

    def test_does_not_flag_ordinary_titles(self):
        self.assertFalse(_is_part_divider("Introduction"))
        self.assertFalse(_is_part_divider("Comparing Citation Styles"))


class TestIsBackMatter(unittest.TestCase):
    def test_recognizes_known_titles(self):
        self.assertTrue(_is_back_matter("Bibliography"))
        self.assertTrue(_is_back_matter("Inhaltsverzeichnis"))
        self.assertTrue(_is_back_matter("Sommaire"))

    def test_does_not_flag_ordinary_titles(self):
        self.assertFalse(_is_back_matter("Comparing Citation Styles"))


class TestNormalizedTitle(unittest.TestCase):
    def test_strips_accents_and_punctuation(self):
        self.assertEqual(_normalized_title("Sommaire!"), "sommaire")
        self.assertEqual(_normalized_title("Über Recht"), "uber recht")


class TestYearFromDate(unittest.TestCase):
    def test_extracts_year_from_iso_style(self):
        self.assertEqual(year_from_date("2019-05"), 2019)

    def test_extracts_year_from_prose_style(self):
        self.assertEqual(year_from_date("May 2019"), 2019)

    def test_returns_none_for_empty_or_missing(self):
        self.assertIsNone(year_from_date(None))
        self.assertIsNone(year_from_date(""))

    def test_returns_none_when_no_four_digit_token(self):
        self.assertIsNone(year_from_date("n/a"))
