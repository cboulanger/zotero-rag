"""``llm.model_names`` entries are case-insensitive glob patterns filtering a live model list."""

import unittest

from pydantic import ValidationError

from backend.config.presets import LLMConfig
from backend.providers.types import ModelInfo


def llm(names):
    return LLMConfig(model_type="remote", model_names=names)


def ids(models):
    return [m.id for m in models]


LIVE = [ModelInfo(id=i) for i in (
    "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-2.5-pro", "gemma-4",
)]


class TestFilterModels(unittest.TestCase):
    def test_plain_names_match_only_themselves(self):
        self.assertEqual(ids(llm(["gemini-3.5-flash"]).filter_models(LIVE)), ["gemini-3.5-flash"])

    def test_wildcards_keep_the_provider_order(self):
        cfg = llm(["gemini-*-flash", "gemini-3.5-flash"])
        self.assertEqual(ids(cfg.filter_models(LIVE)), ["gemini-3.8-flash", "gemini-3.5-flash"])

    def test_result_follows_model_names_order_so_the_first_entry_is_the_default(self):
        cfg = llm(["gemini-2.5-pro", "gemini-3.*"])
        self.assertEqual(ids(cfg.filter_models(LIVE)),
                         ["gemini-2.5-pro", "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"])

    def test_matching_is_case_insensitive_and_a_model_appears_once(self):
        cfg = llm(["GEMINI-3.5-FLASH", "gemini-*-flash"])
        self.assertEqual(ids(cfg.filter_models(LIVE)), ["gemini-3.5-flash", "gemini-3.8-flash"])

    def test_no_match_gives_an_empty_list(self):
        self.assertEqual(llm(["nope"]).filter_models(LIVE), [])

    def test_matches_model_for_query_validation(self):
        cfg = llm(["gemini-3.5-flash", "gemini-*-pro"])
        self.assertTrue(cfg.matches_model("gemini-2.5-pro"))
        self.assertFalse(cfg.matches_model("gemma-4"))


class TestLiteralFallback(unittest.TestCase):
    def test_default_and_static_list_use_only_literal_entries(self):
        cfg = llm(["gemini-*-flash", "gemini-3.5-flash"])
        self.assertEqual(cfg.literal_model_names, ["gemini-3.5-flash"])
        self.assertEqual(cfg.model_name, "gemini-3.5-flash")

    def test_wildcard_only_lists_are_rejected(self):
        with self.assertRaises(ValidationError):
            llm(["*flash*"])


if __name__ == "__main__":
    unittest.main()
