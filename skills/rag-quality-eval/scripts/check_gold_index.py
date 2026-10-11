"""Verify every gold-standard document is actually indexed before an eval run.

``/api/libraries`` listing the gold library only means *some* of its items
are indexed — not that every document the gold questions depend on has
content in the currently active embedding collection. A document can be
Zotero-tagged "indexed" from a *previous* embedding model's collection (the
tag isn't model-scoped) while the active collection has zero chunks for it,
e.g. after a preset/embedding-model switch whose re-index hasn't completed
yet. Running the eval against a gold set with silent indexing gaps measures
"never indexed" as if it were a retrieval or generation defect, which is
exactly how issue #80 (RAG query pipeline evaluation) was first misdiagnosed
as a top_k/score_threshold tuning problem — the real cause, found only by
direct vector-store inspection, was that several gold documents (including
two of this run's own targets) had never been chunked into the active
collection at all.

Usage:
    uv run python skills/rag-quality-eval/scripts/check_gold_index.py
    uv run python skills/rag-quality-eval/scripts/check_gold_index.py --url http://localhost:8119
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DEFAULT_URL, auth_headers, get_zotero_key, http_json, load_gold, resolve_library_id  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--gold", default=None, help="Override path to questions.json")
    args = parser.parse_args()

    gold = load_gold(Path(args.gold)) if args.gold else load_gold()
    zotero_key = get_zotero_key()
    library_id = resolve_library_id(args.url, zotero_key, gold)

    attachment_to_docs: dict[str, list[str]] = {}
    for doc_id, doc in gold["documents"].items():
        for key in doc["attachment_keys"]:
            attachment_to_docs.setdefault(key, []).append(doc_id)
    all_keys = sorted(attachment_to_docs)

    status, data = http_json(
        "POST",
        f"{args.url}/api/indexed-tags/check",
        auth_headers(zotero_key),
        {"library_id": library_id, "attachment_keys": all_keys},
        timeout=60,
    )
    if status != 200 or not isinstance(data, dict):
        print(f"[FAIL] /api/indexed-tags/check: HTTP {status} {str(data)[:200]}")
        return 1

    indexed = set(data.get("indexed", []))
    missing = [key for key in all_keys if key not in indexed]

    if missing:
        print(f"[FAIL] {len(missing)}/{len(all_keys)} gold attachments have NO chunks in the "
              f"currently active embedding collection:")
        for key in missing:
            docs = ", ".join(attachment_to_docs[key])
            print(f"  {key}  (used by: {docs})")
        print(
            "\nRe-index before running the eval, e.g.:\n"
            f"  uv run python bin/index_libraries.py --mode incremental --slug {gold['library']['slug']}\n"
            "then re-run this check."
        )
        return 1

    print(f"[PASS] all {len(all_keys)} gold attachments are indexed in the active collection")
    return 0


if __name__ == "__main__":
    sys.exit(main())
