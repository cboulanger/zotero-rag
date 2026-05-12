"""
TraceCollector — accumulates typed trace events during a single query execution.

Usage:
    collector = TraceCollector(question, library_ids, parameters)
    # pass collector down through service calls; each records its events:
    collector.record(RoutingTrace(...))
    collector.record(LLMCallTrace(...))
    ...
    trace = collector.finalize()  # returns QueryTrace
"""

import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from backend.models.trace import (
    AgentExecutionTrace,
    FallbackTrace,
    LLMCallTrace,
    QueryTrace,
    RoutingTrace,
    Trace,
)


class TraceCollector:
    """Accumulates typed trace events for one query execution."""

    def __init__(self, question: str, library_ids: list[str], parameters: dict):
        self._query_id = str(uuid.uuid4())
        self._start = time.monotonic()
        self._timestamp_start = datetime.now(timezone.utc).isoformat()
        self._question = question
        self._library_ids = library_ids
        self._parameters = parameters
        self._events: list[Trace] = []

    def record(self, trace: Trace) -> None:
        """Append a typed trace event. The subclass type determines how it is assembled."""
        self._events.append(trace)

    def finalize(self) -> QueryTrace:
        """Assemble all recorded events into the top-level QueryTrace."""
        timestamp_end = datetime.now(timezone.utc).isoformat()
        total_duration_ms = int((time.monotonic() - self._start) * 1000)

        routing: Optional[RoutingTrace] = None
        agent_executions: list[AgentExecutionTrace] = []
        llm_calls: list[LLMCallTrace] = []
        fallback_triggered = False

        for event in self._events:
            if isinstance(event, RoutingTrace):
                routing = event
            elif isinstance(event, AgentExecutionTrace):
                agent_executions.append(event)
            elif isinstance(event, LLMCallTrace):
                llm_calls.append(event)
            elif isinstance(event, FallbackTrace):
                fallback_triggered = True

        return QueryTrace(
            query_id=self._query_id,
            timestamp_start=self._timestamp_start,
            question=self._question,
            library_ids=self._library_ids,
            parameters=self._parameters,
            routing=routing,
            agent_executions=agent_executions,
            llm_calls=llm_calls,
            fallback_triggered=fallback_triggered,
            timestamp_end=timestamp_end,
            total_duration_ms=total_duration_ms,
        )
