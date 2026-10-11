"""Prepare, check and track the qualitative (LLM-as-judge) review of an eval run.

Regex matching only proves *verbatim* hits. Paraphrases, translations and
conclusions that must be inferred need reasoning, so the reviewing agent reads
every answer and writes a judgment (rubric: references/judging.md). This
script does the mechanical parts:

  prepare RUN_DIR   write one packet per answered query into RUN_DIR/judge/
                    (question, gold facts with their level, answer, numbered
                    sources, the exact context the LLM saw, automatic findings)
                    plus an empty skeleton RUN_DIR/judgments/<stem>.json to fill
  status RUN_DIR    how many runs are judged / still open, per preset/model
  validate RUN_DIR  list judgments that are missing or incomplete (exit 1)

After the judgments are complete run ``report.py RUN_DIR``; it merges them
(scoring.apply_judgment) and reports judged vs. regex recall.

Usage:
    python skills/rag-quality-eval/scripts/judge.py prepare data/logs/rag_eval/<run> [--force]
    python skills/rag-quality-eval/scripts/judge.py status  data/logs/rag_eval/<run>
    python skills/rag-quality-eval/scripts/judge.py validate data/logs/rag_eval/<run>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scoring  # noqa: E402
from common import load_gold  # noqa: E402


def _runs(run_dir: Path, gold: dict):
    """Yield ``(stem, raw, question)`` for every query that produced an answer."""
    questions = {q["id"]: q for q in gold["questions"]}
    for path in sorted((run_dir / "raw").glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("response") and raw["meta"]["question_id"] in questions:
            yield path.stem, raw, questions[raw["meta"]["question_id"]]


def _skeleton(stem: str, question: dict) -> dict:
    skel = {
        "run": stem,
        "facts": {f["id"]: {"verdict": None, "attribution_ok": None, "evidence": ""} for f in question["facts"]},
        "unsupported_claims": [],
        "attribution_errors": [],
        "citation_support": None,
        "overall": None,
        "notes": "",
    }
    if question.get("inference"):
        skel["inference_quality"] = None
    return skel


def cmd_prepare(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    gold = load_gold()
    (run_dir / "judge").mkdir(exist_ok=True)
    (run_dir / "judgments").mkdir(exist_ok=True)
    queue: dict[str, list[tuple[int, str]]] = defaultdict(list)
    made = 0
    for stem, raw, question in _runs(run_dir, gold):
        judgment_path = run_dir / "judgments" / f"{stem}.json"
        if judgment_path.exists() and not args.force:
            continue
        response = raw["response"]
        auto = scoring.score_run(response, question, gold)
        context = scoring._context_text(response.get("trace"))
        (run_dir / "judge" / f"{stem}.context.txt").write_text(context, encoding="utf-8")
        packet = {
            "run": stem,
            "preset": raw["meta"]["preset"], "model": raw["meta"]["model"],
            "question_id": question["id"], "tier": question["tier"], "language": question["language"],
            "question": question["question"],
            "minimum_answer": question["minimum_answer"],
            "judge_notes": question["judge_notes"],
            "inference_chain": question.get("inference"),
            "facts": [{"id": f["id"], "text": f["text"], "level": f["level"], "weight": f.get("weight", 1),
                       "regex_hit": next((d["in_answer"] for d in auto["facts"] if d["id"] == f["id"]), None)}
                      for f in question["facts"]],
            "answer": auto["answer_text"],
            "sources": [{"n": i + 1, "title": s.get("title"), "item_id": s.get("item_id"), "page": s.get("page_number"),
                         "score": s.get("relevance_score")} for i, s in enumerate(response.get("sources", []))],
            "automatic": {k: auto.get(k) for k in (
                "composite", "verdict", "distinct_cited", "uncited_sentences", "malformed_citations",
                "dangling_citations", "ungrounded_numbers", "hygiene_flags", "detected_language", "words")},
            "context_file": f"judge/{stem}.context.txt",
            "write_judgment_to": f"judgments/{stem}.json",
        }
        (run_dir / "judge" / f"{stem}.packet.json").write_text(
            json.dumps(packet, ensure_ascii=False, indent=1), encoding="utf-8")
        judgment_path.write_text(json.dumps(_skeleton(stem, question), indent=1), encoding="utf-8")
        queue[f"{raw['meta']['preset']} / {raw['meta']['model']}"].append((-question["difficulty"], stem))
        made += 1

    lines = ["# Judging queue", "",
             "For each packet: read `judge/<stem>.packet.json` (+ its context file), then complete "
             "`judgments/<stem>.json` following references/judging.md. Hardest questions first.", ""]
    for group, items in sorted(queue.items()):
        lines.append(f"## {group} ({len(items)})")
        lines += [f"- [ ] `{stem}`" for _d, stem in sorted(items)]
        lines.append("")
    (run_dir / "judge" / "QUEUE.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[OK] {made} packet(s) prepared in {run_dir / 'judge'} (queue: QUEUE.md)")
    return 0


def _status(run_dir: Path, gold: dict) -> tuple[dict, list[tuple[str, list[str]]]]:
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    open_items: list[tuple[str, list[str]]] = []
    for stem, raw, question in _runs(run_dir, gold):
        group = f"{raw['meta']['preset']} / {raw['meta']['model']}"
        path = run_dir / "judgments" / f"{stem}.json"
        problems = ["no judgment file (run `judge.py prepare`)"]
        if path.exists():
            problems = scoring.validate_judgment(json.loads(path.read_text(encoding="utf-8")), question)
        counts[group][1] += 1
        if problems:
            open_items.append((stem, problems))
        else:
            counts[group][0] += 1
    return counts, open_items


def cmd_status(args: argparse.Namespace) -> int:
    counts, _open = _status(Path(args.run_dir), load_gold())
    for group, (done, total) in sorted(counts.items()):
        print(f"{group}: {done}/{total} judged")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    counts, open_items = _status(Path(args.run_dir), load_gold())
    for stem, problems in open_items:
        print(f"[OPEN] {stem}: " + "; ".join(problems))
    total = sum(t for _d, t in counts.values())
    print(f"[{'FAIL' if open_items else 'PASS'}] {total - len(open_items)}/{total} judgments complete")
    return 1 if open_items else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (("prepare", cmd_prepare), ("status", cmd_status), ("validate", cmd_validate)):
        p = sub.add_parser(name)
        p.add_argument("run_dir")
        if name == "prepare":
            p.add_argument("--force", action="store_true", help="overwrite existing judgments (re-judge)")
        p.set_defaults(fn=fn)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
