"""
Pydantic models for RAG query execution traces.

QueryTrace is the top-level container returned when include_trace=True.
All leaf event types inherit from Trace so TraceCollector.record() is typed.
"""

from typing import Optional
from pydantic import BaseModel


class Trace(BaseModel):
    """Abstract base for all trace event objects."""
    pass


class ChunkTrace(Trace):
    """A single chunk returned from the vector store."""
    item_key: str
    attachment_key: Optional[str] = None
    title: str
    authors: list[str] = []
    year: Optional[int] = None
    page_number: Optional[int] = None
    score: float
    text_preview: Optional[str] = None


class RetrievalTrace(Trace):
    """Details of the vector store retrieval step."""
    embedding_model: str
    embedding_dims: int
    search_params: dict            # top_k, min_score, library_ids, filters
    raw_results_count: int
    score_stats: dict              # {"min": float, "max": float, "avg": float}
    documents_grouped: int
    chunks: list[ChunkTrace]


class AgentExecutionTrace(Trace):
    """Summary of a single agent's execution within a query."""
    agent_name: str
    retrieval: Optional[RetrievalTrace] = None   # populated for RAG agent
    catalog_results: Optional[list[dict]] = None  # populated for metadata agent
    context_text: str                             # context assembled and passed to LLM
    sources_count: int
    duration_ms: int


class RoutingTrace(Trace):
    """The routing LLM call that selects agents and extracts filters."""
    prompt: str          # full routing prompt
    llm_response: str    # raw LLM text response
    plan: dict           # parsed QueryPlan as dict
    duration_ms: int


class LLMCallTrace(Trace):
    """A single LLM generate() call with full prompt and response."""
    call_type: str       # "routing" | "rag_generation" | "synthesis"
    model: str
    prompt: str
    response: str
    temperature: float
    max_tokens: int
    duration_ms: int
    timestamp: str       # ISO 8601 UTC


class FallbackTrace(Trace):
    """Emitted when all selected agents returned empty and RAG fallback was triggered."""
    reason: str = "all agents returned empty results"


class QueryTrace(BaseModel):
    """Complete execution trace for a single RAG query."""
    query_id: str
    timestamp_start: str           # ISO 8601 UTC
    question: str
    library_ids: list[str]
    parameters: dict               # top_k, min_score, enable_routing, llm_model
    routing: Optional[RoutingTrace] = None
    agent_executions: list[AgentExecutionTrace] = []
    llm_calls: list[LLMCallTrace] = []
    fallback_triggered: bool = False
    timestamp_end: Optional[str] = None
    total_duration_ms: Optional[int] = None
