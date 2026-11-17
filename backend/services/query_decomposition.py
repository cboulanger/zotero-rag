"""Query decomposition: search with several focused sub-queries, not just the question.

Gold questions Q05, Q08 and Q09 (#87/#90) need passages whose wording is far from
the question's: the chunk holding the answer ranks in the hundreds by cosine
similarity to the whole question, behind chunks from many other documents. A
multi-part or "why" question embeds as an average of its parts, so passages
answering one part score low. This asks the LLM for a few short, self-contained
search queries (one per part or angle, in English, the language most of a
library's text is in), runs one vector search per query plus the original, and
fuses the ranked lists with reciprocal rank fusion (RRF) so every sub-query's
best candidates get a chance at the ``top_k`` window.
"""

import logging
import re

from backend.models.document import SearchResult

logger = logging.getLogger(__name__)

RRF_K = 60  # standard RRF damping constant

_DECOMPOSITION_PROMPT = """You turn a user's question into search queries for a semantic search over the full text of academic documents.

Write {n} or fewer short search queries, one per line. Each query covers one part or one angle of the question and reads like a sentence that might appear in a document answering it (a statement, not a question), in English. Include specific names, numbers or terms from the question. If the question asks "why", add a query for the likely cause. Output only the queries, no numbering, no commentary.

Question: {question}"""

_MAX_QUERY_CHARS = 300
_LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


def build_decomposition_prompt(question: str, max_queries: int) -> str:
    """Prompt asking the LLM for at most ``max_queries`` search queries for ``question``."""
    return _DECOMPOSITION_PROMPT.format(n=max_queries, question=question)


def parse_sub_queries(text: str, max_queries: int, question: str) -> list[str]:
    """Extract up to ``max_queries`` distinct, non-empty query lines from the LLM output.

    List markers and surrounding quotes are stripped; lines identical to the
    question (case-insensitive) are dropped since the original is searched anyway.
    """
    seen = {question.strip().casefold()}
    queries: list[str] = []
    for line in text.splitlines():
        query = _LIST_MARKER.sub("", line).strip().strip('"').strip()
        # Degenerate output (seen live: a repeated token for hundreds of characters) is not a query.
        if not 3 <= len(query) <= _MAX_QUERY_CHARS or query.casefold() in seen:
            continue
        seen.add(query.casefold())
        queries.append(query)
        if len(queries) >= max_queries:
            break
    return queries


def _result_key(result: SearchResult) -> str:
    return result.chunk.metadata.chunk_id


def fuse_ranked_lists(lists: list[list[SearchResult]]) -> list[SearchResult]:
    """Reciprocal rank fusion of several score-ranked result lists.

    Each chunk scores ``sum(1 / (RRF_K + rank))`` over the lists it appears in
    (rank from 1, by each list's own vector score). The returned results carry
    that fused value, rescaled so the best is 1.0, as their score (comparable
    only within this call), sorted descending. The highest-vector-score copy of
    each chunk is the one kept.
    """
    fused: dict[str, float] = {}
    best: dict[str, SearchResult] = {}
    for results in lists:
        for rank, result in enumerate(sorted(results, key=lambda r: r.score, reverse=True), 1):
            key = _result_key(result)
            fused[key] = fused.get(key, 0.0) + 1.0 / (RRF_K + rank)
            if key not in best or result.score > best[key].score:
                best[key] = result
    if not fused:
        return []
    top = max(fused.values())
    ordered = sorted(fused, key=lambda k: fused[k], reverse=True)
    return [best[k].model_copy(update={"score": fused[k] / top}) for k in ordered]
