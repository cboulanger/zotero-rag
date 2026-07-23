"""
One-shot live query debugger against the local backend + test-rag-plugin
library — condenses the key-extraction / library-id lookup / trace-request /
result-summary steps that live-debugging a reported answer-quality bug
otherwise requires re-deriving each session.

Reads API keys straight from the encrypted autoindex key store in-process
(never printed, never passed as a subprocess argument), resolves the
test-rag-plugin group's backend library_id automatically, POSTs to
/api/query with include_trace=true one or more times, and prints a compact
per-run summary: agents used, documents_grouped (retrieval diversity),
whether every source has a citation, and the answer text. Full trace JSON
for each run is written to --output-dir for deeper inspection.

Usage:
    uv run python scripts/debug_live_query.py "Which features does Zotero have that Citavi does not?"
    uv run python scripts/debug_live_query.py "..." --repeat 5
    uv run python scripts/debug_live_query.py "..." --llm-model meta-llama-3.1-8b-instruct
    uv run python scripts/debug_live_query.py "..." --inspect-index

--inspect-index additionally stops the backend (embedded Qdrant only allows
one process to hold the storage directory open at a time), reports how many
chunks each indexed item contributes to the retrieved library, then restarts
the backend. Use this when a run's documents_grouped looks suspiciously low
and you want to check whether a few over-chunked documents are crowding out
others in nearest-neighbor search.

Requires AUTOINDEX_SECRET set in .env and at least one stored key (see
CLAUDE.md's "Live Query Debugging" section). Defaults to the
"test-rag-plugin" group library (Zotero group 6297749) — see CLAUDE.md
for why that's the library to use for this kind of testing.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_SN_CITATION_PATTERN = re.compile(r"\[S\d+(?::\d+)?(?:,\s*S\d+(?::\d+)?)*\]")

_TEST_GROUP_LIBRARY_ID = "6297749"  # test-rag-plugin — see CLAUDE.md


def _get_keys() -> tuple[str, dict[str, str]]:
    """Return (zotero_api_key, {header_name: value}) without ever printing them."""
    from backend.config.settings import get_settings
    from backend.services.autoindex_key_store import AutoIndexKeyStore
    from backend.services.embeddings import env_var_to_header

    settings = get_settings()
    store = AutoIndexKeyStore(settings.autoindex_keys_path, settings.autoindex_secret)
    if not store.enabled:
        raise SystemExit("[FAIL] AUTOINDEX_SECRET is not set; cannot decrypt stored keys.")

    entries = list(store.iter_decrypted())
    if not entries:
        raise SystemExit("[FAIL] No keys stored in the auto-index key store.")

    fp, zotero_key, _entry = entries[0]
    extra_headers: dict[str, str] = {}
    result = store.get_decrypted_embedding_key(fp)
    if result is not None:
        key_name, key_value = result
        extra_headers[env_var_to_header(key_name)] = key_value
    return zotero_key, extra_headers


def _resolve_library_id(base_url: str, zotero_key: str, library_id: str | None) -> str:
    if library_id:
        return library_id
    import httpx

    with httpx.Client(timeout=30.0) as client:
        resp = client.get(f"{base_url}/api/libraries", headers={"X-Zotero-API-Key": zotero_key})
        resp.raise_for_status()
    for lib in resp.json():
        if lib.get("library_id") == _TEST_GROUP_LIBRARY_ID:
            return _TEST_GROUP_LIBRARY_ID
    raise SystemExit(
        f"[FAIL] test-rag-plugin library ({_TEST_GROUP_LIBRARY_ID}) not found in "
        f"/api/libraries — pass --library-ids explicitly."
    )


def _run_query(
    base_url: str,
    question: str,
    library_id: str,
    zotero_key: str,
    extra_headers: dict[str, str],
    llm_model: str | None,
    no_routing: bool,
) -> dict:
    import httpx

    payload: dict = {
        "question": question,
        "library_ids": [library_id],
        "enable_routing": not no_routing,
        "include_trace": True,
    }
    if llm_model:
        payload["llm_model"] = llm_model

    headers = {"Content-Type": "application/json", "X-Zotero-API-Key": zotero_key, **extra_headers}
    with httpx.Client(timeout=300.0) as client:
        resp = client.post(f"{base_url}/api/query", json=payload, headers=headers)
    if resp.status_code != 200:
        raise SystemExit(f"[ERROR] HTTP {resp.status_code}: {resp.text}")
    return resp.json()


def _summarize(data: dict) -> str:
    answer = data.get("answer") or ""
    trace = data.get("trace") or {}
    agents = [e.get("agent_name") for e in trace.get("agent_executions", [])]
    llm_calls = [c.get("call_type") for c in trace.get("llm_calls", [])]
    retrievals = [
        e.get("retrieval") for e in trace.get("agent_executions", []) if e.get("retrieval")
    ]
    docs_grouped = [r.get("documents_grouped") for r in retrievals]
    has_citation = bool(_SN_CITATION_PATTERN.search(answer))
    lines = [
        f"status={data.get('status')} agents={agents} llm_calls={llm_calls} "
        f"documents_grouped={docs_grouped} has_citation={has_citation}",
        f"answer: {answer[:400]}{'...' if len(answer) > 400 else ''}",
    ]
    return "\n".join(lines)


def _inspect_index(library_id: str) -> None:
    """Stop the backend, report per-item chunk counts for library_id, restart."""
    print("[INFO] Stopping backend to get exclusive access to the embedded Qdrant store...", file=sys.stderr)
    subprocess.run(["uv", "run", "python", "scripts/server.py", "stop"], cwd=_PROJECT_ROOT, check=True)
    time.sleep(1)

    try:
        from backend.dependencies import make_vector_store
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        vs = make_vector_store()
        counts: Counter = Counter()
        titles: dict[str, str] = {}
        offset = None
        flt = Filter(must=[FieldCondition(key="library_id", match=MatchValue(value=library_id))])
        total = 0
        while True:
            points, offset = vs.client.scroll(
                collection_name=vs.CHUNKS_COLLECTION,
                scroll_filter=flt,
                limit=500,
                offset=offset,
                with_payload=True,
            )
            for p in points:
                key = p.payload.get("item_key")
                counts[key] += 1
                titles[key] = (p.payload.get("title") or "")[:70]
            total += len(points)
            if offset is None:
                break

        print(f"\n[INDEX] library {library_id}: {total} chunks across {len(counts)} items", file=sys.stderr)
        print("[INDEX] top chunk contributors:", file=sys.stderr)
        for item_key, count in counts.most_common(15):
            print(f"  {count:5d}  {item_key}  {titles.get(item_key)}", file=sys.stderr)
    finally:
        print("[INFO] Restarting backend...", file=sys.stderr)
        subprocess.run(["uv", "run", "python", "scripts/server.py", "start"], cwd=_PROJECT_ROOT, check=True)
        time.sleep(6)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("question")
    parser.add_argument("--library-ids", default=None, metavar="ID", help="Override the auto-resolved test-rag-plugin library id")
    parser.add_argument("--llm-model", default=None, metavar="MODEL")
    parser.add_argument("--no-routing", action="store_true")
    parser.add_argument("--repeat", type=int, default=1, metavar="N", help="Run the query N times (useful given router/LLM non-determinism)")
    parser.add_argument("--output-dir", default=None, metavar="DIR", help="Write full trace JSON per run here (default: data/logs/debug_traces/)")
    parser.add_argument("--url", default="http://localhost:8119")
    parser.add_argument("--inspect-index", action="store_true", help="Also report per-item chunk counts (stops+restarts the backend)")
    args = parser.parse_args()

    zotero_key, extra_headers = _get_keys()
    library_id = _resolve_library_id(args.url, zotero_key, args.library_ids)
    print(f"[INFO] library_id={library_id}", file=sys.stderr)

    out_dir = Path(args.output_dir) if args.output_dir else _PROJECT_ROOT / "data" / "logs" / "debug_traces"
    out_dir.mkdir(parents=True, exist_ok=True)

    for i in range(1, args.repeat + 1):
        data = _run_query(args.url, args.question, library_id, zotero_key, extra_headers, args.llm_model, args.no_routing)
        out_path = out_dir / f"run_{i}.json"
        out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n=== run {i}/{args.repeat} -> {out_path} ===")
        print(_summarize(data))

    if args.inspect_index:
        _inspect_index(library_id)


if __name__ == "__main__":
    main()
