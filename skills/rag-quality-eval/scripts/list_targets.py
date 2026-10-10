"""List which presets and models can be evaluated, and which cannot (and why).

Because the skill runs inside the repository checkout, the classification uses
the backend's own preset loader, compatibility rule and credential check
(``backend.api.config``) instead of guessing. Against a remote instance (or with
``--no-local``) it falls back to ``GET /api/config`` and reports only the
active preset's models.

Status per preset:
  active               currently active on the server
  switchable           same remote embedding model + credentials present: can be
                       switched at runtime (POST /api/config) and evaluated
  credentials_missing  compatible but a provider key is missing (names listed)
  needs_restart        different embedding model or a local-model preset: the
                       existing index would not match; needs MODEL_PRESET + restart
                       (and a re-index) - not evaluated automatically

Usage:
    uv run python skills/rag-quality-eval/scripts/list_targets.py [--url URL] [--json] [--no-local]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    DEFAULT_URL, auth_headers, get_zotero_key, http_json, local_backend_available, local_settings,
)


class _StubRequest:
    """Minimal request object for the backend's credential check (no personal header)."""
    headers: dict = {}


def _discover_local(settings: Any, active_name: str, live_models: list[str]) -> list[dict]:
    from backend.api.config import _compatible_presets, _preset_credentials, _stored_embedding_key_counts
    from backend.config.presets import current_platform, get_preset, list_presets

    data_path = settings.data_path
    available = list_presets(data_path, platform=current_platform())
    active = get_preset(active_name, data_path)
    compatible = set(_compatible_presets(active, data_path, available))
    counts = _stored_embedding_key_counts(settings)
    rows = []
    for name in available:
        preset = get_preset(name, data_path)
        row = {
            "name": name,
            "description": preset.description,
            "embedding": f"{preset.embedding.model_type}:{preset.embedding.model_name}",
            "llm_type": preset.llm.model_type,
            "llm_models": live_models if name == active_name and live_models else list(preset.llm.model_names),
            "live_model_list": bool(preset.llm.models_status_url),
            "missing_credentials": [],
        }
        if name == active_name:
            row["status"] = "active"
        elif name not in compatible:
            row["status"] = "needs_restart"
            row["note"] = "different embedding model or local-model preset: MODEL_PRESET + restart (+ re-index)"
        else:
            missing = _preset_credentials(preset, settings, _StubRequest(), counts)
            row["missing_credentials"] = sorted(missing)
            row["status"] = "credentials_missing" if missing else "switchable"
        rows.append(row)
    return rows


def _discover_remote(config: dict) -> list[dict]:
    switchable = {p["name"]: p for p in config.get("switchable_presets", [])}
    compatible = set(config.get("compatible_presets", []))
    rows = []
    for name in config.get("available_presets", []):
        if name == config["preset_name"]:
            status = "active"
        elif name in switchable and switchable[name]["credentials"] == "ok":
            status = "switchable"
        elif name in compatible:
            status = "credentials_missing"
        else:
            status = "needs_restart"
        rows.append({
            "name": name,
            "status": status,
            "llm_models": config.get("llm_models", []) if name == config["preset_name"] else [],
            "missing_credentials": [],
        })
    return rows


def discover(base_url: str, zotero_key: str | None, use_local: bool = True) -> dict:
    """Active preset, its live model list and a classified list of all presets."""
    status, config = http_json("GET", f"{base_url}/api/config", auth_headers(zotero_key), timeout=60)
    if status != 200 or not isinstance(config, dict):
        raise SystemExit(f"[FAIL] GET {base_url}/api/config -> {status}: {str(config)[:200]}. Is the backend running?")
    settings = local_settings() if use_local and local_backend_available() else None
    if settings is not None:
        try:
            rows = _discover_local(settings, config["preset_name"], config.get("llm_models", []))
            return {"url": base_url, "active": config["preset_name"], "mode": "local-code", "presets": rows,
                    "config": config}
        except Exception as exc:  # backend deps missing etc.
            print(f"[WARN] local discovery failed ({type(exc).__name__}: {exc}); using HTTP only", file=sys.stderr)
    return {"url": base_url, "active": config["preset_name"], "mode": "http-only",
            "presets": _discover_remote(config), "config": config}


def _print_table(info: dict) -> None:
    print(f"backend {info['url']}  active preset: {info['active']}  (discovery: {info['mode']})\n")
    for row in info["presets"]:
        models = row.get("llm_models") or ["?"]
        print(f"{row['status']:<20} {row['name']:<24} models: {len(models)}")
        for m in models:
            print(f"{'':<46}- {m}")
        if row.get("missing_credentials"):
            print(f"{'':<46}missing credentials: {', '.join(row['missing_credentials'])}")
        if row.get("note"):
            print(f"{'':<46}{row['note']}")
        if row.get("live_model_list") and row["status"] != "active":
            print(f"{'':<46}(model list is live; refreshed after switching)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-local", action="store_true", help="do not import backend code; HTTP only")
    args = parser.parse_args()
    info = discover(args.url, get_zotero_key(), use_local=not args.no_local)
    info.pop("config", None)
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        _print_table(info)


if __name__ == "__main__":
    main()
