"""Run the gold questions against the live RAG pipeline for chosen presets/models.

For every selected preset it selects the preset, makes sure its endpoints are
ready, then asks every gold question once per LLM model (each model separately),
saves each raw ``/api/query`` response with ``include_trace=true``, scores it
(scoring.py) and finally restores the original preset and writes the report
(report.py).

How a preset is selected (docs/presets.md): with a Zotero identity key the
harness uses the caller's own preset choice (``PUT /api/config/my-preset``), which
affects nobody else. Without an identity (a loopback dev server) the only
mechanism is the *server default* (``POST /api/config``, or the admin settings
file when the backend runs from this checkout), which changes the preset for every
client and the cron indexer for the duration of the run: only do that on a dev
instance. Either way the original state is restored in a ``finally`` block.

Endpoint readiness (``GET /api/config/health``): a ``cold`` endpoint is woken
(``POST /api/config/warmup``) and polled until ready; a ``paused`` or
``unreachable`` one (stopped on purpose, or nothing provisioned) is skipped with
the remedy instead of producing 10 error rows.

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
    """Select a preset for the evaluation and restore the original state afterwards.

    Preferred: the caller's own choice (``PUT /api/config/my-preset``), invisible to
    other users. Fallback when there is no usable identity: the server default
    (``POST /api/config``; on a loopback checkout, the admin settings file when the
    API refuses), which affects every client until restored.
    """

    def __init__(self, base_url: str, zotero_key: Optional[str], use_local: bool):
        self.base_url = base_url
        self.auth = auth_headers(zotero_key)
        self.has_identity_key = bool(zotero_key)
        self.settings = local_settings() if use_local and local_backend_available() else None
        self.is_loopback = bool(re.match(r"https?://(localhost|127\.0\.0\.1)(:|/|$)", base_url))
        self.original_default: Optional[str] = None
        self.original_effective: Optional[str] = None
        self.original_admin_default: Optional[str] = None
        self.used_user = False
        self.used_default = False
        self.headers_for = lambda name: dict(self.auth)

    def remember(self, default: str, effective: str) -> None:
        self.original_default, self.original_effective = default, effective
        if self.settings is not None and self.is_loopback:
            from backend.services.admin_settings_store import get_default_preset
            self.original_admin_default = get_default_preset(self.settings.data_path)

    def _effective(self, headers: dict) -> Optional[str]:
        status, config = http_json("GET", f"{self.base_url}/api/config", headers, timeout=60)
        return config.get("preset_name") if status == 200 and isinstance(config, dict) else None

    def switch(self, name: str) -> tuple[bool, str]:
        """Make ``name`` the preset this harness runs on; returns ``(ok, how_or_reason)``."""
        headers = self.headers_for(name)
        if self.has_identity_key:
            status, data = http_json("PUT", f"{self.base_url}/api/config/my-preset", headers,
                                     {"preset_name": name}, timeout=120)
            if status == 200:
                self.used_user = True
                if self._effective(headers) == name:
                    return True, "own preset choice"
                return False, "saved as own choice but not honoured (incompatible with the default or invalid)"
            detail = str(data)[:240]
            if not (status == 400 and ("signed-in" in detail or "identity" in detail)):
                return False, f"HTTP {status}: {detail}"
            # no usable identity on this server (loopback): fall through to the server default
        status, data = http_json("POST", f"{self.base_url}/api/config", headers, {"preset_name": name}, timeout=120)
        how = "server default"
        if status in (401, 403) and self.settings is not None and self.is_loopback:
            from backend.services.admin_settings_store import set_default_preset
            set_default_preset(self.settings.data_path, name)
            status, how = 200, "server default (settings file)"
        if status != 200:
            return False, f"HTTP {status}: {str(data)[:240]}"
        self.used_default = True
        if self._effective(headers) != name:
            return False, "server default switched, but this caller runs on its own saved preset"
        return True, how

    def restore(self) -> str:
        """Put the preset state back exactly as it was found."""
        notes = []
        if self.used_user:
            target = None if self.original_effective in (None, self.original_default) else self.original_effective
            status, data = http_json("PUT", f"{self.base_url}/api/config/my-preset",
                                     self.headers_for(target) if target else self.auth, {"preset_name": target},
                                     timeout=120)
            notes.append(f"own choice restored to {target!r}" if status == 200
                         else f"RESTORE OF OWN CHOICE FAILED (HTTP {status}); run PUT /api/config/my-preset manually")
        if self.used_default:
            if self.settings is not None and self.is_loopback:
                from backend.services.admin_settings_store import set_default_preset
                set_default_preset(self.settings.data_path, self.original_admin_default)
                notes.append(f"server default restored to {self.original_admin_default!r} (settings file)")
            else:
                status, data = http_json("POST", f"{self.base_url}/api/config", self.auth,
                                         {"preset_name": self.original_default}, timeout=120)
                notes.append(f"server default restored to {self.original_default!r}" if status == 200
                             else f"RESTORE OF SERVER DEFAULT FAILED (HTTP {status}); switch back manually")
        return "; ".join(notes) or "nothing to restore"


def ensure_ready(base_url: str, headers: dict, timeout: float, poll: float) -> tuple[bool, str]:
    """Check endpoint readiness of the effective preset; wake cold sides.

    Returns ``(ok, reason)``: not ok for ``paused`` or ``unreachable`` sides (with the
    remedy). A side that stays cold past ``timeout`` is reported but not fatal.
    """
    deadline = time.monotonic() + timeout
    warmed = False
    while True:
        status, health = http_json("GET", f"{base_url}/api/config/health", headers, timeout=60)
        if status != 200 or not isinstance(health, dict):
            return True, "health endpoint unavailable (not checked)"
        sides = {side: h for side, h in health.items() if isinstance(h, dict)}
        bad = {side: h for side, h in sides.items() if h.get("status") in ("paused", "unreachable")}
        if bad:
            what = "; ".join(f"{side}: {h['status']} ({h.get('detail', '')})" for side, h in bad.items())
            remedy = ("resume/provision it first (plugin Preferences, or bin/provision.py --preset <name>)"
                      if any(h["status"] == "paused" for h in bad.values())
                      else "check the endpoint URL/key or provision it (bin/provision.py --preset <name>)")
            return False, f"{what} - {remedy}"
        cold = [side for side, h in sides.items() if h.get("status") == "cold"]
        throttled = [side for side, h in sides.items() if h.get("status") == "throttled"]
        if throttled:
            print(f"[WARN] provider reports no capacity right now for: {', '.join(throttled)} (expect 429/503 retries)")
        if not cold:
            return True, ""
        if not warmed:
            http_json("POST", f"{base_url}/api/config/warmup", headers, {}, timeout=60)
            warmed = True
            print(f"[INFO] waking cold endpoint(s): {', '.join(cold)}")
        if time.monotonic() > deadline:
            return True, f"still cold after {timeout:.0f}s ({', '.join(cold)}); first latencies include the cold start"
        time.sleep(poll)


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
    parser.add_argument("--warm-timeout", type=float, default=300.0, help="seconds to wait for a cold endpoint to wake")
    parser.add_argument("--warm-poll", type=float, default=10.0, help="seconds between readiness polls")
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
    extra = {k.strip(): v.strip() for k, v in (h.split(":", 1) for h in args.header if ":" in h)}
    base_headers = {**auth_headers(zotero_key), **extra}
    use_local = not args.no_local
    info = discover(args.url, zotero_key, use_local=use_local, extra_headers=extra)
    plan = build_plan(info, args)

    print(f"[INFO] backend {args.url}, effective preset {info['active']}, server default {info['default']}, "
          f"discovery={info['mode']}")
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

    def headers_for(name: Optional[str]) -> dict:
        """Auth + the provider-key headers the named preset needs (values never printed)."""
        preset_obj = None
        if name and use_local and local_backend_available():
            try:
                from backend.config.presets import get_preset
                preset_obj = get_preset(name, local_settings().data_path)
            except Exception:
                pass
        return provider_headers(preset_obj, base_headers)

    switcher = PresetSwitcher(args.url, zotero_key, use_local)
    switcher.headers_for = headers_for
    switcher.remember(info["default"], info["active"])
    meta: dict[str, Any] = {
        "started": datetime.now(timezone.utc).isoformat(), "url": args.url, "git_commit": _git_commit(),
        "gold_version": gold["version"], "library_id": library_id, "original_preset": info["active"],
        "original_default_preset": info["default"],
        "args": {k: v for k, v in vars(args).items() if k != "header"}, "skipped": [], "switch": {},
    }
    n_done = 0
    try:
        for step in plan:
            if "skip" in step:
                meta["skipped"].append({"preset": step["preset"], "reason": step["skip"]})
                continue
            name = step["preset"]
            if name != info["active"]:
                ok, how = switcher.switch(name)
                if not ok:
                    print(f"[SKIP] {name}: cannot select ({how})")
                    meta["skipped"].append({"preset": name, "reason": f"selection failed: {how}"})
                    continue
                meta["switch"][name] = how
                print(f"[INFO] selected {name} ({how})")
            headers = headers_for(name)
            ready, why = ensure_ready(args.url, headers, args.warm_timeout, args.warm_poll)
            if not ready:
                print(f"[SKIP] {name}: {why}")
                meta["skipped"].append({"preset": name, "reason": why})
                continue
            if why:
                print(f"[WARN] {name}: {why}")
                meta.setdefault("warnings", {})[name] = why
            st, config = http_json("GET", f"{args.url}/api/config", headers, timeout=60)
            live_models = config.get("llm_models", []) if st == 200 and isinstance(config, dict) else step["models"]
            models = select_models(live_models, args.models)

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
