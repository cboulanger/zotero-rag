"""Interactively fill in provider credentials this backend's presets need.

Scans the presets available for this platform (or just ``--preset`` names),
collects each remote side's required client fields (the same fields
``RemoteEmbeddingService``/``RemoteLLMService.required_client_fields*``
expose to the plugin's Preferences pane and to ``GET /api/required-keys``),
and reports or fills in whichever aren't satisfied yet:

- a personal ``api_key`` field (per-request header, e.g. ``KISSKI_API_KEY``)
  is stored on a Zotero-identity entry in the encrypted auto-index key store
  (``<data_path>/system/autoindex_keys.json``) via
  ``backend.services.secret_store``/``AutoIndexKeyStore.set_embedding_key`` --
  the same store ``bin/autoindex_add_key.py`` and the plugin's Preferences
  pane write to. If no identity entry exists yet, a read-only Zotero API key
  is requested first and registered the same way
  ``bin/autoindex_add_key.py`` does.
- a ``shared_api_key``/``shared_base_url`` field (admin-set, global) is
  stored in the admin settings' ``remote_config``
  (``<data_path>/system/admin_settings.json``) via
  ``backend.services.admin_settings_store.update_remote_config``.

A field whose side supports automatic provisioning (RunPod, Hugging Face
endpoints) is reported but never asked for here -- run
``bin/provision.py --preset <name>`` instead, which supplies it directly.

Values are never echoed, printed, or logged. If a value for a field is
already sitting in this checkout's ``.env``, pressing Enter at a secret
prompt reuses it without displaying it, and -- once the value is safely
stored encrypted -- the script offers (default: yes) to delete that line
from ``.env``, since nothing should need it there any more.

Usage:
    uv run python scripts/setup_credentials.py                    # interactive, every preset for this platform
    uv run python scripts/setup_credentials.py --preset remote-kisski --preset huggingface
    uv run python scripts/setup_credentials.py --dry-run           # print missing key names, nothing else; exit 1 if any
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import re
import sys
from pathlib import Path
from typing import Any, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _collect_fields(preset: Any) -> list[dict]:
    """Every client-configurable field this preset's two sides require, tagged with ``side``."""
    from backend.services.embeddings import RemoteEmbeddingService
    from backend.services.llm import RemoteLLMService

    fields = []
    for side, cfg, getter in (
        ("embedding", preset.embedding, RemoteEmbeddingService.required_client_fields),
        ("llm", preset.llm, RemoteLLMService.required_client_fields_for_config),
    ):
        for field in getter(cfg):
            fields.append({**field, "side": side})
    return fields


def _provisionable_sides(preset: Any) -> set[str]:
    """Sides of ``preset`` whose provider can provision its own endpoint+key."""
    try:
        from backend.providers import get_providers
        return {side for side, provider in get_providers(preset).items() if provider.supports_provisioning}
    except Exception:
        return set()


def _gather(preset_names: list[str], data_path: Path) -> dict[tuple[str, str], dict]:
    """``(key_name, kind) -> {description, docs_url, pattern, needed_by: [...], provisionable}``.

    Keyed by ``(key_name, kind)`` rather than just ``key_name``: the same
    literal key name (e.g. ``RUNPOD_API_KEY``) can be a personal ``api_key``
    on one preset and an admin-set ``shared_api_key`` on another, and those
    are stored in two different places.
    """
    from backend.config.presets import get_preset

    unique: dict[tuple[str, str], dict] = {}
    for name in preset_names:
        try:
            preset = get_preset(name, data_path)
        except Exception as exc:
            print(f"[WARN] skipping {name}: {exc}", file=sys.stderr)
            continue
        provisionable_sides = _provisionable_sides(preset)
        for field in _collect_fields(preset):
            cache_key = (field["key_name"], field["kind"])
            entry = unique.setdefault(cache_key, {
                "description": field["description"], "docs_url": field.get("docs_url"),
                "pattern": field.get("pattern"), "needed_by": [],
                "provisionable": field["kind"] in ("shared_api_key", "shared_base_url"),
            })
            entry["needed_by"].append(f"{name}/{field['side']}")
            if field["side"] not in provisionable_sides:
                entry["provisionable"] = False
    return unique


def _is_set(key_name: str, kind: str, stored_counts: dict[str, int], data_path: Path) -> bool:
    if kind in ("shared_api_key", "shared_base_url"):
        from backend.services.admin_settings_store import get_remote_config_value
        return bool(get_remote_config_value(key_name, data_path))
    return stored_counts.get(key_name, 0) > 0


def _classify(fields: dict[tuple[str, str], dict], stored_counts: dict[str, int],
              data_path: Path) -> tuple[list[tuple], list[tuple], list[tuple]]:
    """Split into (ok, provisionable, missing) lists of ``((key_name, kind), info)``."""
    ok, provisionable, missing = [], [], []
    for cache_key, info in sorted(fields.items()):
        key_name, kind = cache_key
        if info["provisionable"]:
            provisionable.append((cache_key, info))
        elif _is_set(key_name, kind, stored_counts, data_path):
            ok.append((cache_key, info))
        else:
            missing.append((cache_key, info))
    return ok, provisionable, missing


def _dotenv_value(key_name: str) -> Optional[str]:
    """``key_name``'s value in this checkout's ``.env``, if any (never printed)."""
    try:
        from dotenv import dotenv_values
        return dotenv_values(_PROJECT_ROOT / ".env").get(key_name) or None
    except Exception:
        return None


def _prompt_value(key_name: str, description: str, pattern: Optional[str], secret: bool,
                  hard_validate: bool) -> Optional[str]:
    """Ask for a value; returns None on skip. Never echoes or logs the input."""
    default = _dotenv_value(key_name) if secret else None
    print(f"\n{key_name} -- {description}")
    if pattern:
        print(f"  expected format: {pattern}")
    hint = " (Enter = reuse the value already in .env)" if default else ""
    reader = getpass.getpass if secret else input
    while True:
        raw = reader(f"  value{hint}, or 'skip': ").strip()
        if raw.lower() == "skip":
            return None
        value = raw or default
        if not value:
            return None
        if pattern and not re.fullmatch(pattern, value):
            if hard_validate:
                print(f"  does not match the expected format ({pattern}); try again.")
                continue
            if input("  does not match the expected format -- store anyway? [y/N] ").strip().lower() != "y":
                continue
        return value


def _maybe_remove_from_env(key_name: str) -> None:
    """If ``key_name`` is set in this checkout's ``.env``, offer to delete that line (default yes).

    Once a value is stored encrypted, leaving a plaintext copy in ``.env`` is
    pure downside: it's one more place the value could leak from, and nothing
    reads it from there any more for this purpose.
    """
    if _dotenv_value(key_name) is None:
        return
    answer = input(f"  remove {key_name} from .env now that it's stored? [Y/n] ").strip().lower()
    if answer in ("", "y", "yes"):
        from dotenv import unset_key
        unset_key(str(_PROJECT_ROOT / ".env"), key_name)
        print(f"  removed {key_name} from .env")
    else:
        print(f"  left {key_name} in .env")


def _ensure_zotero_identity(store: Any) -> Optional[str]:
    """Fingerprint of an existing identity entry, or one newly registered by prompt. None if declined."""
    entries = store.list_metadata()
    if entries:
        if len(entries) > 1:
            print(f"[INFO] {len(entries)} Zotero identities are stored; using the first "
                  f"(user_id={entries[0]['user_id']}).")
        return entries[0]["fingerprint"]
    print("\nA personal provider key is stored against a Zotero identity, and none is "
          "registered yet. Enter a *read-only* Zotero API key to register one (create one at "
          "https://www.zotero.org/settings/keys), or leave blank to skip every personal key below.")
    api_key = getpass.getpass("  read-only Zotero API key, or blank to skip: ").strip()
    if not api_key:
        return None
    from backend.zotero.key_validator import validate_key
    validation = asyncio.run(validate_key(api_key))
    if not validation.read_only:
        print(f"[FAIL] {validation.reason}")
        return None
    fp = store.add(api_key, validation)
    print(f"[OK] registered Zotero identity {fp} ({validation.username}, user_id={validation.user_id})")
    return fp


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preset", action="append", default=None, metavar="NAME",
                        help="limit to these presets (default: every preset for this platform)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print missing (non-provisionable) key names, one per line, and nothing else; "
                             "exit 1 if any are missing, 0 otherwise")
    args = parser.parse_args()

    from backend.config.presets import current_platform, list_presets
    from backend.config.settings import get_settings
    from backend.services import secret_store
    from backend.services.admin_settings_store import update_remote_config
    from backend.services.secret_store import get_key_store

    if not secret_store.secrets_enabled():
        print(f"[FAIL] {secret_store.MISSING_SECRET_MESSAGE}", file=sys.stderr)
        return 1

    settings = get_settings()
    data_path = settings.data_path
    preset_names = args.preset or list_presets(data_path, platform=current_platform())

    fields = _gather(preset_names, data_path)
    store = get_key_store()
    stored_counts = store.count_embedding_keys_by_name() if store.enabled else {}
    ok, provisionable, missing = _classify(fields, stored_counts, data_path)

    if args.dry_run:
        for (key_name, _kind), _info in missing:
            print(key_name)
        return 1 if missing else 0

    print(f"checked {len(preset_names)} preset(s): {len(ok)} credential(s) set, "
          f"{len(provisionable)} provisionable, {len(missing)} missing\n")
    for (key_name, kind), info in ok:
        print(f"ok            {key_name} ({kind})  needed by: {', '.join(info['needed_by'])}")
    for (key_name, kind), info in provisionable:
        print(f"provisionable {key_name} ({kind})  needed by: {', '.join(info['needed_by'])}  "
              f"-- run: uv run python bin/provision.py --preset <name> instead")
    for (key_name, kind), info in missing:
        print(f"missing       {key_name} ({kind})  needed by: {', '.join(info['needed_by'])}")

    if not missing:
        print("\n[OK] nothing to do.")
        return 0

    print(f"\n{len(missing)} credential(s) missing. Enter a value for each, or 'skip'.")
    identity_fp: Optional[str] = None
    stored_now: list[str] = []
    skipped: list[str] = []
    for (key_name, kind), info in missing:
        if kind == "api_key":
            if identity_fp is None:
                identity_fp = _ensure_zotero_identity(store) or ""
            if not identity_fp:
                print(f"  skipping {key_name}: no Zotero identity to attach it to.")
                skipped.append(key_name)
                continue
            value = _prompt_value(key_name, info["description"], info["pattern"], secret=True, hard_validate=False)
            if value is None:
                skipped.append(key_name)
                continue
            store.set_embedding_key(identity_fp, value, key_name)
            stored_now.append(key_name)
            _maybe_remove_from_env(key_name)
        else:  # shared_api_key / shared_base_url
            value = _prompt_value(key_name, info["description"], info["pattern"],
                                  secret=(kind == "shared_api_key"), hard_validate=True)
            if value is None:
                skipped.append(key_name)
                continue
            update_remote_config({key_name: value}, data_path)
            stored_now.append(key_name)
            _maybe_remove_from_env(key_name)

    print()
    if stored_now:
        print(f"[OK] stored: {', '.join(stored_now)}")
    if skipped:
        print(f"[INFO] skipped: {', '.join(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
