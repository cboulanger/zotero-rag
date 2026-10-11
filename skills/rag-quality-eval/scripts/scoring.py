"""Scoring of one RAG answer against a gold question.

Pure functions, no network and no backend needed, so saved runs can be
re-scored at any time (``report.py --rescore``). The citation regex and the
context-insufficient marker are imported from ``backend.services.rag_engine``
when importable so the checks track the real prompt; the fallbacks below must
stay identical (a unit test asserts this when the backend is importable).

Criteria (each 0..1, see references/criteria.md for the rationale):

  fact_recall          weighted share of gold facts present in the answer
  expected_doc_recall  share of the gold documents among the returned sources
  source_count         min(1, distinct sources returned / gold min_sources)
  cited_diversity      min(1, distinct sources cited / gold min_distinct_cited)
  citation_compliance  citation coverage x format-validity (prompt's CRITICAL CITATION RULE)
  grounded_numbers     share of numbers in the answer that occur in the context given to the LLM
  length               1 inside the gold word band, decaying outside it
  language             answer language == question language
  hygiene              no process narration, tool-call/<think> leaks, marker leak, refusal

Regex matching (``answer_patterns``) is only the automatic *floor*. A judgment
file written by the reviewing agent (see judge.py / references/judging.md) is
merged by ``apply_judgment``: it replaces ``fact_recall`` with the judged
recall (verbatim, semantic AND inferred hits all count; swapped attribution and
contradictions do not), adds unsupported-claim, citation-support and
inference-quality criteria, and records where the regex was wrong.
"""

from __future__ import annotations

import re
import statistics
from typing import Any, Optional

try:  # single source of truth: the engine's own regex / marker
    from backend.services.rag_engine import CONTEXT_INSUFFICIENT_MARKER, _SN_CITATION_PATTERN
except Exception:  # backend (or its heavy deps) not importable: keep a local copy
    CONTEXT_INSUFFICIENT_MARKER = "###CONTEXT_INSUFFICIENT###"
    _SN_CITATION_PATTERN = re.compile(r"\[S\d+(?::\d+)?(?:,\s*S\d+(?::\d+)?)*\]")

CITATION_RE = _SN_CITATION_PATTERN

WEIGHTS = {
    "fact_recall": 0.30,
    "expected_doc_recall": 0.10,
    "source_count": 0.05,
    "cited_diversity": 0.05,
    "citation_compliance": 0.15,
    "grounded_numbers": 0.10,
    "length": 0.05,
    "language": 0.10,
    "hygiene": 0.10,
}
# Weights once a judgment exists: the judge's findings replace the regex proxies.
WEIGHTS_JUDGED = {
    "fact_recall": 0.30,
    "expected_doc_recall": 0.08,
    "source_count": 0.04,
    "cited_diversity": 0.04,
    "citation_compliance": 0.10,
    "citation_support": 0.08,
    "groundedness": 0.08,
    "length": 0.04,
    "language": 0.08,
    "hygiene": 0.07,
    "inference_quality": 0.09,   # only for questions that define an 'inference' chain
}
VERDICT_CREDIT = {"verbatim": 1.0, "semantic": 1.0, "inferred": 1.0, "partial": 0.5, "missing": 0.0, "contradicted": 0.0}
PASS_THRESHOLD = 0.85
WARN_THRESHOLD = 0.65
FACT_RECALL_FLOOR = 0.5  # below this a run fails regardless of the composite

# Violations of the generation prompt's citation rules (checked on text with the
# valid citations removed, so a well-formed [S1:3] never triggers them).
MALFORMED_CITATION_PATTERNS = {
    "page_prefix": re.compile(r"\[S\d+:\s*(?:p|pp|page|seite|s)\.?\s*\d+", re.I),   # [S1:p.3]
    "page_range": re.compile(r"\[S\d+:\d+\s*[-–]\s*\d+"),                      # [S1:305-306]
    "literal_P": re.compile(r"\[S\d+:P\]"),                                           # [S1:P]
    "numeric_bracket": re.compile(r"(?<![\w\]])\[\d+(?:\s*[,–-]\s*\d+)*\](?!\()"),  # [1], [4]
    "spelled_source": re.compile(r"\b(?:Source|Quelle|Fuente|Sources|Quellen|Fuentes)\s+\d+\b", re.I),
    "bare_sn": re.compile(r"(?<![\[\w])S\d+(?!\w)"),                                  # S1 without brackets
}

NARRATION_START = re.compile(
    r"^\W*(?:i\s+will|i'll|i\s+am\s+going|let\s+me|let's|here\s+(?:are|is)\s+(?:some|the)\s+(?:relevant|sources)|"
    r"looking\s+(?:through|at)|based\s+on\s+my\s+(?:search|review)|ich\s+werde|je\s+vais|voy\s+a)",
    re.I,
)
TOOL_LEAK = re.compile(r"\b\w+\.call\(|\bfunction_call\s*\(|\btool_call\s*\(|<tool_call>|<function_call>", re.I)
THINK_LEAK = re.compile(r"</?think>", re.I)
REFUSAL = re.compile(
    r"(couldn't find any relevant information|could not find any relevant information|"
    r"kann .{0,30}nicht beantworten|no (?:he|se) encontr)",
    re.I,
)

# Short, unambiguous stop-word lists; enough to tell apart the supported languages.
STOPWORDS = {
    "en": "the and of to that with for are was were which this from their has have been not they than also between".split(),
    "de": "der die das und ist nicht mit von für wurde sind den dem eine einer auch zwischen werden dass bei oder".split(),
    "fr": "le la les des est sont avec pour dans une qui que pas par ont été entre aussi leur sur ce cette".split(),
    "es": "el los las del una que con para por son fue como más entre también sus esta este se su pero".split(),
}


# ---------------------------------------------------------------- helpers
def _words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", CITATION_RE.sub(" ", text), re.UNICODE)


def count_words(text: str) -> int:
    """Word count ignoring citation markers."""
    return len(_words(text))


def detect_language(text: str) -> str:
    """Best-guess language among ``STOPWORDS`` (``unknown`` when evidence is thin)."""
    tokens = [w.lower() for w in _words(text)]
    scores = {lang: sum(1 for t in tokens if t in set(words)) for lang, words in STOPWORDS.items()}
    lang, best = max(scores.items(), key=lambda kv: kv[1])
    runner_up = sorted(scores.values())[-2]
    return lang if best >= 3 and best > runner_up else "unknown"


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower().replace("’", "'"))[:60]


def _doc_matchers(question: dict, gold: dict) -> list[dict]:
    """One matcher per gold document of the question: its acceptable keys and title prefix."""
    out = []
    for doc_id in question["documents"]:
        doc = gold["documents"][doc_id]
        out.append({
            "id": doc_id,
            "keys": set(doc["item_keys"]) | set(doc["attachment_keys"]),
            "title": _norm_title(doc["title"]),
        })
    return out


def _matches(matcher: dict, keys: list[Optional[str]], title: str) -> bool:
    return any(k in matcher["keys"] for k in keys if k) or (
        bool(title) and _norm_title(title) == matcher["title"]
    )


def _context_text(trace: Optional[dict]) -> str:
    if not trace:
        return ""
    return "\n".join(e.get("context_text") or "" for e in trace.get("agent_executions", []))


# ---------------------------------------------------------------- criteria
def score_facts(answer: str, context: str, facts: list[dict]) -> dict:
    """Automatic fact recall (regex floor) plus the retrieval-vs-generation diagnosis.

    Facts without ``answer_patterns`` are judge-only (inference facts): they are
    listed but neither counted here nor diagnosed.
    """
    total = got = 0.0
    detail, missed_retrieval, missed_generation = [], [], []
    by_level: dict[str, list[float]] = {}
    for fact in facts:
        weight = float(fact.get("weight", 1))
        level = fact.get("level", "verbatim")
        if not fact.get("answer_patterns"):
            detail.append({"id": fact["id"], "level": level, "in_answer": None, "in_context": None, "judge_only": True})
            continue

        def hit(text: str) -> bool:
            checks = [bool(re.search(p, text, re.I | re.S)) for p in fact["answer_patterns"]]
            return all(checks) if fact.get("all_of") else any(checks)

        in_answer = hit(answer)
        in_context = hit(context) if context else None
        total += weight
        got += weight if in_answer else 0
        stat = by_level.setdefault(level, [0.0, 0.0])
        stat[0] += weight if in_answer else 0
        stat[1] += weight
        detail.append({"id": fact["id"], "level": level, "in_answer": in_answer, "in_context": in_context})
        if not in_answer and fact.get("docs"):
            # Only a trace tells us where it was lost; patterns are written for the answer
            # language, so an English-source miss in context is an approximation.
            (missed_generation if in_context else missed_retrieval).append(fact["id"])
    return {
        "fact_recall": got / total if total else 1.0,
        "recall_by_level": {lvl: g / t for lvl, (g, t) in by_level.items()},
        "facts": detail,
        "missed_retrieval": missed_retrieval if context else [],
        "missed_generation": missed_generation if context else [],
        "judged": False,
    }


def score_sources(response: dict, question: dict, gold: dict) -> dict:
    """Source coverage: gold documents among returned sources, counts, cited diversity."""
    sources = response.get("sources") or []
    matchers = _doc_matchers(question, gold)
    returned_hits = {
        m["id"] for m in matchers
        if any(_matches(m, [s.get("item_id")], s.get("title", "")) for s in sources)
    }
    trace = response.get("trace") or {}
    chunks = [
        c for e in trace.get("agent_executions", []) if e.get("retrieval")
        for c in e["retrieval"].get("chunks", [])
    ]
    retrieved_hits = {
        m["id"] for m in matchers
        if any(_matches(m, [c.get("item_key"), c.get("attachment_key")], c.get("title", "")) for c in chunks)
    } if chunks else None

    cited = _cited_numbers(response.get("answer_text", ""))
    cited_in_range = {n for n in cited if 1 <= n <= len(sources)}
    cited_on_gold = {
        n for n in cited_in_range
        if any(_matches(m, [sources[n - 1].get("item_id")], sources[n - 1].get("title", "")) for m in matchers)
    }
    distinct_returned = len({s.get("item_id") or s.get("title") for s in sources})
    n_docs = max(len(matchers), 1)
    return {
        "expected_doc_recall": len(returned_hits) / n_docs,
        "retrieved_doc_recall": (len(retrieved_hits) / n_docs) if retrieved_hits is not None else None,
        "source_count": min(1.0, distinct_returned / max(question.get("min_sources", 1), 1)),
        "cited_diversity": min(1.0, len(cited_in_range) / max(question.get("min_distinct_cited", 1), 1)),
        "sources_returned": distinct_returned,
        "distinct_cited": len(cited_in_range),
        "citation_precision": (len(cited_on_gold) / len(cited_in_range)) if cited_in_range else None,
        "gold_docs_missing": sorted(m["id"] for m in matchers if m["id"] not in returned_hits),
    }


def _cited_numbers(text: str) -> set[int]:
    nums: set[int] = set()
    for bracket in CITATION_RE.findall(text):
        nums.update(int(n) for n in re.findall(r"S(\d+)", bracket))
    return nums


def _sentences(text: str) -> list[str]:
    """Split into sentences, gluing a citation that follows the full stop back on."""
    text = re.sub(r"([.!?])\s+(\[S\d+[^\]]*\])", r" \2\1", text)
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def score_citations(answer: str, n_sources: int) -> dict:
    """The prompt's CRITICAL CITATION RULE: every factual sentence ends with a valid [SN]."""
    markers = CITATION_RE.findall(answer)
    remainder = CITATION_RE.sub(" ", answer)
    malformed = {name: len(p.findall(remainder)) for name, p in MALFORMED_CITATION_PATTERNS.items()}
    malformed = {k: v for k, v in malformed.items() if v}
    cited = _cited_numbers(answer)
    dangling = sorted(n for n in cited if n < 1 or n > max(n_sources, 0))
    claims = [
        s for s in _sentences(answer)
        if len(_words(s)) >= 6 and not s.rstrip().endswith(":") and not s.lstrip().startswith("#")
    ]
    covered = [s for s in claims if CITATION_RE.search(s)]
    coverage = (len(covered) / len(claims)) if claims else (1.0 if markers else 0.0)
    issues = sum(malformed.values()) + len(dangling)
    score = 0.0 if not markers else coverage * (1 - min(1.0, 0.25 * issues))
    return {
        "citation_compliance": score,
        "citation_markers": len(markers),
        "citation_coverage": coverage,
        "claim_sentences": len(claims),
        "uncited_sentences": [s[:140] for s in claims if s not in covered][:5],
        "malformed_citations": malformed,
        "dangling_citations": dangling,
    }


def _numbers(text: str) -> set[str]:
    cleaned = CITATION_RE.sub(" ", text)
    found = set()
    for raw in re.findall(r"\d+(?:[.,  \s]?\d{3})*(?:[.,]\d+)?", cleaned):
        value = re.sub(r"[  \s]", "", raw)
        if re.fullmatch(r"\d+(?:[.,]\d{3})+", value):       # 1,613 / 1.613 -> 1613
            value = re.sub(r"[.,]", "", value)
        value = value.replace(",", ".")
        if "." in value:
            value = value.rstrip("0").rstrip(".") or "0"
        found.add(value)
    return found


def score_grounded_numbers(answer: str, context: str) -> dict:
    """Numbers in the answer that never occur in the context (a cheap hallucination proxy)."""
    if not context:
        return {"grounded_numbers": None, "ungrounded_numbers": []}
    answer_nums = {n for n in _numbers(answer) if not re.fullmatch(r"\d", n)}  # skip 1-9: "two sources"
    context_nums = _numbers(context)
    ungrounded = sorted(answer_nums - context_nums)
    ratio = 1.0 if not answer_nums else 1 - len(ungrounded) / len(answer_nums)
    return {"grounded_numbers": ratio, "ungrounded_numbers": ungrounded}


def score_length(answer: str, band: dict) -> dict:
    words = count_words(answer)
    lo, hi = band["min_words"], band["max_words"]
    if words < lo:
        score = words / lo
    elif words > hi:
        score = hi / words
    else:
        score = 1.0
    return {"length": score, "words": words}


def score_language(answer: str, expected: str) -> dict:
    detected = detect_language(CITATION_RE.sub(" ", answer))
    return {"language": 1.0 if detected == expected else 0.0, "detected_language": detected}


def score_hygiene(answer: str) -> dict:
    flags = []
    if NARRATION_START.search(answer):
        flags.append("process_narration")
    if TOOL_LEAK.search(answer):
        flags.append("tool_call_leak")
    if THINK_LEAK.search(answer):
        flags.append("think_block_leak")
    if CONTEXT_INSUFFICIENT_MARKER in answer:
        flags.append("insufficient_marker_visible")
    if REFUSAL.search(answer):
        flags.append("refusal")
    return {"hygiene": max(0.0, 1 - 0.34 * len(flags)), "hygiene_flags": flags}


# ---------------------------------------------------------------- composite
def score_run(response: dict, question: dict, gold: dict) -> dict:
    """All criteria + composite + verdict for one API response (``/api/query``) to a gold question."""
    from common import strip_html  # local import keeps this module importable standalone in tests

    answer = strip_html(response.get("answer") or "")
    response = {**response, "answer_text": answer}
    trace = response.get("trace")
    context = _context_text(trace)
    sources = response.get("sources") or []

    result: dict[str, Any] = {"question_id": question["id"], "answer_text": answer}
    result.update(score_facts(answer, context, question["facts"]))
    result.update(score_sources(response, question, gold))
    result.update(score_citations(answer, len(sources)))
    result.update(score_grounded_numbers(answer, context))
    result.update(score_length(answer, question["length"]))
    result.update(score_language(answer, question["language"]))
    result.update(score_hygiene(answer))

    return finalize(result, response, WEIGHTS)


def finalize(result: dict, response: dict, weights_table: dict) -> dict:
    """Composite + verdict from the criteria present in ``result`` (unmeasured ones drop out)."""
    weights = {k: w for k, w in weights_table.items() if result.get(k) is not None}
    composite = sum(result[k] * w for k, w in weights.items()) / sum(weights.values())
    result["composite"] = composite
    if response.get("status", "complete") != "complete" or not result.get("answer_text"):
        verdict = "fail"
    elif result["fact_recall"] < FACT_RECALL_FLOOR or composite < WARN_THRESHOLD:
        verdict = "fail"
    elif composite < PASS_THRESHOLD:
        verdict = "warn"
    else:
        verdict = "pass"
    result["verdict"] = verdict
    result.update(trace_metrics(response.get("trace")))
    return result


def validate_judgment(judgment: dict, question: dict) -> list[str]:
    """Problems that make a judgment unusable (empty list = complete and valid)."""
    problems = []
    facts = judgment.get("facts", {})
    for fact in question["facts"]:
        entry = facts.get(fact["id"])
        if not entry or entry.get("verdict") not in VERDICT_CREDIT:
            problems.append(f"fact {fact['id']}: verdict must be one of {sorted(VERDICT_CREDIT)}")
        elif entry.get("attribution_ok") not in (True, False):
            problems.append(f"fact {fact['id']}: attribution_ok must be true/false")
    support = judgment.get("citation_support")
    if not isinstance(support, (int, float)) or not 0 <= support <= 1:
        problems.append("citation_support must be a number 0..1")
    if question.get("inference"):
        quality = judgment.get("inference_quality")
        if not isinstance(quality, (int, float)) or not 0 <= quality <= 1:
            problems.append("inference_quality must be 0, 0.5 or 1 for this question")
    overall = judgment.get("overall")
    if not isinstance(overall, (int, float)) or not 0 <= overall <= 5:
        problems.append("overall must be a number 0..5")
    return problems


def apply_judgment(result: dict, judgment: dict, question: dict, response: dict) -> dict:
    """Merge the reviewing agent's judgment into an automatic result.

    ``judgment`` schema (see references/judging.md)::

        {"facts": {fact_id: {"verdict": verbatim|semantic|inferred|partial|missing|contradicted,
                              "attribution_ok": bool, "evidence": "..."}},
         "unsupported_claims": [...], "attribution_errors": [...],
         "citation_support": 0..1, "inference_quality": 0..1 | null,
         "overall": 0..5, "notes": "..."}
    """
    judged = judgment.get("facts", {})
    total = got = 0.0
    by_level: dict[str, list[float]] = {}
    detail, false_pos, false_neg, attribution = [], [], [], []
    auto = {d["id"]: d for d in result["facts"]}
    for fact in question["facts"]:
        weight = float(fact.get("weight", 1))
        level = fact.get("level", "verbatim")
        entry = judged.get(fact["id"], {})
        verdict = entry.get("verdict")
        credit = VERDICT_CREDIT.get(verdict, 0.0)
        if entry.get("attribution_ok") is False and credit > 0:
            credit = 0.0
            attribution.append(fact["id"])
        total += weight
        got += weight * credit
        stat = by_level.setdefault(level, [0.0, 0.0])
        stat[0] += weight * credit
        stat[1] += weight
        regex_hit = auto.get(fact["id"], {}).get("in_answer")
        if regex_hit is True and credit == 0:
            false_pos.append(fact["id"])
        if regex_hit is False and credit > 0:
            false_neg.append(fact["id"])
        detail.append({"id": fact["id"], "level": level, "verdict": verdict, "credit": credit,
                       "regex_hit": regex_hit, "evidence": entry.get("evidence", "")})
    unsupported = judgment.get("unsupported_claims") or []
    groundedness = max(0.0, 1 - 0.34 * len(unsupported))
    if result.get("grounded_numbers") is not None:
        groundedness = min(groundedness, result["grounded_numbers"])
    result.update({
        "fact_recall_auto": result["fact_recall"],
        "fact_recall": got / total if total else 1.0,
        "recall_by_level": {lvl: g / t for lvl, (g, t) in by_level.items()},
        "facts_judged": detail,
        "regex_false_positives": false_pos,
        "regex_false_negatives": false_neg,
        "attribution_errors": sorted(set(judgment.get("attribution_errors") or []) | set(attribution)),
        "unsupported_claims": unsupported,
        "groundedness": groundedness,
        "citation_support": judgment.get("citation_support"),
        "inference_quality": judgment.get("inference_quality") if question.get("inference") else None,
        "judge_overall": judgment.get("overall"),
        "judge_notes": judgment.get("notes", ""),
        "judged": True,
    })
    return finalize(result, response, WEIGHTS_JUDGED)


def trace_metrics(trace: Optional[dict]) -> dict:
    """Pipeline-side diagnostics from the debug trace."""
    if not trace:
        return {"has_trace": False}
    executions = trace.get("agent_executions", [])
    retrievals = [e["retrieval"] for e in executions if e.get("retrieval")]
    llm_calls = trace.get("llm_calls", [])
    generation_calls = [c for c in llm_calls if c.get("call_type") in ("rag_generation", "synthesis")]
    return {
        "has_trace": True,
        "agents": [e.get("agent_name") for e in executions],
        "documents_grouped": [r.get("documents_grouped") for r in retrievals],
        "raw_chunks": [r.get("raw_results_count") for r in retrievals],
        "escalated_retrieval": any(r.get("escalated") for r in retrievals),
        "top_score": max((r.get("score_stats", {}).get("max", 0) for r in retrievals), default=None),
        "context_chars": sum(len(e.get("context_text") or "") for e in executions),
        "llm_calls": len(llm_calls),
        "generation_retries": max(0, len(generation_calls) - 1),
        "prompt_chars": sum(len(c.get("prompt") or "") for c in llm_calls),
        "completion_chars": sum(len(c.get("response") or "") for c in llm_calls),
        "fallback_triggered": bool(trace.get("fallback_triggered")),
        "duration_ms": trace.get("total_duration_ms"),
    }


def aggregate(values: list[float]) -> dict:
    """mean / stdev / min for a list of scores (stdev 0 for a single value)."""
    if not values:
        return {"mean": None, "stdev": None, "min": None}
    return {
        "mean": statistics.fmean(values),
        "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
    }
