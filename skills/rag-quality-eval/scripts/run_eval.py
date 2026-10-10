"""Run the gold questions against the live RAG pipeline for chosen presets/models.

For every selected preset it activates the preset (POST /api/config; falls back
to writing the admin override file when the backend runs from this checkout),
then asks every gold question once per LLM model (each model separately),
saves each raw ``/api/query`` response with ``include_trace=true``, scores it
(scoring.py) and finally restores the original preset and writes the report
(report.py).

WARNING: switching the preset changes the *server-wide* active preset for the
duration of the run (also for the cron indexer and any other client). Only run
against a dev instance. The original preset is restored in a ``finally`` block.

Examples:
    # what would run, no queries sent
    uv run python skills/rag-quality-eval/scripts/run_eval.py --presets all --dry-run
    # the active preset, all its models
    uv run python skills/rag-quality-eval/scripts/run_eval.py --presets active
    # two presets, only the first model of each, 3 repetitions, easy questions only
    uv run python skills/rag-quality-eval/scripts/run_eval.py --presets remote-kisski,runpod \\
        --models first --repeat 3 --max-difficulty 3
    # continue an interrupted run (already-saved responses are reused)
    uv run python skills/rag-quality-eval/scripts/run_eval.py --presets all --resume data/logs/rag_eval/<run>

Credentials: the Zotero identity key comes from $RAG_EVAL_ZOTERO_KEY or the
encrypted key store; provider keys (e.g. KISSKI) from the preset's env vars or
the key store. Values are never printed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report  # noqa: E402
import scoring  # noqa: E402
from common import (  # noqa: E402
    DEFAULT_URL, PROJECT_ROOT, auth_headers, get_zotero_key, http_json, load_gold,
    local_backend_available, local_settings, provider_headers, resolve_library_id,
)
from list_targets import discover  # noqa: E402

WARMUP_QUESTION = "What is a reference manager used for?"
TRANSIENT = {0, 429, 502, 503, 504}


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_ROOT, capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


class PresetSwitcher:
    """Activate presets and restore the original one, via API or the override file."""

    def __init__(self, base_url: str, zotero_key: Optional[str], use_local: bool):
        self.base_url = base_url
        self.headers = auth_headers(zotero_key)
        self.settings = local_settings() if use_local and local_backend_available() else None
        self.is_loopback = bool(re.match(r"https?://(localhost|127\.0\.0\.1)(:|/|$)", base_url))
        self.original_override: Optional[str] = None
        if self.settings is not None and self.is_loopback:
            from backend.services.admin_settings_store import get_active_preset_override
            self.original_override = get_active_preset_override(self.settings.data_path)
        self.original_active: Optional[str] = None

    def remember(self, active: str) -> None:
        self.original_active = active

    def switch(self, name: str) -> tuple[bool, str]:
        status, data = http_json("POST", f"{self.base_url}/api/config", self.headers, {"preset_name": name}, timeout=120)
        if status == 200:
            return True, "api"
        if status in (401, 403) and self.settings is not None and self.is_loopback:
            from backend.services.admin_settings_store import set_active_preset_override
            set_active_preset_override(self.settings.data_path, name)
            return True, "override-file"
        return False, f"HTTP {status}: {str(data)[:200]}"

    def restore(self) -> str:
        if self.original_active is None:
            return "nothing to restore"
        if self.settings is not None and self.is_loopback:
            from backend.services.admin_settings_store import set_active_preset_override
            set_active_preset_override(self.settings.data_path, self.original_override)
            return f"override restored to {self.original_override!r}"
        ok, how = self.switch(self.original_active)
        return f"restored {self.original_active} via {how}" if ok else f"RESTORE FAILED ({how}) - switch back manually"


def _query(base_url: str, headers: dict, payload: dict, timeout: float, retries: int, retry_wait: float) -> tuple[int, Any, int]:
    """POST /api/query with retries on transient errors; returns (status, body, wall_ms)."""
    start = time.monotonic()
    status, body = 0, None
    for attempt in range(retries + 1):
        status, body = http_json("POST", f"{base_url}/api/query", headers, payload, timeout=timeout)
        if status not in TRANSIENT or attempt == retries:
            break
        time.sleep(retry_wait * (attempt + 1))
    return status, body, int((time.monotonic() - start) * 1000)


def build_plan(info: dict, args: argparse.Namespace) -> list[dict]:
    """Ordered list of ``{preset, status, models}``; the active preset first (no switch needed)."""
    rows = {r["name"]: r for r in info["presets"]}
    if args.presets == "active":
        wanted = [info["active"]]
    elif args.presets == "all":
        wanted = [r["name"] for r in info["presets"] if r["status"] in ("active", "switchable")]
    else:
        wanted = [p.strip() for p in args.presets.split(",") if p.strip()]
    plan = []
    for name in sorted(wanted, key=lambda n: n != info["active"]):
        row = rows.get(name)
        if row is None:
            plan.append({"preset": name, "skip": "unknown preset"})
        elif row["status"] in ("needs_restart", "credentials_missing"):
            why = row.get("note") or f"missing credentials: {', '.join(row.get('missing_credentials', []))}"
            plan.append({"preset": name, "skip": f"{row['status']} ({why})"})
        else:
            plan.append({"preset": name, "models": row.get("llm_models") or [], "active": row["status"] == "active"})
    return plan


def select_models(available: list[str], spec: str) -> list[Optional[str]]:
    """``all`` (each model separately), ``first`` (preset default) or a comma list."""
    if not available:
        return [None]  # let the backend use the preset default
    if spec == "all":
        return list(available)
    if spec == "first":
        return [available[0]]
    return [m.strip() for m in spec.split(",") if m.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--presets", required=True, metavar="active|all|NAME[,NAME]",
                        help="which presets to evaluate (all = active + switchable)")
    parser.add_argument("--models", default="all", metavar="all|first|NAME[,NAME]",
                        help="LLM models per preset; 'all' runs each model separately (default)")
    parser.add_argument("--questions", default=None, metavar="Q01,Q05", help="subset of question ids")
    parser.add_argument("--max-difficulty", type=int, default=None, help="only questions up to this difficulty")
    parser.add_argument("--repeat", type=int, default=1, help="repetitions per question (stability; router/LLM are non-deterministic)")
    parser.add_argument("--no-routing", action="store_true", help="bypass the router (pure RAG agent)")
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--library-id", default=None, help="override the gold library id")
    parser.add_argument("--header", action="append", default=[], metavar="'Name: value'", help="extra request header")
    parser.add_argument("--output-dir", default=None, help="default data/logs/rag_eval/<utc timestamp>/")
    parser.add_argument("--resume", default=None, metavar="DIR", help="reuse saved responses in an earlier run dir")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--retries", type=int, default=2, help="retries on 429/502/503/504/connection errors")
    parser.add_argument("--retry-wait", type=float, default=10.0)
    parser.add_argument("--delay", type=float, default=0.5, help="pause between queries (provider rate limits)")
    parser.add_argument("--no-warmup", action="store_true", help="skip the per-model warm-up query")
    parser.add_argument("--no-restore", action="store_true", help="leave the last tested preset active")
    parser.add_argument("--no-local", action="store_true", help="do not import backend code (remote instance)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    args = parser.parse_args()

    gold = load_gold()
    questions = [q for q in gold["questions"]
                 if (not args.questions or q["id"] in args.questions.split(","))
                 and (args.max_difficulty is None or q["difficulty"] <= args.max_difficulty)]
    if not questions:
        raise SystemExit("[FAIL] no questions selected")

    zotero_key = get_zotero_key()
    info = discover(args.url, zotero_key, use_local=not args.no_local)
    plan = build_plan(info, args)
    use_local = not args.no_local

    print(f"[INFO] backend {args.url}, active preset {info['active']}, discovery={info['mode']}")
    total = 0
    for step in plan:
        if "skip" in step:
            print(f"[SKIP] {step['preset']}: {step['skip']}")
            continue
        models = select_models(step["models"], args.models)
        total += len(models) * len(questions) * args.repeat
        print(f"[PLAN] {step['preset']}: {len(models)} model(s) x {len(questions)} questions x {args.repeat} "
              f"= {len(models) * len(questions) * args.repeat} queries")
        for m in models:
            print(f"        - {m or '(preset default)'}")
    print(f"[PLAN] total {total} queries (+ warm-up per model)")
    if args.dry_run or total == 0:
        return 0

    out_dir = Path(args.resume) if args.resume else Path(
        args.output_dir or PROJECT_ROOT / "data" / "logs" / "rag_eval" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    (out_dir / "raw").mkdir(parents=True, exist_ok=True)
    library_id = args.library_id or resolve_library_id(args.url, zotero_key, gold)
    extra = dict(h.split(":", 1) for h in args.header if ":" in h)
    extra = {k.strip(): v.strip() for k, v in extra.items()}
    base_headers = {**auth_headers(zotero_key), **extra}

    switcher = PresetSwitcher(args.url, zotero_key, use_local)
    switcher.remember(info["active"])
    meta: dict[str, Any] = {
        "started": datetime.now(timezone.utc).isoformat(), "url": args.url, "git_commit": _git_commit(),
        "gold_version": gold["version"], "library_id": library_id, "original_preset": info["active"],
        "args": {k: v for k, v in vars(args).items() if k != "header"}, "skipped": [], "health": {},
    }
    n_done = 0
    try:
        for step in plan:
            if "skip" in step:
                meta["skipped"].append({"preset": step["preset"], "reason": step["skip"]})
                continue
            name = step["preset"]
            if not step["active"] or name != info["active"]:
                ok, how = switcher.switch(name)
                if not ok:
                    print(f"[SKIP] {name}: cannot switch ({how})")
                    meta["skipped"].append({"preset": name, "reason": f"switch failed: {how}"})
                    continue
                print(f"[INFO] switched to {name} ({how})")
            st, config = http_json("GET", f"{args.url}/api/config", base_headers, timeout=60)
            live_models = config.get("llm_models", []) if st == 200 and isinstance(config, dict) else step["models"]
            models = select_models(live_models, args.models)
            hst, health = http_json("GET", f"{args.url}/api/config/health", base_headers, timeout=60)
            if hst == 200:
                meta["health"][name] = health
                if any(isinstance(s, dict) and s.get("status") not in ("ready",) for s in health.values() if s):
                    print(f"[WARN] {name}: endpoint health {health} - first latencies may include a cold start")
            preset_obj = None
            if use_local and local_backend_available():
                try:
                    from backend.config.presets import get_preset
                    preset_obj = get_preset(name, local_settings().data_path)
                except Exception:
                    pass
            headers = provider_headers(preset_obj, base_headers)

            for model in models:
                label = model or "default"
                if not args.no_warmup:
                    payload = {"question": WARMUP_QUESTION, "library_ids": [library_id], "enable_routing": False,
                               **({"llm_model": model} if model else {})}
                    s, _b, ms = _query(args.url, headers, payload, args.timeout, args.retries, args.retry_wait)
                    print(f"[WARM] {name}/{label}: HTTP {s} in {ms} ms")
                for q in questions:
                    for rep in range(1, args.repeat + 1):
                        raw_path = out_dir / "raw" / f"{_slug(name)}__{_slug(label)}__{q['id']}__r{rep}.json"
                        if args.resume and raw_path.exists():
                            continue
                        payload: dict[str, Any] = {
                            "question": q["question"], "library_ids": [library_id],
                            "enable_routing": not args.no_routing, "include_trace": True,
                        }
                        if model:
                            payload["llm_model"] = model
                        if args.top_k is not None:
                            payload["top_k"] = args.top_k
                        if args.min_score is not None:
                            payload["min_score"] = args.min_score
                        status, body, wall_ms = _query(args.url, headers, payload, args.timeout, args.retries,
                                                       args.retry_wait)
                        record = {
                            "meta": {"preset": name, "model": label, "question_id": q["id"], "rep": rep,
                                     "wall_ms": wall_ms, "http_status": status,
                                     "timestamp": datetime.now(timezone.utc).isoformat()},
                            "response": body if status == 200 and isinstance(body, dict) else None,
                            "error": None if status == 200 else str(body)[:500],
                        }
                        raw_path.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
                        n_done += 1
                        if record["response"]:
                            s = scoring.score_run(record["response"], q, gold)
                            print(f"[{s['verdict'].upper():4}] {q['id']} {name}/{label} r{rep}: composite={s['composite']:.2f} "
                                  f"recall={s['fact_recall']:.2f} cited={s['distinct_cited']} words={s['words']} "
                                  f"lang={s['detected_language']} {wall_ms / 1000:.1f}s")
                        else:
                            print(f"[ERR ] {q['id']} {name}/{label} r{rep}: HTTP {status} {record['error'][:100]}")
                        time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\n[WARN] interrupted - restoring preset and reporting what was collected")
    finally:
        if not args.no_restore:
            print(f"[INFO] {switcher.restore()}")
        meta["finished"] = datetime.now(timezone.utc).isoformat()
        meta["queries_run"] = n_done
        (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    report.build_report(out_dir)
    print(f"[OK] report: {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
