"""
Trace a RAG query and save the full execution trace to JSON.

Usage:
    uv run python scripts/query_trace.py "What is autopoiesis?" \\
        --library-ids 12345678 \\
        --output trace.json

The script calls POST /api/query with include_trace=true and writes
the .trace field (plus the normal response fields) to the output file.
"""

import argparse
import json
import os
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trace a RAG query and save intermediate steps to JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("question", help="Question to ask the RAG system")
    parser.add_argument(
        "--library-ids",
        nargs="+",
        required=True,
        metavar="ID",
        help="One or more library IDs to search",
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        metavar="FILE",
        help="Write JSON trace to FILE (default: print to stdout)",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("ZOTERO_RAG_URL", "http://localhost:8119"),
        help="Backend base URL (default: $ZOTERO_RAG_URL or http://localhost:8119)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("ZOTERO_RAG_API_KEY", ""),
        metavar="KEY",
        help="API key (default: $ZOTERO_RAG_API_KEY)",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        metavar="N",
        help="Number of chunks to retrieve (default: preset value)",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=None,
        metavar="F",
        help="Minimum similarity score threshold (default: preset value)",
    )
    parser.add_argument(
        "--no-routing",
        action="store_true",
        help="Disable the routing LLM call (go straight to RAG)",
    )
    parser.add_argument(
        "--llm-model",
        default=None,
        metavar="MODEL",
        help="Override the preset LLM model name",
    )
    parser.add_argument(
        "--trace-only",
        action="store_true",
        help="Output only the .trace field, omitting the answer and sources",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        import httpx
    except ImportError:
        print("httpx is required: uv pip install httpx", file=sys.stderr)
        sys.exit(1)

    payload: dict = {
        "question": args.question,
        "library_ids": args.library_ids,
        "enable_routing": not args.no_routing,
        "include_trace": True,
    }
    if args.top_k is not None:
        payload["top_k"] = args.top_k
    if args.min_score is not None:
        payload["min_score"] = args.min_score
    if args.llm_model is not None:
        payload["llm_model"] = args.llm_model

    headers: dict = {"Content-Type": "application/json"}
    if args.api_key:
        headers["X-API-Key"] = args.api_key

    url = args.url.rstrip("/") + "/api/query"
    print(f"POST {url}", file=sys.stderr)
    print(f"  question: {args.question!r}", file=sys.stderr)
    print(f"  library_ids: {args.library_ids}", file=sys.stderr)

    try:
        with httpx.Client(timeout=300.0) as client:
            response = client.post(url, json=payload, headers=headers)
    except httpx.ConnectError as exc:
        print(f"[ERROR] Could not connect to {url}: {exc}", file=sys.stderr)
        sys.exit(1)

    if response.status_code != 200:
        print(f"[ERROR] HTTP {response.status_code}: {response.text}", file=sys.stderr)
        sys.exit(1)

    data = response.json()

    if args.trace_only:
        output = data.get("trace") or {}
    else:
        output = data

    json_text = json.dumps(output, indent=2, ensure_ascii=False)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(json_text)
            fh.write("\n")
        trace = data.get("trace") or {}
        agents = [e.get("agent_name") for e in trace.get("agent_executions", [])]
        llm_calls = [c.get("call_type") for c in trace.get("llm_calls", [])]
        print(
            f"[OK] Trace saved to {args.output} "
            f"(agents={agents}, llm_calls={llm_calls}, "
            f"total_ms={trace.get('total_duration_ms')})",
            file=sys.stderr,
        )
    else:
        print(json_text)


if __name__ == "__main__":
    main()
