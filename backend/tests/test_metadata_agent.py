"""
Unit tests for MetadataAgent.
"""

import unittest
from unittest.mock import MagicMock, patch

from backend.models.filters import MetadataFilters
from backend.services.base_agent import AgentResult
from backend.services.metadata_agent import MetadataAgent, MetadataResult, _format_authors, _results_to_context


class TestFormatAuthors(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(_format_authors([]), "Unknown")

    def test_single(self):
        self.assertEqual(_format_authors(["Luhmann, N."]), "Luhmann, N.")

    def test_two(self):
        self.assertEqual(_format_authors(["Luhmann, N.", "Habermas, J."]), "Luhmann, N. & Habermas, J.")

    def test_three_or_more(self):
        self.assertEqual(_format_authors(["Luhmann, N.", "Habermas, J.", "Parsons, T."]), "Luhmann, N. et al.")


class TestResultsToContext(unittest.TestCase):

    def test_empty_results(self):
        text = _results_to_context([])
        self.assertIn("No items found", text)

    def test_formats_single_result(self):
        result = MetadataResult(
            item_id="ABC",
            library_id="1",
            title="Social Systems",
            authors=["Luhmann, N."],
            year=1984,
            item_type="book",
            text_preview="Systems theory is",
        )
        text = _results_to_context([result])
        self.assertIn("[M1]", text)
        self.assertIn("Social Systems", text)
        self.assertIn("1984", text)
        self.assertIn("book", text)
        self.assertIn("Systems theory is", text)

    def test_no_year_or_type(self):
        result = MetadataResult(
            item_id="XYZ",
            library_id="1",
            title="Unknown Document",
            authors=[],
            year=None,
            item_type=None,
            text_preview=None,
        )
        text = _results_to_context([result])
        self.assertIn("[M1]", text)
        self.assertNotIn("None", text)


class TestMetadataAgentExecute(unittest.IsolatedAsyncioTestCase):

    def _make_agent(self, payloads: list[dict]) -> MetadataAgent:
        store = MagicMock()
        store.get_items_by_metadata.return_value = payloads
        return MetadataAgent(store)

    async def test_returns_agent_result(self):
        agent = self._make_agent([])
        result = await agent.execute(
            question="List books by Luhmann",
            library_ids=["1"],
            filters=MetadataFilters(),
        )
        self.assertIsInstance(result, AgentResult)
        self.assertEqual(result.agent_name, "metadata")

    async def test_passes_library_ids_and_filters(self):
        store = MagicMock()
        store.get_items_by_metadata.return_value = []
        agent = MetadataAgent(store)
        filters = MetadataFilters(authors=["luhmann"])
        await agent.execute(
            question="Books by Luhmann",
            library_ids=["42"],
            filters=filters,
        )
        store.get_items_by_metadata.assert_called_once_with(
            library_ids=["42"],
            filters=filters,
            limit=unittest.mock.ANY,
        )

    async def test_empty_library_ids_passed_as_none(self):
        store = MagicMock()
        store.get_items_by_metadata.return_value = []
        agent = MetadataAgent(store)
        await agent.execute(question="Q", library_ids=[], filters=MetadataFilters())
        call_kwargs = store.get_items_by_metadata.call_args.kwargs
        self.assertIsNone(call_kwargs["library_ids"])

    async def test_builds_sources_from_payloads(self):
        agent = self._make_agent([
            {"item_key": "ABC", "library_id": "1", "title": "T", "authors": [], "year": 2000, "item_type": "book"},
        ])
        result = await agent.execute(question="Q", library_ids=["1"], filters=MetadataFilters())
        self.assertEqual(len(result.sources), 1)
        self.assertEqual(result.sources[0]["item_id"], "ABC")
        self.assertEqual(result.sources[0]["score"], 1.0)

    async def test_results_sorted_by_year(self):
        agent = self._make_agent([
            {"item_key": "C", "library_id": "1", "title": "C", "authors": [], "year": 2000},
            {"item_key": "A", "library_id": "1", "title": "A", "authors": [], "year": 1980},
            {"item_key": "B", "library_id": "1", "title": "B", "authors": [], "year": None},
        ])
        result = await agent.execute(question="Q", library_ids=["1"], filters=MetadataFilters())
        years = [s["year"] for s in result.sources]
        # year=None should be last
        self.assertEqual(years[-1], None)
        # 1980 before 2000
        self.assertLess(years.index(1980) if 1980 in years else 0,
                        years.index(2000) if 2000 in years else 1)

    async def test_context_text_contains_items(self):
        agent = self._make_agent([
            {"item_key": "X", "library_id": "1", "title": "Social Systems", "authors": ["Luhmann, N."], "year": 1984},
        ])
        result = await agent.execute(question="Q", library_ids=["1"], filters=MetadataFilters())
        self.assertIn("Social Systems", result.context_text)
        self.assertIn("1984", result.context_text)

    async def test_custom_limit_via_kwargs(self):
        store = MagicMock()
        store.get_items_by_metadata.return_value = []
        agent = MetadataAgent(store)
        await agent.execute(question="Q", library_ids=[], filters=MetadataFilters(), metadata_limit=10)
        call_kwargs = store.get_items_by_metadata.call_args.kwargs
        self.assertEqual(call_kwargs["limit"], 10)


if __name__ == "__main__":
    unittest.main()
