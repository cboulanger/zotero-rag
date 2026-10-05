"""
Query router — classifies a question and extracts bibliographic filters.

A single LLM call assembles a routing prompt from each registered agent's
capability_prompt, then parses the JSON response into a QueryPlan.
Falls back to a RAG-only plan on any parse failure.
"""

from __future__ import annotations

import json
import logging
import time
import unicodedata
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from backend.models.conversation import ChatTurn
from backend.models.filters import CitationTarget, MetadataFilters
from backend.models.trace import LLMCallTrace, RoutingTrace
from backend.services.base_agent import BaseAgent, QueryPlan
from backend.services.llm import LLMService

if TYPE_CHECKING:
    from backend.services.trace_collector import TraceCollector

logger = logging.getLogger(__name__)

_ROUTING_GUIDANCE = """
General guidance:
- Include "rag" for most questions — it reads document content to answer.
- Use "metadata" ONLY when the question is about the library catalog itself:
  e.g. "What papers by Smith are in my library?", "Show me books from 2010–2015."
  DO NOT use "metadata" alone for questions about real-world topics, concepts,
  organisations, events, or arguments — even if they use "listing" or "exist" language.
  Those questions require "rag" to read document content.
- Reference-management tool and product names (Zotero, EndNote, Mendeley, RefWorks,
  Papers, Citavi, etc.) are topics being discussed, not authors — capitalized as they
  are, they are still never people's names. NEVER populate "authors" with a tool or
  product name. A question comparing or asking about such tools (e.g. "What features
  does EndNote have that Zotero does not?") is a content/comparison question — route
  it to "rag", not "metadata", and leave "authors" empty unless a specific person's
  name is also mentioned.
- Combine both agents only when the question BOTH lists catalog items AND asks about content.
- Use "mentions" when the question asks which publications CITE, DISCUSS, RESPOND TO, or
  MENTION a specific named work — as opposed to questions about work BY that person.
  Populate citation_targets with one entry per cited work (author surname, optional year,
  optional distinctive title keywords) and leave the cited author OUT of "authors".
  Example: "Which publications cite Wiethölter's 1975 article and discuss Teubner's Globale
  Bukowina?" -> agents: ["mentions"], authors: [] (NOT ["wiethölter", "teubner"] — they are
  cited, not authored-by), citation_targets: [
    {"author": "wiethölter", "year": 1975, "title_keywords": []},
    {"author": "teubner", "year": null, "title_keywords": ["bukowina"]}
  ].
- "mentions" is expensive (a client-side full-text scan) and approximate (word co-occurrence,
  not a verified citation) — only select it when the question is clearly about citation or
  discussion of a specific named work, not a general topic search (that's "rag").
- Default when uncertain: {"agents": ["rag"], ...rest null/empty}
"""


_GERMAN_DIACRITIC_EXPANSIONS = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}


def _normalize_forms(text: str) -> set[str]:
    """Return both a diacritic-stripped and a German-transliterated,
    casefolded form of `text`, so an entity written either way (e.g.
    "Wiethölter", "Wietholter", or "Wiethoelter") matches consistently
    whichever form appears on the candidate side vs. the question side."""
    folded = text.casefold()
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", folded) if not unicodedata.combining(c)
    )
    translit = folded
    for umlaut, expansion in _GERMAN_DIACRITIC_EXPANSIONS.items():
        translit = translit.replace(umlaut, expansion)
    return {stripped, translit}


def _mentioned_in_question(candidate: str, question_forms: set[str]) -> bool:
    """True if `candidate` (in any normalized form) is a substring of the
    question (in any normalized form). Substring containment — not exact
    token equality — so German inflection (e.g. genitive "Wiethölters")
    matches for free without needing a stemmer."""
    if not candidate:
        return False
    return any(
        cand_form in q_form
        for cand_form in _normalize_forms(candidate)
        for q_form in question_forms
    )


def _filter_mentioned(candidates: list[str], question_forms: set[str]) -> tuple[list[str], list[str]]:
    """Split `candidates` into (kept, dropped) by whether each is mentioned
    in the question."""
    kept: list[str] = []
    dropped: list[str] = []
    for candidate in candidates:
        (kept if _mentioned_in_question(candidate, question_forms) else dropped).append(candidate)
    return kept, dropped


_PROMPT_TEMPLATE = """\
You are a query router for an academic library system.
You have these agents available:

{agent_blocks}
{conversation_block}
Question: "{question}"

Return ONLY a valid JSON object — no other text:
{{
  "agents": ["rag"],
  "year_min": null,
  "year_max": null,
  "authors": [],
  "item_types": [],
  "title_keywords": [],
  "citation_targets": [],
  "routing_description": null,
  "clarification_needed": false,
  "clarification_question": null
}}

Field explanations:
- agents: list of agent names to invoke (must be from the agents listed above)
- year_min / year_max: earliest/latest year mentioned (integer or null)
- authors: author last names mentioned in the question (lowercase strings) — WRITTEN BY only
- item_types: e.g. ["book", "journalArticle"] if item type is specified; empty otherwise
- title_keywords: ONLY populate when the user explicitly names a specific document title
  they want to find (e.g. "find the paper called 'X'"). Leave empty for topic/keyword searches.
- citation_targets: list of {{author, year, title_keywords}} for works the question asks about
  being CITED/DISCUSSED by other publications (see "mentions" agent below). Leave empty unless
  the question is clearly about citation/discussion of specific named work(s).
- routing_description: brief one-sentence note on why these agents were chosen
- clarification_needed: true ONLY when the question is an unconstrained catalog-style request
  (e.g. "What has X written about?", "List everything on topic Y") with no year range, specific
  title, or other narrowing detail — asking the user to narrow it is better than guessing.
  Leave false for any question with enough specificity to search directly, and false whenever
  a conversation history is present and the follow-up is clearly building on it.
- clarification_question: if clarification_needed is true, a short question asking the user to
  narrow by year, author, item type, or topic; otherwise null.
{guidance}"""


class QueryRouter:
    """Routes a question to the appropriate agents and extracts metadata filters."""

    def __init__(self, llm_service: LLMService):
        self._llm = llm_service

    def _build_prompt(
        self,
        agents: list[BaseAgent],
        question: str,
        conversation_history: Optional[list[ChatTurn]] = None,
        max_conversation_context_chars: int = 6000,
    ) -> str:
        agent_blocks = "\n\n".join(
            f"[AGENT: {a.name}]\n{a.capability_prompt}" for a in agents
        )
        conversation_block = ""
        if conversation_history:
            rendered = _render_conversation_history(conversation_history, max_conversation_context_chars)
            if rendered:
                conversation_block = f"\nConversation so far (oldest first):\n{rendered}\n"
        return _PROMPT_TEMPLATE.format(
            agent_blocks=agent_blocks,
            conversation_block=conversation_block,
            question=question,
            guidance=_ROUTING_GUIDANCE,
        )

    async def route(
        self,
        question: str,
        agents: list[BaseAgent],
        trace: Optional[TraceCollector] = None,
        conversation_history: Optional[list[ChatTurn]] = None,
        max_conversation_context_chars: int = 6000,
    ) -> QueryPlan:
        """
        Call the LLM to classify the question and extract filters.

        Returns a QueryPlan with agents_to_use + MetadataFilters.
        Falls back to RAG-only plan on any failure.
        """
        if not agents:
            return QueryPlan()

        prompt = self._build_prompt(agents, question, conversation_history, max_conversation_context_chars)
        valid_names = {a.name for a in agents}
        t0 = time.monotonic()
        raw = ""

        try:
            raw = await self._llm.generate(prompt=prompt, max_tokens=256, temperature=0.0)
            duration_ms = int((time.monotonic() - t0) * 1000)
            data = _parse_json(raw)

            selected = [n for n in data.get("agents", ["rag"]) if n in valid_names]
            if not selected:
                selected = ["rag"]

            question_forms = _normalize_forms(question)

            authors, dropped_authors = _filter_mentioned(data.get("authors") or [], question_forms)
            title_keywords, dropped_title_keywords = _filter_mentioned(
                data.get("title_keywords") or [], question_forms
            )

            citation_targets = []
            dropped_citation_authors = []
            for ct in data.get("citation_targets") or []:
                if isinstance(ct, dict) and ct.get("author"):
                    author = str(ct["author"])
                    if _mentioned_in_question(author, question_forms):
                        citation_targets.append(CitationTarget(
                            author=author,
                            year=ct.get("year"),
                            title_keywords=ct.get("title_keywords") or [],
                        ))
                    else:
                        dropped_citation_authors.append(author)

            dropped_filters: dict[str, list[str]] = {}
            if dropped_authors:
                dropped_filters["authors"] = dropped_authors
            if dropped_title_keywords:
                dropped_filters["title_keywords"] = dropped_title_keywords
            if dropped_citation_authors:
                dropped_filters["citation_targets.author"] = dropped_citation_authors
            if dropped_filters:
                logger.warning(
                    "QueryRouter: dropped filter entries not found in question text: %s",
                    dropped_filters,
                )

            plan = QueryPlan(
                agents_to_use=selected,
                filters=MetadataFilters(
                    year_min=data.get("year_min"),
                    year_max=data.get("year_max"),
                    authors=authors,
                    item_types=data.get("item_types") or [],
                    title_keywords=title_keywords,
                    citation_targets=citation_targets,
                ),
                routing_description=data.get("routing_description"),
                clarification_needed=bool(data.get("clarification_needed", False)),
                clarification_question=data.get("clarification_question"),
                dropped_filters=dropped_filters or None,
            )

            if trace is not None:
                trace.record(RoutingTrace(
                    prompt=prompt,
                    llm_response=raw,
                    plan=plan.model_dump(),
                    duration_ms=duration_ms,
                ))
                trace.record(LLMCallTrace(
                    call_type="routing",
                    model=self._llm.model_name,
                    prompt=prompt,
                    response=raw,
                    temperature=0.0,
                    max_tokens=256,
                    duration_ms=duration_ms,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                ))

            return plan

        except Exception as exc:
            logger.warning("QueryRouter: routing failed (%s) — falling back to RAG", exc)
            return QueryPlan()


def _render_conversation_history(history: list[ChatTurn], max_chars: int) -> str:
    """Render the most recent turns that fit under max_chars, dropping older ones."""
    if not history:
        return ""
    kept: list[str] = []
    total = 0
    for turn in reversed(history):
        block = f"Q: {turn.question}\nA: {turn.answer}"
        if total + len(block) > max_chars and kept:
            break
        kept.append(block)
        total += len(block)
    kept.reverse()
    return "\n\n".join(kept)


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
