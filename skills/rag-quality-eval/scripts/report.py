"""Aggregate saved evaluation runs into a report.

Reads ``<run_dir>/raw/*.json`` (written by run_eval.py), re-scores every
response with the *current* gold file and scoring code (so criteria or gold
fixes never require new queries), merges the reviewing agent's judgments from
``<run_dir>/judgments/`` when present (judge.py), and writes into the run
directory:

  results.jsonl        one scored record per query
  report.json          aggregates per preset/model, tier, language
  report.md            human-readable summary (print this for the user)

Usage:
    python skills/rag-quality-eval/scripts/report.py data/logs/rag_eval/<run> [--baseline <other run dir>]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scoring  # noqa: E402
from common import load_gold  # noqa: E402

CRITERIA = list(scoring.WEIGHTS)
TIERS = ("easy", "medium", "hard")


def load_records(run_dir: Path, gold: dict) -> list[dict]:
    """Score every raw response of a run dir (+ judgment if complete); failures become failing records."""
    questions = {q["id"]: q for q in gold["questions"]}
    records = []
    for path in sorted((run_dir / "raw").glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        meta = raw["meta"]
        question = questions.get(meta["question_id"])
        if question is None:
            continue
        base = {**meta, "tier": question["tier"], "difficulty": question["difficulty"],
                "q_language": question["language"], "file": path.name, "stem": path.stem}
        if raw.get("response"):
            scored = scoring.score_run(raw["response"], question, gold)
            scored["sources"] = [s.get("title", "")[:80] for s in raw["response"].get("sources", [])]
            jpath = run_dir / "judgments" / f"{path.stem}.json"
            if jpath.exists():
                judgment = json.loads(jpath.read_text(encoding="utf-8"))
                problems = scoring.validate_judgment(judgment, question)
                if problems:
                    scored["judgment_problems"] = problems
                else:
                    scored = scoring.apply_judgment(scored, judgment, question, raw["response"])
            records.append({**base, **scored})
        else:
            records.append({**base, "verdict": "fail", "composite": 0.0, "fact_recall": 0.0,
                            "error": raw.get("error") or "no response", "answer_text": ""})
    return records


def _mean(values: list) -> Optional[float]:
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def _pct(values: list[float], q: float) -> Optional[float]:
    values = sorted(values)
    if not values:
        return None
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def summarize_group(rows: list[dict]) -> dict:
    """Aggregates for all records of one (preset, model)."""
    ok = [r for r in rows if "error" not in r]
    per_question: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        per_question[r["question_id"]].append(r["composite"])
    stdevs = [statistics.pstdev(v) for v in per_question.values() if len(v) > 1]
    non_en = [r for r in ok if r["q_language"] != "en"]
    en = [r for r in ok if r["q_language"] == "en"]
    judged = [r for r in ok if r.get("judged")]
    levels: dict[str, list[float]] = defaultdict(list)
    for r in ok:
        for level, value in (r.get("recall_by_level") or {}).items():
            levels[level].append(value)
    malformed, hygiene = Counter(), Counter()
    for r in ok:
        malformed.update(r.get("malformed_citations", {}))
        hygiene.update(r.get("hygiene_flags", []))
    summary: dict[str, Any] = {
        "runs": len(rows), "errors": len(rows) - len(ok),
        "verdicts": dict(Counter(r["verdict"] for r in rows)),
        "composite": _mean([r["composite"] for r in rows]),
        "criteria": {c: _mean([r.get(c) for r in ok]) for c in CRITERIA},
        "by_tier": {t: _mean([r["composite"] for r in rows if r["tier"] == t]) for t in TIERS},
        "recall_en": _mean([r["fact_recall"] for r in en]),
        "recall_non_en": _mean([r["fact_recall"] for r in non_en]),
        "language_match_non_en": _mean([r["language"] for r in non_en]),
        "judged_runs": len(judged),
        "recall_by_level": {lvl: _mean(v) for lvl, v in levels.items()},
        "recall_auto": _mean([r.get("fact_recall_auto", r["fact_recall"]) for r in ok]),
        "regex_false_positives": sum(len(r.get("regex_false_positives", [])) for r in judged),
        "regex_false_negatives": sum(len(r.get("regex_false_negatives", [])) for r in judged),
        "unsupported_claims_total": sum(len(r.get("unsupported_claims", [])) for r in judged),
        "attribution_errors_total": sum(len(r.get("attribution_errors", [])) for r in judged),
        "citation_support": _mean([r.get("citation_support") for r in judged]),
        "inference_quality": _mean([r.get("inference_quality") for r in judged]),
        "judge_overall": _mean([r.get("judge_overall") for r in judged]),
        "stability_stdev": _mean(stdevs),
        "latency_p50_ms": _pct([r["wall_ms"] for r in ok], 0.5),
        "latency_p95_ms": _pct([r["wall_ms"] for r in ok], 0.95),
        "avg_words": _mean([r.get("words") for r in ok]),
        "generation_retry_rate": _mean([1.0 if r.get("generation_retries") else 0.0 for r in ok if r.get("has_trace")]),
        "escalation_rate": _mean([1.0 if r.get("escalated_retrieval") else 0.0 for r in ok if r.get("has_trace")]),
        "avg_distinct_cited": _mean([r.get("distinct_cited") for r in ok]),
        "avg_sources_returned": _mean([r.get("sources_returned") for r in ok]),
        "malformed_citations": dict(malformed), "hygiene_flags": dict(hygiene),
        "ungrounded_numbers_total": sum(len(r.get("ungrounded_numbers", [])) for r in ok),
        "lost_in_retrieval": sum(len(r.get("missed_retrieval", [])) for r in ok),
        "lost_in_generation": sum(len(r.get("missed_generation", [])) for r in ok),
        "uncited_answers": sum(1 for r in ok if not r.get("citation_markers")),
        "est_prompt_tokens": (sum(r.get("prompt_chars", 0) for r in ok) // 4) if ok else None,
        "est_completion_tokens": (sum(r.get("completion_chars", 0) for r in ok) // 4) if ok else None,
    }
    if summary["recall_en"] is not None and summary["recall_non_en"] is not None:
        summary["multilingual_gap"] = summary["recall_en"] - summary["recall_non_en"]
    else:
        summary["multilingual_gap"] = None
    return summary


def _f(value: Optional[float], digits: int = 2, pct: bool = False) -> str:
    if value is None:
        return "-"
    return f"{value * 100:.0f}%" if pct else f"{value:.{digits}f}"


def render_markdown(groups: dict[str, dict], records: list[dict], gold: dict, meta: dict,
                    baseline: Optional[dict]) -> str:
    lines = ["# RAG quality evaluation", ""]
    if meta:
        lines += [f"- backend `{meta.get('url')}` commit `{meta.get('git_commit')}`, gold v{meta.get('gold_version')}, "
                  f"library {meta.get('library_id')}", f"- started {meta.get('started')}, finished {meta.get('finished')}"]
        for s in meta.get("skipped", []):
            lines.append(f"- SKIPPED `{s['preset']}`: {s['reason']}")
    ranked = sorted(groups.items(), key=lambda kv: -(kv[1]["composite"] or 0))
    answered = [r for r in records if "error" not in r]
    n_judged = sum(1 for r in answered if r.get("judged"))
    if n_judged < len(answered):
        lines += ["", f"> **Automatic floor only for {len(answered) - n_judged} of {len(answered)} answered runs.** "
                  "Regex matching proves verbatim hits only; semantic hits, translations and inferred conclusions are "
                  "invisible to it. Run `judge.py prepare`, judge the packets (references/judging.md) and re-run "
                  "`report.py`. Scores below are provisional for unjudged runs."]
        pending = [r for r in answered if r.get("judgment_problems")]
        if pending:
            lines.append(f"> {len(pending)} judgment file(s) are incomplete (see `judge.py validate`).")
    lines += ["", "## Ranking (composite 0-1: pass >= 0.85, warn >= 0.65; fact recall < 0.5 always fails)", "",
              "| Preset / model | Composite | Pass/warn/fail | Recall | Easy | Medium | Hard | Errors | p50 s | Stability (stdev) |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for name, g in ranked:
        v = g["verdicts"]
        lines.append(
            f"| {name} | {_f(g['composite'])} | {v.get('pass', 0)}/{v.get('warn', 0)}/{v.get('fail', 0)} | "
            f"{_f(g['criteria']['fact_recall'], pct=True)} | {_f(g['by_tier']['easy'])} | {_f(g['by_tier']['medium'])} | "
            f"{_f(g['by_tier']['hard'])} | {g['errors']} | {_f((g['latency_p50_ms'] or 0) / 1000 if g['latency_p50_ms'] else None, 1)} | "
            f"{_f(g['stability_stdev'], 3)} |")

    lines += ["", "## Criteria means per preset / model", "",
              "| Preset / model | " + " | ".join(CRITERIA) + " |", "|---|" + "---|" * len(CRITERIA)]
    for name, g in ranked:
        lines.append(f"| {name} | " + " | ".join(_f(g["criteria"][c]) for c in CRITERIA) + " |")

    judged_groups = [(n, g) for n, g in ranked if g["judged_runs"]]
    if judged_groups:
        lines += ["", "## Judged findings (reviewing agent)", "",
                  "| Preset / model | Judged | Recall judged | Recall regex | Citation support | Inference quality | "
                  "Unsupported claims | Attribution errors | Overall (0-5) |", "|---|---|---|---|---|---|---|---|---|"]
        for name, g in judged_groups:
            lines.append(f"| {name} | {g['judged_runs']}/{g['runs'] - g['errors']} | {_f(g['criteria']['fact_recall'], pct=True)} | "
                         f"{_f(g['recall_auto'], pct=True)} | {_f(g['citation_support'], pct=True)} | "
                         f"{_f(g['inference_quality'], pct=True)} | {g['unsupported_claims_total']} | "
                         f"{g['attribution_errors_total']} | {_f(g['judge_overall'], 1)} |")
        lines += ["", "### Recall by reasoning level", "",
                  "verbatim = literal figures/names, semantic = same meaning in other words or another language, "
                  "inference = conclusion that must be derived by combining sources.", "",
                  "| Preset / model | Verbatim | Semantic | Inference |", "|---|---|---|---|"]
        for name, g in judged_groups:
            rl = g["recall_by_level"]
            lines.append(f"| {name} | {_f(rl.get('verbatim'), pct=True)} | {_f(rl.get('semantic'), pct=True)} | "
                         f"{_f(rl.get('inference'), pct=True)} |")
        lines += ["", "### Regex vs. judge disagreements", "",
                  "False negatives = the judge found a hit the regex missed (paraphrase, translation, inference); "
                  "false positives = the regex matched but the judge says the fact is wrong, swapped or absent.", "",
                  "| Preset / model | Regex false negatives | Regex false positives |", "|---|---|---|"]
        for name, g in judged_groups:
            lines.append(f"| {name} | {g['regex_false_negatives']} | {g['regex_false_positives']} |")

    lines += ["", "## Multilingual behaviour", "",
              "Fact recall on English questions vs non-English questions (cross-lingual retrieval + answer in the "
              "question's language).", "",
              "| Preset / model | Recall en | Recall non-en | Gap | Answer-language match (non-en) |", "|---|---|---|---|---|"]
    for name, g in ranked:
        lines.append(f"| {name} | {_f(g['recall_en'], pct=True)} | {_f(g['recall_non_en'], pct=True)} | "
                     f"{_f(g['multilingual_gap'], pct=True)} | {_f(g['language_match_non_en'], pct=True)} |")

    lines += ["", "## Pipeline diagnostics", "",
              "| Preset / model | Avg sources | Avg cited | Uncited answers | Retry rate | Escalation | Facts lost in retrieval | "
              "Facts lost in generation | Ungrounded numbers | Avg words |", "|---|---|---|---|---|---|---|---|---|---|"]
    for name, g in ranked:
        lines.append(f"| {name} | {_f(g['avg_sources_returned'], 1)} | {_f(g['avg_distinct_cited'], 1)} | {g['uncited_answers']} | "
                     f"{_f(g['generation_retry_rate'], pct=True)} | {_f(g['escalation_rate'], pct=True)} | "
                     f"{g['lost_in_retrieval']} | {g['lost_in_generation']} | {g['ungrounded_numbers_total']} | "
                     f"{_f(g['avg_words'], 0)} |")

    lines += ["", "## Structural-guideline violations", ""]
    any_violation = False
    for name, g in ranked:
        issues = {**g["malformed_citations"], **g["hygiene_flags"]}
        if issues or g["uncited_answers"]:
            any_violation = True
            lines.append(f"- **{name}**: " + ", ".join(f"{k} x{v}" for k, v in sorted(issues.items()))
                         + (f", {g['uncited_answers']} answer(s) without any citation" if g["uncited_answers"] else ""))
    if not any_violation:
        lines.append("- none detected")

    qids = [q["id"] for q in gold["questions"]]
    lines += ["", "## Composite per question", "", "| Preset / model | " + " | ".join(qids) + " |",
              "|---|" + "---|" * len(qids)]
    by_group_q: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        by_group_q[f"{r['preset']} / {r['model']}"][r["question_id"]].append(r["composite"])
    for name, _g in ranked:
        cells = [_f(_mean(by_group_q[name].get(q, []))) for q in qids]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")

    if baseline:
        lines += ["", "## Change vs baseline", "", "| Preset / model | Baseline | Now | Delta |", "|---|---|---|---|"]
        for name, g in ranked:
            before = baseline.get(name, {}).get("composite")
            delta = (g["composite"] - before) if before is not None and g["composite"] is not None else None
            sign = "" if delta is None or delta < 0 else "+"
            lines.append(f"| {name} | {_f(before)} | {_f(g['composite'])} | {sign}{_f(delta, 3)} |")

    errors = [r for r in records if "error" in r]
    if errors:
        lines += ["", "## Errors", ""]
        for r in errors[:20]:
            lines.append(f"- {r['preset']}/{r['model']}/{r['question_id']}: HTTP {r['http_status']} {r['error'][:140]}")
    lines.append("")
    return "\n".join(lines)


def build_report(run_dir: Path, baseline_dir: Optional[Path] = None) -> dict:
    """Score a run dir and write all report files; returns the aggregate dict."""
    run_dir = Path(run_dir)
    gold = load_gold()
    records = load_records(run_dir, gold)
    meta_path = run_dir / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        grouped[f"{r['preset']} / {r['model']}"].append(r)
    groups = {name: summarize_group(rows) for name, rows in grouped.items()}

    baseline = None
    if baseline_dir:
        bp = Path(baseline_dir) / "report.json"
        baseline = json.loads(bp.read_text())["groups"] if bp.exists() else None

    with (run_dir / "results.jsonl").open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    (run_dir / "report.json").write_text(json.dumps({"meta": meta, "groups": groups}, indent=2), encoding="utf-8")
    (run_dir / "report.md").write_text(render_markdown(groups, records, gold, meta, baseline), encoding="utf-8")
    return groups


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run_dir")
    parser.add_argument("--baseline", default=None, help="earlier run dir to diff against")
    args = parser.parse_args()
    groups = build_report(Path(args.run_dir), Path(args.baseline) if args.baseline else None)
    print((Path(args.run_dir) / "report.md").read_text(encoding="utf-8"))
    return 0 if groups else 1


if __name__ == "__main__":
    sys.exit(main())
