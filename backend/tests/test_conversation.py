"""Unit tests for ChatTurn."""

import unittest

from backend.models.conversation import ChatTurn
from backend.services.base_agent import QueryPlan


class TestChatTurn(unittest.TestCase):
    def test_defaults(self):
        turn = ChatTurn(question="Q1", answer="A1")
        self.assertEqual(turn.agents_used, [])
        self.assertEqual(turn.source_refs, [])
        self.assertIsNone(turn.query_plan)

    def test_carries_query_plan(self):
        plan = QueryPlan(agents_to_use=["rag"])
        turn = ChatTurn(question="Q1", answer="A1", query_plan=plan)
        self.assertEqual(turn.query_plan.agents_to_use, ["rag"])

    def test_round_trips_through_json(self):
        turn = ChatTurn(question="Q1", answer="A1", agents_used=["rag"], source_refs=["c1", "c2"])
        restored = ChatTurn.model_validate_json(turn.model_dump_json())
        self.assertEqual(restored, turn)


if __name__ == "__main__":
    unittest.main()
