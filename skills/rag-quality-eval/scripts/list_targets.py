"""List which presets and models can be evaluated, and which cannot (and why).

Because the skill runs inside the repository checkout, the classification uses
the backend's own preset loader, compatibility rule, provider layer and
credential check instead of guessing. Against a remote instance (or with
``--no-local``) it falls back to ``GET /api/config`` and reports only the
active preset's models.

Two preset notions exist on the server (docs/presets.md): the *default* preset
(admin-set, what everybody runs on) and the caller's *effective* preset (their
own saved choice, else the default). "active" below is the effective one.

Status per preset:
  active               the caller's effective preset
  switchable           compatible with the default (both sides remote, same
                       embedding model) and every key it needs can be supplied
                       by this harness: it can be selected at runtime
  credentials_missing  compatible, but a key is missing (names listed): export
                       the key as $<KEY_NAME> or store it (bin/autoindex_add_key.py)
  needs_restart        incompatible with the default (different embedding model,
                       or a local-model side): the existing index would not
                       match; needs MODEL_PRESET + restart (+ re-index), so it is
                       not evaluated automatically

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
    missing_provider_keys, provider_headers,
)


def _providers_label(preset: Any) -> str:
    """e.g. ``embedding=kisski/user llm=anthropic/user`` (empty when invalid)."""
    try:
        from backend.providers import get_providers
        providers = get_providers(preset)
    except Exception:
        return ""
    parts = []
    for side, provider in providers.items():
        scope = (getattr(preset, side).provider or None)
        scope_name = getattr(scope, "scope", None) or getattr(provider, "default_scope", "")
        parts.append(f"{side}={provider.id}/{scope_name}")
    return " ".join(parts)


def _has_live_models(preset: Any) -> bool:
    try:
        from backend.providers import get_providers
        return bool(get_providers(preset)["llm"].has_live_models)
    except Exception:
        return False


def _discover_local(settings: Any, effective_name: str, live_models: list[str],
                    extra_headers: dict[str, str]) -> tuple[str, list[dict]]:
    """Classify every listed preset; returns ``(default_name, rows)``."""
    from backend.api.config import _compatible_presets
    from backend.config.presets import current_platform, get_preset, list_presets

    data_path = settings.data_path
    default = settings.get_default_preset()
    available = list_presets(data_path, platform=current_platform())
    compatible = set(_compatible_presets(default, data_path, available))
    rows = []
    for name in available:
        preset = get_preset(name, data_path)
        headers = provider_headers(preset, extra_headers)
        row = {
            "name": name,
            "description": preset.description,
            "embedding": f"{preset.embedding.model_type}:{preset.embedding.model_name}",
            "llm_type": preset.llm.model_type,
            "providers": _providers_label(preset),
            "llm_models": live_models if name == effective_name and live_models else list(preset.llm.model_names),
            "live_model_list": _has_live_models(preset),
            "is_default": name == default.name,
            "missing_credentials": [],
        }
        if name == effective_name:
            row["status"] = "active"
        elif name not in compatible:
            row["status"] = "needs_restart"
            row["note"] = "incompatible with the default (different embedding model or a local side): " \
                          "MODEL_PRESET + restart (+ re-index)"
        else:
            missing = missing_provider_keys(preset, headers)
            row["missing_credentials"] = missing
            row["status"] = "credentials_missing" if missing else "switchable"
        rows.append(row)
    return default.name, rows


def _discover_remote(config: dict) -> list[dict]:
    """HTTP-only classification from GET /api/config (credentials as seen by the request headers)."""
    selectable = {p["name"]: p for p in config.get("selectable_presets") or config.get("switchable_presets", [])}
    compatible = set(config.get("compatible_presets", []))
    effective = config["preset_name"]
    rows = []
    for name in config.get("available_presets", []):
        entry = selectable.get(name)
        if name == effective:
            status = "active"
        elif entry and entry.get("credentials") == "ok":
            status = "switchable"
        elif name in compatible:
            status = "credentials_missing"
        else:
            status = "needs_restart"
        rows.append({
            "name": name, "status": status, "is_default": name == config.get("default_preset"),
            "llm_models": config.get("llm_models", []) if name == effective else [],
            "missing_credentials": (entry or {}).get("missing_keys", []),
        })
    return rows


def discover(base_url: str, zotero_key: str | None, use_local: bool = True,
             extra_headers: dict[str, str] | None = None) -> dict:
    """Effective + default preset, the live model list and a classified list of all presets."""
    headers = {**auth_headers(zotero_key), **(extra_headers or {})}
    status, config = http_json("GET", f"{base_url}/api/config", headers, timeout=60)
    if status != 200 or not isinstance(config, dict):
        raise SystemExit(f"[FAIL] GET {base_url}/api/config -> {status}: {str(config)[:200]}. Is the backend running?")
    effective = config["preset_name"]
    default = config.get("default_preset") or effective
    settings = local_settings() if use_local and local_backend_available() else None
    mode, rows = "http-only", None
    if settings is not None:
        try:
            default, rows = _discover_local(settings, effective, config.get("llm_models", []), extra_headers or {})
            mode = "local-code"
        except Exception as exc:  # backend deps missing, preset file broken, ...
            print(f"[WARN] local discovery failed ({type(exc).__name__}: {exc}); using HTTP only", file=sys.stderr)
    if rows is None:
        rows = _discover_remote(config)
    return {"url": base_url, "active": effective, "default": default, "mode": mode, "presets": rows,
            "config": config}


def _print_table(info: dict) -> None:
    print(f"backend {info['url']}  effective preset: {info['active']}  server default: {info['default']}  "
          f"(discovery: {info['mode']})\n")
    for row in info["presets"]:
        models = row.get("llm_models") or ["?"]
        flag = " (default)" if row.get("is_default") else ""
        print(f"{row['status']:<20} {row['name']}{flag}  models: {len(models)}")
        if row.get("providers"):
            print(f"{'':<20} providers: {row['providers']}")
        for m in models:
            print(f"{'':<22}- {m}")
        if row.get("missing_credentials"):
            print(f"{'':<20} missing credentials: {', '.join(row['missing_credentials'])}")
        if row.get("note"):
            print(f"{'':<20} {row['note']}")
        if row.get("live_model_list") and row["status"] != "active":
            print(f"{'':<20} (model list is live; refreshed after switching)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--no-local", action="store_true", help="do not import backend code; HTTP only")
    parser.add_argument("--header", action="append", default=[], metavar="'Name: value'", help="extra request header")
    args = parser.parse_args()
    extra = {k.strip(): v.strip() for k, v in (h.split(":", 1) for h in args.header if ":" in h)}
    info = discover(args.url, get_zotero_key(), use_local=not args.no_local, extra_headers=extra)
    info.pop("config", None)
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        _print_table(info)


if __name__ == "__main__":
    main()
