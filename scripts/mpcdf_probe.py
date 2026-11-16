"""Inspect a user's MPCDF jobs directly: list them, read their logs, time requests.

Uses the session the backend stored for the user (``<data_path>/system/mpcdf/<user>/``)
and only *restores* it: no login, no logout, so the backend's session stays valid
(a login to the MPCDF web interface would end it). Endpoint keys are used in-process
and never printed. Reads the job log through mpcdf-llm's log viewer API.

Usage (from the repo root, with the backend's data directory and AUTOINDEX_SECRET):

    uv run python scripts/mpcdf_probe.py endpoints
    uv run python scripts/mpcdf_probe.py logs reranker              # tail of the job serving a model matching "reranker"
    uv run python scripts/mpcdf_probe.py logs 69 --follow           # endpoint id; Ctrl-C to stop
    uv run python scripts/mpcdf_probe.py logs e5 --download job.log # the whole file
    uv run python scripts/mpcdf_probe.py latency reranker --docs 1,1,5,20
    uv run python scripts/mpcdf_probe.py latency reranker --staggered 4 --every 3
    uv run python scripts/mpcdf_probe.py latency e5                 # embeddings; gpt-oss: a short chat completion

``latency`` times requests one after another (``--docs``: documents per re-ranking
request), or sends ``--staggered N`` one-document requests ``--every`` seconds apart and
reports when each finishes: with the pooling delay seen on vLLM 0.30.0/ROCm (see
docs/providers.md, "Known limitation"), later requests all finish together one cycle
(about 53 s) after the first.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx

from backend.config.settings import get_settings
from backend.plugins.mpcdf.sessions import SealedSessionStore, user_key

try:
    from mpcdf_llm.client import MpcdfClient
    from mpcdf_llm.models import Endpoint
except ImportError:  # pragma: no cover - the optional dependency is missing
    sys.exit("[FAIL] mpcdf-llm is not installed (uv sync installs it from its git repository)")

DOC = "Zotero is a free reference manager that stores citations and PDFs. " * 12


async def _client(user_id: int | None) -> MpcdfClient:
    """A client on the user's stored session (restore only)."""
    directory = Path(get_settings().data_path) / "system" / "mpcdf" / user_key(user_id)
    session = SealedSessionStore(directory).load()
    if session is None:
        sys.exit(f"[FAIL] no stored MPCDF session in {directory}; log in from Preferences first")
    client = MpcdfClient()
    if not await client.restore(session):
        await client.close()
        sys.exit("[FAIL] the stored MPCDF session has ended; log in again from Preferences")
    return client


def _pick(endpoints: list[Endpoint], target: str) -> Endpoint:
    """An endpoint by id (all digits) or by a substring of its model name."""
    if target.isdigit():
        found = [e for e in endpoints if e.id == int(target)]
    else:
        found = [e for e in endpoints if target.lower() in (e.model or "").lower()]
    if not found:
        sys.exit(f"[FAIL] no endpoint matches {target!r}; see the `endpoints` command")
    return max(found, key=lambda e: e.id)


async def cmd_endpoints(args: argparse.Namespace) -> None:
    client = await _client(args.user_id)
    try:
        eps = await client.list_endpoints(include_history=args.history)
    finally:
        await client.close()
    if not eps:
        print("no endpoints")
    for e in eps:
        print(f"{e.id}\t{e.status}\t{e.model}\tgpus={e.gpus}\targs: {e.args}\tenvs: {e.envs or '-'}")


async def cmd_logs(args: argparse.Namespace) -> None:
    client = await _client(args.user_id)
    try:
        ep = _pick(await client.list_endpoints(), args.target)
        if args.download:
            f = await client.download_logs(ep.id, status=ep.status)
            out = Path(args.download)
            out = out / f.filename if out.is_dir() else out
            out.write_bytes(f.content)
            print(f"[OK] endpoint {ep.id}: {f.filename}, {len(f.content)} bytes -> {out}")
        elif args.follow:
            try:
                async for line in client.stream_logs(ep.id, status=ep.status):
                    print(line, flush=True)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
        else:
            snap = await client.read_logs(ep.id, status=ep.status)
            print(f"# endpoint {ep.id} ({ep.model}), {snap.log_file_path}", file=sys.stderr)
            for line in snap.lines[-args.lines:]:
                print(line)
    finally:
        await client.close()


def _request(ep: Endpoint, docs: int, tag: str) -> tuple[str, dict]:
    """Path (relative to the base URL) and body of a small request for the endpoint's model."""
    model = ep.model or ""
    if "rerank" in model.lower():
        documents = ["d" + tag] if docs <= 1 else [f"{DOC}{i}" for i in range(docs)]
        return "/rerank", {"model": model, "query": "What is Zotero? " + tag, "documents": documents}
    if ep.framework.value == "vLLM" and ("e5" in model or "embed" in model.lower()):
        return "/v1/embeddings", {"model": model, "input": [f"query: what is zotero {tag}"] * max(docs, 1)}
    return "/v1/chat/completions", {"model": model, "max_tokens": 20,
                                    "messages": [{"role": "user", "content": f"Say ok. {tag}"}]}


async def cmd_latency(args: argparse.Namespace) -> None:
    client = await _client(args.user_id)
    try:
        ep = _pick(await client.list_endpoints(), args.target)
    finally:
        await client.close()
    creds = ep.credentials(ep.model)
    base = creds.base_url.rstrip("/")
    root = base[:-3] if base.endswith("/v1") else base
    headers = {"Authorization": f"Bearer {creds.api_key.get_secret_value()}"}
    print(f"# endpoint {ep.id} ({ep.model}), args: {ep.args}, envs: {ep.envs or '-'}")
    async with httpx.AsyncClient(timeout=args.timeout) as http:
        async def send(docs: int, tag: str) -> str:
            path, body = _request(ep, docs, tag)
            try:
                r = await http.post(root + path, json=body, headers=headers)
                return str(r.status_code)
            except httpx.HTTPError as exc:
                return type(exc).__name__

        if args.staggered:
            t0 = time.monotonic()

            async def one(i: int) -> None:
                await asyncio.sleep(i * args.every)
                sent = time.monotonic() - t0
                status = await send(1, str(i))
                print(f"request {i}: sent at {sent:6.1f}s, done at {time.monotonic() - t0:6.1f}s ({status})", flush=True)

            await asyncio.gather(*(one(i) for i in range(args.staggered)))
        else:
            for i, docs in enumerate(int(x) for x in args.docs.split(",")):
                t = time.monotonic()
                status = await send(docs, str(i))
                print(f"{docs:4d} doc(s): {status} in {time.monotonic() - t:6.1f}s", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user-id", type=int, default=None, help="Zotero user id (default: the loopback user)")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("endpoints", help="list the user's jobs with their arguments and environment")
    p.add_argument("--history", action="store_true", help="include ended jobs")
    p = sub.add_parser("logs", help="a job's log: tail, --follow or --download")
    p.add_argument("target", help="endpoint id, or a substring of the model name")
    p.add_argument("--lines", type=int, default=60, help="lines of the tail to print (default 60)")
    p.add_argument("--follow", action="store_true", help="keep printing new lines until Ctrl-C")
    p.add_argument("--download", metavar="FILE|DIR", help="save the whole log file")
    p = sub.add_parser("latency", help="time requests to a job")
    p.add_argument("target", help="endpoint id, or a substring of the model name")
    p.add_argument("--docs", default="1,1,5,20", help="documents per sequential request (default 1,1,5,20)")
    p.add_argument("--staggered", type=int, default=0, metavar="N", help="send N one-document requests concurrently")
    p.add_argument("--every", type=float, default=3.0, help="seconds between staggered requests (default 3)")
    p.add_argument("--timeout", type=float, default=300.0, help="per-request timeout in seconds (default 300)")
    args = ap.parse_args()
    asyncio.run({"endpoints": cmd_endpoints, "logs": cmd_logs, "latency": cmd_latency}[args.command](args))


if __name__ == "__main__":
    main()
