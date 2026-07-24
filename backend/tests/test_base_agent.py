"""Unit tests for AgentResult/QueryPlan new fields and the NeedsUserInputError hierarchy."""

import unittest

from backend.services.base_agent import (
    AgentResult, NeedsClarificationError, NeedsClientEvidenceError,
    NeedsUserInputError, QueryPlan,
)


class TestAgentResultNewFields(unittest.TestCase):
    def test_defaults(self):
        r = AgentResult(agent_name="rag", context_text="x")
        self.assertEqual(r.source_refs, [])
        self.assertFalse(r.needs_clarification)
        self.assertIsNone(r.clarification_message)
        self.assertEqual(r.clarification_suggestions, [])

    def test_can_set_clarification_fields(self):
        r = AgentResult(
            agent_name="metadata", context_text="too many",
            needs_clarification=True, clarification_message="narrow it down",
        )
        self.assertTrue(r.needs_clarification)
        self.assertEqual(r.clarification_message, "narrow it down")


class TestQueryPlanNewFields(unittest.TestCase):
    def test_defaults(self):
        p = QueryPlan()
        self.assertFalse(p.clarification_needed)
        self.assertIsNone(p.clarification_question)


class TestNeedsUserInputHierarchy(unittest.TestCase):
    def test_client_evidence_error_is_a_needs_user_input_error(self):
        exc = NeedsClientEvidenceError(citation_targets=[], plan=QueryPlan())
        self.assertIsInstance(exc, NeedsUserInputError)

    def test_clarification_error_is_a_needs_user_input_error(self):
        exc = NeedsClarificationError(message="narrow it down", plan=QueryPlan())
        self.assertIsInstance(exc, NeedsUserInputError)
        self.assertEqual(exc.message, "narrow it down")
        self.assertIsInstance(exc.plan, QueryPlan)


if __name__ == "__main__":
    unittest.main()
