"""Maintain the gold standard against the real library texts.

Subcommands:

  verify   Download the PDF full text (Zotero public full-text endpoint) of
           every document cited by gold/questions.json and check that each
           fact's ``source_patterns`` really occur in it. Run this after
           editing the gold file or after the library changed.
  corpus   Regenerate gold/corpus.md: the library's top-level items (key,
           type, year, authors, title, PDF key, full-text size) plus which
           gold questions use each one.

Examples:
    python skills/rag-quality-eval/scripts/fetch_gold_corpus.py verify
    python skills/rag-quality-eval/scripts/fetch_gold_corpus.py corpus

Full texts are cached under .local/rag_eval_corpus/ (gitignored); pass
--refresh to re-download. Needs outbound access to api.zotero.org.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import GOLD_PATH, PROJECT_ROOT, SKILL_DIR, ZOTERO_API, http_json, load_gold  # noqa: E402

CACHE_DIR = PROJECT_ROOT / ".local" / "rag_eval_corpus"


def _normalize(text: str) -> str:
    """Collapse whitespace and unify typographic quotes so patterns stay simple."""
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text)


def fetch_fulltext(group_id: str, attachment_key: str, refresh: bool = False) -> str:
    """Full text of an attachment (cached on disk)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{attachment_key}.txt"
    if cache.exists() and not refresh:
        return cache.read_text(encoding="utf-8")
    status, data = http_json("GET", f"{ZOTERO_API}/groups/{group_id}/items/{attachment_key}/fulltext", timeout=90)
    if status != 200 or not isinstance(data, dict):
        raise SystemExit(f"[FAIL] full text for {attachment_key}: HTTP {status} {str(data)[:120]}")
    cache.write_text(data.get("content", ""), encoding="utf-8")
    return data.get("content", "")


def cmd_verify(args: argparse.Namespace) -> int:
    gold = load_gold(Path(args.gold)) if args.gold else load_gold()
    group_id = gold["library"]["library_id"]
    texts: dict[str, str] = {}
    for doc_id, doc in gold["documents"].items():
        texts[doc_id] = _normalize(" ".join(
            fetch_fulltext(group_id, key, args.refresh) for key in doc["attachment_keys"]
        ))
        print(f"[INFO] {doc_id}: {len(texts[doc_id])} chars", file=sys.stderr)

    failures = 0
    checked = 0
    for question in gold["questions"]:
        for doc_id in question["documents"]:
            if doc_id not in texts:
                print(f"[FAIL] {question['id']}: unknown document id {doc_id}")
                failures += 1
        for fact in question["facts"]:
            docs = fact.get("docs") or []
            if not docs:
                continue  # style/structure fact, not grounded in a document
            corpus = " ".join(texts[d] for d in docs)
            explicit = fact.get("source_patterns")
            patterns = explicit or fact["answer_patterns"]
            if not patterns:
                continue  # judge-only inference fact: nothing to grep for
            # explicit source_patterns must ALL occur; derived ones need just one
            hits = [bool(re.search(p, corpus, re.I)) for p in patterns]
            ok = all(hits) if explicit else any(hits)
            checked += 1
            if not ok:
                failures += 1
                missing = [p for p, h in zip(patterns, hits) if not h]
                print(f"[FAIL] {question['id']}/{fact['id']}: not found in {docs}: {missing}")
    print(f"[{'PASS' if not failures else 'FAIL'}] {checked} grounded facts checked, {failures} failure(s)")
    return 1 if failures else 0


def cmd_corpus(args: argparse.Namespace) -> int:
    gold = load_gold()
    group_id = gold["library"]["library_id"]
    items: list[dict] = []
    start = 0
    while True:
        status, data = http_json(
            "GET", f"{ZOTERO_API}/groups/{group_id}/items?format=json&limit=100&start={start}", timeout=60)
        if status != 200 or not isinstance(data, list):
            raise SystemExit(f"[FAIL] item listing: HTTP {status}")
        items += data
        if len(data) < 100:
            break
        start += 100
        time.sleep(0.5)

    pdf_of: dict[str, str] = {}
    for it in items:
        d = it["data"]
        if d["itemType"] == "attachment" and d.get("contentType") == "application/pdf" and d.get("parentItem"):
            pdf_of.setdefault(d["parentItem"], d["key"])
    used_by: dict[str, list[str]] = {}
    for q in gold["questions"]:
        for doc_id in q["documents"]:
            for key in gold["documents"][doc_id]["item_keys"]:
                used_by.setdefault(key, []).append(q["id"])

    parents = [it["data"] for it in items if it["data"]["itemType"] not in ("attachment", "note")]
    lines = [
        "# Gold corpus: test-rag-plugin library",
        "",
        f"Generated by `scripts/fetch_gold_corpus.py corpus` on {time.strftime('%Y-%m-%d')} from "
        f"<{gold['library']['url']}>.",
        f"{len(parents)} top-level items, {len(pdf_of)} with a PDF attachment. "
        "The 'Gold questions' column shows which questions rely on an item.",
        "",
        "| Key | Type | Year | First authors | Title | PDF key | Gold questions |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in sorted(parents, key=lambda x: x.get("date", "")):
        authors = ", ".join((c.get("lastName") or c.get("name", "")) for c in d.get("creators", [])[:3])
        year = (re.findall(r"(?:19|20)\d{2}", d.get("date", "")) or [""])[0]
        title = d.get("title", "").replace("|", "/")[:110]
        lines.append(
            f"| {d['key']} | {d['itemType']} | {year} | {authors} | {title} | "
            f"{pdf_of.get(d['key'], '-')} | {', '.join(used_by.get(d['key'], [])) or ''} |"
        )
    out = SKILL_DIR / "gold" / "corpus.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[OK] wrote {out} ({len(parents)} items)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (("verify", cmd_verify), ("corpus", cmd_corpus)):
        p = sub.add_parser(name)
        p.add_argument("--refresh", action="store_true", help="ignore the on-disk full-text cache")
        p.add_argument("--gold", default=None, help="alternative gold file (default: gold/questions.json)")
        p.set_defaults(fn=fn)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
