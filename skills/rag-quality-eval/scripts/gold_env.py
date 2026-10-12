"""Create (or publish) the environment the RAG evaluation runs in.

Two situations, one tool:

* **Cloud / restricted session** (no Kreuzberg sidecar, maybe no containers):
  ``up`` starts a backend on embedded Qdrant under ``.local/rag_eval_env/`` and
  restores the production-equivalent index from the Hugging Face store, so the
  whole workflow (run_eval, judge, report) and retrieval experiments work.
* **Full local environment** (Kreuzberg + Qdrant sidecar, a Zotero read key):
  ``fetch-sources`` -> ``publish`` builds the index from the library's PDFs with
  the real extractor and uploads PDFs, snapshot and card to the store.

Commands:

  up [--force-restore] [--snapshot F]   start the backend if needed, restore the index if the gold library is empty, verify
  down                   stop the backend ``up`` started
  status                 backend, extractor, index state
  fetch-sources          PDFs + manifest.json from Zotero into .local/rag_eval_sources/ (needs $RAG_EVAL_ZOTERO_KEY)
  index                  upload the source PDFs to the running backend for indexing
  publish                (local) ensure sources, index, export snapshot, push sources + snapshot + card

Re-indexing in a cloud session uses the ``legacy`` extractor (pypdf + spaCy)
because the Kreuzberg image cannot be pulled there; its chunks differ from
production's, so use it for plumbing only. The restored snapshot is what
retrieval/ranking measurements must run on. Changes to chunking or extraction
need the full local environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    DEFAULT_URL, PROJECT_ROOT, PROXY_KEYS_FLAG, ZOTERO_API, auth_headers, get_zotero_key, http_json,
    load_gold, local_settings, provider_headers,
)
import hf_store  # noqa: E402

ENV_DIR = PROJECT_ROOT / ".local" / "rag_eval_env"
DATA_DIR = ENV_DIR / "data"
PID_FILE = ENV_DIR / "backend.pid"
LOG_FILE = ENV_DIR / "backend.log"
SOURCES_DIR = hf_store.SOURCES_DIR
SNAPSHOT_TOOL = PROJECT_ROOT / "bin" / "library_snapshot.py"


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def library_id(gold: Optional[dict] = None) -> str:
    return (gold or load_gold())["library"]["library_id"]


# ---------------------------------------------------------------- backend
def backend_up(url: str) -> bool:
    return http_json("GET", f"{url}/api/config", timeout=5)[0] == 200


def chunk_count(url: str, lib: str) -> int:
    status, data = http_json("GET", f"{url}/api/migration/export/count?library_id={lib}&collection=chunks", timeout=30)
    return data.get("count", 0) if status == 200 and isinstance(data, dict) else 0


def kreuzberg_reachable() -> bool:
    settings = local_settings()
    url = getattr(settings, "kreuzberg_url", "http://localhost:8100")
    try:
        urllib.request.urlopen(url.rstrip("/") + "/health", timeout=3)
        return True
    except urllib.error.HTTPError:
        return True  # answered, just not on /health
    except Exception:
        return False


def ensure_spacy_model() -> None:
    probe = subprocess.run([sys.executable, "-c", "import en_core_web_sm"], capture_output=True)
    if probe.returncode == 0:
        return
    log("[..] installing the spaCy model en_core_web_sm")
    wheel = "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
    subprocess.run(["uv", "pip", "install", "--python", sys.executable, wheel], check=True, cwd=PROJECT_ROOT)


def start_backend(url: str, port: int, preset: str, extractor: str) -> None:
    ENV_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "DATA_PATH": str(DATA_DIR), "MODEL_PRESET": preset, "EXTRACTOR_BACKEND": extractor,
           "API_HOST": "127.0.0.1"}
    env.pop("QDRANT_URL", None)  # embedded store: this environment has no sidecar
    env.pop("VIRTUAL_ENV", None)
    out = LOG_FILE.open("ab")
    proc = subprocess.Popen(
        ["uv", "run", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_ROOT, env=env, stdout=out, stderr=out, start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid))
    for _ in range(90):
        if backend_up(url):
            return
        if proc.poll() is not None:
            break
        time.sleep(2)
    raise SystemExit(f"[FAIL] backend did not come up; see {LOG_FILE}")


def stop_backend() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        os.killpg(int(PID_FILE.read_text()), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, ValueError):
        pass
    PID_FILE.unlink(missing_ok=True)
    return True


def _run_tool(args: list[str]) -> None:
    subprocess.run(["uv", "run", "python", *args], cwd=PROJECT_ROOT, check=True)


def restore_index(url: str, model: str, repo: str, snapshot: Optional[str] = None) -> None:
    if snapshot:
        log(f"[..] importing {snapshot} into the backend")
        _run_tool([str(SNAPSHOT_TOOL), "import", snapshot, url])
        return
    log(f"[..] pulling the {model} index snapshot from {repo}")
    out = subprocess.run(
        ["uv", "run", "python", str(Path(__file__).with_name("hf_store.py")), "--repo", repo, "--url", url,
         "pull-index", "--model", model],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise SystemExit(f"[FAIL] no snapshot for {model} in {repo}:\n{out.stderr[-600:]}")
    snap = out.stdout.strip().splitlines()[-1]
    log("[..] importing the snapshot into the backend")
    _run_tool([str(SNAPSHOT_TOOL), "import", snap, url])


def cmd_up(a: argparse.Namespace) -> int:
    url = f"http://127.0.0.1:{a.port}" if a.url is None else a.url
    if os.environ.get("HTTPS_PROXY") and not os.environ.get("KISSKI_API_KEY"):
        ENV_DIR.mkdir(parents=True, exist_ok=True)
        PROXY_KEYS_FLAG.write_text("provider credentials are injected by the outbound proxy\n")
        log("[INFO] outbound proxy detected: provider keys are sent as a placeholder the proxy replaces")
    started = False
    if not backend_up(url):
        extractor = a.extractor or ("kreuzberg" if kreuzberg_reachable() else "legacy")
        if extractor == "legacy":
            ensure_spacy_model()
        log(f"[..] starting backend (preset {a.preset}, extractor {extractor}, embedded Qdrant)")
        start_backend(url, a.port, a.preset, extractor)
        started = True
    status, info = http_json("GET", f"{url}/api/migration/embedding-info", timeout=20)
    if status != 200:
        raise SystemExit(f"[FAIL] {url}/api/migration/embedding-info -> HTTP {status}")
    lib = library_id()
    have = chunk_count(url, lib)
    if a.force_restore or have == 0:
        restore_index(url, info["embedding_model_name"], a.repo, a.snapshot)
        have = chunk_count(url, lib)
    log(f"[OK] backend {url} ({'started' if started else 'already running'}), gold library: {have} chunks")
    return subprocess.run(
        ["uv", "run", "python", str(Path(__file__).with_name("check_gold_index.py")), "--url", url], cwd=PROJECT_ROOT
    ).returncode


def cmd_down(a: argparse.Namespace) -> int:
    print("[OK] backend stopped" if stop_backend() else "[INFO] no backend started by gold_env.py")
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    url = a.url or f"http://127.0.0.1:{a.port}"
    if not backend_up(url):
        print(f"backend {url}: down")
        return 1
    status, cfg = http_json("GET", f"{url}/api/config", timeout=10)
    print(f"backend {url}: up, preset {cfg.get('preset_name')}, gold library chunks: {chunk_count(url, library_id())}")
    print(f"kreuzberg sidecar reachable: {kreuzberg_reachable()}; sources: {len(list(SOURCES_DIR.glob('*.pdf')))} PDFs in {SOURCES_DIR}")
    return 0


# ---------------------------------------------------------------- sources / index / publish
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cmd_fetch_sources(a: argparse.Namespace) -> int:
    """Download every PDF attachment of the gold library plus its item metadata."""
    key = get_zotero_key()
    if not key:
        raise SystemExit("[FAIL] attachment files need a read-only Zotero key: set $RAG_EVAL_ZOTERO_KEY")
    gold = load_gold()
    group = library_id(gold)
    headers = {"Zotero-API-Key": key}
    items: list[dict] = []
    start = 0
    while True:
        status, page = http_json("GET", f"{ZOTERO_API}/groups/{group}/items?format=json&limit=100&start={start}",
                                 headers, timeout=60)
        if status != 200 or not isinstance(page, list):
            raise SystemExit(f"[FAIL] Zotero items -> HTTP {status}")
        items += [i["data"] for i in page]
        if len(page) < 100:
            break
        start += 100
    parents = {i["key"]: i for i in items if i.get("itemType") not in ("attachment", "note")}
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    attachments = []
    for att in items:
        if att.get("itemType") != "attachment" or att.get("contentType") != "application/pdf" or att.get("parentItem") not in parents:
            continue
        parent = parents[att["parentItem"]]
        target = SOURCES_DIR / f"{att['key']}.pdf"
        if not target.exists():
            req = urllib.request.Request(f"{ZOTERO_API}/groups/{group}/items/{att['key']}/file", headers=headers)
            try:
                target.write_bytes(urllib.request.urlopen(req, timeout=180).read())
            except urllib.error.HTTPError as exc:
                log(f"[WARN] {att['key']} ({parent.get('title', '')[:50]}): HTTP {exc.code}, skipped")
                continue
        authors = [f"{c.get('lastName', '')}, {c.get('firstName', '')}".strip(", ") if "lastName" in c else c.get("name", "")
                   for c in parent.get("creators", []) if c.get("creatorType") == "author"]
        year = "".join(ch for ch in parent.get("date", "") if ch.isdigit())[-4:] if parent.get("date") else ""
        attachments.append({
            "attachment_key": att["key"], "item_key": parent["key"], "title": parent.get("title", ""),
            "authors": authors, "year": int(year) if year.isdigit() else None, "item_type": parent.get("itemType"),
            "item_version": parent.get("version"), "attachment_version": att.get("version"),
            "doi": parent.get("DOI") or None, "sha256": _sha256(target),
        })
        log(f"[OK] {att['key']} {parent.get('title', '')[:60]}")
    manifest = {"library": gold["library"], "fetched_at": datetime.now(timezone.utc).isoformat(),
                "attachments": sorted(attachments, key=lambda x: x["attachment_key"])}
    (SOURCES_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[OK] {len(attachments)} PDFs in {SOURCES_DIR}")
    return 0


def cmd_index(a: argparse.Namespace) -> int:
    """Upload each source PDF to the backend's /api/index/document (works with any extractor)."""
    import httpx
    manifest_path = SOURCES_DIR / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"[FAIL] {manifest_path} missing: `fetch-sources`, or `hf_store.py pull-sources` and copy it here")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    settings = local_settings()
    headers = {**auth_headers(get_zotero_key())}
    if settings is not None:
        headers = provider_headers(settings.get_hardware_preset(), headers)
    lib = manifest["library"]["library_id"]
    done = failed = 0
    with httpx.Client(timeout=a.timeout) as client:
        for att in manifest["attachments"]:
            meta = {
                "library_id": lib, "library_type": "group", "item_key": att["item_key"],
                "attachment_key": att["attachment_key"], "mime_type": "application/pdf",
                "item_version": att.get("item_version") or 0, "attachment_version": att.get("attachment_version") or 0,
                "title": att["title"], "authors": att["authors"], "year": att["year"], "item_type": att["item_type"],
            }
            pdf = SOURCES_DIR / f"{att['attachment_key']}.pdf"
            resp = client.post(f"{a.url}/api/index/document", headers=headers,
                               files={"file": (pdf.name, pdf.read_bytes(), "application/pdf")},
                               data={"metadata": json.dumps(meta)})
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            # backend/models/document.py's ProcessingStatus: any outcome other than the
            # skipped_* failure variants means the gold library already has (or now has)
            # this document's chunks, which is what `publish` needs before exporting.
            ok = resp.status_code == 200 and body.get("status") in (
                "indexed_fresh", "skipped_duplicate", "copied_cross_library", "copied_same_library",
            )
            done, failed = done + ok, failed + (not ok)
            log(f"[{'OK' if ok else 'FAIL'}] {att['attachment_key']} {body.get('message', resp.status_code)}")
    print(f"[OK] indexed {done}, failed {failed}")
    return 1 if failed else 0


def cmd_publish(a: argparse.Namespace) -> int:
    if not backend_up(a.url):
        raise SystemExit(f"[FAIL] no backend at {a.url}: start the full local one (npm start) with the Kreuzberg sidecar")
    if not (SOURCES_DIR / "manifest.json").exists():
        raise SystemExit("[FAIL] no sources: run `gold_env.py fetch-sources` first")
    extractor = os.environ.get("EXTRACTOR_BACKEND", "kreuzberg")
    snap = PROJECT_ROOT / ".local" / "rag_eval_hf" / "gold-snapshot.jsonl.gz"
    snap.parent.mkdir(parents=True, exist_ok=True)
    store = str(Path(__file__).with_name("hf_store.py"))
    if cmd_index(argparse.Namespace(url=a.url, timeout=a.timeout)) != 0:
        raise SystemExit("[FAIL] indexing had failures; not publishing a partial index")
    _run_tool([str(SNAPSHOT_TOOL), "export", f"groups/{library_id()}", a.url, str(snap)])
    _run_tool([store, "--repo", a.repo, "push-sources"])
    _run_tool([store, "--repo", a.repo, "--url", a.url, "push-index", str(snap), "--extractor", extractor])
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", default=os.environ.get("RAG_EVAL_HF_REPO", hf_store.DEFAULT_REPO))
    p.add_argument("--url", default=None, help=f"backend URL (default http://127.0.0.1:<port>; run_eval uses {DEFAULT_URL})")
    p.add_argument("--port", type=int, default=8119)
    sub = p.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up")
    up.add_argument("--preset", default=os.environ.get("MODEL_PRESET", "remote-kisski"))
    up.add_argument("--extractor", choices=["kreuzberg", "legacy"], default=None)
    up.add_argument("--force-restore", action="store_true")
    up.add_argument("--snapshot", default=None, help="restore from this local snapshot file instead of the store")
    for name in ("down", "status", "fetch-sources"):
        sub.add_parser(name)
    for name in ("index", "publish"):
        s = sub.add_parser(name)
        s.add_argument("--timeout", type=float, default=900.0, help="per-document upload timeout (s)")
    a = p.parse_args(argv)
    if a.url is None:
        a.url = f"http://127.0.0.1:{a.port}"
    return {"up": cmd_up, "down": cmd_down, "status": cmd_status, "fetch-sources": cmd_fetch_sources,
            "index": cmd_index, "publish": cmd_publish}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
