"""Shared JSON-extraction helpers for parsing structured LLM output.

LLMs are asked to "return ONLY JSON" but the real world routinely adds
markdown code fences or a sentence of leading prose anyway -- both helpers
strip that off before parsing. Originally a private helper in
query_router.py; promoted here once chapter_segmentation.py's LLM fallback
needed the identical logic (see docs/superpowers/specs/
2026-07-25-llm-chapter-segmentation-fallback-design.md §3).
"""

import json


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(line for line in lines if not line.startswith("```")).strip()
    return text


def parse_json_object(text: str) -> dict:
    """Extract and parse the first JSON object ({...}) found in *text*."""
    text = _strip_code_fence(text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in LLM response: {text!r}")
    return json.loads(text[start: end + 1])


def parse_json_array(text: str) -> list:
    """Extract and parse the first JSON array ([...]) found in *text*."""
    text = _strip_code_fence(text)
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON array found in LLM response: {text!r}")
    return json.loads(text[start: end + 1])
