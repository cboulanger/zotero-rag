"""
Query router — classifies a question and extracts bibliographic filters.

A single LLM call assembles a routing prompt from each registered agent's
capability_prompt, then parses the JSON response into a QueryPlan.
Falls back to a RAG-only plan on any parse failure.
"""

import json
import logging
from typing import TYPE_CHECKING

from backend.models.filters import MetadataFilters
from backend.services.base_agent import BaseAgent, QueryPlan
from backend.services.llm import LLMService

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_ROUTING_GUIDANCE = """
General guidance:
- Include "rag" for most questions; omit only when the answer is purely bibliographic (listing items)
- Include "metadata" when the question asks what items exist (by author, year range, item type, or title keywords)
- Prefer fewer agents over more when one agent can answer the question alone
- Default when uncertain: {"agents": ["rag"], ...rest null/empty}
"""

_PROMPT_TEMPLATE = """\
You are a query router for an academic library system.
You have these agents available:

{agent_blocks}

Question: "{question}"

Return ONLY a valid JSON object — no other text:
{{
  "agents": ["rag"],
  "year_min": null,
  "year_max": null,
  "authors": [],
  "item_types": [],
  "title_keywords": [],
  "routing_description": null
}}

Field explanations:
- agents: list of agent names to invoke (must be from the agents listed above)
- year_min / year_max: earliest/latest year mentioned (integer or null)
- authors: author last names mentioned in the question (lowercase strings)
- item_types: e.g. ["book", "journalArticle"] if item type is specified; empty otherwise
- title_keywords: significant words from a title mentioned in the question
- routing_description: brief one-sentence note on why these agents were chosen
{guidance}"""


class QueryRouter:
    """Routes a question to the appropriate agents and extracts metadata filters."""

    def __init__(self, llm_service: LLMService):
        self._llm = llm_service

    def _build_prompt(self, agents: list[BaseAgent], question: str) -> str:
        agent_blocks = "\n\n".join(
            f"[AGENT: {a.name}]\n{a.capability_prompt}" for a in agents
        )
        return _PROMPT_TEMPLATE.format(
            agent_blocks=agent_blocks,
            question=question,
            guidance=_ROUTING_GUIDANCE,
        )

    async def route(self, question: str, agents: list[BaseAgent]) -> QueryPlan:
        """
        Call the LLM to classify the question and extract filters.

        Returns a QueryPlan with agents_to_use + MetadataFilters.
        Falls back to RAG-only plan on any failure.
        """
        if not agents:
            return QueryPlan()

        prompt = self._build_prompt(agents, question)
        valid_names = {a.name for a in agents}

        try:
            raw = await self._llm.generate(prompt=prompt, max_tokens=256, temperature=0.0)
            data = _parse_json(raw)

            selected = [n for n in data.get("agents", ["rag"]) if n in valid_names]
            if not selected:
                selected = ["rag"]

            return QueryPlan(
                agents_to_use=selected,
                filters=MetadataFilters(
                    year_min=data.get("year_min"),
                    year_max=data.get("year_max"),
                    authors=data.get("authors") or [],
                    item_types=data.get("item_types") or [],
                    title_keywords=data.get("title_keywords") or [],
                ),
                routing_description=data.get("routing_description"),
            )

        except Exception as exc:
            logger.warning("QueryRouter: routing failed (%s) — falling back to RAG", exc)
            return QueryPlan()


def _parse_json(text: str) -> dict:
    """Extract and parse the first JSON object found in *text*."""
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            line for line in lines
            if not line.startswith("```")
        ).strip()

    # Find the outermost { ... }
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in router response: {text!r}")
    return json.loads(text[start: end + 1])
