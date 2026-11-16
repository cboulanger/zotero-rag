"""Cross-lingual retrieval: an English translation of a non-English question.

A question in German, French or Spanish embeds close to passages in its own
language and far from English ones, so English works that answer it can rank
outside the vector search's whole candidate pool (spot check on a 2.15M-chunk
library: all five German-question/English-work cases missed in the top 500;
the same questions in English found every target at rank 1-4). The re-ranker
has the same bias: it scores an on-topic English abstract 0.004 against the
German question and 0.40 against its translation. ``RAGEngine.query`` therefore
searches with this translation as well and re-ranks every passage against both
versions (``rerank_pool``'s ``extra_questions``), keeping the higher score.

Unlike query decomposition (``query_decomposition.py``), which splits a question
into parts and lowered recall on the gold set, this keeps the question whole.
"""

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TRANSLATION_PROMPT = """Translate this search question into English for a search over academic documents. Keep names, titles and technical terms; translate their meaning where an established English term exists. Output only the translated question, nothing else.

Question: {question}"""

_MAX_TRANSLATION_CHARS = 300
_LABEL = re.compile(r"^\s*(?:[-*•]|\d+[.)]|(?:english\s+)?translation\s*:)\s*", re.IGNORECASE)


def build_translation_prompt(question: str) -> str:
    """Prompt asking the LLM for an English translation of ``question``."""
    return _TRANSLATION_PROMPT.format(question=question)


def parse_translation(text: str, question: str) -> Optional[str]:
    """The translation from the LLM output: its first non-empty line without list
    markers, a ``Translation:`` label or surrounding quotes. None when the output is
    empty, too long to be a question (degenerate output), or the same as ``question``.
    """
    for line in (text or "").splitlines():
        candidate = _LABEL.sub("", line).strip().strip('"“”„\'').strip()
        if not candidate:
            continue
        if len(candidate) > _MAX_TRANSLATION_CHARS or candidate.casefold() == question.strip().casefold():
            return None
        return candidate
    return None


async def translate_question(llm_service: Any, question: str) -> Optional[str]:
    """English translation of ``question``, or None when there is none to use.

    Any failure (provider error, timeout, unusable output) returns None: the
    translation only widens retrieval, so the query goes on with the question alone.
    """
    try:
        text = await llm_service.generate(
            prompt=build_translation_prompt(question), max_tokens=200, temperature=0.0
        )
    except Exception as exc:
        logger.warning(f"Query translation failed ({type(exc).__name__}: {exc}); using the question only")
        return None
    translation = parse_translation(text, question)
    logger.info(f"Query translation: {translation!r}")
    return translation
