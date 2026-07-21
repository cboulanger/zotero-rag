"""
Trace a RAG query and save the full execution trace to JSON.

Usage:
    uv run python scripts/query_trace.py "What is autopoiesis?" \\
        --library-ids 12345678 \\
        --output trace.json

The script calls POST /api/query with include_trace=true and writes
the .trace field (plus the normal response fields) to the output file.

For remote-model presets (e.g. KISSKI), the backend also requires a
client-supplied provider API key header (e.g. X-Kisski-Api-Key) — pass it
with --header 'X-Kisski-Api-Key: <value>' (repeatable). Get your own
plugin-configured keys without ever printing them to the terminal by having
Zotero write them to a local file (requires the MCP Bridge for Zotero
plugin; see CLAUDE.md's "Live query debugging" section for the snippet),
then read them from that file into --header/--api-key.

If the response comes back with status "needs_client_evidence" (a citation/
"mentions" query awaiting full-text evidence only the Zotero client can
gather), this script cannot complete the round trip itself — it prints the
extracted citation_targets and query_plan so you can inspect what the
router decided, then stops (see docs/query-routing.md's two-phase protocol
section for what the plugin does next).
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
        help="Zotero API key, required for non-loopback deployments (default: $ZOTERO_RAG_API_KEY)",
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
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="NAME: VALUE",
        help="Extra request header, e.g. --header 'X-Kisski-Api-Key: sk-...' "
             "(repeatable; required for remote-model presets)",
    )
    return parser.parse_args()


def _parse_header(raw: str) -> tuple[str, str]:
    name, _, value = raw.partition(":")
    if not _:
        raise SystemExit(f"[ERROR] --header must be 'Name: Value', got {raw!r}")
    return name.strip(), value.strip()


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
        headers["X-Zotero-API-Key"] = args.api_key
    for raw_header in args.header:
        name, value = _parse_header(raw_header)
        headers[name] = value

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

    if data.get("status") == "needs_client_evidence":
        print(
            "[INFO] status=needs_client_evidence — the router selected the "
            "'mentions' agent and is waiting on full-text evidence that only "
            "the Zotero client can gather. This script cannot supply it "
            "(that requires plugin/src/mentions.js's findMentionEvidence(), "
            "run inside Zotero). Extracted citation_targets:",
            file=sys.stderr,
        )
        print(json.dumps(data.get("citation_targets"), indent=2, ensure_ascii=False), file=sys.stderr)
        print("\nEchoed query_plan (would be resubmitted with client_evidence attached):", file=sys.stderr)
        print(json.dumps(data.get("query_plan"), indent=2, ensure_ascii=False), file=sys.stderr)

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
