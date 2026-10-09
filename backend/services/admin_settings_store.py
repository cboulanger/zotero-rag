"""Persisted store for global, admin-controlled runtime settings.

Mirrors backend.services.autoindex_scheduler's state-file pattern: a small
JSON file under data_path/system/, atomically written, with a safe default
when the file doesn't exist yet. Unlike backend.config.settings.Settings
(env-var-backed, fixed at process start), this store is meant to be toggled
live by a server admin without a restart or redeploy.

Two fields exist to let an admin swap in a preset whose remote endpoint/API
key rotate at runtime (e.g. the MPCDF LLM Inference Service's ephemeral <=8h
job URLs — see
docs/superpowers/specs/2026-10-08-dynamic-remote-preset-config-design.md):

- ``active_preset_override``: overrides Settings.model_preset without a
  restart, when set to a known preset name.
- ``remote_config``: a flat {env_var_name: value} map for a preset's
  ``shared_base_url_env``/``shared_api_key_env`` fields (see
  backend.config.presets). Plaintext — these values are short-lived
  (hours) and scoped to a sandboxed job, so the Fernet-encryption
  machinery used for long-lived personal keys
  (backend.services.autoindex_key_store) isn't warranted here.
"""

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

DEFAULT_ADMIN_SETTINGS = {
    "index_snapshots": False,
    "active_preset_override": None,
    "remote_config": {},
}


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write a small JSON state file (Windows-safe via os.replace).

    Mirrors CronIndexer._write_status's pattern (backend/services/cron_indexer.py)
    and autoindex_scheduler._atomic_write_json.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp", prefix=path.stem + "_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_admin_settings(data_path: Path) -> dict:
    """Missing, corrupt, or non-dict-shaped file reads as the safe default."""
    settings_path = data_path / "system" / "admin_settings.json"
    try:
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(DEFAULT_ADMIN_SETTINGS)
    if not isinstance(loaded, dict):
        return copy.deepcopy(DEFAULT_ADMIN_SETTINGS)
    merged = copy.deepcopy(DEFAULT_ADMIN_SETTINGS)
    merged.update(loaded)
    return merged


def write_admin_settings(data_path: Path, settings: dict) -> None:
    """Replaces the entire settings file — callers must pass the complete
    current settings dict, not a partial update (same full-replace contract
    as autoindex_scheduler.write_scheduler_state)."""
    _atomic_write_json(data_path / "system" / "admin_settings.json", settings)


def get_active_preset_override(data_path: Path) -> Optional[str]:
    """The admin-set preset name overriding Settings.model_preset, or None."""
    return read_admin_settings(data_path).get("active_preset_override")


def set_active_preset_override(data_path: Path, preset_name: Optional[str]) -> None:
    """Set (or clear, with None) the runtime preset override."""
    state = read_admin_settings(data_path)
    state["active_preset_override"] = preset_name
    write_admin_settings(data_path, state)


def get_remote_config_value(data_path: Path, key_name: str) -> Optional[str]:
    """Look up one stored shared remote-config value (a base_url or api_key), or None."""
    return read_admin_settings(data_path).get("remote_config", {}).get(key_name)


def resolve_shared_value(data_path: Path, env_var_name: str) -> Optional[str]:
    """Resolve a preset's shared base_url/api_key: the admin-set remote_config
    override first, then the process environment."""
    return get_remote_config_value(data_path, env_var_name) or os.getenv(env_var_name)


def normalize_base_url(base_url: str) -> str:
    """Append '/v1' to a base URL that's missing it, leaving one that already
    ends with '/v1' untouched.

    Guards against the common mistake of pasting a dynamic job endpoint
    (e.g. MPCDF's `https://llm.mpcdf.mpg.de/<job-id>`) without the trailing
    `/v1` the OpenAI-compatible API path requires — the openai SDK appends
    `/embeddings`/`/chat/completions` directly to whatever base_url it's
    given, so a missing `/v1` segment silently turns into a 404.
    """
    stripped = base_url.rstrip("/")
    if stripped.endswith("/v1"):
        return stripped
    return f"{stripped}/v1"


def update_remote_config(data_path: Path, values: dict) -> dict:
    """Merge `values` into the stored remote_config map and persist.

    Merges rather than replaces — setting MPCDF_LLM_BASE_URL must not wipe
    an already-stored MPCDF_EMBEDDING_BASE_URL. Returns the full resulting
    remote_config dict.
    """
    state = read_admin_settings(data_path)
    remote_config = dict(state.get("remote_config", {}))
    remote_config.update(values)
    state["remote_config"] = remote_config
    write_admin_settings(data_path, state)
    return remote_config
