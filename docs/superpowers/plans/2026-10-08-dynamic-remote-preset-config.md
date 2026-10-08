# Dynamic Remote-Preset Config + Runtime Preset Switching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an admin configure the `remote-mpcdf` preset's rotating MPCDF endpoint/API-key values and switch the active preset, both at runtime (no backend restart), with the values picked up by interactive requests *and* the cron auto-indexer.

**Architecture:** Two new admin-set fields live in the existing `backend/services/admin_settings_store.py` JSON file (`active_preset_override`, `remote_config`). `Settings.get_hardware_preset()` and the two remote-service factories (`RemoteEmbeddingService`, `RemoteLLMService`) read them with the same precedence everywhere: shared store → process env var → error. Two FastAPI endpoints (`POST /api/config`, `POST /api/config/remote-fields`), both admin-gated via the existing `require_authorized_group_admin` dependency, let the plugin write them. The plugin's existing `renderServiceApiKeyFields` renderer grows a branch for the new field kinds.

**Tech Stack:** Python/FastAPI/Pydantic (backend), vanilla JS + XHTML (Zotero plugin), pytest (`unittest.TestCase`-style), Node's built-in test runner.

**Spec:** `docs/superpowers/specs/2026-10-08-dynamic-remote-preset-config-design.md`

**Two deliberate deviations from the spec, found while mapping it onto the actual code:**
1. The spec proposed a new `backend/services/remote_config_store.py` module. `backend/services/admin_settings_store.py` already exists and does exactly this job ("a small JSON file under data_path/system/, atomically written... meant to be toggled live by a server admin without a restart") for an unrelated setting (`index_snapshots`). This plan extends that file instead of adding a near-duplicate module.
2. The spec said the runtime preset override should *not* survive a restart. Reusing `admin_settings_store.py` means it naturally does persist (same as `index_snapshots` already does) — which is arguably the more useful behavior anyway (a crash/redeploy doesn't silently snap back to a KISSKI-rate-limited preset without the admin noticing). This plan persists it; flag it to the user as a change from the approved spec.

---

## Task 1: Extend `admin_settings_store.py` with preset-override + shared remote-config

**Files:**
- Modify: `backend/services/admin_settings_store.py`
- Modify: `backend/tests/test_admin_settings_store.py`

- [ ] **Step 1: Write the new/updated tests**

Replace the full contents of `backend/tests/test_admin_settings_store.py` with:

```python
"""Unit tests for the admin settings JSON state store."""

import tempfile
import unittest
from pathlib import Path

from backend.services.admin_settings_store import (
    read_admin_settings,
    write_admin_settings,
    get_active_preset_override,
    set_active_preset_override,
    get_remote_config_value,
    update_remote_config,
)


class TestAdminSettingsStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_returns_defaults_when_file_missing(self):
        settings = read_admin_settings(self.data_path)
        self.assertEqual(settings, {
            "index_snapshots": False,
            "active_preset_override": None,
            "remote_config": {},
        })

    def test_write_then_read_round_trips(self):
        write_admin_settings(self.data_path, {"index_snapshots": True})
        self.assertEqual(read_admin_settings(self.data_path), {
            "index_snapshots": True,
            "active_preset_override": None,
            "remote_config": {},
        })

    def test_write_then_read_false_round_trips(self):
        write_admin_settings(self.data_path, {"index_snapshots": True})
        write_admin_settings(self.data_path, {"index_snapshots": False})
        self.assertEqual(read_admin_settings(self.data_path), {
            "index_snapshots": False,
            "active_preset_override": None,
            "remote_config": {},
        })

    def test_read_returns_default_on_corrupt_file(self):
        system_dir = self.data_path / "system"
        system_dir.mkdir(parents=True)
        (system_dir / "admin_settings.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(read_admin_settings(self.data_path), {
            "index_snapshots": False,
            "active_preset_override": None,
            "remote_config": {},
        })

    def test_read_returns_default_when_file_contains_valid_but_non_dict_json(self):
        system_dir = self.data_path / "system"
        system_dir.mkdir(parents=True)
        (system_dir / "admin_settings.json").write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(read_admin_settings(self.data_path), {
            "index_snapshots": False,
            "active_preset_override": None,
            "remote_config": {},
        })

    def test_active_preset_override_defaults_to_none(self):
        self.assertIsNone(get_active_preset_override(self.data_path))

    def test_set_then_get_active_preset_override_round_trips(self):
        set_active_preset_override(self.data_path, "remote-mpcdf")
        self.assertEqual(get_active_preset_override(self.data_path), "remote-mpcdf")

    def test_set_active_preset_override_none_clears_it(self):
        set_active_preset_override(self.data_path, "remote-mpcdf")
        set_active_preset_override(self.data_path, None)
        self.assertIsNone(get_active_preset_override(self.data_path))

    def test_set_active_preset_override_preserves_index_snapshots(self):
        write_admin_settings(self.data_path, {"index_snapshots": True})
        set_active_preset_override(self.data_path, "remote-mpcdf")
        state = read_admin_settings(self.data_path)
        self.assertTrue(state["index_snapshots"])
        self.assertEqual(state["active_preset_override"], "remote-mpcdf")

    def test_get_remote_config_value_defaults_to_none(self):
        self.assertIsNone(get_remote_config_value(self.data_path, "MPCDF_LLM_BASE_URL"))

    def test_update_remote_config_then_get_round_trips(self):
        update_remote_config(self.data_path, {"MPCDF_LLM_BASE_URL": "https://llm.mpcdf.mpg.de/abc123/v1"})
        self.assertEqual(
            get_remote_config_value(self.data_path, "MPCDF_LLM_BASE_URL"),
            "https://llm.mpcdf.mpg.de/abc123/v1",
        )

    def test_update_remote_config_merges_without_dropping_other_keys(self):
        update_remote_config(self.data_path, {"MPCDF_LLM_BASE_URL": "https://llm.mpcdf.mpg.de/abc123/v1"})
        update_remote_config(self.data_path, {"MPCDF_EMBEDDING_BASE_URL": "https://llm.mpcdf.mpg.de/def456/v1"})
        self.assertEqual(
            get_remote_config_value(self.data_path, "MPCDF_LLM_BASE_URL"),
            "https://llm.mpcdf.mpg.de/abc123/v1",
        )
        self.assertEqual(
            get_remote_config_value(self.data_path, "MPCDF_EMBEDDING_BASE_URL"),
            "https://llm.mpcdf.mpg.de/def456/v1",
        )

    def test_update_remote_config_overwrites_same_key(self):
        update_remote_config(self.data_path, {"MPCDF_LLM_BASE_URL": "https://llm.mpcdf.mpg.de/old/v1"})
        update_remote_config(self.data_path, {"MPCDF_LLM_BASE_URL": "https://llm.mpcdf.mpg.de/new/v1"})
        self.assertEqual(
            get_remote_config_value(self.data_path, "MPCDF_LLM_BASE_URL"),
            "https://llm.mpcdf.mpg.de/new/v1",
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest backend/tests/test_admin_settings_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'get_active_preset_override'` (and the three existing round-trip tests fail on the dict-equality mismatch even before the import error, depending on collection order).

- [ ] **Step 3: Implement the new store functions**

Replace the full contents of `backend/services/admin_settings_store.py` with:

```python
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
        return dict(DEFAULT_ADMIN_SETTINGS)
    if not isinstance(loaded, dict):
        return dict(DEFAULT_ADMIN_SETTINGS)
    merged = dict(DEFAULT_ADMIN_SETTINGS)
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest backend/tests/test_admin_settings_store.py -v`
Expected: PASS (15 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/services/admin_settings_store.py backend/tests/test_admin_settings_store.py
git commit -m "feat(backend): add preset-override and shared remote-config to admin settings store"
```

---

## Task 1b: Fix two issues the code-quality review of Task 1 found

Code review of Task 1's commit surfaced two real problems, not anticipated when the plan was written. Fix both before continuing — later tasks build on this file.

**Files:**
- Modify: `backend/services/admin_settings_store.py`
- Modify: `backend/api/admin_settings.py`
- Modify: `backend/tests/test_admin_settings_store.py`
- Modify: `backend/tests/test_admin_settings_api.py`

- [ ] **Step 1: Write the failing regression tests**

Add to `backend/tests/test_admin_settings_store.py`, inside `class TestAdminSettingsStore`:

```python
    def test_read_admin_settings_does_not_leak_mutable_state_across_data_paths(self):
        """DEFAULT_ADMIN_SETTINGS["remote_config"] is one module-level dict; a
        shallow dict(DEFAULT_ADMIN_SETTINGS) copy still shares that nested dict
        object, so mutating one data_path's returned remote_config in place
        (bypassing update_remote_config) must not leak into another, unrelated
        data_path's defaults."""
        with tempfile.TemporaryDirectory() as other_tmp:
            other_data_path = Path(other_tmp)
            update_remote_config(self.data_path, {"LEAKED_KEY": "leaked_value"})
            other_state = read_admin_settings(other_data_path)
            self.assertEqual(other_state["remote_config"], {})
```

Add to `backend/tests/test_admin_settings_api.py`, inside `class AdminSettingsApiTest`:

```python
    def test_put_preserves_active_preset_override_and_remote_config(self):
        """PUT /api/admin/settings only has an `index_snapshots` field in its
        request body, but write_admin_settings is a full-replace — so a naive
        `write_admin_settings(data_path, body.model_dump())` silently wipes
        active_preset_override/remote_config every time someone flips the
        unrelated index-snapshots checkbox."""
        from backend.services.admin_settings_store import (
            set_active_preset_override, update_remote_config, read_admin_settings,
        )
        set_active_preset_override(get_settings().data_path, "remote-mpcdf")
        update_remote_config(get_settings().data_path, {"MPCDF_LLM_BASE_URL": "https://x/v1"})
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))

        r = self.client.put("/api/admin/settings", json={"index_snapshots": True})
        self.assertEqual(r.status_code, 200)

        state = read_admin_settings(get_settings().data_path)
        self.assertTrue(state["index_snapshots"])
        self.assertEqual(state["active_preset_override"], "remote-mpcdf")
        self.assertEqual(state["remote_config"], {"MPCDF_LLM_BASE_URL": "https://x/v1"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_admin_settings_store.py backend/tests/test_admin_settings_api.py -v`
Expected: FAIL — the leak test fails because `other_state["remote_config"]` is `{"LEAKED_KEY": "leaked_value"}` instead of `{}`; the PUT test fails because `active_preset_override`/`remote_config` come back `None`/`{}` after the PUT.

- [ ] **Step 3: Fix the shared-mutable-default in `admin_settings_store.py`**

Add `import copy` to the top of `backend/services/admin_settings_store.py` (alongside the existing `import json`/`import os`/`import tempfile`).

Replace `read_admin_settings` (both `return dict(DEFAULT_ADMIN_SETTINGS)` lines and the `merged = dict(DEFAULT_ADMIN_SETTINGS)` line) so every place that copies `DEFAULT_ADMIN_SETTINGS` uses `copy.deepcopy` instead of `dict(...)`:

```python
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
```

- [ ] **Step 4: Fix the data-loss hazard in `backend/api/admin_settings.py`**

Replace `put_admin_settings` (currently):

```python
@router.put("/admin/settings", summary="Update global admin-controlled settings (admin only)", response_model=AdminSettings)
async def put_admin_settings(
    body: AdminSettings,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
) -> AdminSettings:
    settings = get_settings()
    await asyncio.to_thread(write_admin_settings, settings.data_path, body.model_dump())
    return body
```

with:

```python
@router.put("/admin/settings", summary="Update global admin-controlled settings (admin only)", response_model=AdminSettings)
async def put_admin_settings(
    body: AdminSettings,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
) -> AdminSettings:
    """Merges `body` into the full current state before writing, rather than
    replacing it outright — `AdminSettings` only models `index_snapshots`, but
    the on-disk state also carries `active_preset_override`/`remote_config`
    (backend.services.admin_settings_store), which a naive full-replace here
    would silently wipe every time this endpoint is used."""
    settings = get_settings()
    state = await asyncio.to_thread(read_admin_settings, settings.data_path)
    state.update(body.model_dump())
    await asyncio.to_thread(write_admin_settings, settings.data_path, state)
    return body
```

(`read_admin_settings` is already imported at the top of this file alongside `write_admin_settings` — no new import needed.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_admin_settings_store.py backend/tests/test_admin_settings_api.py -v`
Expected: PASS

- [ ] **Step 6: Run the full backend suite to confirm no regression**

Run: `uv run pytest backend/tests/ -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add backend/services/admin_settings_store.py backend/api/admin_settings.py backend/tests/test_admin_settings_store.py backend/tests/test_admin_settings_api.py
git commit -m "fix(backend): stop admin settings store from leaking defaults and PUT /api/admin/settings from wiping them"
```

---

## Task 2: Fix the `remote-mpcdf` preset to use shared dynamic fields

**Files:**
- Modify: `backend/config/presets.py:314-359`
- Modify: `backend/tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_config.py`, inside `class TestPresets(unittest.TestCase)`:

```python
    def test_get_preset_remote_mpcdf_uses_shared_dynamic_fields(self):
        """remote-mpcdf has no static base_url — both base_url and API key are
        resolved at request time from the shared admin-set store (see
        backend.services.admin_settings_store), not baked into the preset."""
        preset = get_preset("remote-mpcdf")

        self.assertEqual(preset.name, "remote-mpcdf")
        self.assertEqual(preset.embedding.model_type, "remote")
        self.assertNotIn("base_url", preset.embedding.model_kwargs)
        self.assertEqual(preset.embedding.model_kwargs["shared_base_url_env"], "MPCDF_EMBEDDING_BASE_URL")
        self.assertEqual(preset.embedding.model_kwargs["shared_api_key_env"], "MPCDF_EMBEDDING_API_KEY")
        self.assertEqual(preset.llm.model_type, "remote")
        self.assertNotIn("base_url", preset.llm.model_kwargs)
        self.assertEqual(preset.llm.model_kwargs["shared_base_url_env"], "MPCDF_LLM_BASE_URL")
        self.assertEqual(preset.llm.model_kwargs["shared_api_key_env"], "MPCDF_LLM_API_KEY")
        # Same embedding model as remote-kisski — same vector space, so the two
        # presets are mutually hot-swappable at runtime (see Task 6).
        self.assertEqual(preset.embedding.model_name, get_preset("remote-kisski").embedding.model_name)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest backend/tests/test_config.py -k remote_mpcdf -v`
Expected: FAIL — `AssertionError` on `assertNotIn("base_url", ...)` (the current preset has a literal `base_url` key) or `KeyError` on `model_kwargs["shared_base_url_env"]`.

- [ ] **Step 3: Fix the preset — surgical edits only, do not replace the whole block**

**Important:** the user has been hand-editing this exact `remote-mpcdf` block directly on this branch *while this plan was being written and executed* — at least two commits so far (`b3e6991` reworded the `description`; a later commit changed the embedding job's CLI-args comment from `--task embed` to `--runner pooling --convert embed`, reflecting something they learned running the real MPCDF job). Both are real, valuable, in-progress corrections. **Before touching this file, run `git log --oneline -5 -- backend/config/presets.py` and read the live current content of the `remote-mpcdf` block — do not assume it still matches any snapshot quoted in this plan.** If it's changed again, that's expected; don't revert it.

Make exactly these four targeted substitutions inside the `remote-mpcdf` block, and touch nothing else in it — not the `description` text, not any comment, not the CLI-args lines:

1. In the `embedding=EmbeddingConfig(...)` block's `model_kwargs`, find the line:
   ```python
                "base_url": os.environ.get("MPCDF_EMBEDDING_BASE_URL"),
   ```
   and replace it with:
   ```python
                "shared_base_url_env": "MPCDF_EMBEDDING_BASE_URL",
   ```

2. In the same `model_kwargs` dict, find the line:
   ```python
                "api_key_env": "MPCDF_API_KEY",
   ```
   (the one inside the `embedding=` block) and replace it with:
   ```python
                "shared_api_key_env": "MPCDF_EMBEDDING_API_KEY",
   ```

3. In the `llm=LLMConfig(...)` block's `model_kwargs`, find the line:
   ```python
                "base_url": os.environ.get("MPCDF_LLM_BASE_URL"),
   ```
   and replace it with:
   ```python
                "shared_base_url_env": "MPCDF_LLM_BASE_URL",
   ```

4. In the same `model_kwargs` dict, find the line:
   ```python
                "api_key_env": "MPCDF_API_KEY",
   ```
   (the one inside the `llm=` block — note both the embedding and llm blocks currently have an identically-worded `"api_key_env": "MPCDF_API_KEY",` line; make sure you change the LLM block's copy here, not a second edit to the embedding block's) and replace it with:
   ```python
                "shared_api_key_env": "MPCDF_LLM_API_KEY",
   ```

After these four edits, `grep -n "base_url\"\|api_key_env\"" backend/config/presets.py` within the `remote-mpcdf` block's line range should show zero remaining matches for the old `"base_url":`/`"api_key_env":` keys in that block (other presets like `remote-kisski` legitimately keep `"base_url"`/`"api_key_env"` — don't touch those).

(Two env vars changed name: `MPCDF_API_KEY` is now split into `MPCDF_EMBEDDING_API_KEY`/`MPCDF_LLM_API_KEY`, since MPCDF's embedding job and LLM job are separate Slurm jobs that each get their own generated key.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest backend/tests/test_config.py -k remote_mpcdf -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/config/presets.py backend/tests/test_config.py
git commit -m "fix(config): switch remote-mpcdf to shared dynamic base_url/api_key fields"
```

---

## Task 3: Runtime preset override in `Settings.get_hardware_preset()`

**Files:**
- Modify: `backend/config/settings.py:1-17,321-323`
- Modify: `backend/tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Add `import tempfile` to the top of `backend/tests/test_config.py` (alongside the existing `import os`), and add these three tests inside `class TestSettings(unittest.TestCase)`:

```python
    def test_get_hardware_preset_uses_active_preset_override_when_set(self):
        from backend.services.admin_settings_store import set_active_preset_override
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(model_preset="cpu-only", data_path=tmp)
            set_active_preset_override(settings.data_path, "remote-mpcdf")
            self.assertEqual(settings.get_hardware_preset().name, "remote-mpcdf")

    def test_get_hardware_preset_falls_back_to_model_preset_when_no_override_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(model_preset="cpu-only", data_path=tmp)
            self.assertEqual(settings.get_hardware_preset().name, "cpu-only")

    def test_get_hardware_preset_ignores_unknown_override(self):
        from backend.services.admin_settings_store import set_active_preset_override
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(model_preset="cpu-only", data_path=tmp)
            set_active_preset_override(settings.data_path, "no-such-preset")
            self.assertEqual(settings.get_hardware_preset().name, "cpu-only")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_config.py -k "active_preset_override or falls_back_to_model_preset" -v`
Expected: FAIL — `test_get_hardware_preset_uses_active_preset_override_when_set` fails because `get_hardware_preset()` still returns `"cpu-only"` (the override is ignored today).

- [ ] **Step 3: Implement the override**

In `backend/config/settings.py`, add a logger near the top (after the existing imports, before `class Settings`):

```python
import logging
```

Add this import line alongside the other `import` lines at the top of the file (after `import os`), and add directly below the existing imports block (after `from .presets import HardwarePreset, get_preset`):

```python
logger = logging.getLogger(__name__)
```

Then replace the existing `get_hardware_preset` method (currently):

```python
    def get_hardware_preset(self) -> HardwarePreset:
        """Get the configured hardware preset."""
        return get_preset(self.model_preset)
```

with:

```python
    def get_hardware_preset(self) -> HardwarePreset:
        """Get the configured hardware preset.

        An admin-set runtime override (backend.services.admin_settings_store,
        set via POST /api/config) takes precedence over MODEL_PRESET, letting
        an admin hot-swap between presets without a restart — see
        docs/superpowers/specs/2026-10-08-dynamic-remote-preset-config-design.md.
        An override naming an unknown preset (e.g. after a code change removes
        it) is ignored with a warning, falling back to MODEL_PRESET.
        """
        from backend.services.admin_settings_store import get_active_preset_override
        override = get_active_preset_override(self.data_path)
        if override:
            try:
                return get_preset(override)
            except ValueError:
                logger.warning(
                    "active_preset_override=%r is not a known preset; falling back to MODEL_PRESET=%r",
                    override, self.model_preset,
                )
        return get_preset(self.model_preset)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_config.py -v`
Expected: PASS (whole file — confirms nothing else regressed)

- [ ] **Step 5: Commit**

```bash
git add backend/config/settings.py backend/tests/test_config.py
git commit -m "feat(backend): let an admin-set runtime override pick the active hardware preset"
```

---

## Task 4: Shared-field resolution in `RemoteEmbeddingService`

**Files:**
- Modify: `backend/services/embeddings.py:180-183,391-428`
- Modify: `backend/tests/test_embeddings.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_embeddings.py`, inside `class TestRemoteEmbeddingService(unittest.IsolatedAsyncioTestCase)` (after `test_init`):

```python
    def test_required_client_fields_reports_kind_api_key_for_kisski_style_config(self):
        config = EmbeddingConfig(
            model_type="remote",
            model_name="multilingual-e5-large-instruct",
            model_kwargs={"base_url": "https://chat-ai.academiccloud.de/v1", "api_key_env": "KISSKI_API_KEY"},
        )
        fields = RemoteEmbeddingService.required_client_fields(config)
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0]["key_name"], "KISSKI_API_KEY")
        self.assertEqual(fields[0]["kind"], "api_key")

    def test_required_client_fields_reports_shared_kinds_for_mpcdf_style_config(self):
        config = EmbeddingConfig(
            model_type="remote",
            model_name="multilingual-e5-large-instruct",
            model_kwargs={
                "shared_base_url_env": "MPCDF_EMBEDDING_BASE_URL",
                "shared_api_key_env": "MPCDF_EMBEDDING_API_KEY",
            },
        )
        fields = RemoteEmbeddingService.required_client_fields(config)
        by_key = {f["key_name"]: f for f in fields}
        self.assertEqual(len(fields), 2)
        self.assertEqual(by_key["MPCDF_EMBEDDING_BASE_URL"]["kind"], "shared_base_url")
        self.assertEqual(by_key["MPCDF_EMBEDDING_API_KEY"]["kind"], "shared_api_key")

    @patch("openai.AsyncOpenAI")
    async def test_get_client_resolves_shared_fields_from_store_over_env(self, mock_openai_cls):
        import os
        import tempfile
        from pathlib import Path
        from backend.config.settings import get_settings, reset_settings
        from backend.services.admin_settings_store import update_remote_config

        reset_settings()
        self.addCleanup(reset_settings)
        with tempfile.TemporaryDirectory() as tmp:
            get_settings().data_path = Path(tmp)
            update_remote_config(get_settings().data_path, {
                "MPCDF_EMBEDDING_BASE_URL": "https://llm.mpcdf.mpg.de/abc123/v1",
                "MPCDF_EMBEDDING_API_KEY": "store-key",
            })
            with patch.dict(os.environ, {"MPCDF_EMBEDDING_BASE_URL": "https://should-not-be-used/v1"}):
                config = EmbeddingConfig(
                    model_type="remote",
                    model_name="multilingual-e5-large-instruct",
                    model_kwargs={
                        "shared_base_url_env": "MPCDF_EMBEDDING_BASE_URL",
                        "shared_api_key_env": "MPCDF_EMBEDDING_API_KEY",
                    },
                )
                service = RemoteEmbeddingService(config)
                service._get_client()

        _, kwargs = mock_openai_cls.call_args
        self.assertEqual(kwargs["base_url"], "https://llm.mpcdf.mpg.de/abc123/v1")
        self.assertEqual(kwargs["api_key"], "store-key")

    async def test_get_client_falls_back_to_env_when_shared_store_empty(self):
        import os
        import tempfile
        from pathlib import Path
        from backend.config.settings import get_settings, reset_settings

        reset_settings()
        self.addCleanup(reset_settings)
        with tempfile.TemporaryDirectory() as tmp:
            get_settings().data_path = Path(tmp)
            with patch.dict(os.environ, {
                "MPCDF_EMBEDDING_BASE_URL": "https://llm.mpcdf.mpg.de/from-env/v1",
                "MPCDF_EMBEDDING_API_KEY": "env-key",
            }):
                config = EmbeddingConfig(
                    model_type="remote",
                    model_name="multilingual-e5-large-instruct",
                    model_kwargs={
                        "shared_base_url_env": "MPCDF_EMBEDDING_BASE_URL",
                        "shared_api_key_env": "MPCDF_EMBEDDING_API_KEY",
                    },
                )
                service = RemoteEmbeddingService(config)
                with patch("openai.AsyncOpenAI") as mock_openai_cls:
                    service._get_client()
                    _, kwargs = mock_openai_cls.call_args
        self.assertEqual(kwargs["base_url"], "https://llm.mpcdf.mpg.de/from-env/v1")
        self.assertEqual(kwargs["api_key"], "env-key")

    async def test_get_client_raises_clear_error_when_shared_base_url_unset(self):
        from backend.config.settings import get_settings, reset_settings
        import tempfile
        from pathlib import Path

        reset_settings()
        self.addCleanup(reset_settings)
        with tempfile.TemporaryDirectory() as tmp:
            get_settings().data_path = Path(tmp)
            config = EmbeddingConfig(
                model_type="remote",
                model_name="multilingual-e5-large-instruct",
                model_kwargs={
                    "shared_base_url_env": "MPCDF_EMBEDDING_BASE_URL",
                    "shared_api_key_env": "MPCDF_EMBEDDING_API_KEY",
                },
            )
            service = RemoteEmbeddingService(service_config := config, api_key="explicit-key")
            with self.assertRaises(ValueError) as ctx:
                service._get_client()
        self.assertIn("MPCDF_EMBEDDING_BASE_URL", str(ctx.exception))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_embeddings.py -k "required_client_fields or shared" -v`
Expected: FAIL — `AttributeError: type object 'RemoteEmbeddingService' has no attribute 'required_client_fields'`

- [ ] **Step 3: Rename and generalize `required_api_keys`, add shared-field resolution**

In `backend/services/embeddings.py`, replace the base-class stub (currently lines 180-183):

```python
    @staticmethod
    def required_api_keys(config: "EmbeddingConfig") -> list[dict]:
        """Return API keys required by this service (empty for local services)."""
        return []
```

with:

```python
    @staticmethod
    def required_client_fields(config: "EmbeddingConfig") -> list[dict]:
        """Return the client-configurable fields required by this service (empty for local services)."""
        return []
```

Replace `RemoteEmbeddingService.required_api_keys` (currently lines 391-403):

```python
    @staticmethod
    def required_api_keys(config: EmbeddingConfig) -> list[dict]:
        """Return the API key required by this remote embedding service."""
        if config.model_type != "remote":
            return []
        api_key_env = config.model_kwargs.get("api_key_env", "OPENAI_API_KEY")
        return [{
            "key_name": api_key_env,
            "header_name": env_var_to_header(api_key_env),
            "description": f"API key for remote embeddings ({config.model_name})",
            "docs_url": docs_url_for_key(api_key_env),
            "required_for": ["indexing"],
        }]
```

with:

```python
    @staticmethod
    def required_client_fields(config: EmbeddingConfig) -> list[dict]:
        """Return the fields required by this remote embedding service: a
        personal API key (``api_key_env``, per-request header, unchanged), and/or
        a shared admin-set base_url/API key (``shared_base_url_env``/
        ``shared_api_key_env`` — see backend.services.admin_settings_store)."""
        if config.model_type != "remote":
            return []
        fields: list[dict] = []
        if "api_key_env" in config.model_kwargs:
            env_var = config.model_kwargs["api_key_env"]
            fields.append({
                "key_name": env_var, "header_name": env_var_to_header(env_var), "kind": "api_key",
                "description": f"API key for remote embeddings ({config.model_name})",
                "docs_url": docs_url_for_key(env_var), "required_for": ["indexing"],
            })
        elif "shared_api_key_env" not in config.model_kwargs:
            env_var = "OPENAI_API_KEY"
            fields.append({
                "key_name": env_var, "header_name": env_var_to_header(env_var), "kind": "api_key",
                "description": f"API key for remote embeddings ({config.model_name})",
                "docs_url": docs_url_for_key(env_var), "required_for": ["indexing"],
            })
        if "shared_base_url_env" in config.model_kwargs:
            env_var = config.model_kwargs["shared_base_url_env"]
            fields.append({
                "key_name": env_var, "header_name": env_var_to_header(env_var), "kind": "shared_base_url",
                "description": f"Shared endpoint URL for remote embeddings ({config.model_name})",
                "docs_url": docs_url_for_key(env_var), "required_for": ["indexing"],
            })
        if "shared_api_key_env" in config.model_kwargs:
            env_var = config.model_kwargs["shared_api_key_env"]
            fields.append({
                "key_name": env_var, "header_name": env_var_to_header(env_var), "kind": "shared_api_key",
                "description": f"Shared API key for remote embeddings ({config.model_name})",
                "docs_url": docs_url_for_key(env_var), "required_for": ["indexing"],
            })
        return fields
```

Replace `_get_client` (currently lines 405-428):

```python
    def _get_client(self):
        """Lazy-initialize the AsyncOpenAI client."""
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError:
                raise RuntimeError(
                    "openai package is required for remote embeddings. "
                    "Install it with: uv add openai"
                )
            api_key_env = self.config.model_kwargs.get("api_key_env", "OPENAI_API_KEY")
            api_key = self._api_key or os.getenv(api_key_env)
            if not api_key:
                raise ValueError(
                    f"API key not found. Set the {api_key_env} environment variable."
                )
            base_url = self.config.model_kwargs.get("base_url")
            if base_url:
                self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
                logger.debug(f"OpenAI-compatible client initialised with base_url={base_url}")
            else:
                self._client = AsyncOpenAI(api_key=api_key)
                logger.debug("OpenAI embeddings client initialised")
        return self._client
```

with:

```python
    def _get_client(self):
        """Lazy-initialize the AsyncOpenAI client."""
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError:
                raise RuntimeError(
                    "openai package is required for remote embeddings. "
                    "Install it with: uv add openai"
                )

            shared_url_env = self.config.model_kwargs.get("shared_base_url_env")
            shared_key_env = self.config.model_kwargs.get("shared_api_key_env")
            data_path = None
            if shared_url_env or shared_key_env:
                from backend.config.settings import get_settings
                from backend.services.admin_settings_store import get_remote_config_value
                data_path = get_settings().data_path

            if shared_key_env:
                api_key = self._api_key or get_remote_config_value(data_path, shared_key_env) or os.getenv(shared_key_env)
                if not api_key:
                    raise ValueError(
                        f"API key not configured. POST it to /api/config/remote-fields as "
                        f'{{"values": {{"{shared_key_env}": ...}}}}, or set the {shared_key_env} '
                        f"environment variable."
                    )
            else:
                api_key_env = self.config.model_kwargs.get("api_key_env", "OPENAI_API_KEY")
                api_key = self._api_key or os.getenv(api_key_env)
                if not api_key:
                    raise ValueError(
                        f"API key not found. Set the {api_key_env} environment variable."
                    )

            if shared_url_env:
                base_url = get_remote_config_value(data_path, shared_url_env) or os.getenv(shared_url_env)
                if not base_url:
                    raise ValueError(
                        f"Base URL not configured. POST it to /api/config/remote-fields as "
                        f'{{"values": {{"{shared_url_env}": ...}}}}, or set the {shared_url_env} '
                        f"environment variable."
                    )
            else:
                base_url = self.config.model_kwargs.get("base_url")

            if base_url:
                self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
                logger.debug(f"OpenAI-compatible client initialised with base_url={base_url}")
            else:
                self._client = AsyncOpenAI(api_key=api_key)
                logger.debug("OpenAI embeddings client initialised")
        return self._client
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_embeddings.py -v`
Expected: PASS (whole file — confirms nothing else regressed)

- [ ] **Step 5: Commit**

```bash
git add backend/services/embeddings.py backend/tests/test_embeddings.py
git commit -m "feat(backend): resolve shared base_url/api_key fields in RemoteEmbeddingService"
```

---

## Task 5: Shared-field resolution in `RemoteLLMService`

**Files:**
- Modify: `backend/services/llm.py:22-25,241-259,285-312,349-354`
- Modify: `backend/tests/test_llm.py`

- [ ] **Step 1: Write the failing tests**

Add to `backend/tests/test_llm.py`, inside `class TestRemoteLLMService(unittest.IsolatedAsyncioTestCase)` (after `test_init`):

```python
    async def test_required_client_fields_reports_shared_kinds_for_mpcdf_style_preset(self):
        mpcdf_preset = HardwarePreset(
            name="test-mpcdf",
            description="Test MPCDF preset",
            embedding=EmbeddingConfig(model_type="remote", model_name="multilingual-e5-large-instruct"),
            llm=LLMConfig(
                model_type="remote",
                model_names="openai/gpt-oss-120b",
                model_kwargs={
                    "shared_base_url_env": "MPCDF_LLM_BASE_URL",
                    "shared_api_key_env": "MPCDF_LLM_API_KEY",
                },
            ),
            rag=RAGConfig(),
            memory_budget_gb=0.5,
        )
        mock_settings = Mock(spec=Settings)
        mock_settings.get_hardware_preset.return_value = mpcdf_preset

        fields = RemoteLLMService.required_client_fields(mock_settings)
        by_key = {f["key_name"]: f for f in fields}
        self.assertEqual(len(fields), 2)
        self.assertEqual(by_key["MPCDF_LLM_BASE_URL"]["kind"], "shared_base_url")
        self.assertEqual(by_key["MPCDF_LLM_API_KEY"]["kind"], "shared_api_key")

    async def test_get_openai_client_resolves_shared_fields_from_store_over_env(self):
        import os
        import tempfile
        from pathlib import Path
        from backend.config.settings import get_settings, reset_settings
        from backend.services.admin_settings_store import update_remote_config

        reset_settings()
        self.addCleanup(reset_settings)
        with tempfile.TemporaryDirectory() as tmp:
            get_settings().data_path = Path(tmp)
            update_remote_config(get_settings().data_path, {
                "MPCDF_LLM_BASE_URL": "https://llm.mpcdf.mpg.de/abc123/v1",
                "MPCDF_LLM_API_KEY": "store-key",
            })
            mpcdf_preset = HardwarePreset(
                name="test-mpcdf",
                description="Test MPCDF preset",
                embedding=EmbeddingConfig(model_type="remote", model_name="multilingual-e5-large-instruct"),
                llm=LLMConfig(
                    model_type="remote",
                    model_names="openai/gpt-oss-120b",
                    model_kwargs={
                        "shared_base_url_env": "MPCDF_LLM_BASE_URL",
                        "shared_api_key_env": "MPCDF_LLM_API_KEY",
                    },
                ),
                rag=RAGConfig(),
                memory_budget_gb=0.5,
            )
            mock_settings = Mock(spec=Settings)
            mock_settings.get_hardware_preset.return_value = mpcdf_preset
            mock_settings.log_file = None

            with patch.dict(os.environ, {"MPCDF_LLM_BASE_URL": "https://should-not-be-used/v1"}):
                service = RemoteLLMService(mock_settings)
                with patch("openai.AsyncOpenAI") as mock_openai_cls:
                    service._get_openai_client()
                    _, kwargs = mock_openai_cls.call_args

        self.assertEqual(kwargs["base_url"], "https://llm.mpcdf.mpg.de/abc123/v1")
        self.assertEqual(kwargs["api_key"], "store-key")

    async def test_get_openai_client_raises_clear_error_when_shared_api_key_unset(self):
        import tempfile
        from pathlib import Path
        from backend.config.settings import get_settings, reset_settings

        reset_settings()
        self.addCleanup(reset_settings)
        with tempfile.TemporaryDirectory() as tmp:
            get_settings().data_path = Path(tmp)
            mpcdf_preset = HardwarePreset(
                name="test-mpcdf",
                description="Test MPCDF preset",
                embedding=EmbeddingConfig(model_type="remote", model_name="multilingual-e5-large-instruct"),
                llm=LLMConfig(
                    model_type="remote",
                    model_names="openai/gpt-oss-120b",
                    model_kwargs={
                        "shared_base_url_env": "MPCDF_LLM_BASE_URL",
                        "shared_api_key_env": "MPCDF_LLM_API_KEY",
                    },
                ),
                rag=RAGConfig(),
                memory_budget_gb=0.5,
            )
            mock_settings = Mock(spec=Settings)
            mock_settings.get_hardware_preset.return_value = mpcdf_preset
            mock_settings.log_file = None

            service = RemoteLLMService(mock_settings)
            with self.assertRaises(ValueError) as ctx:
                service._get_openai_client()
        self.assertIn("MPCDF_LLM_API_KEY", str(ctx.exception))
```

Check the top of `backend/tests/test_llm.py` already imports `HardwarePreset, EmbeddingConfig, LLMConfig, RAGConfig, Settings, Mock, patch` — if any are missing, add them to the existing import lines (don't duplicate).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_llm.py -k "required_client_fields or shared" -v`
Expected: FAIL — `AttributeError: type object 'RemoteLLMService' has no attribute 'required_client_fields'`

- [ ] **Step 3: Rename and generalize `required_api_keys`, add shared-field resolution**

In `backend/services/llm.py`, replace the base-class stub (currently lines 22-25):

```python
    @staticmethod
    def required_api_keys(settings: "Settings") -> list[dict]:
        """Return API keys required by this service (empty for local services)."""
        return []
```

with:

```python
    @staticmethod
    def required_client_fields(settings: "Settings") -> list[dict]:
        """Return the client-configurable fields required by this service (empty for local services)."""
        return []
```

Replace `RemoteLLMService.required_api_keys` (currently lines 241-259):

```python
    @staticmethod
    def required_api_keys(settings: Settings) -> list[dict]:
        """Return the API key required by this remote LLM service."""
        config = settings.get_hardware_preset().llm
        if config.model_type != "remote":
            return []
        if "api_key_env" in config.model_kwargs:
            api_key_env = config.model_kwargs["api_key_env"]
        elif "claude" in config.model_name.lower() or "anthropic" in config.model_name.lower():
            api_key_env = "ANTHROPIC_API_KEY"
        else:
            api_key_env = "OPENAI_API_KEY"
        return [{
            "key_name": api_key_env,
            "header_name": env_var_to_header(api_key_env),
            "description": f"API key for remote LLM ({config.model_name})",
            "docs_url": docs_url_for_key(api_key_env),
            "required_for": ["querying"],
        }]
```

with:

```python
    @staticmethod
    def required_client_fields(settings: Settings) -> list[dict]:
        """Return the fields required by this remote LLM service (see
        RemoteEmbeddingService.required_client_fields for the api_key/shared_* kinds)."""
        config = settings.get_hardware_preset().llm
        if config.model_type != "remote":
            return []
        fields: list[dict] = []
        if "api_key_env" in config.model_kwargs:
            env_var = config.model_kwargs["api_key_env"]
            fields.append({
                "key_name": env_var, "header_name": env_var_to_header(env_var), "kind": "api_key",
                "description": f"API key for remote LLM ({config.model_name})",
                "docs_url": docs_url_for_key(env_var), "required_for": ["querying"],
            })
        elif "shared_api_key_env" not in config.model_kwargs:
            env_var = "ANTHROPIC_API_KEY" if (
                "claude" in config.model_name.lower() or "anthropic" in config.model_name.lower()
            ) else "OPENAI_API_KEY"
            fields.append({
                "key_name": env_var, "header_name": env_var_to_header(env_var), "kind": "api_key",
                "description": f"API key for remote LLM ({config.model_name})",
                "docs_url": docs_url_for_key(env_var), "required_for": ["querying"],
            })
        if "shared_base_url_env" in config.model_kwargs:
            env_var = config.model_kwargs["shared_base_url_env"]
            fields.append({
                "key_name": env_var, "header_name": env_var_to_header(env_var), "kind": "shared_base_url",
                "description": f"Shared endpoint URL for remote LLM ({config.model_name})",
                "docs_url": docs_url_for_key(env_var), "required_for": ["querying"],
            })
        if "shared_api_key_env" in config.model_kwargs:
            env_var = config.model_kwargs["shared_api_key_env"]
            fields.append({
                "key_name": env_var, "header_name": env_var_to_header(env_var), "kind": "shared_api_key",
                "description": f"Shared API key for remote LLM ({config.model_name})",
                "docs_url": docs_url_for_key(env_var), "required_for": ["querying"],
            })
        return fields
```

Replace `_get_openai_client` (currently lines 285-312):

```python
    def _get_openai_client(self):
        """Lazy initialize OpenAI client."""
        if self._openai_client is None:
            try:
                from openai import AsyncOpenAI

                # Get API key from config or default environment variable
                api_key_env = self.llm_config.model_kwargs.get("api_key_env", "OPENAI_API_KEY")
                api_key = self.api_key or os.getenv(api_key_env)
                if not api_key:
                    raise ValueError(f"API key not found in environment variable: {api_key_env}")

                # Check for custom base URL (for OpenAI-compatible APIs)
                base_url = self.llm_config.model_kwargs.get("base_url")
                # Allow per-preset timeout override via model_kwargs; default 120 s
                timeout = float(self.llm_config.model_kwargs.get("timeout", 120))
                if base_url:
                    self._openai_client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
                    logger.info(f"Initialized OpenAI-compatible client with base_url: {base_url}, timeout: {timeout}s")
                else:
                    self._openai_client = AsyncOpenAI(api_key=api_key, timeout=timeout)
                    logger.info(f"Initialized OpenAI client, timeout: {timeout}s")

            except ImportError:
                logger.error("OpenAI package not installed. Install with: uv add openai")
                raise RuntimeError("Missing openai package")

        return self._openai_client
```

with:

```python
    def _get_openai_client(self):
        """Lazy initialize OpenAI client."""
        if self._openai_client is None:
            try:
                from openai import AsyncOpenAI

                shared_url_env = self.llm_config.model_kwargs.get("shared_base_url_env")
                shared_key_env = self.llm_config.model_kwargs.get("shared_api_key_env")
                data_path = None
                if shared_url_env or shared_key_env:
                    from backend.services.admin_settings_store import get_remote_config_value
                    data_path = self.settings.data_path

                if shared_key_env:
                    api_key = self.api_key or get_remote_config_value(data_path, shared_key_env) or os.getenv(shared_key_env)
                    if not api_key:
                        raise ValueError(
                            f"API key not configured. POST it to /api/config/remote-fields as "
                            f'{{"values": {{"{shared_key_env}": ...}}}}, or set the {shared_key_env} '
                            f"environment variable."
                        )
                else:
                    api_key_env = self.llm_config.model_kwargs.get("api_key_env", "OPENAI_API_KEY")
                    api_key = self.api_key or os.getenv(api_key_env)
                    if not api_key:
                        raise ValueError(f"API key not found in environment variable: {api_key_env}")

                if shared_url_env:
                    base_url = get_remote_config_value(data_path, shared_url_env) or os.getenv(shared_url_env)
                    if not base_url:
                        raise ValueError(
                            f"Base URL not configured. POST it to /api/config/remote-fields as "
                            f'{{"values": {{"{shared_url_env}": ...}}}}, or set the {shared_url_env} '
                            f"environment variable."
                        )
                else:
                    base_url = self.llm_config.model_kwargs.get("base_url")

                timeout = float(self.llm_config.model_kwargs.get("timeout", 120))
                if base_url:
                    self._openai_client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
                    logger.info(f"Initialized OpenAI-compatible client with base_url: {base_url}, timeout: {timeout}s")
                else:
                    self._openai_client = AsyncOpenAI(api_key=api_key, timeout=timeout)
                    logger.info(f"Initialized OpenAI client, timeout: {timeout}s")

            except ImportError:
                logger.error("OpenAI package not installed. Install with: uv add openai")
                raise RuntimeError("Missing openai package")

        return self._openai_client
```

Finally, in `generate()` (currently around line 351), replace:

```python
            has_base_url = "base_url" in self.llm_config.model_kwargs
```

with:

```python
            has_base_url = (
                "base_url" in self.llm_config.model_kwargs
                or "shared_base_url_env" in self.llm_config.model_kwargs
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_llm.py -v`
Expected: PASS (whole file)

- [ ] **Step 5: Commit**

```bash
git add backend/services/llm.py backend/tests/test_llm.py
git commit -m "feat(backend): resolve shared base_url/api_key fields in RemoteLLMService"
```

---

## Task 6: `/api/config` and `/api/config/remote-fields` endpoints

**Files:**
- Modify: `backend/api/config.py`
- Modify: `backend/dependencies.py:97-113`
- Modify: `backend/tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Add `import tempfile` to the top of `backend/tests/test_config.py` if Task 3 hasn't already (it has — skip if present). Append this new class at the end of the file, before the `if __name__ == "__main__":` block:

```python
class TestConfigApi(unittest.TestCase):
    """Endpoint tests for GET/POST /api/config and POST /api/config/remote-fields."""

    def setUp(self):
        from backend.main import app
        reset_settings()
        self.tmp = tempfile.TemporaryDirectory()
        s = get_settings()
        s.data_path = Path(self.tmp.name)
        s.model_preset = "remote-kisski"
        self.app = app
        from fastapi.testclient import TestClient
        self.client = TestClient(app)

    def tearDown(self):
        self.app.dependency_overrides.clear()
        self.tmp.cleanup()
        reset_settings()

    def _override_admin(self, identity):
        from backend.dependencies import require_authorized_group_admin
        self.app.dependency_overrides[require_authorized_group_admin] = lambda: identity

    def test_get_config_reflects_current_preset_name(self):
        r = self.client.get("/api/config")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["preset_name"], "remote-kisski")

    def test_get_config_lists_compatible_presets_for_remote_kisski(self):
        r = self.client.get("/api/config")
        compatible = set(r.json()["compatible_presets"])
        self.assertIn("remote-kisski", compatible)
        self.assertIn("remote-mpcdf", compatible)
        self.assertIn("windows-test", compatible)
        self.assertIn("apple-silicon-kisski", compatible)
        self.assertNotIn("remote-openai", compatible)  # different embedding model
        self.assertNotIn("cpu-only", compatible)  # local preset

    def test_post_config_requires_admin(self):
        from unittest.mock import AsyncMock
        from backend.services.zotero_identity import ZoteroIdentity
        get_settings().api_host = "rag.example.com"
        get_settings().authorized_group_id = 999
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=identity)), \
             patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=False)):
            r = self.client.post(
                "/api/config", json={"preset_name": "remote-mpcdf"},
                headers={"X-Zotero-API-Key": "K"},
            )
        self.assertEqual(r.status_code, 403)

    def test_post_config_switches_to_compatible_preset_as_admin(self):
        from backend.services.zotero_identity import ZoteroIdentity
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        r = self.client.post("/api/config", json={"preset_name": "remote-mpcdf"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["preset_name"], "remote-mpcdf")
        r2 = self.client.get("/api/config")
        self.assertEqual(r2.json()["preset_name"], "remote-mpcdf")

    def test_post_config_rejects_incompatible_preset_as_admin(self):
        from backend.services.zotero_identity import ZoteroIdentity
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        r = self.client.post("/api/config", json={"preset_name": "remote-openai"})
        self.assertEqual(r.status_code, 400)

    def test_post_config_rejects_unknown_preset_as_admin(self):
        from backend.services.zotero_identity import ZoteroIdentity
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        r = self.client.post("/api/config", json={"preset_name": "no-such-preset"})
        self.assertEqual(r.status_code, 400)

    def test_required_keys_reports_shared_kind_and_is_set_for_mpcdf(self):
        from backend.services.zotero_identity import ZoteroIdentity
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        self.client.post("/api/config", json={"preset_name": "remote-mpcdf"})
        r = self.client.get("/api/required-keys")
        self.assertEqual(r.status_code, 200)
        by_key = {k["key_name"]: k for k in r.json()["keys"]}
        self.assertEqual(by_key["MPCDF_EMBEDDING_BASE_URL"]["kind"], "shared_base_url")
        self.assertFalse(by_key["MPCDF_EMBEDDING_BASE_URL"]["is_set"])

    def test_remote_fields_requires_admin(self):
        from unittest.mock import AsyncMock
        from backend.services.zotero_identity import ZoteroIdentity
        get_settings().api_host = "rag.example.com"
        get_settings().authorized_group_id = 999
        identity = ZoteroIdentity(user_id=1, username="u", targets=["users/1"])
        with patch("backend.main.resolve_zotero_identity", new=AsyncMock(return_value=identity)), \
             patch("backend.zotero.group_roles.is_group_admin", new=AsyncMock(return_value=False)):
            r = self.client.post(
                "/api/config/remote-fields",
                json={"values": {"MPCDF_EMBEDDING_BASE_URL": "https://llm.mpcdf.mpg.de/abc/v1"}},
                headers={"X-Zotero-API-Key": "K"},
            )
        self.assertEqual(r.status_code, 403)

    def test_remote_fields_as_admin_sets_value_and_is_reflected_in_required_keys(self):
        from backend.services.zotero_identity import ZoteroIdentity
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        self.client.post("/api/config", json={"preset_name": "remote-mpcdf"})
        r = self.client.post(
            "/api/config/remote-fields",
            json={"values": {"MPCDF_EMBEDDING_BASE_URL": "https://llm.mpcdf.mpg.de/abc/v1"}},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["is_set"]["MPCDF_EMBEDDING_BASE_URL"])
        r2 = self.client.get("/api/required-keys")
        by_key = {k["key_name"]: k for k in r2.json()["keys"]}
        self.assertTrue(by_key["MPCDF_EMBEDDING_BASE_URL"]["is_set"])

    def test_remote_fields_rejects_unknown_key_as_admin(self):
        from backend.services.zotero_identity import ZoteroIdentity
        self._override_admin(ZoteroIdentity(user_id=1, username="admin", targets=["users/1"]))
        self.client.post("/api/config", json={"preset_name": "remote-mpcdf"})
        r = self.client.post(
            "/api/config/remote-fields",
            json={"values": {"NOT_A_REAL_FIELD": "x"}},
        )
        self.assertEqual(r.status_code, 400)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest backend/tests/test_config.py::TestConfigApi -v`
Expected: FAIL — `AttributeError: module 'backend.services.embeddings' has no attribute 'required_client_fields'`-style errors are already fixed by Tasks 4/5; here expect 404s (`/api/config/remote-fields` doesn't exist yet) and `AssertionError` on `compatible_presets` (field doesn't exist on `ConfigResponse` yet) and on `preset_name` not changing after POST (current endpoint is a no-op).

- [ ] **Step 3: Implement the endpoint changes**

Replace the full contents of `backend/api/config.py` with:

```python
"""
Configuration API endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Dict, List, Optional
import logging
import os

from backend.config.settings import get_settings
from backend.config.presets import PRESETS, HardwarePreset
from backend.dependencies import require_authorized_group_admin
from backend.services.admin_settings_store import (
    set_active_preset_override,
    update_remote_config,
    get_remote_config_value,
)
from backend.services.embeddings import RemoteEmbeddingService, env_var_to_header
from backend.services.llm import RemoteLLMService
from backend.services.zotero_identity import ZoteroIdentity
from backend.utils.kisski import fetch_kisski_rag_models

router = APIRouter()
logger = logging.getLogger(__name__)


def _compatible_presets(current: HardwarePreset) -> List[str]:
    """Presets safe to switch to at runtime without a restart: both the
    embedding and LLM must be remote (no local model to load/unload), and
    the embedding model must match exactly — same model means the same
    vector space, so the already-open VectorStore singleton stays valid.
    """
    if current.embedding.model_type != "remote" or current.llm.model_type != "remote":
        return [current.name]
    return [
        name for name, preset in PRESETS.items()
        if preset.embedding.model_type == "remote"
        and preset.llm.model_type == "remote"
        and preset.embedding.model_name == current.embedding.model_name
    ]


class ConfigResponse(BaseModel):
    """Current configuration response."""
    preset_name: str
    preset_description: str
    api_version: str
    embedding_model: str
    embedding_model_type: str  # "local" | "remote"
    llm_model: str  # default (first) model name — kept for backward compatibility
    llm_models: List[str]  # all model names for the active preset
    vector_db_path: str
    model_cache_dir: str
    available_presets: List[str]
    compatible_presets: List[str]
    # RAG configuration
    default_top_k: int
    default_min_score: float
    max_chunk_size: int


class ApiKeyRequirement(BaseModel):
    """A single client-configurable field required by the active preset."""
    key_name: str
    header_name: str
    kind: str = "api_key"  # "api_key" (personal, per-request header) | "shared_base_url" | "shared_api_key" (admin-set, global)
    description: str
    docs_url: Optional[str] = None
    required_for: List[str]
    is_set: Optional[bool] = None  # only meaningful for shared_* kinds — never exposes the value itself


class RequiredKeysResponse(BaseModel):
    """List of client-configurable fields required by the active preset."""
    keys: List[ApiKeyRequirement]


class ConfigUpdateRequest(BaseModel):
    """Configuration update request."""
    preset_name: Optional[str] = None
    vector_db_path: Optional[str] = None
    model_cache_dir: Optional[str] = None
    embedding_api_key: Optional[str] = None
    llm_api_key: Optional[str] = None


class RemoteFieldsUpdateRequest(BaseModel):
    """Body for POST /api/config/remote-fields."""
    values: Dict[str, str]


class RemoteFieldsResponse(BaseModel):
    """Presence map after an update — never echoes the values themselves."""
    is_set: Dict[str, bool]


@router.get("/config", response_model=ConfigResponse)
def get_config(request: Request):
    """
    Get current configuration and available presets.

    For presets with a live models endpoint (e.g. KISSKI), the LLM model list is
    fetched dynamically and ordered by availability. Falls back to the preset's
    static model list if the live fetch fails.

    Returns:
        Current configuration including active preset and model settings.
    """
    settings = get_settings()
    preset = settings.get_hardware_preset()

    # Try to fetch a live, ordered model list for presets that support it
    llm_models = preset.llm.model_names
    if preset.llm.models_status_url:
        api_key_env = preset.llm.model_kwargs.get("api_key_env", "")
        base_url = preset.llm.model_kwargs.get("base_url", "")
        if api_key_env and base_url:
            header_name = env_var_to_header(api_key_env)
            api_key = (
                request.headers.get(header_name)
                or os.environ.get(api_key_env)
                or ""
            )
            if api_key:
                try:
                    live_models = fetch_kisski_rag_models(base_url, api_key)
                    if live_models:
                        llm_models = [m.id for m in live_models]
                except Exception as exc:
                    logger.warning(
                        "Could not fetch live models from %s: %s — using preset fallback",
                        base_url, exc,
                    )

    return ConfigResponse(
        preset_name=preset.name,
        preset_description=preset.description,
        api_version=settings.version,
        embedding_model=preset.embedding.model_name,
        embedding_model_type=preset.embedding.model_type,
        llm_model=llm_models[0] if llm_models else preset.llm.model_name,
        llm_models=llm_models,
        vector_db_path=str(settings.vector_db_path),
        model_cache_dir=str(settings.model_weights_path),
        available_presets=list(PRESETS.keys()),
        compatible_presets=_compatible_presets(preset),
        # RAG configuration from preset
        default_top_k=preset.rag.top_k,
        default_min_score=preset.rag.score_threshold,
        max_chunk_size=preset.rag.max_chunk_size
    )


@router.post("/config", response_model=ConfigResponse)
async def update_config(
    update: ConfigUpdateRequest,
    request: Request,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
):
    """
    Switch the active hardware preset at runtime (admin only).

    Takes effect immediately for every subsequent request, including the
    cron auto-indexer — no backend restart needed. Only presets compatible
    with the currently active one (see `compatible_presets` on GET /api/config)
    can be switched to; anything else (a different embedding model, or a
    local-model preset) still requires MODEL_PRESET + a restart, since it
    would invalidate the already-open vector store or require loading a
    local model into memory.

    Args:
        update: Must set `preset_name`. Other fields are accepted but
            ignored — this backend has no other runtime-mutable config.

    Raises:
        HTTPException: 400 if `preset_name` is missing, unknown, or not in
            the current `compatible_presets` list.
    """
    settings = get_settings()

    if not update.preset_name:
        raise HTTPException(status_code=400, detail="preset_name is required.")
    if update.preset_name not in PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid preset: {update.preset_name}. Available: {list(PRESETS.keys())}",
        )

    current = settings.get_hardware_preset()
    compatible = _compatible_presets(current)
    if update.preset_name not in compatible:
        raise HTTPException(
            status_code=400,
            detail=f"Preset '{update.preset_name}' cannot be switched to at runtime from "
                   f"'{current.name}' (requires the same remote embedding model). "
                   f"Compatible presets: {compatible}. To use a different embedding model or a "
                   f"local-model preset, set MODEL_PRESET and restart the backend instead.",
        )

    set_active_preset_override(settings.data_path, update.preset_name)
    return get_config(request)


@router.post("/config/remote-fields", response_model=RemoteFieldsResponse)
async def set_remote_fields(
    update: RemoteFieldsUpdateRequest,
    identity: Optional[ZoteroIdentity] = Depends(require_authorized_group_admin),
) -> RemoteFieldsResponse:
    """
    Set one or more shared, admin-controlled remote-config values (a
    preset's `shared_base_url_env`/`shared_api_key_env` fields — see
    backend.services.admin_settings_store) for the currently active
    preset. Admin-gated: this is global state, visible to every caller
    and to the cron auto-indexer, not a per-user setting.

    Raises:
        HTTPException: 400 if any key in `values` isn't declared by the
            active preset's embedding/LLM config.
    """
    settings = get_settings()
    preset = settings.get_hardware_preset()

    allowed_keys: set = set()
    for key_info in RemoteEmbeddingService.required_client_fields(preset.embedding):
        if key_info["kind"] in ("shared_base_url", "shared_api_key"):
            allowed_keys.add(key_info["key_name"])
    for key_info in RemoteLLMService.required_client_fields(settings):
        if key_info["kind"] in ("shared_base_url", "shared_api_key"):
            allowed_keys.add(key_info["key_name"])

    unknown = set(update.values.keys()) - allowed_keys
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown remote-config key(s) for preset '{preset.name}': {sorted(unknown)}. "
                   f"Allowed: {sorted(allowed_keys)}",
        )

    merged = update_remote_config(settings.data_path, update.values)
    return RemoteFieldsResponse(is_set={k: bool(v) for k, v in merged.items()})


@router.get("/required-keys", response_model=RequiredKeysResponse)
async def get_required_api_keys():
    """
    List the client-configurable fields required by the current backend preset.

    `kind="api_key"` entries are personal: the client should send each in the
    specified HTTP header on indexing/querying requests, overriding any
    server-side environment variable with the same name.

    `kind="shared_base_url"`/`"shared_api_key"` entries are global, admin-set
    values (e.g. the MPCDF preset's rotating job endpoint/key) — set them via
    POST /api/config/remote-fields, not a per-request header. `is_set`
    reports whether a value is already available (from the shared store or
    an environment variable), without ever exposing the value itself.
    """
    settings = get_settings()
    preset = settings.get_hardware_preset()

    seen: Dict[str, ApiKeyRequirement] = {}

    def _merge(key_info: dict) -> None:
        key_name = key_info["key_name"]
        if key_name in seen:
            seen[key_name].required_for = list(
                set(seen[key_name].required_for) | set(key_info["required_for"])
            )
            return
        is_set = None
        if key_info["kind"] in ("shared_base_url", "shared_api_key"):
            is_set = bool(
                get_remote_config_value(settings.data_path, key_name) or os.environ.get(key_name)
            )
        seen[key_name] = ApiKeyRequirement(**key_info, is_set=is_set)

    for key_info in RemoteEmbeddingService.required_client_fields(preset.embedding):
        _merge(key_info)
    for key_info in RemoteLLMService.required_client_fields(settings):
        _merge(key_info)

    return RequiredKeysResponse(keys=list(seen.values()))


class ModelStatus(BaseModel):
    """Availability status for a single remote model."""
    model: str
    demand: int
    status: str


class ModelsStatusResponse(BaseModel):
    """Per-model availability metrics for the active preset."""
    models: List[ModelStatus]


@router.get("/models/status", response_model=ModelsStatusResponse)
def get_models_status(request: Request):
    """
    Return per-model demand/availability metrics for the active preset.

    Fetches the live model list from the configured ``models_status_url``,
    filters to RAG-suitable models, and returns them ordered by demand
    (most available first).  Returns an empty list if the preset has no
    ``models_status_url`` or if the upstream call fails.

    The ``status`` field contains a human-readable availability label:
    ``"available"`` (demand 0), ``"busy"`` (1–5), or ``"very busy"`` (6+).
    """
    settings = get_settings()
    preset = settings.get_hardware_preset()

    if not preset.llm.models_status_url:
        return ModelsStatusResponse(models=[])

    api_key_env = preset.llm.model_kwargs.get("api_key_env", "")
    base_url = preset.llm.model_kwargs.get("base_url", "")
    if not base_url:
        return ModelsStatusResponse(models=[])

    header_name = env_var_to_header(api_key_env) if api_key_env else ""
    api_key = (
        (request.headers.get(header_name) if header_name else None)
        or (os.environ.get(api_key_env) if api_key_env else None)
        or ""
    )

    try:
        live_models = fetch_kisski_rag_models(base_url, api_key)
    except Exception as exc:
        logger.warning("Could not fetch model status from %s: %s", preset.llm.models_status_url, exc)
        return ModelsStatusResponse(models=[])

    return ModelsStatusResponse(models=[
        ModelStatus(model=m.id, demand=m.demand, status=m.availability)
        for m in live_models
    ])


@router.get("/version")
async def get_version():
    """
    Get backend API version.

    Used by Zotero plugin to check compatibility.

    Returns:
        API version information.
    """
    settings = get_settings()
    return {
        "api_version": settings.version,
        "service": "Zotero RAG API"
    }
```

Note the two behavior changes from the previous file, both intentional:
- `get_config` now derives `preset` via `settings.get_hardware_preset()` instead of `PRESETS[settings.model_preset]` — the old code bypassed any runtime override entirely.
- `update_config`/`POST /api/config` is no longer a documented no-op; it's admin-gated and actually switches the preset.

- [ ] **Step 4: Update `get_client_api_keys` in `dependencies.py` to use the renamed method and skip shared fields**

In `backend/dependencies.py`, replace `get_client_api_keys` (currently lines 97-113):

```python
def get_client_api_keys(request: Request) -> dict[str, str]:
    """Extract client-supplied API keys from request headers."""
    settings = get_settings()
    preset = settings.get_hardware_preset()
    keys: dict[str, str] = {}
    for key_info in RemoteEmbeddingService.required_api_keys(preset.embedding):
        val = request.headers.get(key_info["header_name"])
        logger.debug("DEBUG header %s: %s", key_info["header_name"], "present" if val else "absent/empty")  # DEBUG
        if val:
            keys[key_info["key_name"]] = val
    for key_info in RemoteLLMService.required_api_keys(settings):
        val = request.headers.get(key_info["header_name"])
        logger.debug("DEBUG header %s: %s", key_info["header_name"], "present" if val else "absent/empty")  # DEBUG
        if val:
            keys[key_info["key_name"]] = val
    logger.debug("DEBUG client_api_keys resolved: %s", list(keys.keys()))  # DEBUG
    return keys
```

with:

```python
def get_client_api_keys(request: Request) -> dict[str, str]:
    """Extract client-supplied *personal* API keys from request headers.

    Only `kind == "api_key"` fields are read from headers — a `shared_base_url`/
    `shared_api_key` field (e.g. remote-mpcdf) is deliberately never read from a
    per-request header; it's admin-set globally via POST /api/config/remote-fields
    and resolved server-side (backend.services.admin_settings_store), the same for
    every caller and for the cron indexer.
    """
    settings = get_settings()
    preset = settings.get_hardware_preset()
    keys: dict[str, str] = {}
    for key_info in RemoteEmbeddingService.required_client_fields(preset.embedding):
        if key_info["kind"] != "api_key":
            continue
        val = request.headers.get(key_info["header_name"])
        if val:
            keys[key_info["key_name"]] = val
    for key_info in RemoteLLMService.required_client_fields(settings):
        if key_info["kind"] != "api_key":
            continue
        val = request.headers.get(key_info["header_name"])
        if val:
            keys[key_info["key_name"]] = val
    return keys
```

(This also drops three leftover `# DEBUG` log lines from a prior investigation, per this project's convention of removing code marked `# DEBUG` once it's no longer needed.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest backend/tests/test_config.py backend/tests/ -v`
Expected: PASS — run the *whole* `backend/tests/` suite here (not just `test_config.py`), since this task renames a method (`required_api_keys` → `required_client_fields`) that other test files or modules may still reference.

- [ ] **Step 6: Grep for any remaining references to the old method name**

Run: `grep -rn "required_api_keys" backend/ plugin/ docs/ --include=*.py --include=*.js`
Expected: no output (every call site was updated in Tasks 4-6). If anything shows up, update it before continuing.

- [ ] **Step 7: Commit**

```bash
git add backend/api/config.py backend/dependencies.py backend/tests/test_config.py
git commit -m "feat(backend): add POST /api/config/remote-fields and runtime preset switching"
```

---

## Task 7: Plugin — render shared fields in `renderServiceApiKeyFields`, add `setSharedRemoteField`

**Files:**
- Modify: `plugin/src/zotero-rag.js:682-764`

- [ ] **Step 1: Add `setSharedRemoteField`**

In `plugin/src/zotero-rag.js`, directly after the closing brace of `fetchRequiredApiKeys()` (currently ending at line 680, right before the `renderServiceApiKeyFields` JSDoc block), insert:

```javascript
	/**
	 * Persist one shared, admin-controlled remote-config value (a preset's
	 * `shared_base_url_env`/`shared_api_key_env` field, e.g. for the MPCDF
	 * preset's rotating job endpoint/key) to the backend. Unlike a personal
	 * API key, this is never stored in a local pref or sent as a per-request
	 * header — it's global state shared by every caller and the cron
	 * auto-indexer. Admin-gated server-side (require_authorized_group_admin).
	 * @param {string} keyName
	 * @param {string} value
	 * @returns {Promise<{ok: boolean, is_set?: boolean, error?: string}>}
	 */
	async setSharedRemoteField(keyName, value) {
		try {
			const response = await fetch(`${this.backendURL}/api/config/remote-fields`, {
				method: 'POST',
				headers: this.getAuthHeaders({ 'Content-Type': 'application/json' }),
				body: JSON.stringify({ values: { [keyName]: value } }),
			});
			if (!response.ok) {
				const err = await response.json().catch(() => ({}));
				return { ok: false, error: err.detail || `HTTP ${response.status}` };
			}
			/** @type {{is_set: Record<string, boolean>}} */
			const data = await response.json();
			return { ok: true, is_set: data.is_set ? data.is_set[keyName] : undefined };
		} catch (e) {
			return { ok: false, error: String(e) };
		}
	}

```

- [ ] **Step 2: Branch `renderServiceApiKeyFields` on `kind`**

Replace the full body of `renderServiceApiKeyFields` (currently lines 699-764, from `renderServiceApiKeyFields(doc, container, placeholder, requiredKeys, onKeyChange) {` through its closing `}`) with:

```javascript
	/**
	 * Render service field input rows into `container`, one row + description
	 * per required field, as reported by the backend's `/api/required-keys`.
	 * Shared between the Preferences pane and the setup wizard so both stay in sync.
	 *
	 * `kind: "api_key"` fields are personal: each binds to
	 * `extensions.zotero-rag.serviceApiKey.<key_name>` and is sent as a
	 * per-request header (unchanged behavior). `kind: "shared_base_url"`/
	 * `"shared_api_key"` fields are global, admin-set values (e.g. the MPCDF
	 * preset's rotating job endpoint/key) — they are never stored in a local
	 * pref; on change they POST to `/api/config/remote-fields` via
	 * `setSharedRemoteField` instead, and the input is cleared back to a
	 * placeholder afterward rather than echoing the value back.
	 *
	 * Note: generated "Get key" links use `target="_blank"`, which is a no-op in a
	 * privileged Zotero document. The caller must provide its own delegated
	 * `a[href]` → `Zotero.launchURL` click handler over (or containing) `container`
	 * so these links actually open in the system browser — see the click handler
	 * on `#zotero-rag-prefs-container` in `preferences.js` for the pattern.
	 * @param {Document} doc - Document to create elements in (the Preferences pane document, or a dialog document)
	 * @param {HTMLElement} container - Element to render rows into (existing dynamic rows are cleared first)
	 * @param {HTMLElement|null} placeholder - Shown/hidden depending on whether requiredKeys is empty
	 * @param {Array<{key_name: string, header_name: string, kind?: string, description: string, docs_url?: string|null, required_for: string[], is_set?: boolean|null}>} requiredKeys
	 * @param {(keyInfo: {key_name: string, header_name: string, kind?: string, description: string, docs_url?: string|null, required_for: string[], is_set?: boolean|null}, value: string) => void} [onKeyChange] - Optional callback invoked after a *personal* ("api_key") field's pref is set, e.g. to re-sync a server-stored copy. Never called for shared_* fields.
	 * @returns {void}
	 */
	renderServiceApiKeyFields(doc, container, placeholder, requiredKeys, onKeyChange) {
		if (!container) return;

		container.querySelectorAll('.service-key-row, .service-key-desc, .service-key-status').forEach(el => el.remove());

		if (!requiredKeys || requiredKeys.length === 0) {
			if (placeholder) placeholder.style.display = '';
			return;
		}
		if (placeholder) placeholder.style.display = 'none';

		for (const keyInfo of requiredKeys) {
			const isShared = keyInfo.kind === 'shared_base_url' || keyInfo.kind === 'shared_api_key';
			const prefKey = `extensions.zotero-rag.serviceApiKey.${keyInfo.key_name}`;
			const storedValue = isShared ? '' : (Zotero.Prefs.get(prefKey, true) || '');

			const row = doc.createElementNS('http://www.w3.org/1999/xhtml', 'div');
			row.className = 'setting-row service-key-row';

			const label = doc.createElementNS('http://www.w3.org/1999/xhtml', 'label');
			label.textContent = `${keyInfo.key_name}:`;
			label.setAttribute('for', `zotero-rag-key-${keyInfo.key_name}`);

			const input = /** @type {HTMLInputElement} */ (doc.createElementNS('http://www.w3.org/1999/xhtml', 'input'));
			input.type = keyInfo.kind === 'shared_base_url' ? 'text' : 'password';
			input.id = `zotero-rag-key-${keyInfo.key_name}`;
			input.className = 'setting-input';
			input.value = storedValue;
			input.placeholder = isShared
				? (keyInfo.is_set ? 'Configured — enter a new value to replace it' : 'Not yet configured')
				: 'Enter API key';

			const status = doc.createElementNS('http://www.w3.org/1999/xhtml', 'span');
			status.className = 'service-key-status';
			status.id = `zotero-rag-key-status-${keyInfo.key_name}`;

			input.addEventListener('change', async (e) => {
				const value = /** @type {HTMLInputElement} */ (e.target).value;
				if (isShared) {
					if (!value) return;
					status.textContent = 'Saving…';
					const result = await this.setSharedRemoteField(keyInfo.key_name, value);
					status.textContent = result.ok ? 'Saved.' : `Error: ${result.error}`;
					if (result.ok) {
						/** @type {HTMLInputElement} */ (e.target).value = '';
						/** @type {HTMLInputElement} */ (e.target).placeholder = 'Configured — enter a new value to replace it';
					}
				} else {
					Zotero.Prefs.set(prefKey, value, true);
					if (typeof onKeyChange === 'function') {
						onKeyChange(keyInfo, value);
					}
				}
			});

			row.appendChild(label);
			row.appendChild(input);
			row.appendChild(status);
			container.appendChild(row);

			if (keyInfo.description) {
				const desc = doc.createElementNS('http://www.w3.org/1999/xhtml', 'div');
				desc.className = 'setting-description service-key-desc';
				const sharedNote = isShared ? ' (shared — admin only, affects every user and the cron indexer)' : '';
				desc.textContent = `${keyInfo.description}${sharedNote} (used for: ${keyInfo.required_for.join(', ')})`;
				if (keyInfo.docs_url && /^https?:\/\//i.test(keyInfo.docs_url)) {
					desc.appendChild(doc.createTextNode(' '));
					const link = doc.createElementNS('http://www.w3.org/1999/xhtml', 'a');
					link.setAttribute('href', keyInfo.docs_url);
					link.setAttribute('target', '_blank');
					link.textContent = 'Get key';
					desc.appendChild(link);
				}
				container.appendChild(desc);
			}
		}
	}
```

- [ ] **Step 3: Sanity-check the file still parses**

Run: `node --check plugin/src/zotero-rag.js`
Expected: no output (exit code 0)

- [ ] **Step 4: Commit**

```bash
git add plugin/src/zotero-rag.js
git commit -m "feat(plugin): render shared base_url/api_key fields, add setSharedRemoteField"
```

---

## Task 8: Plugin — "Active Model Preset" fieldset in Preferences

**Files:**
- Modify: `plugin/src/preferences.xhtml:48-58`

- [ ] **Step 1: Insert the new fieldset**

In `plugin/src/preferences.xhtml`, between the closing `</html:fieldset>` of "Backend Server" (currently line 48) and the `<!-- Service API Keys -->` comment (currently line 50), insert:

```xml

  <!-- Active Model Preset (admin only; populated from the backend's compatible_presets) -->
  <html:fieldset class="settings-group" id="zotero-rag-preset-group">
    <html:legend>Active Model Preset</html:legend>
    <html:div class="setting-row">
      <html:label for="zotero-rag-preset-select">Preset:</html:label>
      <html:select id="zotero-rag-preset-select" class="setting-input">
        <html:option value="">(not connected)</html:option>
      </html:select>
    </html:div>
    <html:div class="setting-description" id="zotero-rag-preset-description"></html:div>
    <html:div class="setting-description" id="zotero-rag-preset-status"></html:div>
  </html:fieldset>
```

- [ ] **Step 2: Sanity-check the file is well-formed XML**

Run: `node -e "require('node:child_process').execSync('xmllint --noout plugin/src/preferences.xhtml')"`

If `xmllint` isn't installed, instead visually confirm every opened tag in the new block is closed (it is, above) and that the block sits at the same indentation level as its sibling `<html:fieldset>` blocks.

- [ ] **Step 3: Commit**

```bash
git add plugin/src/preferences.xhtml
git commit -m "feat(plugin): add Active Model Preset dropdown to Preferences"
```

---

## Task 9: Plugin — wire the preset dropdown in `preferences.js`

**Files:**
- Modify: `plugin/src/preferences.js` (insert after line 269, the end of the existing required-keys fetch block)

- [ ] **Step 1: Insert the preset dropdown wiring**

In `plugin/src/preferences.js`, directly after this existing block (ending at line 269):

```javascript
	// Refresh from server in background and re-render if the list has changed
	this.fetchRequiredApiKeys().then(() =>
		this.renderServiceApiKeyFields(doc, serviceKeysContainer, serviceKeysPlaceholder, this.requiredApiKeys, onServiceKeyChange)
	);
```

insert:

```javascript

	// Active preset dropdown (admin only — a non-admin or non-loopback caller gets
	// a 403/400 from POST /api/config and refreshPresetState() reverts the select).
	const presetSelect = doc.getElementById('zotero-rag-preset-select');
	const presetDescription = doc.getElementById('zotero-rag-preset-description');
	const presetStatus = doc.getElementById('zotero-rag-preset-status');

	/**
	 * Re-fetch GET /api/config and repopulate the preset dropdown from
	 * `compatible_presets`, selecting the currently active one.
	 * @returns {Promise<void>}
	 */
	const refreshPresetState = async () => {
		if (!presetSelect) return;
		try {
			const response = await fetch(`${this.backendURL}/api/config`, { headers: this.getAuthHeaders() });
			if (!response.ok) return;
			/** @type {{preset_name: string, preset_description: string, compatible_presets: string[]}} */
			const data = await response.json();
			presetSelect.innerHTML = '';
			for (const name of data.compatible_presets) {
				const option = doc.createElementNS('http://www.w3.org/1999/xhtml', 'option');
				option.value = name;
				option.textContent = name;
				presetSelect.appendChild(option);
			}
			presetSelect.value = data.preset_name;
			if (presetDescription) presetDescription.textContent = data.preset_description || '';
		} catch (e) {
			this.log('Could not fetch preset config: ' + e);
		}
	};

	if (presetSelect) {
		presetSelect.addEventListener('change', async (e) => {
			const selected = /** @type {HTMLSelectElement} */ (e.target).value;
			if (presetStatus) presetStatus.textContent = 'Switching…';
			try {
				const response = await fetch(`${this.backendURL}/api/config`, {
					method: 'POST',
					headers: this.getAuthHeaders({ 'Content-Type': 'application/json' }),
					body: JSON.stringify({ preset_name: selected }),
				});
				if (!response.ok) {
					const err = await response.json().catch(() => ({}));
					if (presetStatus) presetStatus.textContent = `Error: ${err.detail || response.status}`;
					await refreshPresetState(); // revert the select to the still-active preset
					return;
				}
				if (presetStatus) presetStatus.textContent = 'Switched.';
				await refreshPresetState();
				// The new preset likely needs different dynamic fields filled in right away.
				await this.fetchRequiredApiKeys();
				this.renderServiceApiKeyFields(doc, serviceKeysContainer, serviceKeysPlaceholder, this.requiredApiKeys, onServiceKeyChange);
			} catch (e) {
				if (presetStatus) presetStatus.textContent = `Error: ${e}`;
			}
		});
		refreshPresetState();
	}
```

- [ ] **Step 2: Sanity-check the file still parses**

Run: `node --check plugin/src/preferences.js`
Expected: no output (exit code 0)

- [ ] **Step 3: Commit**

```bash
git add plugin/src/preferences.js
git commit -m "feat(plugin): wire the Active Model Preset dropdown to POST /api/config"
```

---

## Task 10: Plugin tests for the new renderer branch and `setSharedRemoteField`

**Files:**
- Modify: `plugin/test/zotero-rag.test.js`

- [ ] **Step 1: Add fake-DOM helpers and the new tests**

Append to `plugin/test/zotero-rag.test.js` (at the end of the file):

```javascript

// --- Tests for the shared_base_url/shared_api_key branch of renderServiceApiKeyFields ---

/**
 * Minimal fake element sufficient for renderServiceApiKeyFields: tracks the
 * handful of properties/methods it touches (className, textContent, type,
 * value, placeholder, setAttribute, addEventListener, appendChild) plus a
 * `dispatchChange` test helper that simulates a 'change' event.
 * @returns {any}
 */
function makeFakeElement() {
	const listeners = {};
	return {
		className: '', textContent: '', id: '', type: undefined, value: '', placeholder: '',
		children: [],
		setAttribute() {},
		appendChild(child) { this.children.push(child); },
		addEventListener(event, handler) { listeners[event] = handler; },
		dispatchChange(value) {
			this.value = value;
			if (listeners.change) return listeners.change({ target: this });
		},
	};
}

/** @returns {any} a fake `doc` whose createElementNS always returns a fresh fake element */
function makeFakeDoc() {
	return { createElementNS: () => makeFakeElement(), createTextNode: () => makeFakeElement() };
}

/** @returns {any} a fake container tracking every element appended to it */
function makeFakeContainer() {
	const appended = [];
	return {
		appended,
		querySelectorAll: () => ({ forEach: () => {} }),
		appendChild(el) { appended.push(el); },
	};
}

/**
 * Find the <input>-equivalent fake element among a row's children: the only
 * child whose `.type` was explicitly set to 'text' or 'password' (the label
 * and status span never set `.type`, so it stays `undefined`).
 * @param {any} row
 * @returns {any}
 */
function findInputChild(row) {
	return row.children.find(el => el.type === 'text' || el.type === 'password');
}

test('renderServiceApiKeyFields renders a shared_base_url field as a text input that POSTs via setSharedRemoteField instead of writing a local pref', async () => {
	const prefs = {};
	const zotero = { Prefs: { get: (k) => prefs[k], set: (k, v) => { prefs[k] = v; } } };
	const plugin = loadPlugin(zotero, {}, {});
	let posted = null;
	plugin.setSharedRemoteField = async (keyName, value) => {
		posted = { keyName, value };
		return { ok: true, is_set: true };
	};

	const doc = makeFakeDoc();
	const container = makeFakeContainer();
	const requiredKeys = [{
		key_name: 'MPCDF_EMBEDDING_BASE_URL',
		header_name: 'X-Mpcdf-Embedding-Base-Url',
		kind: 'shared_base_url',
		description: 'Shared endpoint URL',
		docs_url: null,
		required_for: ['indexing'],
		is_set: false,
	}];

	plugin.renderServiceApiKeyFields(doc, container, null, requiredKeys, () => {
		throw new Error('onKeyChange must not be called for a shared field');
	});

	const row = container.appended.find(el => el.children.length > 0);
	const input = findInputChild(row);
	assert.strictEqual(input.type, 'text');
	assert.strictEqual(input.value, ''); // never prefilled from a pref

	await input.dispatchChange('https://llm.mpcdf.mpg.de/abc123/v1');

	assert.deepStrictEqual(posted, { keyName: 'MPCDF_EMBEDDING_BASE_URL', value: 'https://llm.mpcdf.mpg.de/abc123/v1' });
	assert.strictEqual(prefs['extensions.zotero-rag.serviceApiKey.MPCDF_EMBEDDING_BASE_URL'], undefined);
});

test('renderServiceApiKeyFields renders a shared_api_key field as a password input', () => {
	const zotero = { Prefs: { get: () => null, set: () => {} } };
	const plugin = loadPlugin(zotero, {}, {});
	const doc = makeFakeDoc();
	const container = makeFakeContainer();
	const requiredKeys = [{
		key_name: 'MPCDF_EMBEDDING_API_KEY',
		header_name: 'X-Mpcdf-Embedding-Api-Key',
		kind: 'shared_api_key',
		description: 'Shared API key',
		docs_url: null,
		required_for: ['indexing'],
		is_set: true,
	}];

	plugin.renderServiceApiKeyFields(doc, container, null, requiredKeys, () => {});

	const row = container.appended.find(el => el.children.length > 0);
	const input = findInputChild(row);
	assert.strictEqual(input.type, 'password');
	assert.strictEqual(input.placeholder, 'Configured — enter a new value to replace it');
});

test('renderServiceApiKeyFields still writes a personal api_key field to a local pref (existing behavior unchanged)', async () => {
	const prefs = {};
	const zotero = { Prefs: { get: (k) => prefs[k], set: (k, v) => { prefs[k] = v; } } };
	const plugin = loadPlugin(zotero, {}, {});
	const doc = makeFakeDoc();
	const container = makeFakeContainer();
	const requiredKeys = [{
		key_name: 'KISSKI_API_KEY', header_name: 'X-Kisski-Api-Key', kind: 'api_key',
		description: 'API key', docs_url: null, required_for: ['indexing', 'querying'],
	}];
	let changed = null;

	plugin.renderServiceApiKeyFields(doc, container, null, requiredKeys, (keyInfo, value) => { changed = { keyInfo, value }; });

	const row = container.appended.find(el => el.children.length > 0);
	const input = findInputChild(row);
	assert.strictEqual(input.type, 'password');

	await input.dispatchChange('my-kisski-key');

	assert.strictEqual(prefs['extensions.zotero-rag.serviceApiKey.KISSKI_API_KEY'], 'my-kisski-key');
	assert.strictEqual(changed.value, 'my-kisski-key');
});

// --- Tests for setSharedRemoteField itself ---

test('setSharedRemoteField POSTs to /api/config/remote-fields and returns is_set on success', async () => {
	/** @type {Array<{url: string, init: any}>} */
	const fetchCalls = [];
	const fetchStub = async (url, init) => {
		fetchCalls.push({ url, init });
		return { ok: true, status: 200, json: async () => ({ is_set: { MPCDF_EMBEDDING_BASE_URL: true } }) };
	};
	const zotero = { Prefs: { get: () => null } };
	const plugin = loadPlugin(zotero, {}, {}, { fetch: fetchStub });
	plugin.backendURL = 'http://localhost:8119';
	plugin.requiredApiKeys = [];

	const result = await plugin.setSharedRemoteField('MPCDF_EMBEDDING_BASE_URL', 'https://llm.mpcdf.mpg.de/abc123/v1');

	assert.strictEqual(fetchCalls.length, 1);
	assert.strictEqual(fetchCalls[0].url, 'http://localhost:8119/api/config/remote-fields');
	assert.deepStrictEqual(
		JSON.parse(fetchCalls[0].init.body),
		{ values: { MPCDF_EMBEDDING_BASE_URL: 'https://llm.mpcdf.mpg.de/abc123/v1' } },
	);
	assert.deepStrictEqual(result, { ok: true, is_set: true });
});

test('setSharedRemoteField returns ok:false with the server detail message on a non-2xx response', async () => {
	const fetchStub = async () => ({ ok: false, status: 400, json: async () => ({ detail: 'Unknown remote-config key' }) });
	const zotero = { Prefs: { get: () => null } };
	const plugin = loadPlugin(zotero, {}, {}, { fetch: fetchStub });
	plugin.backendURL = 'http://localhost:8119';
	plugin.requiredApiKeys = [];

	const result = await plugin.setSharedRemoteField('NOT_A_REAL_FIELD', 'x');

	assert.deepStrictEqual(result, { ok: false, error: 'Unknown remote-config key' });
});
```

- [ ] **Step 2: Run the new tests to verify they fail first**

Run: `node --test plugin/test/zotero-rag.test.js 2>&1 | grep -A5 "shared_base_url\|shared_api_key\|setSharedRemoteField"`
Expected: FAIL — `plugin.setSharedRemoteField is not a function` (if Task 7 weren't already done) or, since Task 7 IS already done by this point in the plan, these should already largely pass; run this step anyway to confirm, and treat any failure as a real bug in Task 7's implementation to fix before moving on.

- [ ] **Step 3: Run the full plugin test suite**

Run: `node --test plugin/test/*.test.js`
Expected: PASS — every test in the file, old and new.

- [ ] **Step 4: Commit**

```bash
git add plugin/test/zotero-rag.test.js
git commit -m "test(plugin): cover shared-field rendering and setSharedRemoteField"
```

---

## Task 11: Full-suite verification and spec/CLAUDE.md cross-check

**Files:** none modified — verification only.

- [ ] **Step 1: Run the full backend test suite**

Run: `uv run pytest backend/tests/ -v`
Expected: PASS, no failures, no new warnings about the renamed `required_api_keys` method.

- [ ] **Step 2: Run the full plugin test suite**

Run: `node --test plugin/test/*.test.js`
Expected: PASS.

- [ ] **Step 3: Re-read the design spec and confirm every section has a corresponding task**

Open `docs/superpowers/specs/2026-10-08-dynamic-remote-preset-config-design.md` and check off each of its numbered design sections (§1-§6) against Tasks 1-9 above. Confirm the two deviations noted at the top of this plan (reusing `admin_settings_store.py`, persisting the preset override) are the *only* deviations — everything else should match section-for-section.

- [ ] **Step 4: Confirm no other caller bypasses the shared-field resolution**

The spec's open items flagged this explicitly: Task 4/5 put the shared-store fallback inside `RemoteEmbeddingService`/`RemoteLLMService` themselves (not in `backend/dependencies.py`) specifically because `cron_indexer.py:711` calls `create_embedding_service(...)` directly, bypassing `dependencies.py::make_embedding_service()`. Confirm that's the *only* other direct construction site, so nothing else still only sees the env-var fallback:

Run: `grep -rn "RemoteEmbeddingService(\|RemoteLLMService(\|create_embedding_service(\|create_llm_service(" backend/ --include=*.py | grep -v tests/ | grep -v "def create_"`
Expected: every call site shown is either inside `backend/dependencies.py` (the HTTP request path) or `backend/services/cron_indexer.py` (already covered by Tasks 4/5 putting the fix inside the service classes themselves). If a third call site turns up, trace whether it ever uses a preset with `shared_base_url_env`/`shared_api_key_env` set — if so, it already benefits automatically (same reasoning as cron_indexer.py), since the fix lives inside the service classes, not in any particular caller.

- [ ] **Step 5: Manually exercise the new endpoints against a running dev backend** (optional, if a dev backend is available per this project's "Live Server" instructions)

```bash
# From the project root, with the backend running (npm start) and AUTHORIZED_GROUP_ID
# unset (loopback — no admin key needed):
curl -s localhost:8119/api/config | python3 -m json.tool   # should show compatible_presets
curl -s -X POST localhost:8119/api/config -H 'Content-Type: application/json' \
  -d '{"preset_name": "remote-mpcdf"}' | python3 -m json.tool
curl -s localhost:8119/api/required-keys | python3 -m json.tool   # should list MPCDF_* as shared_* kinds, is_set:false
curl -s -X POST localhost:8119/api/config/remote-fields -H 'Content-Type: application/json' \
  -d '{"values": {"MPCDF_EMBEDDING_BASE_URL": "https://example.invalid/v1"}}' | python3 -m json.tool
curl -s localhost:8119/api/required-keys | python3 -m json.tool   # MPCDF_EMBEDDING_BASE_URL should now show is_set:true
```

- [ ] **Step 6: Known test-coverage gap — flag, don't silently drop**

`preferences.js` has *no* existing automated tests at all (only `plugin/test/zotero-rag.test.js` exists, covering `zotero-rag.js`) — testing `initPrefPane`'s full wiring would mean mocking ~15 DOM elements by ID with no established harness to build on. Task 10 covers the renderer branch and `setSharedRemoteField` in isolation (where the real logic lives), but the preset-`<select>` wiring added in Task 9 (`refreshPresetState`, the `change` listener calling `POST /api/config`) has no automated test. Don't add one speculatively here — if the project later gets a `preferences.test.js` harness, add coverage for this dropdown then. For now, verify it by hand: open Preferences on a dev backend running `remote-kisski` or similar, confirm the dropdown lists `remote-mpcdf`/`windows-test`/`apple-silicon-kisski`, switch to one, and confirm the Service API Keys section immediately re-renders with that preset's fields.

- [ ] **Step 7: No commit for this task** — it's verification-only. If any step fails, go back to the relevant task, fix it there, and re-commit that task (don't accumulate fixes here).
