"""Tests for cross-lingual retrieval: translating non-English questions into English
(backend/services/query_translation.py) and searching/re-ranking with both versions."""

import unittest
from unittest.mock import AsyncMock, Mock

from backend.services.query_translation import (
    build_translation_prompt,
    parse_translation,
    translate_question,
)

DE = "Welche Werke analysieren die Rechtsdogmatik?"
EN = "Which works analyse legal doctrine?"


class ParseTranslationTest(unittest.TestCase):
    def test_first_nonempty_line_stripped_of_quotes_and_label(self):
        self.assertEqual(parse_translation(f'\n"{EN}"\n', DE), EN)
        self.assertEqual(parse_translation(f"Translation: {EN}", DE), EN)
        self.assertEqual(parse_translation(f"- {EN}\nsome commentary", DE), EN)

    def test_identical_output_is_ignored(self):
        self.assertIsNone(parse_translation("what is zotero?", "What is Zotero?"))

    def test_degenerate_output_is_ignored(self):
        self.assertIsNone(parse_translation("", DE))
        self.assertIsNone(parse_translation("   \n  ", DE))
        self.assertIsNone(parse_translation("a" * 400, DE))  # over the 300-character limit

    def test_prompt_contains_the_question(self):
        self.assertIn(DE, build_translation_prompt(DE))


class TranslateQuestionTest(unittest.IsolatedAsyncioTestCase):
    async def test_returns_translation(self):
        llm = Mock()
        llm.generate = AsyncMock(return_value=EN)
        self.assertEqual(await translate_question(llm, DE), EN)
        self.assertEqual(llm.generate.await_args.kwargs["temperature"], 0.0)

    async def test_llm_error_returns_none(self):
        llm = Mock()
        llm.generate = AsyncMock(side_effect=RuntimeError("provider down"))
        self.assertIsNone(await translate_question(llm, DE))


if __name__ == "__main__":
    unittest.main()
