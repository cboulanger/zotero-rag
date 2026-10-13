"""Maintenance helpers for gold/questions.json.

  validate   schema + consistency checks (ids unique, documents exist, regexes
             compile, difficulty/categories valid, language mix)
  format     rewrite the file in the canonical compact layout (one fact per
             line) so diffs stay reviewable after programmatic edits
  stats      question counts by tier / language / fact level

Usage:
    python skills/rag-quality-eval/scripts/gold_tools.py validate
    python skills/rag-quality-eval/scripts/gold_tools.py format
    python skills/rag-quality-eval/scripts/gold_tools.py stats
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import GOLD_PATH, load_gold, tier_of  # noqa: E402

CATEGORY_TYPES = {
    "difficulty": ("1", "2", "3", "4", "5"),
    "domain": ("facts", "routing", "mentions", "metadata"),
    "language": ("en", "de", "fr", "es"),
    "scope": ("single-doc", "multi-doc", "catalog"),
}
AGENTS = ("rag", "metadata", "mentions")
LEVELS = ("verbatim", "semantic", "inference")
LANGUAGES = ("en", "de", "fr", "es")


def _check_categories(q: dict) -> list[str]:
    """Categories are 'type:value' strings of known types; difficulty/language must agree with the fields."""
    problems = []
    cats = q.get("categories", [])
    for cat in cats:
        kind, _, value = cat.partition(":")
        if kind not in CATEGORY_TYPES:
            problems.append(f"{q['id']}: unknown category type in {cat!r}")
        elif value not in CATEGORY_TYPES[kind]:
            problems.append(f"{q['id']}: bad category value {cat!r}")
    for kind in ("difficulty", "domain"):
        if sum(1 for c in cats if c.startswith(kind + ":")) != 1:
            problems.append(f"{q['id']}: exactly one {kind}: category required")
    if f"difficulty:{q.get('difficulty')}" not in cats:
        problems.append(f"{q['id']}: difficulty field and difficulty: category disagree")
    if f"language:{q.get('language')}" not in cats:
        problems.append(f"{q['id']}: language field and language: category disagree")
    return problems


def validate(gold: dict) -> list[str]:
    """Return a list of problems (empty = valid)."""
    problems: list[str] = []
    ids = [q["id"] for q in gold["questions"]]
    if len(ids) != len(set(ids)):
        problems.append("duplicate question ids")
    for q in gold["questions"]:
        qid = q["id"]
        routing_only = q.get("scoring") == "routing"
        for field in ("question", "minimum_answer", "facts", "documents", "language",
                      "difficulty", "judge_notes", "categories") + (() if routing_only else (
                          "length", "min_sources", "min_distinct_cited")):
            if field not in q:
                problems.append(f"{qid}: missing '{field}'")
        problems += _check_categories(q)
        if routing_only and not q.get("expected_agents"):
            problems.append(f"{qid}: routing-only questions need expected_agents")
        for agent in q.get("expected_agents", []):
            if agent not in AGENTS:
                problems.append(f"{qid}: unknown agent {agent!r}")
        if "tier" in q:
            problems.append(f"{qid}: 'tier' is derived from difficulty (common.tier_of); remove the field")
        if q.get("language") not in LANGUAGES:
            problems.append(f"{qid}: unsupported language {q.get('language')!r} (scoring.STOPWORDS)")
        for doc_id in q.get("documents", []):
            if doc_id not in gold["documents"]:
                problems.append(f"{qid}: unknown document {doc_id}")
        if tier_of(q) == "hard" and not q.get("inference") and not routing_only and q.get("facts"):
            problems.append(f"{qid}: hard questions need an 'inference' chain for the judge")
        fact_ids = set()
        for fact in q.get("facts", []):
            fid = fact.get("id", "?")
            if fid in fact_ids:
                problems.append(f"{qid}/{fid}: duplicate fact id")
            fact_ids.add(fid)
            if fact.get("level") not in LEVELS:
                problems.append(f"{qid}/{fid}: level must be one of {LEVELS}")
            if not fact.get("answer_patterns") and fact.get("level") != "inference":
                problems.append(f"{qid}/{fid}: only inference facts may be judge-only (no answer_patterns)")
            for key in ("answer_patterns", "source_patterns"):
                for pattern in fact.get(key, []):
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        problems.append(f"{qid}/{fid}: bad regex {pattern!r}: {exc}")
            for doc_id in fact.get("docs", []):
                if doc_id not in gold["documents"]:
                    problems.append(f"{qid}/{fid}: unknown doc {doc_id}")
    if sum(1 for q in gold["questions"] if q["language"] != "en") < 3:
        problems.append("fewer than 3 non-English questions (multilingual criterion needs them)")
    return problems


def _fmt_fact(fact: dict) -> str:
    return "        " + json.dumps(fact, ensure_ascii=False)


def format_gold(gold: dict) -> str:
    """Canonical layout: structure indented, each fact and document on one line."""
    out = ["{"]
    scalars = {k: v for k, v in gold.items() if k not in ("documents", "questions")}
    for key, value in scalars.items():
        out.append(f"  {json.dumps(key)}: {json.dumps(value, ensure_ascii=False, indent=2).replace(chr(10), chr(10) + '  ')},")
    out.append('  "documents": {')
    docs = list(gold["documents"].items())
    for i, (doc_id, doc) in enumerate(docs):
        out.append(f"    {json.dumps(doc_id)}: {json.dumps(doc, ensure_ascii=False)}{',' if i < len(docs) - 1 else ''}")
    out.append("  },")
    out.append('  "questions": [')
    for qi, q in enumerate(gold["questions"]):
        out.append("    {")
        keys = [k for k in q if k != "facts"]
        for k in keys[: keys.index("minimum_answer") + 1] if "minimum_answer" in keys else keys:
            out.append(f"      {json.dumps(k)}: {json.dumps(q[k], ensure_ascii=False)},")
        out.append('      "facts": [')
        for fi, fact in enumerate(q["facts"]):
            out.append(_fmt_fact(fact) + ("," if fi < len(q["facts"]) - 1 else ""))
        out.append("      ],")
        rest = [k for k in keys[keys.index("minimum_answer") + 1:]] if "minimum_answer" in keys else []
        for ri, k in enumerate(rest):
            out.append(f"      {json.dumps(k)}: {json.dumps(q[k], ensure_ascii=False)}{',' if ri < len(rest) - 1 else ''}")
        out.append("    }" + ("," if qi < len(gold["questions"]) - 1 else ""))
    out.append("  ]")
    out.append("}")
    return "\n".join(out) + "\n"


def stats(gold: dict) -> str:
    qs = gold["questions"]
    levels = Counter(f["level"] for q in qs for f in q["facts"])
    lines = [
        f"{len(qs)} questions, {sum(len(q['facts']) for q in qs)} facts, {len(gold['documents'])} documents",
        "by tier:     " + ", ".join(f"{k}={v}" for k, v in Counter(tier_of(q) for q in qs).items()),
        "by domain:   " + ", ".join(f"{k}={v}" for k, v in sorted(Counter(c[7:] for q in qs for c in q.get("categories", []) if c.startswith("domain:")).items())),
        "by difficulty: " + ", ".join(f"{k}={v}" for k, v in sorted(Counter(q["difficulty"] for q in qs).items())),
        "by language: " + ", ".join(f"{k}={v}" for k, v in Counter(q["language"] for q in qs).items()),
        "fact level:  " + ", ".join(f"{k}={v}" for k, v in levels.items()),
        "judge-only facts: " + str(sum(1 for q in qs for f in q["facts"] if not f.get("answer_patterns"))),
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("cmd", choices=["validate", "format", "stats"])
    parser.add_argument("--gold", default=str(GOLD_PATH))
    args = parser.parse_args()
    path = Path(args.gold)
    gold = load_gold(path)
    if args.cmd == "validate":
        problems = validate(gold)
        for p in problems:
            print(f"[FAIL] {p}")
        print(f"[{'FAIL' if problems else 'PASS'}] {len(problems)} problem(s)")
        return 1 if problems else 0
    if args.cmd == "format":
        path.write_text(format_gold(gold), encoding="utf-8")
        json.loads(path.read_text(encoding="utf-8"))  # round-trip check
        print(f"[OK] formatted {path}")
        return 0
    print(stats(gold))
    return 0


if __name__ == "__main__":
    sys.exit(main())
