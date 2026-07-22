"""
Conversation state for follow-up chat turns on a RAG result note.

A ChatTurn is echoed back verbatim by the client on every follow-up request —
the backend keeps no server-side session state. See docs/query-routing.md's
"Follow-up conversations" section: this deployment restarts often (see root
CLAUDE.md's hotfix workflow), so a stateless design means a backend restart
mid-conversation is a non-event, not a failure mode.
"""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel

from backend.services.base_agent import QueryPlan


class ChatTurn(BaseModel):
    """One prior turn in a follow-up conversation."""

    question: str
    answer: str
    agents_used: list[str] = []
    source_refs: list[str] = []   # opaque evidence refs from that turn — see AgentResult.source_refs
    query_plan: Optional[QueryPlan] = None
