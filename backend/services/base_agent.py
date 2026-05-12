"""
Abstract base class for query agents.

Each agent registers itself with the QueryOrchestrator and contributes:
- a unique name used in QueryPlan.agents_to_use
- a capability_prompt injected into the routing LLM prompt at runtime
- an execute() method that returns a standardised AgentResult
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional
from pydantic import BaseModel

from backend.models.filters import MetadataFilters

if TYPE_CHECKING:
    from backend.services.trace_collector import TraceCollector


class AgentResult(BaseModel):
    """Uniform result returned by every agent."""

    agent_name: str
    context_text: str        # formatted text block for the synthesis LLM prompt
    # SourceInfo is defined in rag_engine to avoid a circular import; typed as Any here.
    sources: list = []


class QueryPlan(BaseModel):
    """Routing decision produced by the QueryRouter."""

    agents_to_use: list[str] = ["rag"]    # names of agents to invoke (must match registered names)
    filters: MetadataFilters = MetadataFilters()
    routing_description: Optional[str] = None   # LLM's brief reasoning, used in synthesis


class BaseAgent(ABC):
    """Abstract base for all query agents."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier used in QueryPlan.agents_to_use."""
        ...

    @property
    @abstractmethod
    def capability_prompt(self) -> str:
        """
        One or more lines describing what this agent does and when to use it.
        Injected verbatim into the QueryRouter's routing prompt at runtime.
        """
        ...

    @abstractmethod
    async def execute(
        self,
        question: str,
        library_ids: list[str],
        filters: MetadataFilters,
        trace: Optional[TraceCollector] = None,
        **kwargs,
    ) -> AgentResult:
        """
        Execute the agent and return a result for synthesis.

        Args:
            question: Original user question.
            library_ids: Libraries to search.
            filters: Extracted bibliographic filters from the routing step.
            trace: Optional collector for recording intermediate trace events.
            **kwargs: Additional parameters (e.g. top_k, min_score for RAGAgent).
        """
        ...
